"""Tracks held in memory while they are being read, so they can be seeked.

<p>A SABR conversation arrives as a sequence of segments and is finished when
it is finished — there is no length to announce at the start and no way to ask
for the middle. Passed straight through, that makes a response a player can
start but cannot navigate: no `Content-Length`, no `Accept-Ranges`, so the
seek bar is decoration and a dragged cursor snaps back.

<p>What fixes it is knowing the size, which the format has told us all along,
and having the bytes to hand when a player asks for the middle. So a track is
read once, into memory, at whatever speed YouTube gives it, while the listener
is served out of the same buffer from wherever they asked to start. The read
carries on when they leave, because the next thing a seeking player does is
open a second request for an offset the first one had not reached yet.

<p>Small on purpose. Three minutes of Opus is under two megabytes and arrives
in a fraction of a second; this is a buffer with an eviction policy, not a
cache to build anything on.
"""
import asyncio
import logging
import time

log = logging.getLogger("cdisc.tracks")

#: How much audio to keep in total. Roughly a hundred ordinary tracks, and a
#: bound that matters more than it looks: without one, a busy server holds
#: every track anyone played since it started.
BUDGET = 256 * 1024 * 1024

#: How long a finished track stays worth keeping. Long enough for a listener
#: to seek around what they are hearing, short enough to be uninteresting.
TTL = 600

_tracks = {}


class Track:
    """One track being read into memory, and everyone reading it back out."""

    def __init__(self, key, total, mime_type, exact=False):
        self.key = key
        self.total = total
        self.mime_type = mime_type
        #: Whether `total` can be promised before the read ends. True for a
        #: file off the CDN, whose length is the length; never for SABR, whose
        #: format states a length the conversation may not deliver.
        self.exact = exact
        #: Resolved once the source has answered, for whoever has to know it
        #: answered at all before promising anything. None when nobody does.
        self.opened = None
        self.data = bytearray()
        self.done = False
        self.failure = None
        self.limited = False
        self.touched = time.monotonic()
        #: Woken on every arrival, so readers waiting for an offset can look
        #: again rather than poll.
        self._grew = asyncio.Event()

    # ------------------------------------------------------------- filling

    def add(self, chunk):
        self.data += chunk
        self._wake()

    def finish(self, failure=None, limited=False):
        self.done = True
        self.failure = failure
        self.limited = limited
        self._wake()

    def _wake(self):
        self._grew.set()
        self._grew.clear()

    # ------------------------------------------------------------- reading

    async def wait_for(self, upto):
        """
        Waits until `upto` bytes exist, or the read ends without them.

        <p>Returns how much there is, which is all a reader needs: less than
        asked for means the track ended there.
        """
        while len(self.data) < upto and not self.done:
            await self._grew.wait()
        return len(self.data)

    async def read_from(self, start, end):
        """Yields the bytes between two offsets, waiting for the slow half."""
        at = start
        while at <= end:
            have = await self.wait_for(at + 1)
            if have <= at:
                return
            take = min(end, have - 1)
            yield bytes(self.data[at:take + 1])
            at = take + 1

    @property
    def size(self):
        """
        The length worth promising, or None when there is none to promise.

        <p>Only once the read has finished, and then it is what actually
        arrived rather than what the format claimed. The two are not always the
        same: an unattested session is served sixty seconds and a
        `contentLength` for the whole track, so believing the format means
        promising a megabyte that never comes — and a player told to expect it
        waits for it on every seek, which is exactly what a stuck seek bar
        looks like.

        <p>While the read is still running there is no honest number, so the
        first listener gets a plain stream and everyone after them, including
        the same listener seeking, gets a length and ranges. That costs nothing
        in practice: a track fills in a fraction of a second.

        <p>A file off the CDN is the exception: its length is exact, so it is
        promised from the first byte, until a read that ended early says
        otherwise.
        """
        if self.done:
            return len(self.data)
        return self.total if self.exact else None

    @property
    def short(self):
        """Whether the read ended before the length the format promised."""
        return self.done and bool(self.total) and len(self.data) < self.total


def get(key):
    """A track already in hand, or None."""
    track = _tracks.get(key)
    if track is None:
        return None

    # A read that failed is worth forgetting rather than serving again: the
    # next request should get a fresh conversation and its own error.
    if track.done and track.failure is not None:
        _tracks.pop(key, None)
        return None
    if track.done and time.monotonic() - track.touched > TTL:
        _tracks.pop(key, None)
        return None

    track.touched = time.monotonic()
    return track


def start(key, total, mime_type, exact=False):
    """Puts a new track in the buffer, evicting whatever has to go."""
    track = Track(key, total, mime_type, exact=exact)
    _tracks[key] = track
    _evict()
    return track


def _evict():
    """Drops finished tracks, oldest first, until the budget is met again."""
    held = sum(len(t.data) for t in _tracks.values())
    if held <= BUDGET:
        return

    # Only finished ones: a track still being read has a listener attached to
    # it, and dropping it would cut them off to make room for someone newer.
    finished = sorted((t for t in _tracks.values() if t.done),
                      key=lambda t: t.touched)
    for track in finished:
        if held <= BUDGET:
            break
        held -= len(track.data)
        _tracks.pop(track.key, None)
        log.info("Dropped %s from the buffer to stay under budget", track.key)


def describe():
    """What is held, for the index page."""
    return {"tracks": len(_tracks),
            "bytes": sum(len(t.data) for t in _tracks.values())}
