"""Getting a playable audio URL, and being honest about it when there isn't one.

<p>There is a ladder here rather than a single call, because YouTube's answer
depends on who asks and what they can prove. Each rung is tried only when the
one above it failed for a reason the next rung could actually fix, and the
result says which rung answered — a track that only plays signed in is worth
knowing about before the cookies expire.
"""
import asyncio
import logging
import re
import time

from .. import net
from ..web import Failure
import base64

from . import (account, cipher, clients, harvest, innertube, player_script,
               sabr, visitor)

log = logging.getLogger("cdisc.streams")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Only used to ask the browser client what the account can see, which is a
#: diagnosis rather than a stream, so the exact version matters little.
WEB_VERSION = "2.20260828.01.00"

#: Statuses that mean "this might work as somebody else". Anything outside this
#: is about the video rather than about who is asking, and retrying with an
#: account only spends the account.
NEEDS_ACCOUNT = {"LOGIN_REQUIRED", "AGE_VERIFICATION_REQUIRED"}

#: The signed-in browser answer, kept for the few seconds between the ladder
#: asking it and the SABR planner needing the very same thing. Two identical
#: /player calls per track is a third of the wait before a note is heard, and
#: the second one cannot say anything the first did not.
_RECENT = {}
_RECENT_TTL = 30


def _remember(video_id, payload):
    _RECENT[video_id] = (time.monotonic(), payload)
    # Nothing here is worth a sweeper; the map only grows while tracks start.
    if len(_RECENT) > 64:
        for key, (at, _) in list(_RECENT.items()):
            if time.monotonic() - at > _RECENT_TTL:
                _RECENT.pop(key, None)


def _recalled(video_id):
    found = _RECENT.get(video_id)
    if not found:
        return None
    at, payload = found
    if time.monotonic() - at > _RECENT_TTL:
        _RECENT.pop(video_id, None)
        return None
    return payload


def _audio_formats(payload):
    streaming = payload.get("streamingData") or {}
    formats = list(streaming.get("adaptiveFormats") or [])
    formats += list(streaming.get("formats") or [])
    return [f for f in formats if str(f.get("mimeType", "")).startswith("audio")]


def _chosen_audio(payload):
    """
    The audio format `/audio` would end up serving, whichever road it takes.

    <p>Not restricted to formats carrying a `url`, unlike {@link _best}: a
    video served over SABR has no links and is still read and handed back as
    audio, and the highest-bitrate format is what both roads choose. So this
    answers the question "what will come out of the stream" without opening
    it — which is the whole point of a metadata call that is worth making.
    """
    return max(_audio_formats(payload),
               key=lambda f: int(f.get("bitrate") or 0), default=None)


def _audio_facts(chosen):
    """
    Everything about the format that the same answer already carried.

    <p>All of it arrived in the `/player` response either way, so leaving it
    out only means the caller asks again for something we were handed and
    threw away. `loudness_db` is the one worth naming: it is YouTube's own
    normalisation figure, and a player that ignores it plays one track louder
    than the next for no reason anybody can hear a cause for.
    """
    if chosen is None:
        return None
    mime = str(chosen.get("mimeType", ""))
    bitrate = int(chosen.get("bitrate") or 0)
    length = chosen.get("contentLength")
    return {
        "content_type": mime.split(";")[0] or None,
        "codec": (mime.split('codecs="')[1].rstrip('"')
                  if 'codecs="' in mime else None),
        "itag": chosen.get("itag"),
        "ext": "m4a" if "mp4" in mime else "webm",
        "bitrate": bitrate or None,
        "abr": round(bitrate / 1000) if bitrate else None,
        "sample_rate": (int(chosen["audioSampleRate"])
                        if chosen.get("audioSampleRate") else None),
        "channels": chosen.get("audioChannels"),
        # Only on the formats that carry a link; SABR answers omit it, and a
        # guess would be worse than the absence.
        "content_length": int(length) if length else None,
        "duration_ms": (int(chosen["approxDurationMs"])
                        if chosen.get("approxDurationMs") else None),
        "loudness_db": chosen.get("loudnessDb"),
    }


def _microformat(payload):
    """The half of the answer that `videoDetails` does not repeat."""
    return (payload.get("microformat") or {}).get("playerMicroformatRenderer") or {}


