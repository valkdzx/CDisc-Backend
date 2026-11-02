"""
A Spotify link, answered with the YouTube recording of it.

<p>A track link is answered with one match. An album or a playlist link is
answered with its track list, because the caller has to see what is on it
before it can ask for anything from it; `&index=` then names a line of that
list and gets the same single-track answer a track link would have given.

<p>With `&beta=true` a track — by link, by bare id, or by `&index=` — is
answered instead with the track object exactly as `GET /v1/tracks/{id}`
returns it, and no YouTube search is made: the plugin that asks for it does its
own matching. Behind a flag for now, because the plugins already out there read
the matched shape.
"""
import time
from urllib.parse import urlencode

from ..spotify import api, match
from ..web import Failure, json

#: A match does not go stale: the same ISRC finds the same recording tomorrow.
#: Kept only so a queue of twenty discs from one album is twenty lookups the
#: first time and none the second.
CACHE_TTL = 24 * 3600

#: Track lists are cached too, and for the same reason: a client that walks an
#: album asks for the list once per track otherwise. They are held apart from
#: matches by kind, so that an album and a track cannot share a key.
_cache = {}


async def handle_spotify(request):
    raw = request.match_info.get("id") or request.rel_url.query.get("url", "")
    kind, identifier = api.locate(raw)
    beta = _beta(request)

    if kind == "track":
        if beta:
            return json(await _raw(request, identifier))
        return json(await _matched(request, identifier))

    kind, entity = await _entity(request, kind, identifier)
    if kind == "track":
        if beta:
            return json(await _raw(request, identifier))
        return json(await _matched(request, identifier, entity))

    asked = request.rel_url.query.get("index")
    if asked is None:
        return json(_listing(request, entity))

    position, chosen = _at(entity, asked)
    if beta:
        return json(await _raw(request, chosen["id"]))
    payload = await _matched(request, chosen["id"], chosen)
    return json(dict(payload, **{"from": {
        "type": entity["type"],
        "id": entity["id"],
        "name": entity["name"],
        "index": position,
        "of": len(entity["tracks"]),
    }}))


def _beta(request):
    return request.rel_url.query.get("beta", "").lower() in ("1", "true", "yes")


async def _raw(request, identifier):
    """The raw track object, cached by its Spotify id apart from matches."""
    cached = _cache.get(("raw", identifier))
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]

    found = await api.raw_track(request.app["config"], identifier)
    _cache[("raw", identifier)] = (time.monotonic(), found)
    return found


async def _entity(request, kind, identifier):
    """What the link names, from the cache when it has been read before."""
    cached = _cache.get(("entity", kind, identifier))
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]

    found = await api.entity(request.app["config"], kind, identifier)
    _cache[("entity", kind, identifier)] = (time.monotonic(), found)
    return found


def _at(entity, asked):
    """
    The track at the position the caller named, counting from one.

    <p>From one because the list says so: every line of it carries the `index`
    to send back, so there is nothing to work out and nothing to be off by.
    """
    try:
        position = int(asked)
    except ValueError:
        raise Failure(f"index is not a number: {asked!r}", 400, "bad_index")

    tracks = entity["tracks"]
    if not 1 <= position <= len(tracks):
        raise Failure(
            f"This {entity['type']} has {len(tracks)} tracks, numbered 1 to "
            f"{len(tracks)}; asked for {position}", 404, "no_such_index")

    return position, tracks[position - 1]


def _listing(request, entity):
    """An album or a playlist as a list, one line per track."""
    base = _base(request)
    tracks = [dict(index=position, **track, resolve_url="%s/spotify?%s" % (
        base, urlencode(dict({"url": entity["url"] or entity["id"],
                              "index": position},
                             **({"beta": "true"} if _beta(request) else {})))))
              for position, track in enumerate(entity["tracks"], 1)]

    listing = dict(entity, count=len(tracks), tracks=tracks)
    if entity["truncated"]:
        listing["note"] = (
            f"Only the first {len(tracks)} of {entity['total']} tracks are "
            f"listed; index counts within these.")
    return listing


async def _matched(request, identifier, known=None):
    """
    The YouTube recording of one track, cached by its Spotify id.

    <p>Keyed by the track rather than by the request, so that the fourth track
    of an album and a link to that same track are one lookup between them.
    """
    cached = _cache.get(("track", identifier))
    if cached and time.monotonic() - cached[0] < CACHE_TTL:
        return cached[1]

    config = request.app["config"]

    # A track read as part of an album carries no ISRC — the album endpoint
    # does not put one there — and an ISRC is the difference between finding
    # the recording and finding something with the same name. So one more
    # request, and only when the list did not already come with one.
    track = known if known and known.get("isrc") else await api.track(config, identifier)

    chosen, how = await match.to_youtube(track, config.web_client_version)

    want = track["duration_ms"] / 1000 if track.get("duration_ms") else None
    drift = (None if want is None or not chosen.get("duration")
             else round(chosen["duration"] - want, 1))

    base = _base(request)
    payload = {
        "spotify": track,
        "matched_by": how,
        "duration_drift_seconds": drift,
        "youtube": {
            "id": chosen["id"],
            "title": chosen["title"],
            "channel": chosen["channel"],
            "duration": chosen["duration"],
            "url": f"https://www.youtube.com/watch?v={chosen['id']}",
            "audio_url": f"{base}/audio/{chosen['id']}",
            "proxy_audio_url": f"{base}/audio/{chosen['id']}?proxy=true",
        },
    }

    _cache[("track", identifier)] = (time.monotonic(), payload)
    return payload


def _base(request):
    return request.app["config"].public_url or str(request.url.origin())
