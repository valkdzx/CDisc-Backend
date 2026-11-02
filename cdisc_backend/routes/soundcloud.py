"""A SoundCloud link, answered with its metadata and a direct audio URL."""
from urllib.parse import urlparse

from ..soundcloud import api
from ..web import Failure, json


def _artwork(url):
    """The large artwork, rather than the 100px thumbnail the API defaults to."""
    return url.replace("-large.", "-t500x500.") if url else None


async def handle_soundcloud(request):
    url = request.rel_url.query.get("url", "").strip()
    host = urlparse(url).hostname or ""
    if not (host == "soundcloud.com" or host.endswith(".soundcloud.com")
            or host == "on.soundcloud.com"):
        raise Failure("Missing or non-SoundCloud url", 400, "bad_url")

    track = await api.resolve(url)
    if track.get("kind") != "track":
        raise Failure(f"That link is a {track.get('kind')}, not a track",
                      400, "not_a_track")
    if not track.get("streamable") or track.get("policy") == "BLOCK":
        raise Failure("This track cannot be streamed", 403, "not_streamable")

    transcoding, stream_url = await api.best_stream(track)
    if not stream_url:
        raise Failure("No playable stream for this track", 502, "no_stream")

    user = track.get("user") or {}
    publisher = track.get("publisher_metadata") or {}
    fmt = transcoding.get("format", {})

    return json({
        "id": track["id"],
        "title": track.get("title"),
        "artist": publisher.get("artist") or user.get("username"),
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "url": user.get("permalink_url"),
            "avatar": _artwork(user.get("avatar_url")),
        },
        "duration": (track.get("full_duration") or track.get("duration") or 0) / 1000,
        "genre": track.get("genre"),
        "tags": track.get("tag_list"),
        "description": track.get("description"),
        "artwork": _artwork(track.get("artwork_url") or user.get("avatar_url")),
        "created_at": track.get("created_at"),
        "release_date": track.get("release_date"),
        "plays": track.get("playback_count"),
        "likes": track.get("likes_count"),
        "isrc": publisher.get("isrc"),
        "url": track.get("permalink_url"),
        "stream": {
            "url": stream_url,
            "protocol": fmt.get("protocol"),
            "mime_type": fmt.get("mime_type"),
            "preset": transcoding.get("preset"),
            # A Go+ track without an account plays only its 30-second preview.
            "snipped": transcoding.get("snipped", False),
        },
    })