def _artwork(details):
    """
    The thumbnails YouTube actually has, rather than the ones we can spell.

    <p>The `img.youtube.com/vi/...` addresses are guesses that happen to be
    right most of the time — `maxresdefault.jpg` is a 404 for a good many
    videos. These came with the answer and are known to exist, so a caller
    that wants a disc face should prefer the largest of them and fall back to
    the guessed ones rather than the other way round.
    """
    shots = ((details.get("thumbnail") or {}).get("thumbnails") or [])
    return [{"url": shot.get("url"),
             "width": shot.get("width"),
             "height": shot.get("height")}
            for shot in shots if shot.get("url")]


def _best(formats):
    """
    The best audio format that can actually be fetched.

    <p>Only ones carrying a `url`. A format with a `signatureCipher` needs the
    player script run against it to become a URL, and one with neither is SABR's
    business — both are real formats and neither is a link.
    """
    playable = [f for f in formats if f.get("url")]
    if not playable:
        return None
    return max(playable, key=lambda f: int(f.get("bitrate") or 0))


def _describe(video_id, payload, chosen, how, user_agent=None):
    details = payload.get("videoDetails") or {}
    mime = chosen.get("mimeType", "")
    return {
        "video_id": video_id,
        "title": details.get("title"),
        "author": details.get("author"),
        "duration": int(details.get("lengthSeconds") or 0),
        "mime_type": mime.split(";")[0],
        "direct_url": chosen["url"],
        "resolved_by": how,
        # The CDN checks this against the client the URL was minted for and
        # answers 403 when it disagrees, so whoever fetches the link next —
        # this backend proxying, or the plugin going direct — has to send it.
        "user_agent": user_agent,
        # Exact, because these URLs are only served in bounded ranges and an
        # open-ended one is refused the same way as no range at all.
        "content_length": (int(chosen["contentLength"])
                           if chosen.get("contentLength") else None),
        "stream": {
            "itag": chosen.get("itag"),
            "abr": round(int(chosen.get("bitrate") or 0) / 1000),
            "ext": "m4a" if "mp4" in mime else "webm",
            "filesize_mb": (round(int(chosen["contentLength"]) / 1048576, 2)
                            if chosen.get("contentLength") else None),
            "url": chosen["url"],
        },
    }


def _pad(text):
    return text + "=" * (-len(text) % 4)


async def sabr_plan(video_id, cookies, config):
    """
    Everything needed to read this video as a SABR conversation.

    <p>The last rung, and the only one that works for a video served to an
    account alone: those answers carry no URLs at all, only an endpoint to talk
    to and a config blob to quote back. Reading it is {@link sabr.SabrStream};
    this is the part that finds out whether there is anything to read.
    """
    web = clients.web(config.web_client_version)
    stamp = await player_script.signature_timestamp()

    # Whose session is this? Everything in the request has to answer that the
    # same way, and an account outranks a token.
    #
    # A po-token comes with the visitor id it was minted against and is refused
    # beside any other, so the two travel together - but that pair belongs to
    # an anonymous browser somewhere else. Sending it alongside an account's
    # cookies tells YouTube two stories and is answered LOGIN_REQUIRED, which
    # looks exactly like having no account at all. Measured: the same video
    # that answers OK to the account's own visitor id answers LOGIN_REQUIRED to
    # the pair's.
    #
    # So: signed in, the session speaks and the token stays home. Signed out,
    # the pair is all there is.
    signed_in = cookies.signed_in
    if signed_in:
        asked = await visitor.for_account(cookies)
        po_token = None
    elif config.po_token and config.visitor_data:
        asked = config.visitor_data
        po_token = config.po_token
    else:
        asked = await visitor.current()
        po_token = config.po_token or None

    # The ladder has usually just asked this exact question. Asking it again
    # costs a round trip before any audio moves, and cannot answer differently.
    payload = _recalled(video_id) if signed_in else None
    if payload is None:
        _, payload = await innertube.player(
            video_id, web, visitor_data=asked,
            cookie_headers=cookies.headers() if cookies.signed_in else None,
            jar=cookies if cookies.signed_in else None,
            signature_timestamp=stamp)

    state = innertube.playability(payload)
    streaming = payload.get("streamingData") or {}
    url = streaming.get("serverAbrStreamingUrl")
    blob = ((payload.get("playerConfig") or {}).get("mediaCommonConfig") or {}
            ).get("mediaUstreamerRequestConfig") or {}
    ustreamer = blob.get("videoPlaybackUstreamerConfig")

    audio = _audio_formats(payload)
    if state["status"] != "OK" or not url or not ustreamer or not audio:
        return None, state

    best = max(audio, key=lambda f: int(f.get("bitrate") or 0))
    video = [f for f in (streaming.get("adaptiveFormats") or [])
             if str(f.get("mimeType", "")).startswith("video")]
    cheapest = min(video, key=lambda f: int(f.get("bitrate") or 0)) if video else None
    details = payload.get("videoDetails") or {}

    # The "n" has to be undone before this URL is any use — see cipher.
    resolved = await cipher.resolve(url, player_script.script_url(),
                                    config.cipher_url, config.cipher_password)

    return {
        "video_id": video_id,
        "url": resolved,
        "ustreamer": base64.urlsafe_b64decode(_pad(ustreamer)),
        "client": web,
        "itag": int(best["itag"]),
        "last_modified": int(best.get("lastModified") or 0),
        "video_itag": int(cheapest["itag"]) if cheapest else None,
        "video_last_modified": int(cheapest.get("lastModified") or 0) if cheapest else 0,
        "po_token": base64.urlsafe_b64decode(_pad(po_token)) if po_token else None,
        "mime_type": str(best.get("mimeType", "")).split(";")[0],
        # What the whole track weighs. SABR never says so — the format does,
        # and it is what lets the reply carry a length and answer a seek.
        "content_length": (int(best["contentLength"])
                           if best.get("contentLength") else 0),
        "duration_ms": int(details.get("lengthSeconds") or 0) * 1000,
        "title": details.get("title"),
        "author": details.get("author"),
        "bitrate": int(best.get("bitrate") or 0),
    }, state


