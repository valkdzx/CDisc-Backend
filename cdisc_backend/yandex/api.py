"""Yandex Music's API, spoken the way its Android app speaks it.

<p>An OAuth token of an account is all it takes: the same token opens the
catalogue and, when the account has Plus, full tracks. Without Plus every
answer is a thirty-second preview, which the reply says rather than hides.

<p>A stream is found two ways. `/get-file-info` is what the apps use now: one
signed request, one link. `/download-info` is what they used before — two
requests and an MD5 — and it is kept because it is the one that has never
changed: when the newer road refuses, this one still answers.
"""
import base64
import hashlib
import hmac
import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from .. import net
from ..web import Failure

log = logging.getLogger("cdisc.yandex")

API = "https://api.music.yandex.net"
SITE = "https://music.yandex.ru"

HEADERS = {
    "User-Agent": "Yandex-Music-API",
    "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
    "Accept-Language": "ru",
}

#: The key the apps sign `/get-file-info` with. Not a secret of anybody's —
#: it ships inside every copy of the app.
FILE_INFO_KEY = b"p93jhgh689SBReK6ghtw62"

#: And the salt for the older road's link, equally public.
DOWNLOAD_SALT = "XGRlBW9FXlekgbPrRHuSiA"

#: Unencrypted codecs only: "encraw" would need the key applied to every byte.
RAW_CODECS = ("mp3", "aac", "he-aac")

#: Where reading a playlist stops, for the same reason as Spotify's.
MAX_TRACKS = 1000

_TRACK_ID = re.compile(r"^(\d+)(?::(\d+))?$")
_UUID = re.compile(r"^[A-Za-z]{2}\.[0-9a-f-]{36}$")

#: What may stand in one segment of an API path. The ids come from the
#: caller's URL, and aiohttp hands a route its segment already decoded, so
#: `..%2Faccount` arrives as a path of its own and `%3F` as a query: sent on
#: as they are, they reach whatever endpoint the caller names, with this
#: account's token on it.
_SEGMENT = re.compile(r"[A-Za-z0-9_.:-]+")

_cache = {}
CACHE_TTL = 3600


def _segment(value):
    """One id, checked before it becomes part of an API path."""
    value = str(value)
    # Dots alone are allowed by the pattern and are still a way up: the URL
    # is normalised on the way out, so `/tracks/..` is `/`.
    if not _SEGMENT.fullmatch(value) or not value.strip("."):
        raise Failure(f"Not a Yandex Music id: {value!r}", 400, "bad_id")
    return value


def _headers(config):
    if not config.has_yandex:
        raise Failure("Yandex Music token is not configured", 503, "no_yandex")
    return dict(HEADERS, Authorization=f"OAuth {config.yandex_token}")


async def _call(config, method, path, **kwargs):
    """One API call, unwrapped from its `result`, with errors said plainly."""
    http = await net.session()
    async with http.request(method, API + path, headers=_headers(config),
                            timeout=net.timeout(15), **kwargs) as reply:
        try:
            body = await reply.json(content_type=None)
        except ValueError:
            body = None
        if reply.status in (401, 403):
            raise Failure(f"Yandex Music refused the token (HTTP {reply.status})",
                          502, "yandex_auth")
        if reply.status == 404:
            raise Failure("No such thing on Yandex Music", 404, "not_found")
        if reply.status != 200 or not isinstance(body, dict):
            error = (body or {}).get("error") if isinstance(body, dict) else None
            raise Failure(f"Yandex Music answered {reply.status}: {error or ''}".rstrip(": "),
                          502, "yandex_error")
        return body.get("result")


# ---------------------------------------------------------------- links

def locate(text):
    """
    The kind and the identifier out of a link, or a bare track id.

    <p>An album track link carries both ids, and the track's is the one that
    matters; `12345:678` is how the API itself writes the pair.
    """
    text = (text or "").strip()
    if _TRACK_ID.match(text):
        return "track", text.split(":")[0]
    if _UUID.match(text):
        return "playlist", text

    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    if not re.fullmatch(r"(?:[\w-]+\.)*music\.yandex\.[a-z]{2,3}", host):
        raise Failure(f"Not a Yandex Music link: {text!r}", 400, "bad_link")

    parts = [p for p in parsed.path.split("/") if p]
    if "track" in parts and parts.index("track") + 1 < len(parts):
        return "track", parts[parts.index("track") + 1]
    if parts[:1] == ["album"] and len(parts) > 1:
        return "album", parts[1]
    if parts[:1] == ["users"] and len(parts) > 3 and parts[2] == "playlists":
        return "playlist", f"{parts[1]}:{parts[3]}"
    if parts[:1] == ["playlists"] and len(parts) > 1:
        return "playlist", parts[1]
    raise Failure(f"Not a Yandex Music track, album or playlist: {text!r}",
                  400, "bad_link")


