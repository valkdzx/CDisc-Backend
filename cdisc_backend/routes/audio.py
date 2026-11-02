"""Audio: the reason the plugin talks to this at all."""
import asyncio
import base64
import contextlib

from aiohttp import web
from aiohttp.client_exceptions import (ClientConnectionError,
                                     ClientConnectionResetError)

from .. import net
from ..web import Failure, json, require_diagnostics
from .. import tracks
from ..youtube import (cipher, clients, innertube, live, player_script,
                       sabr_probe, streams, visitor)

#: What to pass through when proxying, and nothing else. The CDN checks these
#: against the signature on the URL, so they are not decoration; everything
#: else is our business rather than the client's.
FORWARD = ("Range", "If-Range", "Accept", "Accept-Encoding")
RETURN = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
          "Cache-Control", "Last-Modified", "ETag")

#: Every way a listener can vanish mid-stream. aiohttp raises its own class
#: rather than the builtin for a write to a closed transport, so catching the
#: builtins alone turns an ordinary disconnect into a 500 and a stack trace.
#: The plain ConnectionError too, not only its subclasses: a transport lost
#: while waiting to drain is raised as ConnectionError("Connection lost").
GONE = (ConnectionError, ClientConnectionResetError, ClientConnectionError,
        asyncio.CancelledError)

#: The longest file worth holding in memory for a proxied play. Half an hour
#: of Opus; past that it is a mix or a stream recording, a lot of memory for
#: one listener, and it is passed straight through instead.
HOLD_AT_MOST = 32 * 1024 * 1024

#: How far past what has arrived a seek may land and still be served out of
#: the buffer. About a minute of audio, which arrives in well under a second.
AHEAD = 1024 * 1024


async def handle_audio(request):
    video_id = request.match_info["video_id"]
    query = request.rel_url.query
    cookies = request.app["cookies"]
    config = request.app["config"]

    # ?mode=sabr takes the long road on purpose, for testing it against a
    # video that does not need it.
    forced = query.get("mode", "").lower() == "sabr"

    # A seek is a second request for a track already being read, and the
    # listener is waiting on it with the player stopped. Anything asked of
    # YouTube first — a ladder, a plan, a signature — is time added to a
    # cursor drag for an answer already in memory.
    #
    # A held file, as opposed to a held conversation, only answers ?proxy=true:
    # a plain request wants the link and a redirect wants to be sent to it,
    # and both can still have what they asked for.
    held = tracks.get(video_id)
    proxied = query.get("proxy", "").lower() == "true"
    if (held is not None and not forced and query.get("json", "").lower() != "true"
            and (proxied or not held.exact)):
        return await _serve_held(request, held)

    data = None
    direct_failure = None
    if not forced:
        try:
            data = await streams.resolve(video_id, cookies, config=config)
        except Failure as e:
            direct_failure = e

    if data is None:
        # No client would hand out a link. There may still be a stream — it is
        # simply not a file, and reading it is this backend's job rather than
        # the caller's. Whatever comes out of here is ordinary audio over HTTP.
        plan, state = await streams.sabr_plan(video_id, cookies, config)
        if plan is None:
            if direct_failure is not None:
                raise direct_failure
            # Nothing to fall back on and nothing to blame it on, so say what
            # YouTube actually answered rather than "no".
            raise Failure(
                "No SABR stream was offered: %s%s"
                % (state.get("status") or "no answer",
                   " - " + state["reason"] if state.get("reason") else ""),
                404, "no_sabr")

        if query.get("json", "").lower() == "true":
            base = config.public_url or str(request.url.origin())
            return json({
                "video_id": video_id,
                "title": plan["title"],
                "author": plan["author"],
                "duration": plan["duration_ms"] // 1000,
                "mime_type": plan["mime_type"],
                "resolved_by": "sabr",
                "direct_url": f"{base}/audio/{video_id}",
                "stream": {"abr": round(plan["bitrate"] / 1000),
                           "ext": "webm" if "webm" in plan["mime_type"] else "m4a",
                           "url": f"{base}/audio/{video_id}"},
            })
        return await _stream_sabr(request, plan)

    if data.get("live"):
        # The link plays five seconds and stops, so nobody is handed it: a
        # broadcast is read here and served as audio, the way SABR is, and a
        # caller asking for JSON is pointed back at this route.
        if query.get("json", "").lower() == "true":
            base = config.public_url or str(request.url.origin())
            here = f"{base}/audio/{video_id}"
            return json(dict(data, direct_url=here, resolved_by="live",
                             user_agent=None,
                             stream=dict(data["stream"], url=here)))
        return await _stream_live(request, data)

    if proxied:
        return await _proxy(request, data)

    # The URL carries the address that asked for it in its signature, so a
    # redirect only works when the client comes out of the same address as this
    # server. Otherwise the CDN answers 403 — and does it intermittently, since
    # IPv6 addresses move around.
    if query.get("redirect", "").lower() == "true":
        raise web.HTTPFound(data["direct_url"])

    return json(data)


