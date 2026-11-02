"""The account as a television signed in with a code, rather than as a browser.

<p>Why not the cookies: a cookies file is the whole browser session of the
account — everything Google would let that browser do, for as long as the
session lives, stolen with one file. A refresh token from the TV sign-in is
one grant to one app, shown in the account's security page as a device, and
revoked from there without touching anything else. The CDisc plugin speaks
this already (YoutubeAccount.java); this is the same thing on the backend.

<p>What it takes, measured there and repeated here:

<ul>
<li>The OAuth client is the TV app's own, read out of the script the
    `youtube.com/tv` page loads. A token is minted for one client and refused
    by every other, so it has to be that one, and it is re-read when Google
    answers `invalid_client` because the app was rebuilt under a new one.
<li>`/youtubei/v1/player` takes the bearer token from TVHTML5 alone, and
    answers 400 to every other client name.
<li>Only the 5.x line of TVHTML5 is still handed links rather than SABR, and
    the stock Cobalt TV is refused with "The page needs to be reloaded" where a
    Samsung TV is not — hence Tizen, and a version dated off the TV page.
<li>The links come as `signatureCipher`, which the cipher service turns into a
    URL against the web player's script, as for every other client here.
</ul>
"""
import asyncio
import logging
import os
import re
import time
import uuid

from .. import net
from . import clients

log = logging.getLogger("cdisc.account")

TV_PAGE = "https://www.youtube.com/tv"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
SCOPES = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"

COBALT_UA = ("Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold "
             "(unlike Gecko), Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)")
TIZEN_UA = ("Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 "
            "(KHTML, like Gecko) Version/2.4.0 TV Safari/538.1")

_BUNDLE = re.compile(r"""src\s*=\s*['"](/s/_/kabuki/_/js/[^'"]+m=base)['"]""")
_CLIENT = re.compile(r'clientId:"([^"]+)",[^"]*?:"([^"]+)"')
_VERSION = re.compile(r'"INNERTUBE_CLIENT_VERSION":"\d+\.(\d{8})')

#: The TV page is read for the client and the version date; both change with
#: an app release, which is rarer than this.
PAGE_TTL = 6 * 3600

#: An access token lives an hour. Renewed this long before it runs out, so
#: no request waits on Google.
RENEW_AHEAD = 600

#: What the version is built from when the TV page could not be read.
FALLBACK_DATE = "20261014"


class SignedOut(Exception):
    """The refresh token was revoked, or the account signed out of the TV."""


#: The process's account, installed at startup. One, like the session it
#: renews over: the ladder asks for it without every caller passing it down.
_current = None


def install(account):
    global _current
    _current = account
    return account


def current():
    """The linked account, or None when none is installed."""
    return _current


