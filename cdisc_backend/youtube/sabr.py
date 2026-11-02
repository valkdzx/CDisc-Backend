"""Reading a SABR stream, and handing it out as an ordinary file.

<p>Most of YouTube is still a URL you can GET with a byte range. What is served
to an account, and increasingly to the browser client, is not: the formats
arrive with no URL at all, only a `serverAbrStreamingUrl` and a config blob.
That endpoint is a conversation. You POST a protobuf saying which format you
want and how far you have got; it answers with UMP-framed parts — some media,
some instructions — and expects the next request to reflect what it just said.

<p>So this holds the conversation and yields the audio out of it, which is what
lets an age-restricted track leave here as a plain audio stream that anything
can play. The account never leaves this machine and the client never learns
that SABR was involved.

<p>Ported from the CDisc plugin's Java reader, field numbers and all, because
a request that is merely close is answered with policy and no media.
"""
import asyncio
import logging

from .. import net
from ..web import Failure
from .sabr_probe import _b, _f32, _v, _varint, _read_varint  # noqa: F401

log = logging.getLogger("cdisc.sabr")

MEDIA_HEADER = 20
MEDIA = 21
MEDIA_END = 22
NEXT_REQUEST_POLICY = 35
FORMAT_INITIALIZATION_METADATA = 42
SABR_REDIRECT = 43
SABR_ERROR = 44
SABR_CONTEXT_UPDATE = 57
STREAM_PROTECTION_STATUS = 58
END_OF_TRACK = 62

TRACK_TYPES_AUDIO_ONLY = 1

PROTECTION_OK = 1
PROTECTION_LIMITED = 2
PROTECTION_BLOCKED = 3

#: Give up rather than talk forever. A three minute track is a handful of
#: requests; a hundred means the conversation is going nowhere.
MAX_REQUESTS = 200


# ------------------------------------------------------------------ protobuf

class Reader:
    """Just enough protobuf to read the messages SABR answers with."""

    def __init__(self, data, offset=0, length=None):
        self.data = data
        self.at = offset
        self.end = len(data) if length is None else offset + length
        self.field = 0
        self.wire = 0
        self._value = None

    def next(self):
        if self.at >= self.end:
            return False
        key, self.at = self._varint()
        self.field, self.wire = key >> 3, key & 7

        if self.wire == 0:
            self._value, self.at = self._varint()
        elif self.wire == 2:
            size, self.at = self._varint()
            self._value = self.data[self.at:self.at + size]
            self.at += size
        elif self.wire == 5:
            self._value = self.data[self.at:self.at + 4]
            self.at += 4
        elif self.wire == 1:
            self._value = self.data[self.at:self.at + 8]
            self.at += 8
        else:
            return False
        return True

    def _varint(self):
        value = 0
        shift = 0
        at = self.at
        while at < self.end:
            byte = self.data[at]
            at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, at
            shift += 7
        return value, at

    @property
    def value(self):
        return self._value


def _read_message(data, fields):
    """Reads the fields named, ignoring everything else."""
    out = {}
    reader = Reader(data)
    while reader.next():
        name = fields.get(reader.field)
        if name:
            out[name] = reader.value
    return out


def _media_header(data):
    out = _read_message(data, {1: "header_id", 3: "itag", 4: "last_modified",
                               8: "init", 9: "sequence", 11: "start_ms",
                               12: "duration_ms", 14: "content_length"})
    return {k: (v if isinstance(v, int) else v) for k, v in out.items()}


def _next_policy(data):
    return _read_message(data, {4: "backoff_ms", 7: "playback_cookie"})


def _context(data):
    return _read_message(data, {1: "type", 3: "value", 4: "send_by_default"})


def _protection(data):
    return _read_message(data, {1: "status"}).get("status", 0)


def _redirect(data):
    value = _read_message(data, {1: "url"}).get("url")
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else None


def _error(data):
    out = _read_message(data, {1: "type", 2: "code"})
    kind = out.get("type")
    return ("%s (code %s)" % (kind.decode("utf-8", "replace")
                              if isinstance(kind, bytes) else kind,
                              out.get("code", 0)))


# ----------------------------------------------------------------- the request

def _format_id(itag, last_modified=0):
    body = _v(1, itag)
    if last_modified:
        body += _v(2, last_modified)
    return body


