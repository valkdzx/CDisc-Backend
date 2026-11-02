"""Drives every route against the real YouTube, without opening a port.

Run it after changing anything: `python smoke.py`. It says what worked and what
did not, and it is the fastest way to find out whether the cookies file is
doing its job.
"""
import asyncio
import json
import sys

from aiohttp.test_utils import TestClient, TestServer

from cdisc_backend.app import build_app
from cdisc_backend.config import Config

ORDINARY = "dQw4w9WgXcQ"
AGE_GATED = "tRAmPLYjbGg"
#: Made for kids: visionOS is not served it and the iOS link stops partway,
#: so this is where a capped link would slip through if nothing checked.
FOR_KIDS = "XqZsoesa55w"


async def show(client, path, note="", headers=None):
    async with client.get(path, headers=headers) as response:
        raw = await response.read()
        kind = response.headers.get("Content-Type", "")

    # An age-restricted track comes back as audio rather than as a link, which
    # is the whole point of the SABR reader — so this has to be able to read a
    # reply that is not text at all.
    if "json" not in kind:
        payload = {"raw": "%s, %d bytes, starts %s"
                          % (kind or "no type", len(raw), raw[:4].hex())}
    else:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            payload = {"raw": raw[:120].decode("utf-8", "replace")}

    mark = "ok " if response.status == 200 else "   "
    print("%s %-38s %d %s" % (mark, path, response.status, note))
    return response.status, payload


async def main():
    config = Config.load()
    app = build_app(config)

    # The diagnostic routes answer 404 without it, which here would read as
    # a broken ladder rather than a locked door.
    diagnostics = ({"X-Diag-Token": config.diagnostics_token}
                   if config.diagnostics_token else None)

    async with TestClient(TestServer(app)) as client:
        status, index = await show(client, "/")
        print("     %d routes listed" % len(index) if isinstance(index, list) else index)

        # Read off the app rather than a route: the index no longer reports
        # the account, and the one route that does is an admin's lever. After
        # the client has started, so a session adopted from the browser counts.
        account = app["cookies"].describe()
        print("     cookies: loaded=%s signed_in=%s count=%s"
              % (account["loaded"], account["signed_in"], account["count"]))
        print()

        await show(client, "/cdisc/public-youtube-data")
        await show(client, "/search?q=%s&limit=3" % "rick astley")
        print()

        status, payload = await show(client, "/audio/" + ORDINARY, "(ordinary video)")
        if status == 200:
            print("     %s — %s, %s kbps, via %s"
                  % (payload["title"], payload["mime_type"],
                     payload["stream"]["abr"], payload["resolved_by"]))
        else:
            print("     %s" % payload)
        print()

        status, payload = await show(client, "/audio/" + AGE_GATED, "(age restricted)")
        if status == 200:
            # Either shape counts as working: a link when some client handed
            # one out, or the audio itself when only SABR would serve it.
            print("     ACCOUNT WORKS: %s"
                  % (("%s via %s" % (payload["title"], payload["resolved_by"]))
                     if "title" in payload else payload["raw"]))
        else:
            print("     %s [%s]" % (payload.get("error"), payload.get("code")))
        print()

        status, payload = await show(client, "/audio/" + FOR_KIDS, "(made for kids)")
        if status == 200 and "resolved_by" in payload:
            # A link here has passed the end-of-file probe; sabr means none did.
            print("     %s via %s" % (payload["title"], payload["resolved_by"]))
        else:
            print("     %s" % (payload.get("raw") or payload))
        print()

        if diagnostics:
            status, payload = await show(client, "/sabr-test/" + ORDINARY,
                                         "(SABR, cookies or not)", diagnostics)
            for label, row in (payload.get("sabr") or {}).items():
                print("     %-16s http=%s bytes=%s"
                      % (label, row.get("http"), row.get("bytes")))
            print()

            status, payload = await show(client, "/streams/" + AGE_GATED,
                                         "(the ladder)", diagnostics)
            for row in payload.get("attempts", []):
                print("     %-11s %-9s http=%-3s %-14s url=%s/%s  %s"
                      % (row["client"], row["as"], row["http"], row["status"],
                         row["with_url"], row["audio_formats"], row["reason"][:36]))
        else:
            print("    /sabr-test and /streams skipped: no diagnostics-token configured")
        print()

        if config.has_spotify:
            status, payload = await show(client, "/spotify/4cOdK2wGLETKBW3PvgPWqT")
            if status == 200:
                print("     %s — matched by %s, drift %ss"
                      % (payload["spotify"]["title"], payload["matched_by"],
                         payload["duration_drift_seconds"]))
        else:
            print("    /spotify skipped: no credentials configured")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
