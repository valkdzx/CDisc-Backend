"""
Yandex Music and VK, served the way YouTube is.

<p>Both are the same shape from out here — a link goes in; a track, an album or
a playlist comes back; a track has audio behind it — so one set of handlers
serves both, each given the module that speaks to its service:

<ul>
<li>`/{service}?url=` — whatever the link names. A track is answered with its
    metadata and its stream; an album or a playlist with its track list, and
    `&index=` then picks a line of it, as `/spotify` does.
<li>`/{service}/audio/{id}` — the stream, as `/audio/{id}` gives YouTube's:
    JSON by default, `?proxy=true` for the audio itself through here,
    `?redirect=true` for a bounce to the source.
<li>`/{service}/info/{id}` — metadata only, as `/info/{id}`.
<li>`/{service}/search?q=` — the service's own search.
</ul>

<p>Proxied audio goes into the same buffer YouTube's does, keyed by service,
so a seek is answered out of memory rather than by asking again. An HLS
playlist — VK's usual answer — is not proxied at all: the player is sent to
it, since lavaplayer plays HLS itself and anything done here would only be a
second copy of the same work.
"""
import asyncio
import logging
from urllib.parse import quote, urlencode

from aiohttp import web

from .. import net, tracks
from ..vk import api as vk
from ..web import Failure, json
from ..yandex import api as yandex
from . import audio

log = logging.getLogger("cdisc.music")

SERVICES = {"yandex": yandex, "vk": vk}


def _service(request):
    name = request.match_info.get("service") or request.path.strip("/").split("/")[0]
    return name, SERVICES[name]


def _base(request):
    return request.app["config"].public_url or str(request.url.origin())


def _links(request, name, ident):
    base = f"{_base(request)}/{name}/audio/{quote(ident, safe='')}"
    return {"audio_url": base, "proxy_audio_url": f"{base}?proxy=true"}


def _flag(request, name):
    return request.rel_url.query.get(name, "").lower() == "true"


# ------------------------------------------------------------------- links

async def handle_link(request):
    name, api = _service(request)
    config = request.app["config"]
    raw = request.rel_url.query.get("url", "").strip()
    if not raw:
        raise Failure("Missing url", 400, "bad_url")

    kind, ident = api.locate(raw)
    if kind == "track":
        return json(await _track_with_stream(request, name, api, ident))

    entity = await api.entity(config, kind, ident)
    asked = request.rel_url.query.get("index")
    if asked is None:
        return json(_listing(request, name, raw, entity))

    try:
        position = int(asked)
    except ValueError:
        raise Failure(f"index is not a number: {asked!r}", 400, "bad_index")
    rows = entity["tracks"]
    if not 1 <= position <= len(rows):
        raise Failure(
            f"This {entity['type']} has {len(rows)} tracks, numbered 1 to "
            f"{len(rows)}; asked for {position}", 404, "no_such_index")

    payload = await _track_with_stream(request, name, api, rows[position - 1]["id"])
    payload["from"] = {"type": entity["type"], "id": entity["id"],
                       "name": entity["name"], "index": position, "of": len(rows)}
    return json(payload)


def _listing(request, name, raw, entity):
    base = _base(request)
    rows = [dict(track, index=position,
                 resolve_url=f"{base}/{name}?"
                             + urlencode({"url": raw, "index": position}),
                 **_links(request, name, track["id"]))
            for position, track in enumerate(entity["tracks"], 1)]
    listing = dict(entity, service=name, count=len(rows), tracks=rows)
    if entity.get("truncated"):
        listing["note"] = (f"Only the first {len(rows)} of {entity['total']} "
                           f"tracks are listed; index counts within these.")
    return listing


async def _track_with_stream(request, name, api, ident):
    """
    Metadata, and the stream beside it when there is one.

    <p>A track that will not play still has a name, so a stream that cannot be
    had is said in `stream_error` rather than turning the whole answer into
    one — the same courtesy `/info` extends to an age-restricted video.
    """
    config = request.app["config"]
    track = await api.track(config, ident)
    payload = dict(track, service=name, **_links(request, name, track["id"]))
    try:
        found = await api.stream(config, track["id"])
        payload["stream"] = _stream_block(request, name, track["id"], found)
    except Failure as e:
        payload["stream"] = None
        payload["stream_error"] = {"error": e.message, "code": e.code}
    return payload


def _stream_block(request, name, ident, found):
    """What the caller needs to open the stream, without its internals."""
    return {
        "url": found["url"],
        "protocol": found.get("protocol", "progressive"),
        "mime_type": found.get("mime_type"),
        "codec": found.get("codec"),
        "bitrate": found.get("bitrate"),
        "content_length": found.get("content_length"),
        "preview": found.get("preview", False),
        "user_agent": found.get("user_agent"),
        "resolved_by": found.get("resolved_by"),
    }


# ------------------------------------------------------------------- audio

