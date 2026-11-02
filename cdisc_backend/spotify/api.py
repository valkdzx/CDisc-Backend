"""The Spotify Web API, as far as this needs it.

<p>Client credentials: an application token, no user and no consent screen.
That is enough for everything here — a track, an album, a public playlist — and
it is the caller who is spared a token rather than this: a link goes in and a
list comes back, with nobody's account involved.
"""
import base64
import re
import time

from .. import net
from ..web import Failure

TOKEN_URL = "https://accounts.spotify.com/api/token"
TRACK_URL = "https://api.spotify.com/v1/tracks/{}"
TRACKS_URL = "https://api.spotify.com/v1/tracks"
ALBUM_URL = "https://api.spotify.com/v1/albums/{}"
PLAYLIST_URL = "https://api.spotify.com/v1/playlists/{}"

#: What a link may point at, in the order a bare id is guessed at.
KINDS = ("track", "album", "playlist")

#: A page of tracks, as large as each endpoint allows — asking for more is
#: refused rather than obliged, and a long playlist read fifty at a time is
#: twice the requests for the same answer.
ALBUM_PAGE = 50
PLAYLIST_PAGE = 100

#: How many tracks may be looked up in one request. Fifty, and the answer comes
#: back in the order the ids were asked in.
BATCH = 50

#: Where reading a playlist stops. A thousand tracks is already a reply of a
#: megabyte, and a playlist longer than that is a library rather than a queue.
#: The answer says when it was cut short rather than presenting the head of a
#: list as the whole of it.
MAX_TRACKS = 1000

LINK = re.compile(r"(track|album|playlist)[/:]([A-Za-z0-9]{22})")
BARE = re.compile(r"[A-Za-z0-9]{22}")

_token = None
_token_until = 0.0

#: How long before it lapses the keeper replaces a token, so that the request
#: arriving at the end of the hour does not pay a round trip to Spotify first.
RENEW_AHEAD = 600


def locate(text):
    """
    The kind and the identifier out of a link, a URI, or a bare id.

    <p>A bare id names its kind nowhere — a track's twenty-two characters and a
    playlist's are the same twenty-two characters — so that comes back as None,
    and {@link entity} settles it by asking.
    """
    text = (text or "").strip()
    found = LINK.search(text)
    if found:
        return found.group(1), found.group(2)
    if BARE.fullmatch(text):
        return None, text
    raise Failure(f"Not a Spotify track, album or playlist: {text!r}",
                  400, "bad_link")


async def renew_if_due(config):
    """
    Mints a token now if the held one is missing or close to lapsing.

    <p>For the keeper, not for requests: run every few minutes, it means no
    request ever finds the token expired — the first one after a restart
    included, since startup asks too.

    :returns: whether a new token was minted
    """
    if not config.has_spotify:
        return False
    if _token and time.monotonic() < _token_until - RENEW_AHEAD:
        return False
    await _access_token(config, force=True)
    return True


async def _access_token(config, force=False):
    global _token, _token_until

    if not config.has_spotify:
        raise Failure("Spotify credentials are not configured", 503, "no_spotify")

    if not force and _token and time.monotonic() < _token_until:
        return _token

    secret = base64.b64encode(
        f"{config.spotify_client_id}:{config.spotify_client_secret}".encode()).decode()

    http = await net.session()
    async with http.post(TOKEN_URL,
                         headers={"Authorization": f"Basic {secret}",
                                  "Content-Type": "application/x-www-form-urlencoded"},
                         data="grant_type=client_credentials",
                         timeout=net.timeout(15)) as response:
        if response.status != 200:
            raise Failure(f"Spotify would not issue a token: HTTP {response.status}",
                          502, "spotify_auth")
        payload = await response.json()

    # A minute of margin, so a request is never sent at the exact moment the
    # token lapses.
    _token_until = time.monotonic() + max(60, int(payload.get("expires_in", 3600)) - 60)
    _token = payload["access_token"]
    return _token


async def _get(config, url, params=None, missing=None):
    """
    A GET against the Web API, with the application token on it.

    :param missing: what to say on a 404, or None to answer with None instead —
        which is how a bare id's kind is found out.
    """
    token = await _access_token(config)

    http = await net.session()
    async with http.get(url, params=params,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=net.timeout(15)) as response:
        if response.status == 404:
            if missing is None:
                return None
            raise Failure(missing, 404, "not_found")
        if response.status == 429:
            # Said plainly, because it is the one Spotify error that goes away
            # on its own and the one a caller should wait out rather than retry.
            raise Failure("Spotify is rate limiting this server", 429,
                          "spotify_rate_limit")
        if response.status != 200:
            raise Failure(f"Spotify answered {response.status}", 502, "spotify")
        return await response.json()


async def _every(config, page):
    """
    A paged list, followed to its end or to MAX_TRACKS, whichever comes first.

    :return: the items, and whether there were more of them than were read
    """
    items = list(page.get("items") or [])
    following = page.get("next")

    while following and len(items) < MAX_TRACKS:
        page = await _get(config, following,
                          missing="That list moved while it was being read")
        items.extend(page.get("items") or [])
        following = page.get("next")

    return items[:MAX_TRACKS], bool(following) or len(items) > MAX_TRACKS


def _artists(data):
    return [a["name"] for a in data.get("artists") or [] if a.get("name")]


def _cover(data):
    """The largest artwork, which is what a client with a screen wants."""
    images = data.get("images") or []
    return (max(images, key=lambda i: i.get("width") or 0).get("url")
            if images else None)