async def _stream_sabr(request, plan):
    """
    Serves a SABR conversation as an ordinary, seekable audio response.

    <p>The conversation itself cannot be seeked — it is a sequence of segments
    that arrives in order and knows nothing about ranges. So it is read once
    into memory, whole, while the listener is served out of the buffer from
    wherever they asked to start. That is what gives the response a length and
    an `Accept-Ranges`, and therefore a working seek bar.

    <p>The read continues after the listener leaves on purpose: a player that
    seeks abandons its first request and immediately opens another at an offset
    the first had not reached. Stopping the read would make every seek a fresh
    conversation from the beginning.
    """
    video_id = plan["video_id"]
    track = tracks.get(video_id)
    if track is None:
        track = tracks.start(video_id, plan["content_length"], plan["mime_type"])
        filling = asyncio.create_task(_fill(request.app, track, plan))
        # Held, because a task nothing refers to may be collected mid-flight.
        request.app["filling"].add(filling)
        filling.add_done_callback(request.app["filling"].discard)

    return await _serve(request, track)


async def _stream_live(request, data):
    """
    Serves a broadcast as one endless audio response, for as long as anyone
    listens.

    <p>Not buffered, unlike everything else here: there is no end to hold and
    no middle to seek to, so each listener has their own read, and it stops
    the moment they leave rather than carrying on for nobody.
    """
    video_id = data["video_id"]

    async def refresh():
        streams.forget_resolved(video_id)
        fresh = await streams.resolve(video_id, request.app["cookies"],
                                      config=request.app["config"])
        return fresh["direct_url"]

    stream = live.LiveStream(data["direct_url"], data["user_agent"], refresh)
    chunks = stream.chunks()

    # The first segment is waited for before anything is promised, so a
    # broadcast that will not start is an error rather than a 200 with
    # nothing after it.
    try:
        first = await chunks.__anext__()
    except StopAsyncIteration:
        raise Failure("The broadcast sent nothing", 502, "live")

    response = web.StreamResponse(status=200)
    response.headers["Content-Type"] = data["mime_type"]
    response.headers["Cache-Control"] = "no-store"
    await response.prepare(request)

    try:
        await response.write(first)
        async for chunk in chunks:
            await response.write(chunk)
    except GONE:
        return response
    except Failure as e:
        # Mid-stream there is no status left to send; the listener hears the
        # end, and the log says why.
        request.app.logger.warning("Live read of %s stopped: %s", video_id, e.message)
    finally:
        await chunks.aclose()

    with contextlib.suppress(*GONE):
        await response.write_eof()
    return response


async def _serve_held(request, track):
    """
    Answers out of the buffer, once whatever fills it has been heard from.

    <p>A file promises its length before the first byte, so a CDN refusal
    has to be known before anything is promised: otherwise it arrives as a
    200 with a `Content-Length` and no body, which a player reads as a
    network fault rather than as the error it is.
    """
    if track.opened is not None:
        # Shielded: a listener giving up must not cancel the answer for the
        # others waiting on it.
        await asyncio.shield(track.opened)
        if track.failure and not track.data:
            raise Failure(track.failure, 502, "cdn")
    return await _serve(request, track)


