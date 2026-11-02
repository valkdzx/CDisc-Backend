"""The application: what is wired to what."""
import asyncio
import contextlib
import logging

from aiohttp import web

from . import net
from .config import Config
from .routes import audio, misc, music, soundcloud, spotify
from .spotify import api as spotify_api
from .web import errors, require_diagnostics
from .youtube import account, harvest, player_script, visitor
from .youtube.cookies import CookieJar

log = logging.getLogger("cdisc")


def build_app(config=None):
    config = config or Config.load()

    app = web.Application(middlewares=[errors])
    app["config"] = config

    # Read once at startup rather than per request: a cookies file is small but
    # it is also the thing an admin edits at three in the morning, and reading
    # it mid-request means half the requests see the old one. /reload-cookies
    # is how it comes back without a restart.
    app["cookies"] = CookieJar.load(config.cookie_file)

    # The account as a TV, when one has been linked with oauth_setup.py.
    # Installed for the ladder to find; an unlinked one simply never answers.
    app["account"] = account.install(account.Account.load(config))

    # Background reads, held so that nothing collects them mid-track.
    app["filling"] = set()

    app.router.add_get("/", misc.handle_index)
    app.router.add_get("/search", misc.handle_search)
    app.router.add_get("/cdisc/public-youtube-data", misc.handle_public_data)

    app.router.add_get("/audio/{video_id}", audio.handle_audio)
    app.router.add_get("/info/{video_id}", audio.handle_info)
    # The name the plugin has always called it by. Kept so a server can be
    # pointed at this backend without touching its config.
    app.router.add_get("/get-info/{video_id}", audio.handle_info)
    app.router.add_get("/streams/{video_id}", audio.handle_streams)
    # POST, because it starts a browser rather than answering a question.
    app.router.add_post("/warm/{video_id}", audio.handle_warm)
    app.router.add_get("/sabr-test/{video_id}", audio.handle_sabr_test)

    app.router.add_get("/spotify", spotify.handle_spotify)
    app.router.add_get("/spotify/{id}", spotify.handle_spotify)

    app.router.add_get("/soundcloud", soundcloud.handle_soundcloud)

    # Yandex Music and VK: the same four routes each, the way YouTube has them.
    for service in music.SERVICES:
        app.router.add_get(f"/{service}", music.handle_link)
        app.router.add_get(f"/{service}/search", music.handle_search)
        app.router.add_get(f"/{service}/info/{{id}}", music.handle_info)
        app.router.add_get(f"/{service}/audio/{{id}}", music.handle_audio)

    app.router.add_post("/reload-cookies", _reload_cookies)

    app.on_startup.append(_startup)
    app.on_shutdown.append(_keep_the_session)
    app.on_cleanup.append(_cleanup)
    return app


async def _reload_cookies(request):
    """
    Picks the session up again: from the browser profile first, the file after.

    <p>The order is the point. A file that has just been replaced by hand is
    the reason this route exists, but a running browser holds something fresher
    than any file, and asking it first means the common case needs no file at
    all.

    <p>Behind the diagnostics token. It is an admin's lever — it wakes the
    browser and rewrites the session file — and the internet has no business
    pulling it in a loop.
    """
    require_diagnostics(request)
    config = request.app["config"]
    text = await harvest.session_cookies(config)

    if text:
        jar = CookieJar.parse(text, source=str(config.cookie_file or ""))
        if jar.signed_in:
            request.app["cookies"] = jar
            if config.cookie_file:
                jar.save()
            return web.json_response(dict(jar.describe(), from_browser=True))

    request.app["cookies"] = CookieJar.load(config.cookie_file)
    return web.json_response(dict(request.app["cookies"].describe(),
                                  from_browser=False))



async def _adopt_browser_session(app):
    """
    Takes the session from the browser profile, when there is one.

    <p>Which settles who owns the account. Two things keeping one session
    current is how it gets lost: each refreshes it, neither sees the other's
    refresh, and the older copy is refused. The browser is the better owner —
    it is a browser — so the file here becomes a copy of what it holds rather
    than a second original.
    """
    config = app["config"]
    text = await harvest.session_cookies(config)
    if not text:
        return

    jar = CookieJar.parse(text, source=str(config.cookie_file or ""))
    if not jar.signed_in:
        return

    app["cookies"] = jar
    if config.cookie_file:
        # Written down as well, so a restart with the service off still has an
        # account, and so the file and the profile do not drift apart.
        jar.save()

    log.info("Adopted the browser profile's session (%d cookies)", len(jar.cookies))