def open_stream(plan):
    """A reader for what sabr_plan found."""
    return sabr.SabrStream(
        plan["url"], plan["ustreamer"], plan["itag"], plan["last_modified"],
        plan["client"], po_token=plan["po_token"],
        video_itag=plan["video_itag"],
        video_last_modified=plan["video_last_modified"],
        duration_ms=plan["duration_ms"])


#: Links already found, so that asking twice for the same track costs once.
#:
#: A player never makes one request. A browser probes, then asks again with a
#: range; a seek is another; the plugin's own reader opens a fresh one whenever
#: it moves. Each of those went down the whole ladder again — measured at 1.5,
#: 3.7 and 1.5 seconds for three identical requests, which is exactly the wait
#: somebody notices before the first note.
#:
#: Five minutes, against a link that lives about six hours. Short enough that a
#: token going stale is never served from here for long, long enough that
#: everything one play does is one resolve.
_RESOLVED = {}
_RESOLVED_TTL = 300


def _recall(video_id):
    found = _RESOLVED.get(video_id)
    if not found:
        return None
    at, data = found
    if time.monotonic() - at > _RESOLVED_TTL:
        _RESOLVED.pop(video_id, None)
        return None
    return data


def _keep(video_id, data):
    _RESOLVED[video_id] = (time.monotonic(), data)
    if len(_RESOLVED) > 256:
        stale = [key for key, (at, _) in _RESOLVED.items()
                 if time.monotonic() - at > _RESOLVED_TTL]
        for key in stale:
            _RESOLVED.pop(key, None)
    return data


def forget_resolved(video_id):
    """Drops a link that has stopped working, so the next play finds another."""
    _RESOLVED.pop(video_id, None)


async def resolve(video_id, cookies, *, allow_account=True, config=None):
    """
    Finds an audio stream for one video, remembering it for a few minutes.

    <p>See {@link _RESOLVED} for why the memory matters more than it looks.
    """
    if not VIDEO_ID.match(video_id or ""):
        raise Failure("Not a YouTube video id", 400, "bad_video_id")

    held = _recall(video_id)
    if held is not None:
        return held

    return _keep(video_id, await _find(video_id, cookies,
                                       allow_account=allow_account, config=config))