def build_request(state):
    """One `VideoPlaybackAbrRequest`, from where the conversation stands."""
    abr = (_v(28, state["player_time_ms"]) + _v(34, 1) + _f32(35, 1.0)
           + _v(40, TRACK_TYPES_AUDIO_ONLY))

    client = state["client"]
    info = (_v(16, int(client.client_id))
            + _b(17, client.version.encode())
            + _b(18, (client.extra.get("osName") or "").encode())
            + _b(19, (client.extra.get("osVersion") or "").encode()))

    streamer = _b(1, info) + _b(2, state.get("po_token")) + _b(3, state.get("cookie"))
    for context in state["contexts"].values():
        streamer += _b(5, _v(1, context["type"]) + _b(2, context["value"]))

    audio = _format_id(state["itag"], state["last_modified"])
    request = _b(1, abr) + _b(5, state["ustreamer"]) + _b(16, audio)

    if state.get("video_itag"):
        request += _b(17, _format_id(state["video_itag"],
                                     state.get("video_last_modified", 0)))
    request += _b(19, streamer)

    # What has already been received, so the server continues rather than
    # starting over. Only once there is something to report.
    if state["last_segment"] >= 0:
        buffered = (_b(1, audio) + _v(2, 0) + _v(3, state["player_time_ms"])
                    + _v(4, 1) + _v(5, state["last_segment"]))
        request += _b(2, audio) + _b(3, buffered)

    return request


# ----------------------------------------------------------------- the framing

class Framing:
    """
    UMP framing, read as it lands rather than out of a finished answer.

    <p>A reply carries a part header — a kind and a length, both UMP varints —
    and then that many bytes, over and over. Read out of a complete body that
    is a loop over an index; read off the wire it is a state machine, because
    any of it can be split across two reads of the socket.

    <p>Fed whatever arrived, it yields whatever is now complete. MEDIA is
    handed on in pieces as it comes, since it is the only large part and its
    payload is audio that has somewhere to go immediately. Everything else is
    parsed as protobuf and so has to be whole — all of them are tens of bytes.
    """

    def __init__(self):
        self._buffer = bytearray()
        #: How much of the MEDIA part being passed through is still to come,
        #: and whose header it belongs to. Zero when between parts.
        self._media_left = 0
        self._media_id = None

    def feed(self, data):
        """Yields `(kind, media_id, payload)` for everything now readable."""
        self._buffer += data

        while True:
            # Mid-MEDIA: everything in hand is audio, up to the part's end.
            if self._media_left:
                take = min(self._media_left, len(self._buffer))
                if not take:
                    return
                payload = bytes(self._buffer[:take])
                del self._buffer[:take]
                self._media_left -= take
                yield MEDIA, self._media_id, payload
                continue

            # A part header. Either varint can be cut in half by a read, and
            # _read_varint says so by answering None — the offset it returns
            # with it is not committed to until the whole frame is here.
            kind, at = _read_varint(self._buffer, 0)
            if kind is None:
                return
            size, at = _read_varint(self._buffer, at)
            if size is None:
                return

            if kind == MEDIA:
                # The first byte of the payload says which header these bytes
                # belong to; the rest is audio and is not worth holding.
                if not size:
                    del self._buffer[:at]
                    continue
                if len(self._buffer) < at + 1:
                    return
                self._media_id = self._buffer[at]
                self._media_left = size - 1
                del self._buffer[:at + 1]
                continue

            if len(self._buffer) < at + size:
                return
            part = bytes(self._buffer[at:at + size])
            del self._buffer[:at + size]
            yield kind, None, part


# ------------------------------------------------------------------ the stream

