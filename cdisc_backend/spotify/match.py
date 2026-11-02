"""Finding the YouTube video of a Spotify recording.

<p>By ISRC first, because a title lies too easily: the remaster, the cover with
the same name, the karaoke version and the sped-up edit are all "the song". An
ISRC names one recording, and when YouTube returns an Art Track for it — the
label's own upload, on the auto-generated "Artist - Topic" channel — that is
the same master the streaming service plays.

<p>It does not always find one. Art Tracks exist for catalogue that a label put
there, so live recordings and unsigned releases often have none, and then this
falls back to searching for the name and has to choose. Which of the two
happened is in the answer, because a wrong guess should be visible rather than
presented as a certainty.
"""
import asyncio
import re
import unicodedata

from ..web import Failure
from ..youtube import search

#: How far a video's length may sit from Spotify's. An Art Track agrees to the
#: second; the slack is for uploads that carry a little silence at the end.
SLACK_SECONDS = 5


def _normalise(text):
    """Casefolded, stripped of punctuation, for comparing names."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).casefold()
    return re.sub(r"[^\w\s]", " ", text).strip()


def _is_art_track(result):
    channel = result.get("channel") or ""
    return channel.endswith(" - Topic")


def _plausible(result, title, artists):
    """
    Whether this result could be the recording at all.

    <p>Length alone is not enough and that is not theoretical: a query YouTube
    has no answer for is padded with whatever it has, and one such video
    matched a track's length to within half a second and went onto a disc as a
    news report. So something has to agree by name — the channel, or the title
    — unless it is an Art Track, which is the label's own upload and needs no
    corroboration.
    """
    if _is_art_track(result):
        return True

    found = _normalise(result.get("title"))
    channel = _normalise(result.get("channel"))
    wanted = _normalise(title)

    if wanted and found and (wanted in found or found in wanted):
        return True

    return any(performer and (performer in found or performer in channel)
               for performer in (_normalise(a) for a in artists))


def _pick(results, title, artists, want_seconds):
    """Art Tracks first, then whatever sits closest to the right length."""
    candidates = [r for r in results if _plausible(r, title, artists)]
    if not candidates:
        return None

    def fits(result):
        if want_seconds is None or not result.get("duration"):
            return True
        return abs(result["duration"] - want_seconds) <= SLACK_SECONDS

    def rank(result):
        drift = (0 if want_seconds is None or not result.get("duration")
                 else abs(result["duration"] - want_seconds))
        return (0 if _is_art_track(result) else 1, drift)

    return min([r for r in candidates if fits(r)] or candidates, key=rank)


def _text_queries(track):
    """
    "Artist - Title" by the main artist, then by all of them.

    <p>The main artist alone first, because a featured one can sink the whole
    query: "CUPSIZE, heavens archives - тогда мы не были вдвоем" finds nothing
    on YouTube, while "CUPSIZE - тогда мы не были вдвоем" finds the song.
    """
    artists = [a for a in track["artists"] if a]
    if not artists:
        return [track["title"]]

    queries = ["%s - %s" % (artists[0], track["title"])]
    if len(artists) > 1:
        queries.append("%s - %s" % (", ".join(artists), track["title"]))
    return queries


async def to_youtube(track, web_client_version):
    """
    :param track: what spotify.api.track returned
    :return: the chosen result, and how it was chosen
    """
    want = track["duration_ms"] / 1000 if track.get("duration_ms") else None
    queries = _text_queries(track)

    # The ISRC and the first title query asked at once. The ISRC still wins
    # when it finds something; but much of the catalogue has no Art Track —
    # Russian labels' above all — and there the ISRC search is a second spent
    # learning nothing before the title search that finds it had even begun.
    # One search wasted when the ISRC answers is the cheaper side of that.
    by_isrc = (asyncio.ensure_future(search.find(track["isrc"], web_client_version, limit=5))
               if track.get("isrc") else None)
    by_title = asyncio.ensure_future(search.find(queries[0], web_client_version, limit=6))

    try:
        if by_isrc is not None:
            chosen = _pick(await by_isrc, track["title"], track["artists"], want)
            if chosen:
                return chosen, "isrc"

        for position, query in enumerate(queries):
            found = (await by_title if position == 0
                     else await search.find(query, web_client_version, limit=6))
            chosen = _pick(found, track["title"], track["artists"], want)
            if chosen:
                return chosen, "title"
    finally:
        for pending in (by_isrc, by_title):
            if pending is None:
                continue
            if pending.done() and not pending.cancelled():
                # Read, so an error in the search nobody needed is not
                # reported later as one that was never retrieved.
                pending.exception()
            else:
                pending.cancel()

    raise Failure("Nothing on YouTube looks like that recording", 404, "no_match")