async def _find(video_id, cookies, *, allow_account=True, config=None):
    """
    Finds an audio stream for one video.

    :param cookies: a CookieJar; an empty one simply means the signed-in rungs
                    of the ladder are skipped
    :param config: needed only for the Studio rung, which has to descramble a
                   signature and therefore needs the cipher service
    :raises Failure: with a code saying which wall was hit — `age_restricted`,
                     `unavailable`, `no_direct_stream`
    """
    if not VIDEO_ID.match(video_id or ""):
        raise Failure("Not a YouTube video id", 400, "bad_video_id")

    visitor_data = await visitor.current()
    seen = []
    named = {}

    async def attempt(client, headers=None, note="anonymous"):
        status, payload = await innertube.player(
            video_id, client, visitor_data=visitor_data,
            cookie_headers=headers, jar=cookies if headers else None)
        seen.append((client.name, note, status, innertube.playability(payload)))
        details = payload.get("videoDetails") or {}
        if details.get("title") and not named:
            named.update(details)
        return client, payload

    # Every client that hands out links, asked at once. They are alternatives
    # rather than a sequence: nothing is learned by waiting for one before
    # starting the next, and in series they were a fifth of a second added to
    # the wait before the first note — paid by every restricted track, which is
    # precisely the case that could least afford it.
    #
    # The order of preference survives: whichever answered, the first client
    # in the list that has audio is the one taken. And it is taken the moment
    # it answers, rather than after the slowest of the rest: visionOS usually
    # answers first and usually has audio, and waiting on the iPhone client to
    # say the same thing was up to a sixth of a second before the first note.
    pending = [asyncio.ensure_future(attempt(c)) for c in clients.STREAM_CLIENTS]
    capped = []
    try:
        for task in pending:
            client, payload = await task
            chosen = _best(_audio_formats(payload))
            if not chosen:
                continue
            if _is_live(payload, chosen):
                # A link that answers with the newest segment and stops. It is
                # still the way in, read a segment at a time — see live.
                return dict(_describe(video_id, payload, chosen,
                                      client.name.lower(), client.user_agent),
                            live=True)
            if not await _serves_the_end(chosen, client.user_agent):
                capped.append(client.name)
                continue
            return _describe(video_id, payload, chosen,
                             client.name.lower(), client.user_agent)
    finally:
        for task in pending:
            task.cancel()

    wants_account = any(s["status"] in NEEDS_ACCOUNT for _, _, _, s in seen)

    # The account as a TV, before the account as a browser: a revocable grant
    # to one app rather than a whole session in a file, and a link rather than
    # a conversation. The cookies stay behind it for as long as they are
    # configured at all.
    tv = account.current()
    if wants_account and allow_account and tv is not None and tv.usable:
        try:
            linked = await _as_the_tv(video_id, tv, config, named)
        except (account.SignedOut, Failure) as e:
            # Reported, not raised: the cookies below may still answer.
            log.warning("TV account could not play %s: %s", video_id, e)
            linked = None
        if linked:
            return linked

    # Third rung: the browser client, as the account. Not a hopeful attempt —
    # it is how an age-restricted video is served, and what it hands back is a
    # SABR conversation rather than a link, which the caller reads. The app
    # clients are not retried with cookies: they answer HTTP 400 to a browser
    # session every time, being apps, so spending three requests to be told so
    # again only delays the audio.
    if wants_account and allow_account and cookies.signed_in:
        headers = cookies.headers()

        # Straight to the road that works, rather than asking the browser
        # client first whether the account can see this. That question cost a
        # third of a second and changed nothing: when the answer was yes we
        # went on to do this anyway, and when it was no this fails by itself.
        unlocked = await _as_the_browser(video_id, cookies, config, None)
        if unlocked:
            return unlocked

        # Only now, and only to tell two different failures apart: an account
        # that cannot see this video, or a browser that would not unlock it.
        # No longer on the path of a track that plays.
        _, web_payload = await innertube.player(
            video_id, clients.web(WEB_VERSION),
            cookie_headers=headers, jar=cookies,
            signature_timestamp=await player_script.signature_timestamp())
        web_state = innertube.playability(web_payload)
        seen.append(("WEB", "signed in", 200, web_state))

        if web_state["status"] == "OK":
            # Handed to sabr_plan rather than asked for twice.
            _remember(video_id, web_payload)

            raise Failure(
                "The account can see this video, but no client would hand out "
                "a link for it. What serves it is SABR.",
                502, "account_ok_no_stream")

    if capped and not wants_account:
        raise Failure(
            "YouTube handed out a link that stops partway through (%s) — the "
            "rest of the track needs a po-token" % ", ".join(capped),
            502, "capped")

    raise Failure(_why(seen, wants_account, cookies),
                  *_status_and_code(seen, wants_account, cookies))


