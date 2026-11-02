"""The real /audio route against a fake, Range-aware CDN: `python test_e2e.py`."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cdisc_backend import tracks
from cdisc_backend.routes import audio
from cdisc_backend.web import errors
from cdisc_backend.youtube import streams

BODY = bytes(range(256)) * 4000          # ~1 MB of "audio"
VIDEO = "aaaaaaaaaaa"
hits = []


async def cdn(request):
    hits.append(request.headers.get("Range"))
    first, _, last = request.headers["Range"][6:].partition("-")
    first = int(first)
    last = int(last) if last else len(BODY) - 1
    response = web.Response(status=206, body=BODY[first:last + 1],
                            content_type="audio/webm")
    response.headers["Content-Range"] = f"bytes {first}-{last}/{len(BODY)}"
    return response


async def main():
    fake = web.Application()
    fake.router.add_get("/videoplayback", cdn)
    async with TestServer(fake) as cdn_server:
        url = str(cdn_server.make_url("/videoplayback"))

        async def resolve(video_id, cookies, **_):
            return {"video_id": video_id, "title": "t", "mime_type": "audio/webm",
                    "direct_url": url, "content_length": len(BODY),
                    "user_agent": "ua", "resolved_by": "fake"}
        streams.resolve = resolve

        app = web.Application(middlewares=[errors])
        app["cookies"], app["config"], app["filling"] = None, None, set()
        app.router.add_get("/audio/{video_id}", audio.handle_audio)

        passed = 0

        def check(cond, what):
            nonlocal passed
            assert cond, what
            passed += 1
            print("ok ", what)

        async with TestClient(TestServer(app)) as client:
            r = await client.get(f"/audio/{VIDEO}?proxy=true")
            body = await r.read()
            check(r.status == 200 and body == BODY, "proxy=true serves 200 and the full body")
            check(tracks.get(VIDEO) is not None, "track is buffered after first proxy")

            before = len(hits)
            r = await client.get(f"/audio/{VIDEO}?proxy=true")
            check(await r.read() == BODY and len(hits) == before,
                  "second hit is served from the buffer, no CDN request")

            r = await client.get(f"/audio/{VIDEO}?proxy=true",
                                 headers={"Range": "bytes=1000-1999"})
            body = await r.read()
            check(r.status == 206, "Range answers 206")
            check(body == BODY[1000:2000], "exact slice")
            check(r.headers["Content-Range"] == f"bytes 1000-1999/{len(BODY)}"
                  and r.headers["Content-Length"] == "1000",
                  "Content-Range and Content-Length")

            r = await client.get(f"/audio/{VIDEO}?proxy=true",
                                 headers={"Range": f"bytes={len(BODY) - 10}-"})
            check(await r.read() == BODY[-10:], "open-ended tail range")

            r = await client.get(f"/audio/{VIDEO}?json=true")
            check((await r.json())["direct_url"] == url, "?json=true returns metadata")

        print(f"\n{passed} passed")


if __name__ == "__main__":
    asyncio.run(main())
