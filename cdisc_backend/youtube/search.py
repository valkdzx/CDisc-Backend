"""Reading what a search came back with, and not asking twice.

<p>The answer is a deep nest of renderers whose shape changes whenever the site
is redesigned, so rather than walking a path this hunts the whole tree for
video renderers. It costs a full traversal of a small document and it survives
layout changes, which a path does not.

<p>The other half of this file is about time. A search is one POST to YouTube
and that POST is the whole of the wait — around four hundred milliseconds on a
good day, against nine for parsing what comes back. Nothing here can make
YouTube answer faster, so the work is in asking it less: the same query twice
in five minutes is answered from memory, and a stale answer goes out
immediately while a fresh one is fetched behind it.
"""
import asyncio
import logging
import time

from . import clients, innertube, visitor

log = logging.getLogger("cdisc.search")

#: The filter chip for "Videos". Sending it halves both the wait and the
#: payload — a quarter of a second and seven hundred kilobytes per search —
#: because YouTube then skips the channel cards, playlist shelves and "people
#: also watched" rails that this file throws away anyway. The blob is what the
#: site itself puts in the URL for that chip; it is opaque by design.
VIDEOS = "EgIQAQ%3D%3D"

#: How long an answer is served without asking YouTube again. Search results
#: for a given query barely move within an hour, let alone five minutes, and
#: the point of this cache is the burst: somebody types, retypes and searches
#: the same thing four times in a row.
TTL = 300

#: And how long past that a kept answer is still worth serving while a fresh
#: one is fetched behind it. An hour-old result list is not wrong, it is just
#: old, and handing it over at once beats making someone wait half a second
#: for a list that will almost certainly be identical.
STALE = 3600

#: Kept per entry regardless of what was asked for, so one cached search
#: answers `limit=3` and `limit=20` alike.
DEPTH = 20

#: query -> (fetched at, results). Small, bounded below, never persisted:
#: restarting the backend is meant to cost one slow search, not a warm-up.
_cache = {}

#: query -> the task already fetching it. A thousand callers asking for the
#: same thing at once is one POST, not a thousand.
_inflight = {}

#: Background refreshes, held so nothing collects them mid-flight.
_refreshing = set()

#: Past this many entries the oldest half goes. A cap rather than an eviction
#: policy: the working set here is tiny and the alternative to a cap is a
#: process that grows for as long as somebody is willing to type.
CAPACITY = 512


def _seconds(label):
    """`4:12` or `1:02:33` into seconds; 0 when there is no label."""
    if not label:
        return 0
    total = 0
    for part in label.split(":"):
        try:
            total = total * 60 + int(part.strip())
        except ValueError:
            return 0
    return total


def _first_run(node):
    """Titles arrive as runs; the first is the whole of it in practice."""
    if not isinstance(node, dict):
        return None
    runs = node.get("runs")
    if isinstance(runs, list) and runs:
        return runs[0].get("text")
    return node.get("simpleText")


def _collect(node, out, limit):
    if len(out) >= limit:
        return

    if isinstance(node, dict):
        video = node.get("videoRenderer") or node.get("compactVideoRenderer")
        if isinstance(video, dict) and video.get("videoId"):
            out.append({
                "id": video["videoId"],
                "title": _first_run(video.get("title")),
                # The desktop site says ownerText; every other surface says one
                # of the bylines. Reading all three costs nothing and is the
                # difference between a channel name and a null the day YouTube
                # changes which one it sends.
                "channel": _first_run(video.get("ownerText")
                                      or video.get("shortBylineText")
                                      or video.get("longBylineText")),
                # Through _first_run rather than straight to simpleText: the
                # same field arrives as runs on some surfaces, and reading only
                # simpleText turns a four-minute track into a zero-second one.
                "duration": _seconds(_first_run(video.get("lengthText"))),
            })
            if len(out) >= limit:
                return
        for child in node.values():
            _collect(child, out, limit)
    elif isinstance(node, list):
        for child in node:
            _collect(child, out, limit)


def _key(query):
    """
    What counts as the same search.

    <p>Case and stray spaces are not a different question, and treating them as
    one is how a cache ends up with four entries for `Metallica One`.
    """
    return " ".join(query.lower().split())


def _trim():
    if len(_cache) <= CAPACITY:
        return
    for key in sorted(_cache, key=lambda k: _cache[k][0])[:len(_cache) // 2]:
        _cache.pop(key, None)


async def _ask(query, web_client_version):
    """
    One search, actually over the wire.

    <p>Returns None rather than [] when YouTube refused, because the two mean
    different things to a cache: an empty list is an answer worth keeping and a
    refusal is worth forgetting.
    """
    visitor_data = await visitor.current()
    status, payload = await innertube.search(
        query, clients.web(web_client_version),
        visitor_data=visitor_data, params=VIDEOS)

    if status != 200:
        log.warning("search for %r refused: HTTP %s", query, status)
        return None

    results = []
    _collect(payload, results, DEPTH)
    return results


async def _fetch(key, query, web_client_version):
    """The same query in flight twice is one request and two awaits."""
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_ask(query, web_client_version))
        _inflight[key] = task
        try:
            results = await task
        finally:
            _inflight.pop(key, None)
    else:
        # Shielded, because this caller giving up — a browser tab closed
        # mid-search — must not cancel the request everyone else is waiting on.
        results = await asyncio.shield(task)

    if results is not None:
        _cache[key] = (time.time(), results)
        _trim()
    return results


def _refresh(key, query, web_client_version):
    """Fetches behind a stale answer that has already gone out."""
    if key in _inflight:
        return

    async def run():
        try:
            await _fetch(key, query, web_client_version)
        except Exception:  # noqa: BLE001 - a refresh that fails changes nothing
            log.debug("background refresh of %r failed", query, exc_info=True)

    task = asyncio.create_task(run())
    _refreshing.add(task)
    task.add_done_callback(_refreshing.discard)


async def find(query, web_client_version, limit=5):
    """
    Searches YouTube.

    <p>An ISRC works as a query and is worth preferring where one is known: a
    title matches covers, remasters and karaoke alike, while an ISRC names one
    recording.

    <p>Answers from memory where it can. `cached()` says whether a given query
    would, which is what the reply's caching headers are built from.
    """
    key = _key(query)
    held = _cache.get(key)

    if held is not None:
        age = time.time() - held[0]
        if age < TTL:
            return held[1][:limit]
        if age < TTL + STALE:
            # Out it goes now; the fresh copy lands in the cache behind it and
            # is what the next caller gets.
            _refresh(key, query, web_client_version)
            return held[1][:limit]

    results = await _fetch(key, query, web_client_version)
    if results is None:
        # YouTube would not answer. A stale list beats no list at all.
        return held[1][:limit] if held is not None else []
    return results[:limit]


def cached(query):
    """How old the kept answer for this query is, or None if there is none."""
    held = _cache.get(_key(query))
    return None if held is None else time.time() - held[0]


def describe():
    """Counts only, for the index. Which queries were run is nobody's business."""
    return {"queries": len(_cache), "in_flight": len(_inflight)}