async def _as_the_tv(video_id, tv, config, known=None):
    """
    The video as a link, asked for by a TV the account is signed in on.

    <p>Formats arrive as `signatureCipher`; the cipher service turns them into
    URLs against the web player, whose signature timestamp is the one sent.
    The link is checked to the end like any other: an account does not make a
    capped link whole.

    :param known: `videoDetails` from an answer that refused the video but
                  still named it
    :returns: a describe() dict, or None when nothing playable came back
    """
    client = await tv.client()
    _, payload = await innertube.player(
        video_id, client, oauth_token=await tv.token(),
        signature_timestamp=await player_script.signature_timestamp())

    state = innertube.playability(payload)
    if state["status"] != "OK":
        log.info("TV account refused %s: %s %s", video_id, state["status"],
                 state["reason"])
        return None

    audio = [f for f in _audio_formats(payload)
             if f.get("url") or f.get("signatureCipher") or f.get("cipher")]
    if not audio:
        return None

    best = max(audio, key=lambda f: int(f.get("bitrate") or 0))
    target = await cipher.unlock(best, player_script.script_url(),
                                 (config.cipher_url if config else None) or cipher.DEFAULT,
                                 (config.cipher_password if config else None) or None)
    if not target:
        return None

    chosen = dict(best, url=target)
    if not await _serves_the_end(chosen, client.user_agent):
        return None

    described = _describe(video_id, payload, chosen, "tv_oauth", client.user_agent)
    if not described["title"] and known:
        # The TV answer often carries no title; the refusals above did.
        described.update(title=known.get("title"), author=known.get("author"),
                         duration=int(known.get("lengthSeconds") or described["duration"]))
    return described


def _is_live(payload, chosen):
    """
    A broadcast still going, as opposed to the recording of one.

    <p>Both halves, because each alone is wrong somewhere: a finished
    broadcast keeps `isLiveContent` and gains a length, and a format can lack
    a length for reasons that have nothing to do with being live.
    """
    details = payload.get("videoDetails") or {}
    return bool(details.get("isLive")) and not chosen.get("contentLength")


async def _serves_the_end(chosen, user_agent):
    """
    Whether the CDN will hand over the last byte of a link, not just the first.

    <p>A link can be a sample wearing a whole track's `contentLength`. The iOS
    client is answered that way for videos made for kids, which visionOS is not
    served at all: measured on one, the CDN gave 206 up to byte 968,066 of
    1,899,360 and 403 from there on — sixty-nine seconds of a two-minute song,
    then silence a player reads as a network fault. The cap sits wherever the
    unattested allowance runs out, which can be past the middle, so the byte
    asked for is the last one: a cap always cuts the end.

    <p>One byte, once per resolve, which is cached. Anything other than a
    refusal counts as serving it: a probe that times out is not a reason to
    fail a track that may well play.
    """
    total = int(chosen.get("contentLength") or 0)
    if not total:
        # Nothing to measure against: a live format has no end to ask for.
        return True

    last = total - 1
    try:
        http = await net.session()
        async with http.get(chosen["url"],
                            headers={"User-Agent": user_agent or "",
                                     "Range": f"bytes={last}-{last}"},
                            timeout=net.timeout(5)) as response:
            await response.read()
            return response.status < 400
    except Exception:                                            # noqa: BLE001
        return True


#: One per process, built the first time an age gate asks for it. A server
#: that never plays a restricted video never makes one, and never needs the
#: browser behind it.
_harvester = None


def harvester(config):
    global _harvester
    if _harvester is None and config is not None:
        _harvester = harvest.Harvester(config)
    return _harvester