async def handle_audio(request):
    name, api = _service(request)
    config = request.app["config"]
    ident = request.match_info["id"]
    key = f"{name}:{ident}"
    proxied = _flag(request, "proxy")

    # A seek is a second request for something already in memory; asking the
    # service for a fresh link first is only time added to a cursor drag.
    held = tracks.get(key)
    if proxied and held is not None:
        return await audio._serve_held(request, held)

    if proxied or _flag(request, "redirect"):
        found = await api.stream(config, ident)
    else:
        # Both at once: they are independent questions, and asked in turn the
        # caller waits for the sum of them.
        found, track = await asyncio.gather(api.stream(config, ident),
                                            api.track(config, ident))

    # A playlist is the player's to read, so ?proxy=true on one is a redirect
    # as well: its segments are fetched one by one, from the source, either way.
    if _flag(request, "redirect") or (proxied and found.get("protocol") == "hls"):
        raise web.HTTPFound(found["url"])

    if not proxied:
        block = _stream_block(request, name, ident, found)
        return json({
            "service": name,
            "id": ident,
            "title": track["title"],
            "author": track["artist"],
            "duration": track["duration"],
            "mime_type": block["mime_type"],
            "direct_url": block["url"],
            "resolved_by": block["resolved_by"],
            "user_agent": block["user_agent"],
            "content_length": block["content_length"],
            "stream": block,
            "track": track,
        })

    return await _proxy_file(request, key, found)


async def _proxy_file(request, key, found):
    """
    A file, streamed through here the moment its first bytes arrive.

    <p>One request to the source, not two. The size is what the buffer needs
    to promise a `Content-Length` and ranges, and the source says it anyway in
    the answer to the read itself — so the read is opened first, the size
    taken off its `Content-Range`, and the listener has their headers as soon
    as the source has answered, with the audio right behind them.
    """
    data = {
        "video_id": key,
        "direct_url": found["url"],
        "mime_type": found.get("mime_type") or "audio/mpeg",
        "content_length": found.get("content_length"),
        "user_agent": found.get("user_agent"),
    }
    if data["content_length"]:
        return await audio._proxy(request, data)

    # A bounded range, as for YouTube's CDN: a plain GET may be throttled.
    headers = {"Range": "bytes=0-"}
    if data["user_agent"]:
        headers["User-Agent"] = data["user_agent"]
    upstream = await _open_first(found.get("mirrors") or [data["direct_url"]],
                                 headers)
    data["direct_url"] = str(upstream.url)

    total = _total(upstream)
    track = tracks.get(key)
    if track is not None or not total or total > audio.HOLD_AT_MOST:
        # Somebody else started the same track while this one was opening, or
        # it is too long (or too vague) to hold: served the plain way instead.
        upstream.release()
        if track is not None:
            return await audio._serve_held(request, track)
        data["content_length"] = total
        return await audio._pass_through(request, data)

    track = tracks.start(key, total, data["mime_type"], exact=True)
    filling = asyncio.create_task(_pump(request.app, track, upstream))
    request.app["filling"].add(filling)
    filling.add_done_callback(request.app["filling"].discard)
    return await audio._serve(request, track)


#: How many mirrors to ask at once. The quickest of three is nearly always
#: as quick as the quickest of all of them, for a third of the requests.
RACE = 3


async def _open_first(urls, headers):
    """
    Opens the same file at several addresses and keeps the first to answer.

    <p>The rest are dropped the moment there is a winner — cancelled if still
    connecting, closed if they answered too — so the cost is a few requests'
    worth of headers, not a few copies of the track.
    """
    http = await net.session()
    attempts = [asyncio.create_task(
                    http.get(url, headers=headers, timeout=net.stream_timeout()))
                for url in urls[:RACE]]
    winner, failures = None, []
    try:
        for next_done in asyncio.as_completed(attempts):
            try:
                reply = await next_done
            except Exception as e:                           # noqa: BLE001
                failures.append(str(e) or type(e).__name__)
                continue
            if reply.status >= 400:
                failures.append(f"HTTP {reply.status}")
                reply.release()
                continue
            winner = reply
            break
    finally:
        for attempt in attempts:
            if not attempt.done():
                attempt.cancel()
            elif (not attempt.cancelled() and attempt.exception() is None
                  and attempt.result() is not winner):
                attempt.result().release()

    if winner is None:
        raise Failure("The source would not serve the file: "
                      + "; ".join(failures), 502, "cdn")
    return winner


def _total(upstream):
    """The whole file's size, from a range answer or a plain one."""
    span = upstream.headers.get("Content-Range", "")
    if upstream.status == 206 and span.rsplit("/", 1)[-1].isdigit():
        return int(span.rsplit("/", 1)[1])
    if upstream.status == 200 and upstream.headers.get("Content-Length", "").isdigit():
        return int(upstream.headers["Content-Length"])
    return None


async def _pump(app, track, upstream):
    """Pours an open answer into the buffer, listener or no listener."""
    failure = None
    try:
        async for chunk in upstream.content.iter_any():
            track.add(chunk)
    except Exception as e:                                   # noqa: BLE001
        failure = str(e) or type(e).__name__
    finally:
        upstream.release()
        track.finish(failure=failure)
    if failure:
        app.logger.warning("Read of %s stopped after %d bytes: %s",
                           track.key, len(track.data), failure)


async def handle_info(request):
    name, api = _service(request)
    ident = request.match_info["id"]
    track = await api.track(request.app["config"], ident)
    return json(dict(track, service=name, **_links(request, name, track["id"])))


async def handle_search(request):
    name, api = _service(request)
    query = request.rel_url.query.get("q", "").strip()
    if not query:
        raise Failure("Missing q", 400, "no_query")
    try:
        limit = max(1, min(20, int(request.rel_url.query.get("limit", 5))))
    except ValueError:
        limit = 5

    found = await api.search(request.app["config"], query, limit)
    return json({"service": name, "query": query,
                 "results": [dict(t, **_links(request, name, t["id"]))
                             for t in found]})