# ---------------------------------------------------------------- reading

def _cover(uri, size="1000x1000"):
    return f"https://{uri.replace('%%', size)}" if uri else None


def describe_track(track):
    """A track as this backend tells it, whatever endpoint it came from."""
    album = (track.get("albums") or [{}])[0]
    artists = [{"id": a.get("id"), "name": a.get("name")}
               for a in track.get("artists") or []]
    title = track.get("title") or ""
    if track.get("version"):
        title = f"{title} ({track['version']})"
    position = album.get("trackPosition") or {}
    labels = album.get("labels") or []
    label = labels[0].get("name") if labels and isinstance(labels[0], dict) \
        else (labels[0] if labels else None)
    track_id = str(track.get("id") or track.get("realId") or "")
    return {
        "id": track_id,
        "title": title,
        "artist": ", ".join(a["name"] for a in artists if a["name"]) or None,
        "artists": artists,
        "album": {
            "id": album.get("id"),
            "title": album.get("title"),
            "year": album.get("year"),
            "genre": album.get("genre"),
            "label": label,
            "volume": position.get("volume"),
            "index": position.get("index"),
        } if album else None,
        "duration": (track.get("durationMs") or 0) / 1000,
        "duration_ms": track.get("durationMs"),
        "explicit": track.get("contentWarning") == "explicit",
        "available": track.get("available", True),
        "artwork": _cover(track.get("coverUri") or album.get("coverUri")),
        "artwork_small": _cover(track.get("coverUri") or album.get("coverUri"), "200x200"),
        "url": (f"{SITE}/album/{album['id']}/track/{track_id}"
                if album.get("id") else f"{SITE}/track/{track_id}"),
    }


async def track(config, track_id):
    """One track, cached: a title does not change between two plays of it."""
    hit = _cache.get(("track", track_id))
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    found = await _call(config, "GET", f"/tracks/{_segment(track_id)}")
    if not found:
        raise Failure("No such Yandex Music track", 404, "not_found")
    described = describe_track(found[0])
    _cache[("track", track_id)] = (time.monotonic(), described)
    return described


async def entity(config, kind, identifier):
    """An album or a playlist, as a name and a list of tracks."""
    if kind == "album":
        album = await _call(config, "GET", f"/albums/{_segment(identifier)}/with-tracks")
        rows = [t for volume in album.get("volumes") or [] for t in volume]
        return {
            "type": "album",
            "id": str(album.get("id")),
            "name": album.get("title"),
            "owner": ", ".join(a.get("name", "") for a in album.get("artists") or []),
            "artwork": _cover(album.get("coverUri")),
            "url": f"{SITE}/album/{album.get('id')}",
            "total": len(rows),
            "truncated": False,
            "tracks": [describe_track(t) for t in rows],
        }

    if ":" in identifier:
        owner, kind_id = identifier.split(":", 1)
        owner, kind_id = _segment(owner), _segment(kind_id)
        playlist = await _call(config, "GET", f"/users/{owner}/playlists/{kind_id}")
        url = f"{SITE}/users/{owner}/playlists/{kind_id}"
    else:
        playlist = await _call(config, "GET", f"/playlist/{_segment(identifier)}",
                               params={"richTracks": "true"})
        url = f"{SITE}/playlists/{identifier}"

    rows = playlist.get("tracks") or []
    total = len(rows)
    rows = rows[:MAX_TRACKS]
    full = [r["track"] for r in rows if r.get("track")]

    # A playlist read by its owner's name may list ids alone; one batched
    # request fills them in rather than one per line.
    if len(full) < len(rows):
        ids = [f"{r['id']}:{r['albumId']}" if r.get("albumId") else str(r["id"])
               for r in rows if r.get("id")]
        full = await _call(config, "POST", "/tracks",
                           data={"track-ids": ",".join(ids)}) or []

    owner = playlist.get("owner") or {}
    return {
        "type": "playlist",
        "id": identifier,
        "name": playlist.get("title"),
        "owner": owner.get("name") or owner.get("login"),
        "artwork": _cover((playlist.get("cover") or {}).get("uri")
                          or playlist.get("ogImage")),
        "url": url,
        "total": total,
        "truncated": total > len(rows),
        "tracks": [describe_track(t) for t in full],
    }