async def _as_the_browser(video_id, cookies, config, known):
    """
    The video as a plain link, using a token a browser earned for it.

    <p>The last rung, and the only one that gets the whole of an age-restricted
    track. Everything above is refused or capped: the app clients will not take
    a browser session at all, the browser client is answered with SABR, and
    SABR without attestation hands back sixty seconds and a content length for
    the whole song.

    <p>What lifts it is a token harvested from a real browser playing this very
    video — see {@link harvest}. With it the same request that answered 403
    answers 206 and the whole file, which is why this returns a link rather
    than a stream: nothing here has to read the audio, so seeking is the
    player's own again.

    :returns: a describe() dict, or None when there is no harvester, no pair,
              or nothing playable came back
    """
    collector = harvester(config)
    if collector is None or not collector.enabled:
        return None

    details = (known or {}).get("videoDetails") or {}
    pair = await collector.pair(video_id, details.get("title"))
    if not pair:
        return None

    token, visitor_data = pair

    _, payload = await innertube.player(
        video_id, clients.WEB_CREATOR, visitor_data=visitor_data,
        cookie_headers=cookies.headers(), jar=cookies,
        signature_timestamp=await player_script.signature_timestamp(),
        po_token=token)

    audio = _audio_formats(payload)
    if not audio:
        # The pair is the likeliest thing to have gone stale, and keeping it
        # would make every later play fail the same way.
        collector.forget(video_id)
        return None

    best = max(audio, key=lambda f: int(f.get("bitrate") or 0))
    target = await cipher.unlock(best, player_script.script_url(),
                                 config.cipher_url or cipher.DEFAULT,
                                 config.cipher_password or None)
    if not target:
        return None

    # On the URL, not in the body: the CDN is what checks this one, and it is
    # the difference between 403 with nothing in it and the whole track.
    target += ("&" if "?" in target else "?") + "pot=" + token

    return _describe(video_id, payload, dict(best, url=target),
                     "web_creator+token", clients.WEB_CREATOR.user_agent)


def _stale(seen, cookies):
    """
    Whether the cookies are a session YouTube has stopped accepting.

    <p>Worth separating from "this account cannot see this video", because the
    two need opposite things from a person: one is a video, the other is a
    five-minute re-export. They look identical from outside — a file full of
    cookies, and LOGIN_REQUIRED — and the tell is that we asked *as the
    account* and were still told to sign in.
    """
    if not cookies.signed_in:
        return False
    return any(how != "anonymous" and state["status"] in NEEDS_ACCOUNT
               for _, how, _, state in seen)


def _status_and_code(seen, wants_account, cookies):
    tv = account.current()
    if wants_account and tv is not None and tv.linked and not cookies.present:
        return 403, ("oauth_signed_out" if tv.signed_out else "account_refused")
    if wants_account:
        if not cookies.present:
            return 403, "age_restricted"
        if not cookies.signed_in:
            return 403, "cookies_signed_out"
        if _stale(seen, cookies):
            return 403, "session_stale"
        return 403, "account_refused"

    statuses = {state["status"] for _, _, _, state in seen}
    if statuses & {"ERROR", "UNPLAYABLE"}:
        return 404, "unavailable"
    return 502, "no_direct_stream"


def _why(seen, wants_account, cookies):
    """A sentence a person can act on, not a dump of five failures."""
    tv = account.current()
    if wants_account and tv is not None and tv.linked and not cookies.present:
        if tv.signed_out:
            return ("The linked YouTube account was signed out of the TV — "
                    "run `python oauth_setup.py` again")
        return "This video needs an account, and the linked one was refused"
    if wants_account and not cookies.present:
        return ("This video needs an account and none is configured "
                "(configure CDISC_YOUTUBE_COOKIES or youtube.cookie-file)")
    if wants_account and not cookies.signed_in:
        return ("This video needs an account, and the cookies file has no "
                "session in it — it was probably exported while signed out")
    if wants_account and _stale(seen, cookies):
        return ("The cookies file has a session in it, but YouTube no longer "
                "accepts it — sign in again and export it fresh, from a private "
                "window that is then closed without signing out")
    if wants_account:
        return "This video needs an account, and the configured one was refused"

    for _, _, _, state in seen:
        if state["subreason"]:
            return state["subreason"]
    for _, _, _, state in seen:
        if state["reason"]:
            return state["reason"]
    return "YouTube offered no playable audio for this video"