class Account:
    """One linked account, its access token kept fresh."""

    def __init__(self, refresh_token=None, token_file=None):
        self.refresh_token = (refresh_token or "").strip() or None
        self.token_file = token_file
        self.signed_out = False

        self._client_id = None
        self._client_secret = None
        self._bundle = None
        self._version_date = None
        self._page_until = 0.0

        self._access = None
        self._access_until = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def load(cls, config):
        """From the environment if set there, the token file otherwise."""
        token = config.oauth_refresh_token
        path = config.oauth_token_file
        if not token and path and path.is_file():
            token = path.read_text(encoding="utf-8").strip().splitlines()[0:1]
            token = token[0].strip() if token else ""
        return cls(token, path)

    # ------------------------------------------------------------- state

    @property
    def linked(self):
        return self.refresh_token is not None

    @property
    def usable(self):
        return self.linked and not self.signed_out

    def describe(self):
        """Enough to diagnose, nothing to leak: no token, no path."""
        return {"linked": self.linked, "signed_out": self.signed_out,
                "access_token": bool(self._access and time.time() < self._access_until)}

    def save(self, token):
        """Writes a refresh token down, readable by this user alone."""
        if not self.token_file:
            return
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_file.with_suffix(".tmp")
        temporary.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.token_file)

    # ------------------------------------------------------------- the client

    async def client(self):
        """The TV client a request as this account is made as."""
        if time.time() > self._page_until:
            try:
                await self._read_page()
            except Exception as e:                               # noqa: BLE001
                log.warning("Could not read the YouTube TV page: %s", e)
        date = self._version_date or FALLBACK_DATE
        return clients.Client(
            name="TVHTML5",
            version="5." + date,
            client_id="7",
            user_agent=TIZEN_UA,
            extra={"osName": "Tizen", "osVersion": "2.4.0",
                   "deviceMake": "Samsung", "deviceModel": "UKS9800",
                   "userAgent": TIZEN_UA},
            scripted=True,
        )

    async def token(self):
        """A current access token, renewing it when it is old or nearly so."""
        if not self.usable:
            raise SignedOut("no YouTube account is linked")
        now = time.time()
        if self._access and now < self._access_until - RENEW_AHEAD:
            return self._access
        async with self._lock:
            if self._access and time.time() < self._access_until - RENEW_AHEAD:
                return self._access
            return await self._refresh()

    async def _refresh(self):
        answer = await self._post(TOKEN_URL, await self._form(
            True, refresh_token=self.refresh_token, grant_type="refresh_token"))
        error = answer.get("error")
        if error == "invalid_client":
            # The TV app was rebuilt under a new client; read it again once.
            self._client_id = self._client_secret = self._bundle = None
            answer = await self._post(TOKEN_URL, await self._form(
                True, refresh_token=self.refresh_token, grant_type="refresh_token"))
            error = answer.get("error")
        if error == "invalid_grant":
            self.signed_out = True
            raise SignedOut("the linked YouTube account was signed out; "
                            "run oauth_setup.py again")
        access = answer.get("access_token")
        if not access:
            raise SignedOut("Google refused to renew the YouTube token: %s" % error)

        self._access = access
        self._access_until = time.time() + int(answer.get("expires_in") or 3600)
        return access

    async def warm(self):
        """Reads the page and mints a token now, so the first play does not."""
        if not self.usable:
            return
        try:
            await self.client()
            await self.token()
            log.info("YouTube account: linked as a TV, token minted")
        except Exception as e:                                   # noqa: BLE001
            log.warning("YouTube account: linked, but %s", e)

    # ------------------------------------------------------------- linking

    async def link(self, show):
        """
        Signs in with a code typed on another device, and keeps the token.

        <p>Blocks until the code is entered or expires — up to half an hour —
        so it belongs in a script rather than on a request.

        :param show: called once with (user_code, verification_url, seconds)
        """
        code = await self._post(DEVICE_URL, await self._form(
            False, scope=SCOPES, device_id=uuid.uuid4().hex, device_model="ytlr::"))
        device_code = code.get("device_code")
        if not device_code:
            raise SignedOut("Google gave no device code: %s" % code.get("error", "no answer"))

        expires = int(code.get("expires_in") or 1800)
        show(code.get("user_code", ""),
             code.get("verification_url") or "https://www.google.com/device", expires)

        interval = max(1, int(code.get("interval") or 5))
        deadline = time.time() + expires
        while time.time() < deadline:
            await asyncio.sleep(interval)
            answer = await self._post(TOKEN_URL, await self._form(
                True, code=device_code,
                grant_type="http://oauth.net/grant_type/device/1.0"))
            error = answer.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 2
                continue
            if error:
                raise SignedOut(answer.get("error_description") or error)

            kept = answer.get("refresh_token")
            if not kept:
                raise SignedOut("Google signed in but gave no refresh token")
            self.refresh_token = kept
            self.signed_out = False
            self._access = answer.get("access_token")
            self._access_until = time.time() + int(answer.get("expires_in") or 3600)
            self.save(kept)
            return kept
        raise SignedOut("the code expired before it was entered")

    # ------------------------------------------------------------- plumbing

    async def _form(self, with_secret, **pairs):
        client_id, secret = await self._oauth_client()
        form = {"client_id": client_id}
        if with_secret:
            form["client_secret"] = secret
        form.update(pairs)
        return form

    async def _oauth_client(self):
        if self._client_id:
            return self._client_id, self._client_secret
        if not self._bundle:
            await self._read_page()

        http = await net.session()
        async with http.get("https://www.youtube.com" + self._bundle,
                            headers={"User-Agent": COBALT_UA},
                            timeout=net.timeout(20)) as response:
            script = await response.text()
        found = _CLIENT.search(script)
        if not found:
            raise SignedOut("the YouTube TV app no longer names its sign-in client")
        self._client_id, self._client_secret = found.group(1), found.group(2)
        return self._client_id, self._client_secret

    async def _read_page(self):
        http = await net.session()
        async with http.get(TV_PAGE, headers={"User-Agent": COBALT_UA,
                                              "Accept-Language": "en-US,en;q=0.9"},
                            timeout=net.timeout(15)) as response:
            page = await response.text()

        bundle = _BUNDLE.search(page)
        version = _VERSION.search(page)
        if bundle:
            if bundle.group(1) != self._bundle:
                self._client_id = self._client_secret = None
            self._bundle = bundle.group(1)
        if version:
            self._version_date = version.group(1)
        self._page_until = time.time() + PAGE_TTL
        if not self._bundle:
            raise SignedOut("the YouTube TV page carried no app script")

    @staticmethod
    async def _post(url, form):
        http = await net.session()
        async with http.post(url, data=form, timeout=net.timeout(20)) as response:
            try:
                return await response.json(content_type=None)
            except ValueError:
                return {"error": "http_%d" % response.status}
