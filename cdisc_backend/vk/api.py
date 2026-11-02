"""VK's audio API, which only a handful of apps are allowed to call.

<p>Audio is closed to ordinary VK applications: a token issued to anything but
Kate Mobile or VK's own clients is answered with one placeholder track called
`audio_api_unavailable`. So the token has to be one of theirs, and every request
has to arrive under that app's user agent — VK checks the two against each
other and refuses the pair when they disagree.

<p>A track's `url` is, these days, an HLS playlist whose segments are AES-128
encrypted rather than a file. It is handed on as it is: lavaplayer plays HLS,
key and all, and the playlist, the key and the segments answer any user agent,
so there is nothing for this backend to do in between.
"""
import logging
import re
import time
from urllib.parse import parse_qs, unquote, urlparse

from .. import net
from ..web import Failure

log = logging.getLogger("cdisc.vk")

API = "https://api.vk.com/method/"
VERSION = "5.131"

#: What VK says instead of a link when the token may not play audio.
UNAVAILABLE = "audio_api_unavailable"

#: owner_id _ audio_id [_ access_key]
_AUDIO = re.compile(r"(-?\d+)_(\d+)(?:_([0-9a-f]+))?")

MAX_TRACKS = 1000
_cache = {}
CACHE_TTL = 3600


async def _call(config, method, **params):
    if not config.has_vk:
        raise Failure("VK token is not configured", 503, "no_vk")

    params = {k: v for k, v in params.items() if v is not None}
    params.update(access_token=config.vk_token, v=VERSION)

    http = await net.session()
    async with http.post(API + method, data=params,
                         headers={"User-Agent": config.vk_user_agent},
                         timeout=net.timeout(15)) as reply:
        if reply.status != 200:
            raise Failure(f"VK answered HTTP {reply.status}", 502, "vk_error")
        body = await reply.json(content_type=None)

    error = body.get("error")
    if error:
        code = error.get("error_code")
        message = error.get("error_msg") or "unknown error"
        if code == 5:
            raise Failure(f"VK refused the token: {message}", 502, "vk_auth")
        if code in (15, 201, 203):
            raise Failure(f"VK will not show this: {message}", 403, "access_denied")
        if code == 3:
            raise Failure(f"This token's app may not call {method}: {message}",
                          502, "vk_method")
        raise Failure(f"VK error {code}: {message}", 502, "vk_error")
    return body.get("response")


# ---------------------------------------------------------------- links

def locate(text):
    """
    The kind and the identifier out of a link, or a bare `owner_id` pair.

    <p>A track is `audio{owner}_{id}`, sometimes with an access key after it;
    a playlist or an album is `{owner}_{id}` and nearly always has one. The
    key is not decoration: without it a playlist that is not public is
    answered as if it did not exist.
    """
    text = unquote((text or "").strip())

    bare = _AUDIO.fullmatch(text.removeprefix("audio"))
    if bare:
        return "track", text.removeprefix("audio")

    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    if not re.fullmatch(r"(?:[\w-]+\.)*vk\.(?:com|ru)", host):
        raise Failure(f"Not a VK link: {text!r}", 400, "bad_link")

    # Opened over another page, the thing itself is in ?z= — which wins, since
    # the page underneath is only where the listener happened to be.
    targets = parse_qs(parsed.query).get("z", []) + [parsed.path]
    for target in targets:
        found = re.search(r"audio_playlist(-?\d+)_(\d+)(?:[/_]([0-9a-f]+))?", target) \
            or re.search(r"music/(?:playlist|album)/(-?\d+)_(\d+)(?:_([0-9a-f]+))?", target)
        if found:
            owner, playlist, key = found.groups()
            return "playlist", "_".join(p for p in (owner, playlist, key) if p)
        found = re.search(r"audio(-?\d+_\d+(?:_[0-9a-f]+)?)", target)
        if found:
            return "track", found.group(1)
    raise Failure(f"Not a VK track or playlist: {text!r}", 400, "bad_link")


# ---------------------------------------------------------------- reading

def _thumb(album):
    thumb = (album or {}).get("thumb") or {}
    for size in ("photo_1200", "photo_600", "photo_300", "photo_270", "photo_135"):
        if thumb.get(size):
            return thumb[size]
    return None


