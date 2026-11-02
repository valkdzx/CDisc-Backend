"""A live broadcast, read a segment at a time and poured out as one stream.

<p>Live is the one thing the direct link does not survive. visionOS hands out
an ordinary-looking URL for a broadcast's audio, and fetched as it is that URL
answers with the newest five-second segment and nothing after it: a player
hears five seconds and then the end of a track that has no end. The HLS
manifest beside it lists every segment, and every one of those answers 403 —
over IPv4 and IPv6, with the client's user agent and without — because a
broadcast's HLS wants a po-token this backend does not have.

<p>The same URL with `&sq=N` is a different matter: it answers with segment N,
and does it the way a broadcast needs. A segment that exists comes back at
once; the next one, not yet cut, is held open until it is (about five seconds,
measured) and then sent; one too far ahead is held and answered 204. So
reading a broadcast is asking for N, then N+1, with no clock of our own.

<p>Each segment is a whole fragmented MP4 — its own `ftyp` and `moov`, then
`moof` and `mdat`. Laid end to end they are not one file but a hundred files,
and a reader that has seen one `moov` does not expect another. So the first
segment goes out whole and every later one goes out as its fragments only.
The fragments already number themselves and carry their own decode times,
which run on across segments, so nothing in them needs rewriting.
"""
import asyncio
import logging

from .. import net
from ..web import Failure

log = logging.getLogger("cdisc.live")

#: Boxes only the first segment keeps. `emsg` goes from every segment: it is
#: ad-break signalling for the video player, nothing a listener hears, and a
#: top-level box a plain MP4 reader is better not asked to skip.
HEADER_BOXES = {b"ftyp", b"moov"}
DROPPED_BOXES = {b"emsg"}

#: How far behind the newest segment to start, in segments. Three are fifteen
#: seconds handed over at once, which is the cushion between a player starting
#: and the first segment it has to wait for — without it, any hiccup in the
#: long poll is a gap in the audio.
BEHIND = 2

#: Answers in a row that carried no segment before the broadcast is taken to
#: have ended. A 204 is already a held request of several seconds, so this is
#: most of a minute of nothing.
GIVE_UP_AFTER = 6


def boxes(data):
    """Top-level MP4 boxes as (type, start, end), without copying anything."""
    at = 0
    while at + 8 <= len(data):
        size = int.from_bytes(data[at:at + 4], "big")
        kind = bytes(data[at + 4:at + 8])
        if size == 1:
            if at + 16 > len(data):
                return
            size = int.from_bytes(data[at + 8:at + 16], "big")
        elif size == 0:
            size = len(data) - at
        if size < 8:
            # A box that claims to be smaller than its own header is a
            # broken segment, and walking on would only misread the rest.
            return
        yield kind, at, min(at + size, len(data))
        at += size


def fragments(segment, first):
    """
    One segment as it belongs in the stream: whole if it is the first, its
    fragments only if not.
    """
    keep = []
    for kind, start, end in boxes(segment):
        if kind in DROPPED_BOXES:
            continue
        if kind in HEADER_BOXES and not first:
            continue
        keep.append(segment[start:end])
    return b"".join(keep)


class LiveStream:
    """One listener's read of a broadcast, from a little behind its edge."""

    def __init__(self, url, user_agent, refresh):
        """
        :param refresh: an async callable returning a fresh URL, for when the
                        one in hand stops being honoured
        """
        self.url = url
        self.user_agent = user_agent
        self.refresh = refresh

    async def _get(self, sq=None):
        target = self.url if sq is None else f"{self.url}&sq={sq}"
        http = await net.session()
        async with http.get(target, headers={"User-Agent": self.user_agent or ""},
                            timeout=net.stream_timeout(read=30)) as response:
            body = await response.read() if response.status == 200 else b""
            head = response.headers.get("X-Head-Seqnum")
            return response.status, body, int(head) if head and head.isdigit() else None

    async def chunks(self):
        """Yields the broadcast's audio, segment by segment, until it ends."""
        status, _, head = await self._get()
        if status >= 400 or head is None:
            raise Failure("The broadcast did not say where its edge is (%d)" % status,
                          502, "live")

        sq = max(0, head - BEHIND)
        first = True
        silent = 0
        refreshed = False

        while silent < GIVE_UP_AFTER:
            status, body, _ = await self._get(sq)

            if status == 200 and body:
                out = fragments(body, first)
                if out:
                    first = False
                    yield out
                sq += 1
                silent = 0
                refreshed = False
                continue

            if status in (403, 410) and not refreshed:
                # A link lives hours, a broadcast can outlive it. One new
                # link per refusal; a second refusal in a row is the answer.
                self.url = await self.refresh()
                refreshed = True
                continue

            if status >= 400 and status != 404:
                raise Failure("The broadcast's segments answered %d" % status,
                              502, "live")

            # 204, or a 404 for a segment not cut yet: the poll was already
            # held, so going again at once is the right pace. The pause is
            # only for a CDN that answers instantly instead.
            silent += 1
            await asyncio.sleep(1)

        log.info("Live read ended at segment %d: nothing new for a while", sq)