async def _serve(request, track):
    """Answers out of the buffer, honouring a range when one is asked for."""
    total = track.size
    wanted = _requested_range(request.headers.get("Range"))

    first, last = 0, (total - 1) if total else None
    if wanted and total:
        first = min(wanted[0], total - 1)
        last = min(wanted[1], total - 1) if wanted[1] is not None else total - 1

    response = web.StreamResponse(status=206 if (wanted and total) else 200)
    response.headers["Content-Type"] = track.mime_type
    response.headers["Cache-Control"] = "no-store"
    if total:
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Content-Length"] = str(last - first + 1)
        if wanted:
            response.headers["Content-Range"] = f"bytes {first}-{last}/{total}"

    await response.prepare(request)

    sent = 0
    try:
        if last is None:
            # No size to promise, so this stays what it used to be: a live
            # stream a player can start but not navigate.
            async for chunk in track.read_from(0, 1 << 62):
                await response.write(chunk)
                sent += len(chunk)
        else:
            async for chunk in track.read_from(first, last):
                await response.write(chunk)
                sent += len(chunk)
    except GONE:
        # The listener walked away, or seeked. Normal, and the read carries on.
        return response

    if track.failure and not sent:
        request.app.logger.warning("Reading %s failed: %s", track.key, track.failure)

    with contextlib.suppress(*GONE):
        await response.write_eof()
    return response


async def _fill(app, track, plan):
    """Reads the whole conversation into the buffer, listener or no listener."""
    stream = streams.open_stream(plan)
    failure = None
    try:
        async for chunk in stream.chunks():
            track.add(chunk)
    except Failure as e:
        failure = e.message
    except Exception as e:                                   # noqa: BLE001
        failure = str(e)
    finally:
        track.finish(failure=failure, limited=stream.limited)

    if failure:
        app.logger.warning("SABR stopped after %d bytes: %s",
                           len(track.data), failure)
    elif stream.limited or track.short:
        # Worth naming rather than logging a byte count: this is the one
        # failure that sounds like a working track until it stops halfway.
        app.logger.warning(
            "SABR served %d of %s bytes (%s of the track) — protection status "
            "%d, the session was not attested. A GVS po-token bound to this "
            "session is what lifts it.",
            len(track.data), track.total or "?",
            "%.0f%%" % (100.0 * len(track.data) / track.total) if track.total else "some",
            stream.protection)


def _requested_range(value):
    """The client's `Range`, as a pair, or None when it did not ask for one."""
    if not value or not value.startswith("bytes="):
        return None
    first, _, last = value[len("bytes="):].partition("-")
    if not first.isdigit():
        return None
    return int(first), (int(last) if last.isdigit() else None)


async def _proxy(request, data):
    """
    Streams the audio through here, so the client never meets the CDN.

    <p>Read once into the buffer, the way a SABR track is, rather than passed
    through request by request. A player never makes one request: a browser
    opens the link, reads enough to see it is audio, drops it and asks again
    from its media player; the plugin asks twice as well; every seek is one
    more. Passed through, each of those was a fresh connection to the CDN —
    anywhere from a tenth of a second to several, measured on the same link
    minutes apart — spent while the player spun. Out of the buffer they cost
    nothing, and the first one costs what it always did.
    """
    total = data.get("content_length")
    if not total or total > HOLD_AT_MOST:
        return await _pass_through(request, data)

    track = tracks.get(data["video_id"])
    if track is None:
        track = tracks.start(data["video_id"], total, data["mime_type"],
                             exact=True)
        track.opened = asyncio.get_running_loop().create_future()
        filling = asyncio.create_task(_fetch(request.app, track, data))
        request.app["filling"].add(filling)
        filling.add_done_callback(request.app["filling"].discard)

    # A seek well past what has arrived would wait for everything before it;
    # a range of its own from the CDN is quicker, and is what it used to get.
    wanted = _requested_range(request.headers.get("Range"))
    if wanted and not track.done and wanted[0] > len(track.data) + AHEAD:
        return await _pass_through(request, data)

    return await _serve_held(request, track)


async def _fetch(app, track, data):
    """Reads a whole file off the CDN into the buffer, listener or no listener."""
    # One bounded range for the whole file: see _pass_through for what the
    # CDN does to a request without one.
    headers = {"Range": f"bytes=0-{track.total - 1}"}
    if data.get("user_agent"):
        headers["User-Agent"] = data["user_agent"]

    failure = None
    try:
        http = await net.session()
        async with http.get(data["direct_url"], headers=headers,
                            timeout=net.stream_timeout()) as upstream:
            if upstream.status >= 400:
                failure = f"The CDN answered {upstream.status}"
            else:
                track.opened.set_result(None)
                async for chunk in upstream.content.iter_any():
                    track.add(chunk)
    except Exception as e:                                   # noqa: BLE001
        failure = str(e) or type(e).__name__
    finally:
        track.finish(failure=failure)
        if not track.opened.done():
            track.opened.set_result(None)

    if failure:
        app.logger.warning("CDN read of %s stopped after %d bytes: %s",
                           track.key, len(track.data), failure)


