"""Visitor ids: the one credential every anonymous request needs.

<p>A visitor id is what YouTube uses to tell one logged-out session from
another. The clients that still hand out direct URLs will not answer without
one — the reply is LOGIN_REQUIRED, "Sign in to confirm you're not a bot" — but
they do not care where it came from, and an ordinary page fetch hands one out.

<p>So this mints one, keeps it for an hour and mints another. There is no
renewal dance and no proof of work: the whole thing is one GET.
"""
import json
import re
import time

from .. import net

HOME = "https://www.youtube.com/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

#: How long a minted id is reused. Long enough that a busy server is not
#: fetching the home page constantly, short enough that a burned one is
#: replaced without anyone restarting anything.
TTL = 3600

_VISITOR = re.compile(r'"visitorData":"([^"]+)"')

_cached = None
_cached_at = 0.0


async def current(force=False):
    """
    A visitor id, minting one if the last is stale.

    <p>Returns None rather than raising when YouTube will not hand one out:
    every caller has something useful to do without one, even if it is only
    reporting a clearer failure than a stack trace.
    """
    global _cached, _cached_at

    if not force and _cached and time.time() - _cached_at < TTL:
        return _cached

    try:
        http = await net.session()
        async with http.get(HOME, headers={"User-Agent": BROWSER_UA},
                            timeout=net.timeout(15)) as response:
            html = await response.text()
    except Exception:
        return _cached

    found = _VISITOR.search(html)
    if not found:
        return _cached

    # The page carries it JSON-escaped, so it is read back the same way rather
    # than by hand: the value contains sequences that a naive unescape breaks.
    _cached = json.loads('"%s"' % found.group(1))
    _cached_at = time.time()
    return _cached


def cached():
    """Whatever is held right now, without going to the network."""
    return _cached


async def renew_if_due(ahead=600):
    """Mints the next id before the held one is stale, for the keeper rather than a request."""
    if _cached and time.time() - _cached_at < TTL - ahead:
        return False
    return bool(await current(force=True))


_session_visitor = None
_session_visitor_at = 0.0


async def for_account(cookies, force=False):
    """
    The visitor id belonging to a signed-in session.

    <p>This exists because of a mistake worth not repeating. A visitor id minted
    anonymously and sent alongside an account's cookies tells YouTube two
    different stories about who is asking — the cookies say one session, the id
    says another — and it is refused for the mismatch rather than for anything
    to do with the video. So when there is an account, its own id is what goes
    with it: the same home page fetch, made as the account, hands out the one
    that belongs to the session.

    <p>Falls back to the anonymous id when the fetch fails, since a request with
    a slightly wrong id still beats one with none at all.
    """
    global _session_visitor, _session_visitor_at

    if not cookies or not cookies.signed_in:
        return await current()

    if not force and _session_visitor and time.time() - _session_visitor_at < TTL:
        return _session_visitor

    headers = {"User-Agent": BROWSER_UA}
    headers.update(cookies.headers())

    try:
        http = await net.session()
        async with http.get(HOME, headers=headers, timeout=net.timeout(15)) as response:
            cookies.absorb(response)
            html = await response.text()
    except Exception:
        return _session_visitor or await current()

    found = _VISITOR.search(html)
    if not found:
        return _session_visitor or await current()

    _session_visitor = json.loads('"%s"' % found.group(1))
    _session_visitor_at = time.time()
    return _session_visitor