def _metadata(video_id, payload, how, signed_in):
    """One `/player` answer, read for everything it is carrying."""
    details = payload.get("videoDetails") or {}
    card = _microformat(payload)
    audio = _audio_facts(_chosen_audio(payload))
    views = details.get("viewCount") or card.get("viewCount")
    return {
        "video_id": video_id,
        "title": details.get("title"),
        "content_type": audio["content_type"] if audio else None,
        "author": details.get("author"),
        "channel_id": details.get("channelId"),
        "length_seconds": int(details.get("lengthSeconds") or 0),
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        "thumbnails": {
            "default": f"https://img.youtube.com/vi/{video_id}/default.jpg",
            "medium": f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
            "high": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            "max": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        },
        # Sized and known to exist, unlike the four above.
        "artwork": _artwork(details),

        "description": details.get("shortDescription"),
        "keywords": details.get("keywords") or [],
        "view_count": int(views) if views else None,
        "is_live": bool(details.get("isLiveContent") or details.get("isLive")),
        "is_private": bool(details.get("isPrivate")),
        "is_unlisted": bool(card.get("isUnlisted")),
        "allow_ratings": details.get("allowRatings"),

        # The app clients answer without a microformat block, so these are
        # empty unless the browser rung is the one that answered. Absent
        # rather than wrong: nothing here is worth a request of its own.
        "channel_url": card.get("ownerProfileUrl"),
        "channel_name": card.get("ownerChannelName") or details.get("author"),
        "publish_date": card.get("publishDate"),
        "upload_date": card.get("uploadDate"),
        "category": card.get("category"),
        "is_family_safe": card.get("isFamilySafe"),
        "available_countries": card.get("availableCountries") or [],

        # Which client answered, and what it said about playing: a caller
        # refused by /audio afterwards can see here whether it was the video
        # or the account.
        "answered_by": how,
        "signed_in": signed_in,
        "playability": innertube.playability(payload),

        # What /audio will hand back, as far as it is knowable without
        # opening the stream. Null when no rung offered formats at all.
        "audio": audio,
    }


#: Metadata, kept for as long as a resolved link is. It is the same question
#: asked of the same video, and a title does not change while somebody is
#: looking at it. Worth more than it sounds: a plugin listing a queue asks for
#: every track at once, and the same track comes round again on every replay.
_INFO = {}
_INFO_TTL = 300


def _info_recall(video_id):
    found = _INFO.get(video_id)
    if not found:
        return None
    at, data = found
    if time.monotonic() - at > _INFO_TTL:
        _INFO.pop(video_id, None)
        return None
    return data


def _info_keep(video_id, data):
    _INFO[video_id] = (time.monotonic(), data)
    if len(_INFO) > 256:
        for key, (at, _) in list(_INFO.items()):
            if time.monotonic() - at > _INFO_TTL:
                _INFO.pop(key, None)
    return data


