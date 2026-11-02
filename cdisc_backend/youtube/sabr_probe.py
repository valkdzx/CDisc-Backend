"""One SABR request, to answer one question.

<p>When a video is served to an account and to nobody else, the answer carries
no URLs at all — only a `serverAbrStreamingUrl` and a config blob. SABR is a
protocol rather than a link: you POST a protobuf saying which format you want
and how far you have got, and the reply is a stream of UMP-framed parts.

<p>The question this exists to settle is whether that endpoint checks the
account. The CDisc plugin already speaks SABR, in Java, and sends no cookies at
all when it does — so if a URL obtained *as the account* is honoured by a
request carrying no cookies, then the account can stay here and the plugin can
do the reading with the code it already has. If it is refused, it cannot.

<p>This writes just enough protobuf to ask once and read the part headers back.
It is a diagnosis, not a reader: nothing here reassembles audio.
"""
import struct

from .. import net

#: Part types, from the UMP framing. Only the ones worth naming in a report.
PARTS = {
    20: "MEDIA_HEADER",
    21: "MEDIA",
    22: "MEDIA_END",
    35: "NEXT_REQUEST_POLICY",
    42: "FORMAT_INITIALIZATION_METADATA",
    43: "SABR_REDIRECT",
    44: "SABR_ERROR",
    57: "SABR_CONTEXT_UPDATE",
    58: "STREAM_PROTECTION_STATUS",
    62: "END_OF_TRACK",
}

PROTECTION = {1: "attested (whole track)", 2: "not attested (sample only)",
              3: "blocked"}

TRACK_TYPES_AUDIO_ONLY = 1


# ------------------------------------------------------------------ protobuf

def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number, wire):
    return _varint((number << 3) | wire)


def _v(number, value):
    return _field(number, 0) + _varint(value)


def _b(number, payload):
    if payload is None:
        return b""
    return _field(number, 2) + _varint(len(payload)) + payload


def _f32(number, value):
    return _field(number, 5) + struct.pack("<f", value)


def _format_id(itag, last_modified=0):
    body = _v(1, itag)
    if last_modified:
        body += _v(2, last_modified)
    return body


def build_request(ustreamer_config, itag, last_modified, client,
                  po_token=None, player_time_ms=0, video_itag=None,
                  video_last_modified=0):
    """
    One `VideoPlaybackAbrRequest`, in the shape the plugin sends.

    <p>Field numbers taken from the Java reader rather than guessed, because a
    request that is merely close is answered with policy and no media, which
    looks exactly like a refusal.
    """
    abr_state = (_v(28, player_time_ms) + _v(34, 1) + _f32(35, 1.0)
                 + _v(40, TRACK_TYPES_AUDIO_ONLY))

    client_info = (_v(16, int(client.client_id))
                   + _b(17, client.version.encode())
                   + _b(18, (client.extra.get("osName") or "").encode())
                   + _b(19, (client.extra.get("osVersion") or "").encode()))

    streamer_context = _b(1, client_info) + _b(2, po_token) + _b(3, None)

    request = (_b(1, abr_state)
               + _b(5, ustreamer_config)
               + _b(16, _format_id(itag, last_modified)))

    # Asking for audio alone is not the same as asking for audio only: the
    # request still has to name the video track it would have taken. One that
    # names none is answered with policy and no media.
    if video_itag:
        request += _b(17, _format_id(video_itag, video_last_modified))

    return request + _b(19, streamer_context)


# ----------------------------------------------------------------- ump frames

def _width_of(first):
    """How wide this integer is, decided by its first byte alone."""
    if first < 0x80:
        return 1
    if first < 0xC0:
        return 2
    if first < 0xE0:
        return 3
    if first < 0xF0:
        return 4
    return 5


def _read_varint(data, at):
    """
    UMP's variable-width integer, which is not protobuf's.

    <p>Leading ones in the first byte count the bytes that follow, and what
    follows is a little-endian number scaled by what the first byte could not
    hold — not a run of seven-bit groups. Reading it the protobuf way works for
    small values and then quietly desynchronises the whole stream, which shows
    up as impossible part types and a track that ends after its first segment.
    """
    if at >= len(data):
        return None, at

    first = data[at]
    width = _width_of(first)
    if at + width > len(data):
        return None, len(data)

    rest = data[at + 1:at + width]
    if width == 1:
        value = first
    elif width == 2:
        value = (first & 0x3F) + 64 * rest[0]
    elif width == 3:
        value = (first & 0x1F) + 32 * (rest[0] + 256 * rest[1])
    elif width == 4:
        value = (first & 0x0F) + 16 * (rest[0] + 256 * (rest[1] + 256 * rest[2]))
    else:
        value = int.from_bytes(rest, "little")

    return value, at + width


def read_parts(data, limit=24):
    """What came back, as a list of part names and sizes."""
    parts = []
    at = 0
    while at < len(data) and len(parts) < limit:
        kind, at = _read_varint(data, at)
        if kind is None:
            break
        size, at = _read_varint(data, at)
        if size is None:
            break

        chunk = data[at:at + size]
        at += size

        entry = {"type": PARTS.get(kind, str(kind)), "bytes": size}
        if kind == 58 and chunk:
            # STREAM_PROTECTION_STATUS: field 1, a varint, and the whole point
            # of asking — 1 is the track, 2 is a sixty second sample.
            status = chunk[1] if len(chunk) > 1 else 0
            entry["protection"] = PROTECTION.get(status, status)
        parts.append(entry)
    return parts


# --------------------------------------------------------------------- asking

async def ask(url, payload, *, cookie_headers=None, user_agent, timeout=25):
    """One POST at the SABR endpoint. Returns the status and what came back."""
    target = url + ("&" if "?" in url else "?") + "rn=1"

    headers = {
        "content-type": "application/x-protobuf",
        "accept": "application/vnd.yt-ump",
        "accept-encoding": "identity",
        "user-agent": user_agent,
    }
    headers.update(cookie_headers or {})

    http = await net.session()
    async with http.post(target, data=payload, headers=headers,
                         timeout=net.timeout(timeout)) as response:
        body = await response.read()
        return response.status, body
