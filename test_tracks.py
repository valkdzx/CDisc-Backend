"""The in-memory buffer, without a network: `python test_tracks.py`."""
import asyncio
import time

from cdisc_backend import tracks


def fresh():
    tracks._tracks.clear()


async def main():
    passed = 0

    def check(cond, what):
        nonlocal passed
        assert cond, what
        passed += 1
        print("ok ", what)

    # SABR: length is hidden until the read is done, then it is what arrived.
    fresh()
    t = tracks.start("sabr", 1000, "audio/webm")
    t.add(b"x" * 400)
    check(t.size is None, "inexact track hides its length while filling")
    t.finish()
    check(t.size == 400, "inexact track's length is what arrived")
    check(t.short, "a read ending below the format's length is short")

    # CDN: length is promised up front.
    fresh()
    t = tracks.start("cdn", 1000, "audio/webm", exact=True)
    check(t.size == 1000, "exact track promises its length up front")
    t.add(b"y" * 1000)
    t.finish()
    check(not t.short, "a complete exact track is not short")

    # Concurrent read while a producer fills.
    fresh()
    t = tracks.start("live", 3000, "audio/webm", exact=True)
    payload = bytes(range(256)) * 12

    async def produce():
        for i in range(0, 3000, 100):
            t.add(payload[i:i + 100])
            await asyncio.sleep(0)
        t.finish()

    async def consume(a, b):
        return b"".join([c async for c in t.read_from(a, b)])

    got, _ = await asyncio.gather(consume(250, 1749), produce())
    check(got == payload[250:1750], "read_from yields the inclusive slice while filling")
    whole = await consume(0, 2999)
    check(whole == payload[:3000], "read_from of the whole finished track")
    past = await consume(2990, 5000)
    check(past == payload[2990:3000], "a read past the end stops at the end")

    # Failed tracks are dropped on get().
    fresh()
    t = tracks.start("bad", 100, "audio/webm")
    t.finish(failure="boom")
    check(tracks.get("bad") is None, "failed track is dropped on get()")
    check("bad" not in tracks._tracks, "and removed from the map")

    # TTL.
    fresh()
    t = tracks.start("old", 10, "audio/webm")
    t.add(b"z" * 10)
    t.finish()
    t.touched = time.monotonic() - tracks.TTL - 1
    check(tracks.get("old") is None, "finished track past TTL is evicted")

    # Budget: a filling track survives, a finished one goes first.
    fresh()
    saved = tracks.BUDGET
    tracks.BUDGET = 1000
    try:
        done = tracks.start("done", 600, "audio/webm", exact=True)
        done.add(b"a" * 600)
        done.finish()
        filling = tracks.start("filling", 600, "audio/webm", exact=True)
        filling.add(b"b" * 600)
        tracks.start("third", 600, "audio/webm", exact=True)
        check("filling" in tracks._tracks, "a filling track survives eviction")
        check("done" not in tracks._tracks, "a finished track is evicted first")
    finally:
        tracks.BUDGET = saved

    print(f"\n{passed} passed")


if __name__ == "__main__":
    asyncio.run(main())