def _track(data, album=None):
    """
    One track in the shape the rest of this passes around.

    <p>An `isrc` key only when the object could have carried one: a track read
    on its own always does, a track read as part of an album never does, and
    the difference matters to {@link match.to_youtube}, which searches by ISRC
    when there is one and by name when there is not. A key holding None would
    say the recording has no ISRC; no key says nobody asked.
    """
    found = {
        "id": data["id"],
        "title": data["name"],
        "artists": _artists(data),
        "album": album if album is not None else (data.get("album") or {}).get("name"),
        "duration_ms": data.get("duration_ms"),
        "url": (data.get("external_urls") or {}).get("spotify"),
    }

    if "external_ids" in data:
        found["isrc"] = (data["external_ids"] or {}).get("isrc")
    if data.get("track_number"):
        found["track_number"] = data["track_number"]
    if data.get("explicit") is not None:
        found["explicit"] = data["explicit"]
    if data.get("preview_url"):
        found["preview"] = data["preview_url"]

    return found


async def track(config, identifier, missing="No such track on Spotify"):
    """Title, artists, album, length and — the point of all this — the ISRC."""
    data = await _get(config, TRACK_URL.format(identifier), missing=missing)
    return _track(data) if data else None


async def raw_track(config, identifier, missing="No such track on Spotify"):
    """
    A track exactly as the Web API returns it, for `&beta=true`.

    <p>Not reshaped and not matched: the plugin that asks for this does its own
    matching, so the whole object — album, artwork, ISRC and all — is passed
    along untouched.
    """
    return await _get(config, TRACK_URL.format(identifier), missing=missing)


async def _with_isrc(config, items):
    """
    An album's tracks again, in full, for the ISRC the album endpoint omits.

    <p>Its track objects are the simplified kind — no `external_ids`, and so no
    ISRC, which is the one field a match actually turns on. Fifty at a time is
    one extra request for an ordinary album, against one lookup per track later
    on; and it means the list a caller reads already says what would be
    searched for, so choosing from it costs nothing further.
    """
    ids = [item["id"] for item in items if item.get("id")]
    full = {}

    for start in range(0, len(ids), BATCH):
        data = await _get(config, TRACKS_URL,
                          {"ids": ",".join(ids[start:start + BATCH])},
                          missing="Those tracks are gone from Spotify")
        for found in (data or {}).get("tracks") or []:
            if found and found.get("id"):
                full[found["id"]] = found

    # A track the batch did not answer for keeps what the album said about it.
    # A line without an ISRC is better than a gap in the list, and the match
    # falls back to searching by name exactly as it did before.
    return [full.get(item.get("id"), item) for item in items]


async def album(config, identifier, missing="No such album on Spotify"):
    """An album, and every track on it in the order it plays."""
    data = await _get(config, ALBUM_URL.format(identifier),
                      {"limit": ALBUM_PAGE}, missing=missing)
    if not data:
        return None

    items, truncated = await _every(config, data.get("tracks") or {})
    items = await _with_isrc(config, items)
    return {
        "type": "album",
        "id": data["id"],
        "name": data["name"],
        "artists": _artists(data),
        "url": (data.get("external_urls") or {}).get("spotify"),
        "cover": _cover(data),
        "release_date": data.get("release_date"),
        "label": data.get("label"),
        "total": data.get("total_tracks") or len(items),
        "truncated": truncated,
        # The album's own name, rather than each track repeating it back from
        # an `album` object the simplified track objects do not have anyway.
        "tracks": [_track(item, album=data["name"]) for item in items],
    }


#: Spotify's own editorial and algorithmic playlists — Discover Weekly,
#: Today's Top Hits, every "made for you" mix — are 404 to an application
#: token, whoever the application is. There is nothing to fix here, so the
#: message says which of the two happened rather than leaving it as "no such".
NO_PLAYLIST = ("No such playlist on Spotify, or it is one of Spotify's own "
               "editorial playlists, which the Web API no longer serves to "
               "applications")


async def playlist(config, identifier, missing=NO_PLAYLIST):
    """A playlist, and the tracks on it that are tracks."""
    data = await _get(config, PLAYLIST_URL.format(identifier),
                      {"limit": PLAYLIST_PAGE}, missing=missing)
    if not data:
        return None

    items, truncated = await _every(config, data.get("tracks") or {})

    # A playlist holds more than tracks: podcast episodes, and local files that
    # exist only on the machine that added them. Neither has anything to look
    # for on YouTube, and both arrive here with no id or the wrong type.
    tracks = [_track(entry["track"]) for entry in items
              if (entry.get("track") or {}).get("type") == "track"
              and entry["track"].get("id")]

    owner = data.get("owner") or {}
    return {
        "type": "playlist",
        "id": data["id"],
        "name": data["name"],
        "owner": owner.get("display_name") or owner.get("id"),
        "description": data.get("description") or None,
        "url": (data.get("external_urls") or {}).get("spotify"),
        "cover": _cover(data),
        "total": (data.get("tracks") or {}).get("total", len(tracks)),
        "truncated": truncated,
        "tracks": tracks,
    }


async def entity(config, kind, identifier):
    """
    Whatever the identifier names.

    :param kind: one of KINDS, or None to try each in turn — a bare id says
        nothing about what it names, and a wrong guess is one 404.
    :return: the kind, and what was read
    """
    readers = {"track": track, "album": album, "playlist": playlist}

    if kind:
        return kind, await readers[kind](config, identifier)

    for guess in KINDS:
        found = await readers[guess](config, identifier, missing=None)
        if found:
            return guess, found

    raise Failure("Nothing on Spotify with that id", 404, "not_found")