async def _pass_through(request, data):
    """Streams one request's worth of the CDN's answer, holding nothing."""
    headers = {name: request.headers[name]
               for name in FORWARD if name in request.headers}
    if data.get("user_agent"):
        # The CDN checks this against the client the URL was minted for.
        headers["User-Agent"] = data["user_agent"]

    # Asked for as a range even when nobody asked for a range. A plain GET of a
    # whole file is throttled by the CDN to roughly twice playback speed —
    # measured on this exact track: 56 seconds for 1.8 MB without a Range
    # header, 2.7 seconds with one. Same bytes, same URL, seconds apart.
    asked_for_a_range = "Range" in headers
    if not asked_for_a_range:
        total = data.get("content_length")
        headers["Range"] = f"bytes=0-{total - 1}" if total else "bytes=0-"

    http = await net.session()
    # Not a total timeout: that one counts the body too, so a minute-long
    # ceiling means every track longer than a minute is cut off mid-transfer.
    # What deserves a bound is silence, not length.
    upstream = await http.get(data["direct_url"], headers=headers,
                              timeout=net.stream_timeout())

    if upstream.status >= 400:
        upstream.release()
        raise Failure(f"The CDN answered {upstream.status}", 502, "cdn")

    # The range was ours, so the answer should not look like a range to a
    # caller that asked for a whole file.
    status = 200 if upstream.status == 206 and not asked_for_a_range else upstream.status

    response = web.StreamResponse(status=status)
    for name in RETURN:
        if name == "Content-Range" and not asked_for_a_range:
            continue
        if name in upstream.headers:
            response.headers[name] = upstream.headers[name]
    response.headers.setdefault("Content-Type", data["mime_type"])
    await response.prepare(request)

    try:
        # Whatever has arrived, the moment it arrives. iter_chunked(n) is
        # read(n) underneath, so it holds every byte back until the nth one
        # lands: a 64 KB chunk size is 64 KB of the track withheld at the
        # start, which is silence after somebody pressed play and nothing
        # waiting on it. readany has no threshold at all, and the chunks grow
        # by themselves as the window opens — small while that matters, large
        # once it does not, without a ramp to tune.
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
    except GONE:
        # The listener walked away mid-track. Normal, and not worth a log line.
        pass
    finally:
        upstream.release()

    with contextlib.suppress(*GONE):
        await response.write_eof()
    return response


async def handle_warm(request):
    """
    Earn a token for this video now, so nobody waits for it later.

    <p>Only age-restricted videos ever need one, and only the first play pays
    — but that first play pays a browser start, which is seconds of silence
    after somebody pressed play. A disc goes into a jukebox before it is
    heard, and a queue says what is coming next: asked then, the wait lands
    where there is nothing to interrupt.

    <p>Answers at once. Whether the run succeeds is not the caller's business:
    if it fails, playback earns one the slow way, exactly as it would have.
    """
    video_id = request.match_info["video_id"]
    if not streams.VIDEO_ID.match(video_id or ""):
        raise Failure("Not a YouTube video id", 400, "bad_video_id")

    collector = streams.harvester(request.app["config"])
    if collector is None or not collector.enabled:
        return json({"video_id": video_id, "warming": False,
                     "reason": "no browser is configured here"})

    if collector.live:
        # A service mints at play time in milliseconds: nothing to earn ahead.
        return json({"video_id": video_id, "warming": False, "ready": True})

    held = collector.cached(video_id) is not None
    started = None if held else collector.warm(video_id,
                                                request.rel_url.query.get("title"))

    return json({"video_id": video_id, "warming": started is not None,
                 "ready": held})


async def handle_streams(request):
    """
    Every client tried and what it answered, for when something is odd.

    <p>Behind a token, and a 404 without one rather than a 401: an endpoint
    that says "wrong token" has admitted it exists. With no token configured it
    is switched off entirely, because the default for a route that reports on
    somebody's session should not be "open".
    """
    require_diagnostics(request)

    video_id = request.match_info["video_id"]
    return json(await streams.diagnose(video_id, request.app["cookies"]))


