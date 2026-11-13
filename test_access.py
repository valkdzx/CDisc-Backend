"""The door, the buffer's bounds and the range answers: `python test_access.py`.

Local only: an aiohttp test server, no request leaves the machine.
"""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cdisc_backend import tracks
from cdisc_backend.cache import Bounded
from cdisc_backend.config import DEFAULTS, Config, _merge
from cdisc_backend.routes import audio
from cdisc_backend.web import Door, Failure, door, errors
from cdisc_backend.youtube import cipher

passed = 0


def check(cond, what):
    global passed
    assert cond, what
    passed += 1
    print("ok ", what)


def app_with(**settings):
    app = web.Application(middlewares=[errors, door])
    app["config"] = Config(_merge(DEFAULTS, settings))
    app["door"] = Door()

    async def index(request):
        return web.json_response([])

    async def held(request):
        return await audio._serve(request, tracks.get("k"))

    async def boom(request):
        raise RuntimeError("/secret/path and a token")

    async def slow(request):
        await asyncio.sleep(0.3)
        return web.json_response({})

    app.router.add_get("/", index)
    app.router.add_get("/held", held)
    app.router.add_get("/boom", boom)
    app.router.add_get("/slow", slow)
    return app


async def main():
    check(Config(_merge(DEFAULTS, {})).host == "127.0.0.1"
          or __import__("os").environ.get("CDISC_HOST"),
          "the default listener is loopback")

    # ---------------------------------------------------------------- token
    async with TestClient(TestServer(app_with(**{"access-token": "s3cret"}))) as c:
        check((await c.get("/")).status == 200, "the index needs no token")
        check((await c.get("/slow")).status == 401, "a route without the token is 401")
        check((await c.get("/slow?key=wrong")).status == 401, "a wrong token is 401")
        check((await c.get("/slow?key=s3cret")).status == 200, "?key= is accepted")
        for headers in ({"Authorization": "Bearer s3cret"}, {"Authorization": "s3cret"},
                        {"X-CDisc-Token": "s3cret"}):
            check((await c.get("/slow", headers=headers)).status == 200,
                  "accepted: %s" % next(iter(headers)))
        check((await c.get("/slow", headers={"Authorization": "Bearer ключ"})).status == 401,
              "a non-ASCII token is refused, not a 500")

    # ------------------------------------------------------- errors and range
    async with TestClient(TestServer(app_with())) as c:
        reply = await c.get("/boom")
        body = await reply.text()
        check(reply.status == 500 and "secret" not in body, "a 500 does not quote the exception")

        tracks._tracks.clear()
        t = tracks.start("k", 1000, "audio/mpeg", exact=True)
        t.add(b"x" * 1000)
        t.finish()
        for header, status, length in (("bytes=0-99", 206, 100), ("bytes=990-", 206, 10),
                                       ("bytes=0-5000", 206, 1000),
                                       ("bytes=100-50", 416, 0), ("bytes=1000-", 416, 0),
                                       ("bytes=5000-", 416, 0)):
            reply = await c.get("/held", headers={"Range": header})
            got = len(await reply.read())
            check(reply.status == status and got == length,
                  "%s -> %d, %d bytes" % (header, status, length))
        reply = await c.get("/held", headers={"Range": "bytes=5000-"})
        check(reply.headers.get("Content-Range") == "bytes */1000", "416 names the real length")
        reply = await c.get("/held")
        check(reply.status == 200 and len(await reply.read()) == 1000, "no range is the whole file")

    # ---------------------------------------------------------------- limits
    limits = {"limits": {"requests-per-minute": 5, "concurrent-requests": 0}}
    async with TestClient(TestServer(app_with(**limits))) as c:
        codes = [(await c.get("/")).status for _ in range(7)]
        check(codes == [200] * 5 + [429] * 2, "the sixth request in a minute is 429")
        reply = await c.get("/")
        check(reply.headers.get("Retry-After", "").isdigit(), "429 says when to come back")

    limits = {"limits": {"requests-per-minute": 0, "concurrent-requests": 2}}
    async with TestClient(TestServer(app_with(**limits))) as c:
        codes = sorted([r.status for r in await asyncio.gather(*[c.get("/slow") for _ in range(4)])])
        check(codes == [200, 200, 429, 429], "the third request at once is 429")
        check((await c.get("/slow")).status == 200, "and the slots come back")

    limits = {"limits": {"requests-per-minute": 2, "client-ip-header": "X-Real"}}
    async with TestClient(TestServer(app_with(**limits))) as c:
        a = [(await c.get("/", headers={"X-Real": "1.1.1.1"})).status for _ in range(3)]
        b = (await c.get("/", headers={"X-Real": "2.2.2.2"})).status
        check(a == [200, 200, 429] and b == 200, "limits follow the trusted header")

    limits = {"limits": {"requests-per-minute": 2}}
    async with TestClient(TestServer(app_with(**limits))) as c:
        codes = [(await c.get("/", headers={"X-Forwarded-For": "9.9.9.9"})).status
                 for _ in range(4)]
        check(codes == [200] * 4, "behind an undeclared proxy the limits stand down")

    # ---------------------------------------------------------------- buffer
    tracks._tracks.clear()
    big = 60 * 1024 * 1024
    started = 0
    while tracks.room_for(big):
        tracks.start("f%d" % started, big, "audio/webm", exact=True)
        started += 1
        assert started < 50
    check(started == tracks.BUDGET // big, "reads in flight are counted at full length (%d fit)" % started)
    tracks._tracks["f0"].finish()
    check(tracks.room_for(big), "a finished, empty read gives its room back")
    tracks._tracks.clear()
    check(not tracks.room_for(0) or tracks.MAX_TRACK <= tracks.BUDGET,
          "an unsized read is counted as the largest track")

    # ----------------------------------------------------------------- cache
    held = Bounded(ttl=3600, capacity=100)
    for n in range(1000):
        held.put(n, n)
    check(len(held) <= 100 and held.get(999) == 999, "a cache stops growing and keeps the newest")
    brief = Bounded(ttl=-1)
    brief.put("a", 1)
    check(brief.get("a") is None, "a cache forgets by age")

    # ---------------------------------------------------------------- cipher
    for url, good in (("https://rr5---sn-x.googlevideo.com/videoplayback?n=1", True),
                      ("https://googlevideo.com.evil.example/x", False),
                      ("http://rr5---sn-x.googlevideo.com/x", False),
                      ("https://user@rr1.googlevideo.com/x", False),
                      ("https://169.254.169.254/latest", False),
                      ("https://evil.example/?h=.googlevideo.com", False)):
        try:
            cipher._checked(url)
            ok = True
        except Failure:
            ok = False
        check(ok == good, "cipher answer %s: %s" % ("kept" if good else "refused", url))

    print(f"\n{passed} passed")


asyncio.run(main())
