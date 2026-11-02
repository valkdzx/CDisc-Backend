"""Borrowing a proof-of-origin pair from a browser that earned it.

<p>An age-restricted video is served to this account as a sixty second sample,
and no token we can build changes that. Measured, and not briefly: a token
minted directly — bound to the video, to the visitor id, or to the account's
datasync id — is refused in every position it can be put, on the player call,
in the SABR request, and as `pot` on the URL. Google signs all of them. YouTube
grades the environment they were made in, and a script is not a browser.

<p>What is accepted is a token a real browser produced while playing that very
video, signed into that very account. Then the whole track arrives: protection
status 1 over SABR, and 206 with every byte on a plain videoplayback URL, where
the same URL without the token answers 403 and nothing.

<p>So this runs one. `generate-browser.js` drives Chrome, plays the video and
reads the pair off the request the page itself sent. It has to be a browser
with somewhere to draw — headless produces a pair that is never attested, which
fails as a track that stops a minute in rather than as an error — so on a
server it wants Xvfb.

<p>It costs a browser start per video, so the answer is kept: a track played
twice does not pay twice, and ten listeners starting the same track at once
launch one browser between them rather than ten.
"""
import asyncio
import json
import logging
import os
import time

from .. import net

log = logging.getLogger("cdisc.harvest")

#: How long a pair is worth reusing. Google reports twelve hours for the
#: integrity token behind it; half of that leaves room for the session
#: rotating underneath, which is the thing that actually ends one early.
DEFAULT_TTL = 6 * 3600

#: Long enough for a browser to start, search, play and settle. Shorter than
#: this and a slow machine looks like a failure.
DEFAULT_TIMEOUT = 240

#: How many browsers /warm may have running at once. Each is a Chrome, and the
#: route answers anyone, so a burst of made-up video ids must not become a
#: burst of browsers. A queue is played a track at a time; two is plenty.
MAX_WARMING = 2


async def session_cookies(config):
    """
    The browser profile's own session, as a cookies file.

    <p>Worth preferring over anything on disk. A browser keeps its session
    current because that is what browsers do, while an exported file starts
    ageing the moment it is written — and the two disagreeing is what an
    age-restricted video refusing for no visible reason usually turns out to
    be.

    :returns: Netscape text, or None when there is no service or no session in
              it
    """
    # Switched off with the rest of the harvest: a mint-url left in the config
    # must not quietly bring back the browser session that was turned off.
    base = getattr(config, "harvest_mint_url", "")
    if not base or not getattr(config, "harvest_enabled", False):
        return None

    try:
        http = await net.session()
        async with http.get(f"{base}/cookies", timeout=net.timeout(60)) as response:
            if response.status != 200:
                log.warning("The minting service would not hand over cookies (%d)",
                            response.status)
                return None
            text = await response.text()
    except Exception as e:                                       # noqa: BLE001
        log.warning("Could not read cookies from %s: %s", base, e)
        return None

    # Signed out, the profile still has consent and visitor cookies in it, and
    # writing those over a working session would be a downgrade dressed as an
    # update.
    if "SAPISID" not in text:
        log.info("The browser profile is not signed in; keeping the cookies file")
        return None

    # And having a SAPISID is not the same as being accepted. A revoked session
    # keeps every cookie it had, so this asks the minting service — which
    # checks by making a signed-in request rather than by looking at the jar —
    # before letting the profile overwrite the file. Anything but a clear yes
    # leaves the file alone: it is the spare, and a spare is only worth having
    # while nothing can quietly replace it with a dead copy.
    if not await _profile_accepted(base):
        return None

    return text


async def _profile_accepted(base):
    """Whether the minting service says its profile is really signed in."""
    try:
        http = await net.session()
        async with http.get(f"{base}/health", timeout=net.timeout(30)) as response:
            if response.status != 200:
                log.warning("The minting service would not report its health (%d)",
                            response.status)
                return False
            health = await response.json()
    except Exception as e:                                       # noqa: BLE001
        log.warning("Could not ask %s whether it is signed in: %s", base, e)
        return False

    if health.get("signedIn") is True:
        return True

    log.info("The browser profile's session is %s; keeping the cookies file",
             "refused" if health.get("signedIn") is False else "unverifiable")
    return False