async def metadata(video_id, cookies):
    """
    Title, author and length, without needing anything playable.

    <p>Separate from {@link resolve} because the two questions come apart: a
    video that is age restricted, region blocked or simply refused still
    carries its `videoDetails`, and a disc showing the right name is better
    than one showing "No name" beside an error the player already reported.

    <p>Every rung is asked at once rather than in turn. They are alternatives,
    not a sequence — nothing is learned by waiting for one before starting the
    next — and in series this was the whole cost of the call: when YouTube is
    asking the anonymous clients to prove they are not a bot, all of them fail
    and the answer comes from the account, which in series meant paying for
    every refusal before the one that works. The order of preference survives:
    whichever answered, the first in the list carrying formats is the one
    taken.

    <p>An answer carrying formats is preferred over the merely first one that
    had a title, because they come apart on exactly the videos this is worth
    asking about: an age-gated track is answered LOGIN_REQUIRED by the app
    clients with its name attached and no `streamingData` at all, while the
    browser client as the account has both.
    """
    if not VIDEO_ID.match(video_id or ""):
        raise Failure("Not a YouTube video id", 400, "bad_video_id")

    held = _info_recall(video_id)
    if held is not None:
        return held

    visitor_data = await visitor.current()
    signed_in = cookies.signed_in

    async def ask(client, how, as_account):
        # The account's own visitor id, not the anonymous one: the two
        # disagree, and a session asked about under somebody else's visitor id
        # is answered LOGIN_REQUIRED — which looks exactly like having no
        # account at all.
        if as_account:
            asked = await visitor.for_account(cookies)
            stamp = await player_script.signature_timestamp()
        else:
            asked, stamp = visitor_data, None
        _, payload = await innertube.player(
            video_id, client, visitor_data=asked,
            cookie_headers=cookies.headers() if as_account else None,
            jar=cookies if as_account else None,
            signature_timestamp=stamp)
        if not (payload.get("videoDetails") or {}).get("title"):
            return None
        return _metadata(video_id, payload, how, as_account)

    # In order of preference. The app clients are asked anonymously only: they
    # answer HTTP 400 to a browser session every time, being apps, so asking
    # them as the account was three round trips spent to be told so. The
    # browser client as the account is last because it is the slowest — it
    # waits on a signature timestamp — but it is started alongside the rest
    # rather than after them, because it is the only one that answers for a
    # restricted video and the only one carrying a microformat.
    plan = [(client, client.name.lower(), False) for client in clients.STREAM_CLIENTS]
    if signed_in:
        plan.append((clients.web(WEB_VERSION), "web", True))

    pending = [asyncio.ensure_future(ask(*row)) for row in plan]
    named = None
    try:
        for task in pending:
            try:
                found = await task
            except Exception:  # noqa: BLE001 - one rung failing is not the answer
                continue
            if found is None:
                continue
            if found["audio"]:
                return _info_keep(video_id, found)
            # Keep the first that at least knows the name, in case nothing
            # below it offers anything playable either.
            if named is None:
                named = found
    finally:
        for task in pending:
            task.cancel()

    if named is not None:
        return _info_keep(video_id, named)
    raise Failure("No such video", 404, "unavailable")


async def diagnose(video_id, cookies):
    """
    Every rung of the ladder, tried and reported rather than short-circuited.

    <p>For looking at when something stops working: it answers "does the
    account still work" and "which clients are being refused today" in one
    request, which is otherwise an afternoon with a script.
    """
    visitor_data = await visitor.current()
    account_visitor = await visitor.for_account(cookies) if cookies.signed_in else None
    rows = []

    attempts = [(client, False) for client in clients.STREAM_CLIENTS]
    attempts.append((clients.web(WEB_VERSION), False))
    if cookies.signed_in:
        attempts += [(client, True) for client in clients.STREAM_CLIENTS]
        attempts.append((clients.web(WEB_VERSION), True))
        # The rung that actually carries age-restricted audio, so the one
        # worth seeing in a report when it stops doing so.
        attempts.append((clients.WEB_CREATOR, True))

    stamp = await player_script.signature_timestamp()

    for client, signed in attempts:
        status, payload = await innertube.player(
            video_id, client,
            visitor_data=account_visitor if signed else visitor_data,
            cookie_headers=cookies.headers() if signed else None,
            jar=cookies if signed else None,
            signature_timestamp=stamp)
        formats = _audio_formats(payload)
        streaming = payload.get("streamingData") or {}

        rows.append({
            "client": client.name,
            "as": "account" if signed else "anonymous",
            "http": status,
            **innertube.playability(payload),
            "audio_formats": len(formats),
            "with_url": len([f for f in formats if f.get("url")]),
            # Which of the three shapes a format arrived in decides what could
            # be done about it: a URL plays, a signature can be deciphered, and
            # SABR alone is a protocol rather than a link.
            "with_cipher": len([f for f in formats if f.get("signatureCipher")]),
            "sabr_only": bool(streaming.get("serverAbrStreamingUrl")) and not formats,
            "sabr_url": bool(streaming.get("serverAbrStreamingUrl")),
            "sample": [{
                "itag": f.get("itag"),
                "mime": str(f.get("mimeType", "")).split(";")[0],
                "bitrate": f.get("bitrate"),
                "has_url": bool(f.get("url")),
                "has_cipher": bool(f.get("signatureCipher")),
                "keys": sorted(k for k in f if k in
                               ("url", "signatureCipher", "cipher", "initRange")),
            } for f in formats[:4]],
        })

    return {"video_id": video_id, "visitor_data": bool(visitor_data),
            "signature_timestamp": stamp,
            "player_script": player_script.script_url(),
            "account_visitor_differs": bool(
                account_visitor and account_visitor != visitor_data),
            "cookies": cookies.describe(), "attempts": rows}
