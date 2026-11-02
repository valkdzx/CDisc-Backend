"""SoundCloud's own web API, spoken the way its web player speaks it.

<p>There is no key to register for any more, so the client id is the one
soundcloud.com ships to every browser, read out of its JavaScript. It changes
when they redeploy, which is why it is kept in memory only and fetched again
the moment the API stops accepting it.
"""
import asyncio
import logging
import re

from .. import net
from ..web import Failure

log = logging.getLogger("cdisc.soundcloud")

API = "https://api-v2.soundcloud.com"
HOME = "https://soundcloud.com/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_SCRIPT = re.compile(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"')
_CLIENT_ID = re.compile(r'client_id\s*[:=]\s*"([A-Za-z0-9]{32})"')

_client_id = None
_lock = asyncio.Lock()


async def client_id(refresh=False):
    """The web player's client id, scraped once and kept until it is refused."""
    global _client_id
    if _client_id and not refresh:
        return _client_id

    async with _lock:
        if _client_id and not refresh:
            return _client_id

        session = await net.session()
        async with session.get(HOME, headers={"User-Agent": UA}) as reply:
            page = await reply.text()

        # The id lives in one of the last bundles; walking from the end finds
        # it in one or two fetches instead of a dozen.
        for script in reversed(_SCRIPT.findall(page)):
            async with session.get(script, headers={"User-Agent": UA}) as reply:
                if reply.status != 200:
                    continue
                found = _CLIENT_ID.search(await reply.text())
            if found:
                _client_id = found.group(1)
                log.info("SoundCloud client id: %s", _client_id)
                return _client_id

    raise Failure("Could not find a SoundCloud client id", 502, "no_client_id")


async def _get(url, params=None):
    """A GET against api-v2, retried once with a fresh client id if refused."""
    session = await net.session()
    for attempt in range(2):
        query = dict(params or {}, client_id=await client_id(refresh=attempt > 0))
        async with session.get(url, params=query, headers={"User-Agent": UA}) as reply:
            if reply.status in (401, 403) and attempt == 0:
                continue
            if reply.status == 404:
                raise Failure("No such SoundCloud track", 404, "not_found")
            if reply.status != 200:
                raise Failure(f"SoundCloud answered {reply.status}", 502,
                              "soundcloud_error")
            return await reply.json(content_type=None)
    raise Failure("SoundCloud refused the client id", 502, "client_id_refused")


async def resolve(url):
    """Whatever a soundcloud.com link points at, as the API describes it."""
    return await _get(f"{API}/resolve", {"url": url})


def _rank(transcoding):
    """Progressive mp3 first — one file, plays anywhere — then HLS, best first."""
    fmt = transcoding.get("format", {})
    progressive = fmt.get("protocol") == "progressive"
    mp3 = "mpeg" in fmt.get("mime_type", "")
    return (not progressive, not mp3, transcoding.get("snipped", False))


async def stream(track, transcoding):
    """The CDN URL for one transcoding. Signed, and good for a few minutes."""
    params = {}
    if track.get("track_authorization"):
        params["track_authorization"] = track["track_authorization"]
    return (await _get(transcoding["url"], params)).get("url")


async def best_stream(track):
    """The most playable transcoding, and the direct URL it resolves to."""
    transcodings = sorted(track.get("media", {}).get("transcodings", []),
                          key=_rank)
    for transcoding in transcodings:
        try:
            url = await stream(track, transcoding)
        except Failure as e:
            log.warning("transcoding %s refused: %s",
                        transcoding.get("preset"), e.message)
            continue
        if url:
            return transcoding, url
    return None, None
