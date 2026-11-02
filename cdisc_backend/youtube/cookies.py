"""The account, as YouTube's own site presents it.

<p>Not OAuth. A device-flow token is minted for one client and refused
everywhere else — measured: /youtubei/v1/player answers 400 INVALID_ARGUMENT to
every client but TVHTML5, including for videos that play perfectly without it.
What youtube.com itself sends is a browser session: the cookies, and a
signature over the SAPISID cookie proving the request came from a page on the
right origin. That is what an age gate is built to accept, so that is what this
speaks.

<p>The signature is cheap and stateless — a SHA-1 of the timestamp, the cookie
and the origin — but it is not a bearer token: it is only valid for the origin
it names, which is why it has to be recomputed rather than cached.

<p>A session is not a constant. Google reissues part of it — the timestamp and
consistency cookies — every few hours through `Set-Cookie`, and a browser
quietly writes the new values back. An export that is never updated therefore
ages out of a file whose stated expiry dates say a year: what expires is not
the date on the cookie but the agreement about which one is current. So this
absorbs what comes back and writes it down, which is the difference between a
session that lasts weeks and one that lasts as long as the account.
"""
import hashlib
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("cdisc.cookies")

#: Written back no more often than this. The rotating cookies change on
#: YouTube's schedule, not ours, and a disk write per request would be absurd.
SAVE_EVERY = 60.0

#: What a cookie line looks like when we have to invent one: a year out, on
#: every youtube.com host, secure. Only used for a cookie Google sent us that
#: was not in the file to begin with.
DEFAULT_LIFETIME = 400 * 86400

ORIGIN = "https://www.youtube.com"

# The cookies that actually matter. A cookies.txt export carries dozens, most
# of them consent and preference noise; sending the lot is a large header on
# every request and one more thing to leak in a log.
WANTED = {
    "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID",
    "SID", "__Secure-1PSID", "__Secure-3PSID",
    "HSID", "SSID", "APISID",
    "LOGIN_INFO", "PREF", "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA",
    "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
}