class Harvester:
    """Pairs, one browser run at a time, remembered while they last."""

    def __init__(self, config):
        self.config = config
        #: video id -> (taken at, po_token, visitor_data)
        self._pairs = {}
        #: video id -> lock, so one run serves everyone waiting on it
        self._locks = {}
        #: runs started by warm(), held so they are not collected mid-run
        self._warming = set()

    @property
    def enabled(self):
        if not self.config.harvest_enabled:
            return False
        return bool(self.config.harvest_mint_url or self.config.harvest_script)

    @property
    def live(self):
        """Whether there is a service holding a browser, rather than a script."""
        return bool(self.config.harvest_mint_url)

    def describe(self):
        """What is held, without saying which videos anyone played."""
        return {"enabled": self.enabled, "live": self.live,
                "pairs": len(self._pairs)}

    def cached(self, video_id):
        """A pair already in hand, or None."""
        found = self._pairs.get(video_id)
        if not found:
            return None

        at, token, visitor = found
        if time.time() - at > self.config.harvest_ttl:
            self._pairs.pop(video_id, None)
            return None
        return token, visitor

    def warm(self, video_id, title=None):
        """
        Starts earning a pair now, for a video that will be played shortly.

        <p>The wait is real — a browser start — and it lands on whoever presses
        play first. It does not have to: a disc goes into a jukebox before it
        is heard, and a queue says what is coming. Asked at that moment, the
        pair is ready by the time anyone is waiting on it.

        <p>Returns at once. A failure here costs nothing, since the play path
        will simply earn one itself.

        <p>Nothing to do with a service: it mints in milliseconds and nothing
        it mints is kept, so warming would only be a token thrown away — one
        that anybody on the internet could ask for in a loop. Without one, the
        route is open too, so it starts at most {@link MAX_WARMING} browsers
        at once and turns the rest away.
        """
        if not self.enabled or self.live or self.cached(video_id):
            return None
        if len(self._warming) >= MAX_WARMING:
            return None
        task = asyncio.ensure_future(self.pair(video_id, title))
        self._warming.add(task)
        task.add_done_callback(self._warming.discard)
        return task

    def forget(self, video_id):
        """
        Drops a pair that has stopped working.

        <p>With a service, the pair is not the thing that went stale — the
        attestation behind it is, and every token minted since is equally
        dead. So it is told to earn a new one rather than asked again.
        """
        self._pairs.pop(video_id, None)
        if self.live:
            asyncio.ensure_future(self._reattest())

    async def _ask(self, video_id):
        """One token from the service, or None when it cannot give one."""
        base = self.config.harvest_mint_url
        try:
            http = await net.session()
            async with http.post(f"{base}/mint", json={"videoId": video_id},
                                 timeout=net.timeout(30)) as response:
                payload = await response.json()
                if response.status != 200:
                    log.warning("The minting service refused %s: %s",
                                video_id, payload.get("error"))
                    return None
        except Exception as e:                                   # noqa: BLE001
            log.warning("Could not reach the minting service at %s: %s", base, e)
            return None

        token, visitor = payload.get("poToken"), payload.get("visitorData")
        if not token or not visitor:
            return None
        return token, visitor

    async def _reattest(self):
        base = self.config.harvest_mint_url
        try:
            http = await net.session()
            async with http.post(f"{base}/reattest", timeout=net.timeout(120)) as response:
                if response.status == 200:
                    log.info("The minting service attested again")
                else:
                    log.warning("The minting service could not attest again (%d)",
                                response.status)
        except Exception as e:                                   # noqa: BLE001
            log.warning("Could not ask the minting service to attest: %s", e)

    async def pair(self, video_id, title=None):
        """
        A pair for this video, from cache or from a browser.

        :param title: what to search for, since the harvester reaches the video
                      through a search result rather than the watch URL — a
                      watch page loaded directly inlines its player response
                      and sends no request to read a token off
        :returns: (po_token, visitor_data), or None when there is no harvester
                  configured or the run failed
        """
        if not self.enabled:
            return None

        # A service mints in milliseconds, so there is nothing to cache and
        # nothing to serialise: caching would only mean holding a token from
        # before the last re-attestation.
        if self.live:
            return await self._ask(video_id)

        held = self.cached(video_id)
        if held:
            return held

        lock = self._locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            # Somebody may have finished the run while we waited for the lock.
            held = self.cached(video_id)
            if held:
                return held

            pair = await self._run(video_id, title)
            if pair:
                self._pairs[video_id] = (time.time(), pair[0], pair[1])
            return pair

    async def _run(self, video_id, title):
        environment = dict(os.environ)
        environment["HARVEST_VIDEO"] = video_id
        # The id works as a search term when nothing better is known: YouTube
        # answers it with that video. A title is better when we have one.
        environment["HARVEST_QUERY"] = title or video_id
        # How long the browser keeps watching after the token has already gone
        # out. Ten seconds was most of a first play; the pair is captured on
        # the request, and dwelling only collects further candidates to choose
        # between.
        environment["HARVEST_DWELL"] = str(self.config.harvest_dwell)
        if self.config.cookie_file:
            environment["HARVEST_COOKIES"] = str(self.config.cookie_file)
        if self.config.harvest_display:
            environment["DISPLAY"] = self.config.harvest_display

        command = [self.config.harvest_node, self.config.harvest_script, "--json"]
        log.info("Harvesting a token for %s — starting a browser", video_id)

        began = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=os.path.dirname(self.config.harvest_script) or None,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            log.warning("Could not start the harvester (%s): %s", command[0], e)
            return None

        try:
            out, err = await asyncio.wait_for(process.communicate(),
                                              timeout=self.config.harvest_timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            log.warning("The harvester did not finish in %ds for %s",
                        self.config.harvest_timeout, video_id)
            return None

        took = time.monotonic() - began
        if process.returncode != 0:
            # Its own last line says more than the exit code does.
            reason = (err or b"").decode("utf-8", "replace").strip().splitlines()
            log.warning("The harvester failed for %s after %.0fs: %s",
                        video_id, took, reason[-1] if reason else "no output")
            return None

        try:
            payload = json.loads((out or b"").decode("utf-8", "replace"))
            token = payload["poToken"]
            visitor = payload["visitorData"]
        except (ValueError, KeyError) as e:
            log.warning("The harvester said something unreadable for %s: %s",
                        video_id, e)
            return None

        log.info("Harvested a token for %s in %.0fs (%d chars)",
                 video_id, took, len(token))
        return token, visitor