async def _startup(app):
    await _adopt_browser_session(app)
    cookies = app["cookies"]
    if cookies.signed_in:
        log.info("YouTube account: signed in, %d cookies from %s",
                 len(cookies.cookies), cookies.source)
    elif cookies.present:
        log.warning("YouTube cookies file has no session in it — age-restricted "
                    "videos will still be refused. Export it while signed in.")
    elif app["account"].linked:
        log.info("YouTube cookies: none. The linked TV account answers for age gates.")
    else:
        log.info("YouTube account: none. Age-restricted videos will be refused.")

    if cookies.signed_in:
        app["sweeper"] = asyncio.create_task(_sweep(app))

    # Minted here so the first real request does not pay for it, and so a
    # server that cannot reach YouTube at all says so at startup.
    # Both are cached for hours; both cost a page fetch and, for the player
    # script, a megabyte of JavaScript. Paying for them here rather than in
    # the first request is the difference between a track that starts and a
    # track that starts eventually.
    # The account's own visitor id is a third page fetch, and a heavy one —
    # the signed-in home page is close to two megabytes. Left to the first
    # request, it is most of the wait before the first track plays.
    warm = [visitor.current(), player_script.signature_timestamp(),
            _quietly("Spotify token", spotify_api.renew_if_due(app["config"]))]
    if cookies.signed_in:
        warm.append(visitor.for_account(cookies))
    # The TV page and a first access token: two Google round trips that
    # otherwise land on the first age-restricted play.
    if app["account"].linked:
        warm.append(app["account"].warm())
    else:
        log.info("YouTube TV account: none linked (python oauth_setup.py)")

    minted, stamp = (await asyncio.gather(*warm))[:2]
    if minted:
        log.info("Visitor id: minted")
    else:
        log.warning("Could not mint a visitor id — YouTube may be unreachable")

    if stamp:
        log.info("Player script: %s", player_script.script_url())

    app["keeper"] = asyncio.create_task(_keep_fresh(app))


#: How often the keeper looks. Everything it renews is renewed ten minutes
#: before it lapses, so a look every two minutes is always in time.
KEEPER_INTERVAL = 120


async def _keep_fresh(app):
    """
    Renews every token and id before a request could find it expired.

    <p>Each of them would otherwise be renewed by whichever request arrived
    after it lapsed — a Spotify token every hour, a visitor id every hour, the
    TV account's access token every hour, the player script every few — and
    that request waited a round trip or two for it. Here nobody waits: the
    renewal happens between requests, and a failure is logged and tried again
    on the next look rather than handed to a listener.
    """
    config = app["config"]
    tv = app["account"]
    while True:
        await asyncio.sleep(KEEPER_INTERVAL)
        jobs = [_quietly("Spotify token", spotify_api.renew_if_due(config)),
                _quietly("visitor id", visitor.renew_if_due()),
                _quietly("player script", player_script.renew_if_due())]
        if tv.usable:
            # token() renews by itself once it is within ten minutes of lapsing.
            jobs.append(_quietly("TV account token", tv.token()))
        await asyncio.gather(*jobs)


async def _quietly(what, job):
    """Runs one renewal, logging rather than raising when it fails."""
    try:
        return await job
    except asyncio.CancelledError:
        raise
    except Exception as e:                                       # noqa: BLE001
        log.warning("Could not renew the %s: %s", what, getattr(e, "message", None) or e)
        return None


async def _sweep(app):
    """
    Writes a refreshed session back, now and then.

    <p>The values arrive one reply at a time and are cheap to hold, so this is
    only about the file: a disk write per request would be absurd, and never
    writing at all is how an exported session quietly ages out. Every minute is
    far more often than Google rotates anything.
    """
    cookies = app["cookies"]
    try:
        while True:
            await asyncio.sleep(60)
            cookies.maybe_save()
    except asyncio.CancelledError:
        raise


async def _keep_the_session(app):
    """On the way down, write whatever the last requests picked up."""
    for name in ("sweeper", "keeper"):
        task = app.get(name)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    cookies = app["cookies"]
    if cookies.signed_in:
        cookies.save()


async def _cleanup(app):
    await net.close()
