"""Live segments laid end to end, without a network: `python test_live.py`."""
import asyncio

from cdisc_backend.youtube import live


def box(kind, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def segment(n):
    return (box(b"ftyp", b"iso6") + box(b"moov", b"m" * 20) + box(b"emsg", b"ad")
            + box(b"moof", bytes([n]) * 4) + box(b"mdat", bytes([n]) * 16))


passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1
    print("ok ", what)


kinds = [k for k, _, _ in live.boxes(segment(1))]
check(kinds == [b"ftyp", b"moov", b"emsg", b"moof", b"mdat"], "boxes reads every top-level box")

first = live.fragments(segment(1), first=True)
check([k for k, _, _ in live.boxes(first)] == [b"ftyp", b"moov", b"moof", b"mdat"],
      "first segment keeps its header, loses emsg")
later = live.fragments(segment(2), first=False)
check([k for k, _, _ in live.boxes(later)] == [b"moof", b"mdat"],
      "later segments keep fragments only")

large = (1).to_bytes(4, "big") + b"mdat" + (24).to_bytes(8, "big") + b"x" * 8
check([(k, e - s) for k, s, e in live.boxes(large)] == [(b"mdat", 24)], "64-bit box size")
check(list(live.boxes(b"\x00\x00\x00\x04moov")) == [], "a box smaller than its header stops the walk")


class FakeLive(live.LiveStream):
    """Head at 10; 10..12 exist, 13 is refused once, then the broadcast ends."""

    def __init__(self):
        super().__init__("u", "ua", self.renew)
        self.asked = []
        self.renewed = 0

    async def renew(self):
        self.renewed += 1
        return "fresh"

    async def _get(self, sq=None):
        self.asked.append((self.url, sq))
        if sq is None:
            return 200, segment(10), 10
        if sq <= 12:
            return 200, segment(sq), 12
        if sq == 13 and self.renewed == 0:
            return 403, b"", None
        return 204, b"", 12


async def run():
    live.asyncio.sleep, real = (lambda _: real(0)), live.asyncio.sleep
    try:
        stream = FakeLive()
        out = b"".join([c async for c in stream.chunks()])
    finally:
        live.asyncio.sleep = real
    return stream, out


stream, out = asyncio.run(run())
check(stream.asked[1] == ("u", 10 - live.BEHIND), "starts BEHIND segments before the head")
check(sum(1 for k, _, _ in live.boxes(out) if k == b"moov") == 1, "one moov in the whole stream")
check(sum(1 for k, _, _ in live.boxes(out) if k == b"moof") == 5, "every segment's fragment, in order")
check(stream.renewed == 1 and stream.asked[-1][0] == "fresh", "a 403 renews the link once")
check(sum(1 for _, sq in stream.asked if sq == 13) == 1 + live.GIVE_UP_AFTER,
      "ends after GIVE_UP_AFTER empty answers")

print(f"\n{passed} passed")