def describe_track(audio):
    """A track as this backend tells it."""
    album = audio.get("album") or {}
    main = audio.get("main_artists") or []
    featured = audio.get("featured_artists") or []
    key = audio.get("access_key")
    ident = f"{audio.get('owner_id')}_{audio.get('id')}" + (f"_{key}" if key else "")
    return {
        "id": ident,
        "title": audio.get("title") + (f" ({audio['subtitle']})"
                                       if audio.get("subtitle") else ""),
        "artist": audio.get("artist"),
        "artists": [{"id": a.get("id"), "name": a.get("name")}
                    for a in main + featured],
        "album": {
            "id": f"{album.get('owner_id')}_{album.get('id')}",
            "title": album.get("title"),
            "access_key": album.get("access_key"),
        } if album else None,
        "duration": audio.get("duration"),
        "explicit": bool(audio.get("is_explicit")),
        # Set when a rightholder has pulled the track in this region, or the
        # track is only for subscribers. The url is then empty or a stub.
        "restricted": audio.get("content_restricted"),
        "genre_id": audio.get("genre_id"),
        "date": audio.get("date"),
        "artwork": _thumb(album),
        "url": f"https://vk.com/audio{ident}",
    }


async def _audio(config, ident):
    """The raw track, with its link, as `audio.getById` gives it."""
    found = await _call(config, "audio.getById", audios=ident)
    if not found:
        raise Failure("No such VK track, or it is not visible to this token",
                      404, "not_found")
    audio = found[0]
    if UNAVAILABLE in (audio.get("url") or "") or audio.get("title") == UNAVAILABLE:
        raise Failure("This VK token may not play audio: it has to be issued to "
                      "Kate Mobile or VK's own app, and used under its user "
                      "agent", 502, "vk_no_audio_access")
    return audio


async def track(config, ident):
    hit = _cache.get(("track", ident))
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    described = describe_track(await _audio(config, ident))
    _cache[("track", ident)] = (time.monotonic(), described)
    return described


async def entity(config, kind, identifier):
    """A playlist or an album, which VK both calls playlists."""
    owner, playlist_id, *rest = identifier.split("_")
    key = rest[0] if rest else None

    meta = await _call(config, "audio.getPlaylistById", owner_id=owner,
                       playlist_id=playlist_id, access_key=key)
    rows, offset = [], 0
    while len(rows) < MAX_TRACKS:
        page = await _call(config, "audio.get", owner_id=owner,
                           playlist_id=playlist_id, access_key=key,
                           offset=offset, count=200)
        items = (page or {}).get("items") or []
        rows += items
        offset += len(items)
        if not items or offset >= (page or {}).get("count", 0):
            break

    total = (meta or {}).get("count") or len(rows)
    main = (meta or {}).get("main_artists") or []
    return {
        "type": "album" if (meta or {}).get("album_type") else "playlist",
        "id": identifier,
        "name": (meta or {}).get("title"),
        "owner": ", ".join(a.get("name", "") for a in main) or None,
        "artwork": ((meta or {}).get("photo") or {}).get("photo_600")
                   or _thumb({"thumb": ((meta or {}).get("thumbs") or [{}])[0]}),
        "url": f"https://vk.com/music/playlist/{identifier}",
        "total": total,
        "truncated": total > len(rows[:MAX_TRACKS]),
        "tracks": [describe_track(a) for a in rows[:MAX_TRACKS]],
    }


async def search(config, query, limit):
    found = await _call(config, "audio.search", q=query, count=limit,
                        auto_complete=1, sort=2)
    return [describe_track(a) for a in (found or {}).get("items") or []][:limit]


# ---------------------------------------------------------------- streams

async def stream(config, ident):
    """
    Where the audio is: the HLS playlist, or a file for the odd track that
    still has one.

    <p>Never cached. VK's links are signed and expire, and the whole point of
    asking is to open the answer straight away.
    """
    audio = await _audio(config, ident)
    url = audio.get("url") or ""
    if not url:
        raise Failure("VK gave no link for this track%s" % (
            " (restricted: %s)" % audio["content_restricted"]
            if audio.get("content_restricted") else ""), 403, "no_stream")

    hls = ".m3u8" in urlparse(url).path
    return {
        "url": url,
        "protocol": "hls" if hls else "progressive",
        "mime_type": "application/vnd.apple.mpegurl" if hls else "audio/mpeg",
        "content_length": None,
        "user_agent": None,
        "resolved_by": "audio.getById",
    }