class SabrStream:
    """One track's worth of conversation, yielding audio as it arrives."""

    def __init__(self, url, ustreamer, itag, last_modified, client, *,
                 po_token=None, video_itag=None, video_last_modified=0,
                 duration_ms=0):
        self.url = url
        self.state = {
            "ustreamer": ustreamer,
            "itag": itag,
            "last_modified": last_modified,
            "client": client,
            "po_token": po_token,
            "video_itag": video_itag,
            "video_last_modified": video_last_modified,
            "cookie": None,
            "contexts": {},
            "player_time_ms": 0,
            "last_segment": -1,
        }
        self.duration_ms = duration_ms
        self.protection = PROTECTION_OK
        self.request_number = 0
        self.backoff_ms = 0

    async def _post(self):
        """
        One round of the conversation, yielding its parts as they arrive.

        <p>Rather than after they all have. A reply carries seconds of audio,
        and reading it whole before looking at any of it put every one of those
        seconds ahead of the first note — the listener waited out a download
        that had nothing left to tell us once its first segment had landed.
        """
        if self.backoff_ms:
            # Not politeness: a server told to come back in five seconds
            # answers an immediate request with the same instruction and no
            # media, so ignoring it is a loop that never advances.
            await asyncio.sleep(self.backoff_ms / 1000)
            self.backoff_ms = 0

        target = self.url + ("&" if "?" in self.url else "?") + "rn=%d" % self.request_number
        self.request_number += 1

        http = await net.session()
        frames = Framing()
        async with http.post(
                target, data=build_request(self.state),
                headers={"content-type": "application/x-protobuf",
                         "accept": "application/vnd.yt-ump",
                         "accept-encoding": "identity",
                         "user-agent": self.state["client"].user_agent},
                timeout=net.timeout(30)) as response:
            # Asked before a byte of the body is touched, so a refusal is never
            # half-parsed as audio.
            if response.status != 200:
                raise Failure("The SABR endpoint answered %d" % response.status,
                              502, "sabr")

            # iter_any rather than iter_chunked: the latter is read(n)
            # underneath and holds everything back until the nth byte lands,
            # which is the very wait this exists to remove.
            async for data in response.content.iter_any():
                for part in frames.feed(data):
                    yield part

    async def chunks(self):
        """Yields the audio, in the order it arrives, until the track ends."""
        finished = False

        while not finished and self.request_number < MAX_REQUESTS:
            headers = {}
            unwanted = set()
            produced = False

            async for kind, media_id, part in self._post():
                if kind == MEDIA_HEADER:
                    header = _media_header(part)
                    header_id = header.get("header_id", 0)
                    # Bytes for a format we did not ask for must not be
                    # spliced into the audio, so the id is remembered either
                    # way and the wrong ones are remembered as wrong. The
                    # header always precedes its own media, so this is known
                    # by the time the bytes turn up.
                    if header.get("itag") and header["itag"] != self.state["itag"]:
                        unwanted.add(header_id)
                    else:
                        headers[header_id] = header

                elif kind == MEDIA:
                    if media_id in unwanted:
                        continue
                    produced = True
                    yield part

                elif kind == MEDIA_END and part:
                    header = headers.get(part[0])
                    if header and not header.get("init"):
                        # Where playback has got to, which is what the next
                        # request is answered against.
                        end = header.get("start_ms", 0) + header.get("duration_ms", 0)
                        self.state["player_time_ms"] = max(
                            self.state["player_time_ms"], end)
                        self.state["last_segment"] = max(
                            self.state["last_segment"], header.get("sequence", 0))

                elif kind == NEXT_REQUEST_POLICY:
                    policy = _next_policy(part)
                    self.backoff_ms = policy.get("backoff_ms", 0) or 0
                    if policy.get("playback_cookie"):
                        self.state["cookie"] = policy["playback_cookie"]

                elif kind == SABR_CONTEXT_UPDATE:
                    context = _context(part)
                    if context.get("value") is not None:
                        self.state["contexts"][context.get("type", 0)] = {
                            "type": context.get("type", 0),
                            "value": context["value"],
                        }

                elif kind == STREAM_PROTECTION_STATUS:
                    self.protection = _protection(part) or self.protection

                elif kind == SABR_REDIRECT:
                    fresh = _redirect(part)
                    if fresh:
                        self.url = fresh

                elif kind == SABR_ERROR:
                    raise Failure("SABR refused the stream: " + _error(part),
                                  502, "sabr_error")

                elif kind == END_OF_TRACK:
                    finished = True

            if self.duration_ms and self.state["player_time_ms"] >= self.duration_ms:
                finished = True

            # A round that moved nothing and asked for nothing is a stall
            # rather than a pause, and looping on it only makes it a busy one.
            if not produced and not finished and not self.backoff_ms:
                if self.state["last_segment"] < 0 and self.request_number > 3:
                    raise Failure("SABR sent no audio", 502, "sabr_silent")
                if self.request_number > 6 and not produced:
                    finished = True

    @property
    def limited(self):
        """True when YouTube served a sample rather than the track."""
        return self.protection == PROTECTION_LIMITED
