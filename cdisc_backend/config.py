"""Settings, read once at startup.

A JSON file rather than environment variables alone, because half of these are
credentials that want to sit in a file with tight permissions rather than in a
process listing. The environment still wins where it is set, which is what
makes this work unchanged inside a container.
"""
import json
import os
from pathlib import Path

# The project root: the folder holding run.py and the local config.json.
ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "public-url": "",
    # /streams is a diagnostic: it asks YouTube eight times per call and
    # reports whether an account is loaded. Empty means the route does not
    # exist at all, which is the right default for something reachable from
    # the internet.
    "diagnostics-token": "",
    "tls": {
        # Empty paths disable TLS in this process. Terminate TLS at a reverse
        # proxy, or supply deployment-specific paths through environment vars.
        "certfile": "",
        "keyfile": "",
    },
    "youtube": {
        # Account credentials are opt-in and should be supplied outside Git.
        "cookie-file": "",
        "oauth-token-file": "",
        "web-client-version": "2.20260828.01.00",
        # Handed out by /cdisc/public-youtube-data. Left empty the route says so
        # rather than serving a pair that was never set.
        "visitor-data": "",
        # A streaming URL carries an "n" the player is meant to run a function
        # over. Doing that here would mean running YouTube's JavaScript, so it
        # is asked of a service that already does. Without it the SABR endpoint
        # answers 403 and nothing else.
        "cipher-url": "https://cipher.kikkia.dev",
        "cipher-password": "",
        "po-token": "",
    },
    "spotify": {
        "client-id": "",
        "client-secret": "",
    },
    "yandex": {
        # An OAuth token of the account whose subscription plays the tracks.
        # Without Plus the API still answers, with thirty-second previews.
        "token": "",
        # "hq" is MP3 320 on a subscription. "lossless" is FLAC, which Yandex
        # serves only encrypted, so it falls back to the older road anyway.
        "quality": "hq",
    },
    "vk": {
        # A token issued to an app that is allowed audio at all: Kate Mobile
        # or VK's own. Any other app's token is answered with a placeholder
        # track called audio_api_unavailable.
        "token": "",
        # Checked against the app the token was issued to. A Kate token under
        # any other user agent is refused as if it had expired.
        "user-agent": "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; "
                      "x86; unknown Android SDK built for x86; en)",
    },
}