async def search(config, query, limit):
    found = await _call(config, "GET", "/search",
                        params={"text": query, "type": "track", "page": 0,
                                "nocorrect": "false"})
    rows = ((found or {}).get("tracks") or {}).get("results") or []
    return [describe_track(t) for t in rows[:limit]]


# ---------------------------------------------------------------- streams

async def stream(config, track_id):
    """
    A direct link to the track's audio, and what it is.

    <p>The link is signed and short-lived — a minute or two — so it is never
    cached here; whoever asked for it is expected to open it now.
    """
    numeric = track_id.split(":")[0]
    try:
        found = await _file_info(config, numeric)
        if found:
            return found
    except Failure as e:
        log.info("get-file-info refused %s (%s), trying download-info",
                 numeric, e.message)
    return await _download_info(config, numeric)


async def _file_info(config, track_id):
    """The current road: one signed request."""
    params = {
        "ts": str(int(time.time())),
        "trackId": track_id,
        "quality": config.yandex_quality,
        "codecs": ",".join(RAW_CODECS),
        "transports": "raw",
    }
    message = "".join(params.values()).replace(",", "").encode()
    digest = hmac.new(FILE_INFO_KEY, message, hashlib.sha256).digest()
    params["sign"] = base64.b64encode(digest).decode()[:-1]

    result = await _call(config, "GET", "/get-file-info", params=params)
    info = (result or {}).get("downloadInfo") or {}
    url = info.get("url") or (info.get("urls") or [None])[0]
    if not url or info.get("transport", "raw") != "raw":
        return None

    codec = info.get("codec") or "mp3"
    return {
        "url": url,
        # The same file on several storage nodes. Which answers first varies
        # by a factor of three from one track to the next, so the proxy asks
        # a few at once and keeps whichever is quickest.
        "mirrors": list(dict.fromkeys([url] + (info.get("urls") or []))),
        "codec": codec,
        "mime_type": _mime(codec),
        "bitrate": info.get("bitrate"),
        "content_length": info.get("size"),
        "gain": info.get("gain"),
        "preview": info.get("quality") == "preview",
        "resolved_by": "get-file-info",
    }


async def _download_info(config, track_id):
    """The older road: a list of variants, then a signed path to one of them."""
    variants = await _call(config, "GET", f"/tracks/{_segment(track_id)}/download-info")
    if not variants:
        raise Failure("Yandex Music offered no stream for this track", 404,
                      "no_stream")

    # Full tracks before previews, mp3 before aac, then the highest bitrate.
    chosen = min(variants, key=lambda v: (bool(v.get("preview")),
                                          v.get("codec") != "mp3",
                                          -(v.get("bitrateInKbps") or 0)))

    http = await net.session()
    async with http.get(chosen["downloadInfoUrl"], headers=_headers(config),
                        timeout=net.timeout(15)) as reply:
        if reply.status != 200:
            raise Failure(f"Yandex Music's storage answered {reply.status}",
                          502, "yandex_error")
        text = await reply.text()

    root = ET.fromstring(text)
    fields = {child.tag: (child.text or "") for child in root}
    path = fields["path"]
    sign = hashlib.md5((DOWNLOAD_SALT + path[1:] + fields["s"]).encode()).hexdigest()
    codec = chosen.get("codec") or "mp3"

    return {
        "url": f"https://{fields['host']}/get-mp3/{sign}/{fields['ts']}{path}",
        "codec": codec,
        "mime_type": _mime(codec),
        "bitrate": chosen.get("bitrateInKbps"),
        "content_length": None,
        "gain": chosen.get("gain"),
        "preview": bool(chosen.get("preview")),
        "resolved_by": "download-info",
    }


def _mime(codec):
    return "audio/mpeg" if codec == "mp3" else "audio/aac"