class CookieJar:
    """A parsed cookies.txt, and the headers it lets us send."""

    def __init__(self, cookies=None, source=None, lines=None):
        self.cookies = cookies or {}
        self.source = source
        #: The file as it was read, kept verbatim so writing it back changes
        #: the values we were given and nothing else — a cookies.txt carries
        #: entries we neither send nor understand, and losing them on save
        #: would be a silent edit of the user's file.
        self.lines = lines or []
        self._dirty = False
        self._saved_at = 0.0

    # -------------------------------------------------------------- loading

    @classmethod
    def load(cls, path):
        """
        Reads a Netscape cookies.txt.

        <p>Never raises for an absent file: not having an account is a normal
        state for this backend, and the caller decides what that costs.
        """
        if path is None:
            return cls()

        path = Path(path)
        if not path.is_file():
            return cls()

        text = path.read_text(encoding="utf-8", errors="replace")
        return cls.parse(text, source=str(path))

    @classmethod
    def parse(cls, text, source=None):
        """
        The same, from text rather than from a file.

        <p>Because the session is no longer only ever read off disk: a browser
        profile holds a live one, and it arrives over HTTP in exactly this
        format.
        """
        cookies = {}
        for line in text.splitlines():
            # "#HttpOnly_" is a prefix some exporters put on the domain rather
            # than a comment, so it has to be stripped before the comment test.
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                continue

            domain, _, _, _, _, name, value = parts[:7]
            if "youtube.com" not in domain and "google.com" not in domain:
                continue
            if name in WANTED:
                cookies[name] = value.strip()

        return cls(cookies, source=source, lines=text.splitlines())

    # -------------------------------------------------------------- asking

    @property
    def present(self):
        """Whether there is an account here at all."""
        return bool(self.cookies)

    @property
    def signed_in(self):
        """
        Whether these cookies can actually sign a request.

        <p>A file can parse perfectly and still be useless: an export taken
        while signed out carries consent cookies and nothing else. The
        signature needs SAPISID, so its presence is the honest test.
        """
        return bool(self._sapisid())

    def _sapisid(self):
        for name in ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"):
            if self.cookies.get(name):
                return self.cookies[name]
        return None

    def describe(self):
        """
        Enough to diagnose, nothing to leak.

        <p>Deliberately no path. These routes are reachable from outside, and
        where the session file lives is not something a stranger needs to be
        told — it names a user, a layout, and a file worth stealing. The count
        answers the only question that matters from out there: is an account
        loaded and can it sign.
        """
        return {
            "loaded": self.present,
            "signed_in": self.signed_in,
            "count": len(self.cookies),
        }

    def describe_locally(self):
        """The same, plus the path — for the startup log, which stays here."""
        return dict(self.describe(), source=self.source,
                    cookies=sorted(self.cookies))

    # ------------------------------------------------------------ rotating

    def absorb(self, response):
        """
        Takes any refreshed cookies out of a reply.

        <p>Google rotates `__Secure-1PSIDTS` and the `SIDCC` family on its own
        schedule and expects whoever holds the session to keep up. Ignoring the
        header is what makes an exported file go stale: nothing announces it,
        and weeks later an age-gated video answers LOGIN_REQUIRED while
        everything else still plays.

        <p>Only cookies we already care about are taken, and only when the
        value actually changed, so an ordinary request costs a dict lookup.
        """
        try:
            headers = response.headers.getall("Set-Cookie", ())
        except (AttributeError, KeyError):
            return

        for header in headers:
            pair = header.split(";", 1)[0].strip()
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()

            # An empty value is a deletion, and a deletion arriving beside a
            # working session is far more likely to be a stray host than a real
            # sign-out. Refusing it costs nothing; obeying it costs the account.
            if name not in WANTED or not value:
                continue
            if self.cookies.get(name) == value:
                continue

            self.cookies[name] = value
            self._dirty = True

    def maybe_save(self):
        """Writes the file back, at most once a minute, only when changed."""
        if not self._dirty or not self.source:
            return False
        now = time.monotonic()
        if now - self._saved_at < SAVE_EVERY:
            return False
        return self.save()

    def save(self):
        """
        Writes the session back where it was read from.

        <p>Through a temporary file and a rename, because this is the only copy
        of the account. A process killed midway through an ordinary write would
        leave a truncated cookies.txt, which reads as "signed out" — the exact
        failure this whole mechanism exists to prevent.
        """
        if not self.source:
            return False

        path = Path(self.source)
        try:
            text = "\n".join(self._rewritten()) + "\n"
            temporary = path.with_name(path.name + ".new")
            temporary.write_text(text, encoding="utf-8")
            # Before the rename, so the file is never briefly readable by
            # everyone: what is in it signs somebody in.
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError as e:
            # Not fatal: the session in memory is still good, and a backend
            # that cannot write its cookies file should keep serving audio.
            log.warning("Could not write the cookies file back: %s", e)
            return False

        self.lines = text.splitlines()
        self._dirty = False
        self._saved_at = time.monotonic()
        log.info("Refreshed the YouTube session on disk")
        return True

    def _rewritten(self):
        """The file with current values in it, everything else left alone."""
        written = set()
        out = []

        for line in self.lines:
            stripped = line
            prefix = ""
            if stripped.startswith("#HttpOnly_"):
                prefix, stripped = "#HttpOnly_", stripped[len("#HttpOnly_"):]

            parts = stripped.split("\t")
            if len(parts) < 7 or not stripped.strip() or stripped.lstrip().startswith("#"):
                out.append(line)
                continue

            name = parts[5]
            if name in self.cookies:
                parts[6] = self.cookies[name]
                written.add(name)
            out.append(prefix + "\t".join(parts))

        # A cookie Google sent that the export never had. Rare, but dropping it
        # would mean the next request argues with the answer we just got.
        expiry = str(int(time.time() + DEFAULT_LIFETIME))
        for name, value in self.cookies.items():
            if name not in written:
                out.append("\t".join((".youtube.com", "TRUE", "/", "TRUE",
                                      expiry, name, value)))

        return out

    # -------------------------------------------------------------- sending

    def headers(self, origin=ORIGIN):
        """
        What to add to an InnerTube request so it arrives as this account.

        <p>Empty when there is nothing to send, so a caller can splat it into
        a header dict either way and get an anonymous request for free.
        """
        sapisid = self._sapisid()
        if not sapisid:
            return {}

        stamp = int(time.time())
        digest = hashlib.sha1(
            f"{stamp} {sapisid} {origin}".encode("utf-8")).hexdigest()

        return {
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "Authorization": f"SAPISIDHASH {stamp}_{digest}",
            "Origin": origin,
            "X-Origin": origin,
            # Which signed-in account, when a browser holds several. Zero is
            # the first, and a cookies file from a single-account browser has
            # no other.
            "X-Goog-AuthUser": "0",
        }