def _merge(base, overlay):
    """Overlay wins, but only where it actually says something."""
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """The settings, and the questions worth asking of them."""

    def __init__(self, data):
        self._data = data

    @classmethod
    def load(cls):
        path = Path(os.environ.get("CDISC_BACKEND_CONFIG") or (ROOT / "config.json"))
        stored = {}
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
        return cls(_merge(DEFAULTS, stored))

    # ------------------------------------------------------------------ basics

    @property
    def host(self):
        return os.environ.get("CDISC_HOST") or self._data["host"]

    @property
    def port(self):
        return int(os.environ.get("CDISC_PORT") or self._data["port"])

    @property
    def public_url(self):
        """The address clients reach this by, for building links in replies."""
        return (os.environ.get("CDISC_PUBLIC_URL")
                or self._data.get("public-url") or "").rstrip("/")

    @property
    def diagnostics_token(self):
        return (os.environ.get("CDISC_DIAG_TOKEN")
                or self._data.get("diagnostics-token") or "")

    # --------------------------------------------------------------------- tls

    @property
    def ssl_context(self):
        """
        The certificate this serves, or None to serve plain HTTP.

        <p>Terminating TLS here is one less moving part than a reverse proxy,
        and it is what this replaced. Behind nginx or Caddy leave the paths
        empty — two things terminating TLS is one thing too many.

        <p>Loaded at startup and held. Certbot renews every couple of months
        and the process would keep serving the expired chain, so the renewal
        hook has to restart this: `--deploy-hook "systemctl restart cdisc-backend"`.
        """
        certfile = os.environ.get("CDISC_TLS_CERT") or self._data["tls"]["certfile"]
        if not certfile:
            return None

        keyfile = os.environ.get("CDISC_TLS_KEY") or self._data["tls"]["keyfile"]

        import ssl
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile or None)
        return context

    # ----------------------------------------------------------------- youtube

    @property
    def cookie_file(self):
        """
        The Netscape cookies file, as an absolute path, or None when the
        setting is empty.

        <p>Resolved against the project root rather than the working directory,
        so it does not matter where the process was started from.
        """
        raw = os.environ.get("CDISC_YOUTUBE_COOKIES") or self._data["youtube"]["cookie-file"]
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else (ROOT / path)

    @property
    def oauth_token_file(self):
        """Where the TV sign-in's refresh token is kept, or None for nowhere."""
        raw = (os.environ.get("CDISC_YOUTUBE_OAUTH_FILE")
               or self._data["youtube"].get("oauth-token-file", ""))
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else (ROOT / path)

    @property
    def oauth_refresh_token(self):
        """
        The refresh token from the environment, which wins over the file.

        <p>Not from config.json: a token that signs in as the account belongs
        in a file of its own with its own permissions, not beside a port number
        in a file people paste into chats.
        """
        return os.environ.get("CDISC_YOUTUBE_REFRESH_TOKEN", "").strip()

    @property
    def web_client_version(self):
        return self._data["youtube"]["web-client-version"]

    @property
    def visitor_data(self):
        """
        A visitor id from the config, if one was given.

        <p>Normally empty: one is minted at startup and again every hour, and a
        fresh one beats a stored one. It is worth setting only when it is half
        of a proof-of-origin pair, because a po-token is minted against one
        visitor id and refused beside any other — the two are one value in two
        halves rather than two settings.

        <p>`visitor-id` is the name this had before it was clear that the value
        is visitorData rather than the id inside it. Still read, so a file
        written against the old spelling keeps working.
        """
        return (os.environ.get("CDISC_VISITOR_DATA")
                or os.environ.get("CDISC_VISITOR_ID")
                or self._data["youtube"].get("visitor-data")
                or self._data["youtube"].get("visitor-id", ""))

    @property
    def cipher_url(self):
        return (os.environ.get("CDISC_CIPHER_URL")
                or self._data["youtube"].get("cipher-url")
                or "https://cipher.kikkia.dev")

    @property
    def cipher_password(self):
        return (os.environ.get("CDISC_CIPHER_PASSWORD")
                or self._data["youtube"].get("cipher-password") or "")

    @property
    def po_token(self):
        return os.environ.get("CDISC_PO_TOKEN") or self._data["youtube"]["po-token"]

    # ----------------------------------------------------------------- harvest

    def _harvest(self, key, default=None):
        return (self._data["youtube"].get("harvest") or {}).get(key, default)

    @property
    def harvest_enabled(self):
        """
        Whether to start a browser for a video that only a browser can unlock.

        <p>Off by default. It is a real cost — a Chrome start per video — and a
        real dependency: node, playwright and, on a server, Xvfb. A server that
        never plays age-restricted music needs none of it.
        """
        return bool(os.environ.get("CDISC_HARVEST")
                    or self._harvest("enabled", False))

    @property
    def harvest_script(self):
        """Where generate-browser.js lives, absolute."""
        raw = (os.environ.get("CDISC_HARVEST_SCRIPT")
               or self._harvest("script", ""))
        if not raw:
            return ""
        path = Path(raw)
        return str(path if path.is_absolute() else (ROOT / path))

    @property
    def harvest_node(self):
        return (os.environ.get("CDISC_HARVEST_NODE")
                or self._harvest("node", "node"))

    @property
    def harvest_display(self):
        """
        The X display to give the browser, when it needs telling.

        <p>Empty on a desktop, where there is one already. On a server this is
        the Xvfb display, and it is not optional: a browser with nowhere to
        draw produces a pair that is never attested, which looks like a track
        that stops rather than like a missing display.
        """
        return (os.environ.get("CDISC_HARVEST_DISPLAY")
                or self._harvest("display", ""))

    @property
    def harvest_mint_url(self):
        """
        A minting service to ask, instead of starting a browser per video.

        <p>The service holds one attested browser and mints from it, which is
        what Chrome itself does: the expensive part is the attestation, and it
        is good for twelve hours. Asking it costs milliseconds, so nothing has
        to be cached, warmed or waited for.

        <p>Empty falls back to running {@link harvest_script} per video, which
        works and takes seconds.
        """
        return (os.environ.get("CDISC_MINT_URL")
                or self._harvest("mint-url", "")).rstrip("/")

    @property
    def harvest_dwell(self):
        """
        Seconds the browser keeps watching once the token has gone out.

        <p>Three rather than ten. The pair is read off a request, and the
        request happens as playback starts; the rest was time spent watching a
        video nobody is in the room for.
        """
        return int(self._harvest("dwell-seconds", 3))

    @property
    def harvest_timeout(self):
        return int(self._harvest("timeout-seconds", 240))

    @property
    def harvest_ttl(self):
        return int(self._harvest("ttl-seconds", 6 * 3600))

    # ----------------------------------------------------------------- spotify

    @property
    def spotify_client_id(self):
        return (os.environ.get("SPOTIFY_CLIENT_ID")
                or self._data["spotify"]["client-id"])

    @property
    def spotify_client_secret(self):
        return (os.environ.get("SPOTIFY_CLIENT_SECRET")
                or self._data["spotify"]["client-secret"])

    @property
    def has_spotify(self):
        return bool(self.spotify_client_id and self.spotify_client_secret)

    # ------------------------------------------------------------------ yandex

    def _section(self, name):
        return self._data.get(name) or {}

    @property
    def yandex_token(self):
        return (os.environ.get("YANDEX_MUSIC_TOKEN")
                or self._section("yandex").get("token") or "")

    @property
    def yandex_quality(self):
        return (os.environ.get("YANDEX_MUSIC_QUALITY")
                or self._section("yandex").get("quality") or "hq")

    @property
    def has_yandex(self):
        return bool(self.yandex_token)

    # ---------------------------------------------------------------------- vk

    @property
    def vk_token(self):
        return os.environ.get("VK_TOKEN") or self._section("vk").get("token") or ""

    @property
    def vk_user_agent(self):
        return (os.environ.get("VK_USER_AGENT")
                or self._section("vk").get("user-agent")
                or DEFAULTS["vk"]["user-agent"])

    @property
    def has_vk(self):
        return bool(self.vk_token)