async def handle_info(request):
    """
    Metadata only.

    <p>Answers for videos that will not play, too: an age-restricted track
    still has a name, and a disc labelled correctly beside a clear refusal is
    better than one labelled "No name".

    <p>Carries `content_type` as well — the mime type `/audio` would answer
    with — so a caller that needs both the name and the format asks once
    rather than opening the stream to read a header off it. And with it
    everything else the same `/player` answer already contained: artwork with
    sizes, description, publish date, view count, the playability verdict, and
    an `audio` block down to the loudness figure. None of it costs a request;
    leaving it out only means being asked for it again later.
    """
    video_id = request.match_info["video_id"]
    return json(await streams.metadata(video_id, request.app["cookies"]))


async def handle_sabr_test(request):
    """
    Does the SABR endpoint check the account, or only the URL it issued?

    <p>The one thing standing between an age-restricted track and playing. If
    a stream URL obtained as the account is honoured by a request carrying no
    cookies, the account can stay on this machine and the plugin — which
    already speaks SABR and already sends no cookies — can do the reading. If
    it is refused, it cannot, and the account would have to live on every
    server instead.
    """
    require_diagnostics(request)

    video_id = request.match_info["video_id"]
    cookies = request.app["cookies"]
    config = request.app["config"]

    web_client = clients.web(config.web_client_version)

    # The three have to agree. A po-token is minted against one visitor id and
    # read in the light of the client presenting it, so when there is a pair
    # configured it is the pair's own id that goes with it — not the session's
    # and not a freshly minted one. Getting this wrong is a 403 that looks
    # like a refusal rather than like a mismatch.
    po_token = config.po_token or None
    if po_token and config.visitor_data:
        asked_visitor = config.visitor_data
    else:
        asked_visitor = await visitor.for_account(cookies)

    status, payload = await innertube.player(
        video_id, web_client,
        visitor_data=asked_visitor,
        cookie_headers=cookies.headers() if cookies.signed_in else None,
        signature_timestamp=await player_script.signature_timestamp())

    streaming = payload.get("streamingData") or {}
    url = streaming.get("serverAbrStreamingUrl")
    config_blob = ((payload.get("playerConfig") or {})
                   .get("mediaCommonConfig") or {}).get("mediaUstreamerRequestConfig") or {}
    ustreamer = config_blob.get("videoPlaybackUstreamerConfig")

    if not url or not ustreamer:
        raise Failure("No SABR stream was offered for this video", 404,
                      "no_sabr")

    # The `n` undone first, as sabr_plan does. Asked with the one it was
    # given, the endpoint answers 403 and nothing else whoever is asking, so
    # the question below would never be put — see cipher.
    url = await cipher.resolve(url, player_script.script_url(),
                               config.cipher_url, config.cipher_password)

    audio = [f for f in (streaming.get("adaptiveFormats") or [])
             if str(f.get("mimeType", "")).startswith("audio")]
    if not audio:
        raise Failure("No audio formats in the response", 404, "no_audio")

    best = max(audio, key=lambda f: int(f.get("bitrate") or 0))
    video = [f for f in (streaming.get("adaptiveFormats") or [])
             if str(f.get("mimeType", "")).startswith("video")]
    cheapest = min(video, key=lambda f: int(f.get("bitrate") or 0)) if video else None
    body = sabr_probe.build_request(
        base64.urlsafe_b64decode(ustreamer + "=" * (-len(ustreamer) % 4)),
        int(best["itag"]), int(best.get("lastModified") or 0), web_client,
        po_token=(base64.urlsafe_b64decode(po_token + "=" * (-len(po_token) % 4))
                  if po_token else None),
        video_itag=int(cheapest["itag"]) if cheapest else None,
        video_last_modified=int(cheapest.get("lastModified") or 0) if cheapest else 0)

    results = {}
    for label, headers in (("without cookies", None),
                           ("with cookies", cookies.headers() if cookies.signed_in else None)):
        if label == "with cookies" and not headers:
            continue
        try:
            code, raw = await sabr_probe.ask(
                url, body, cookie_headers=headers,
                user_agent=web_client.user_agent)
            results[label] = {"http": code, "bytes": len(raw),
                              "parts": sabr_probe.read_parts(raw)}
        except Exception as e:  # noqa: BLE001 - reported, not raised
            results[label] = {"error": str(e)}

    return json({
        "video_id": video_id,
        "po_token": bool(po_token),
        "visitor_from": "po-token pair" if (po_token and config.visitor_data)
                        else ("account" if cookies.signed_in else "minted"),
        "playability": innertube.playability(payload),
        "itag": best.get("itag"),
        "mime": str(best.get("mimeType", "")).split(";")[0],
        "sabr": results,
    })
