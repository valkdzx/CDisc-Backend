"""YouTube's player script, and the one number the browser client needs from it.

<p>The web player ships a script, and a request claiming to be that player has
to say which version of it is running: the `signatureTimestamp` baked into it.
Without one, a WEB /player request is answered with formats carrying neither a
URL nor a signature — there is nothing to sign against, so nothing is offered.

<p>Cached hard. The script changes every few days and fetching a megabyte of
obfuscated JavaScript per request would be absurd.
"""
import re
import time

from .. import net

HOME = "https://www.youtube.com/"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

#: Six hours. The script is replaced far less often than that, and a stale
#: timestamp fails loudly rather than quietly, so there is no reason to check
#: more eagerly.
TTL = 6 * 3600

_JS_URL = re.compile(r'"jsUrl":"([^"]+base\.js)"')
_STS = re.compile(r"signatureTimestamp[:=](\d+)")

_url = None
_timestamp = None
_at = 0.0


async def signature_timestamp(force=False):
    """
    The timestamp of the player script the site is currently serving.

    <p>None when it cannot be read, which is not fatal: the caller loses the
    browser client and still has the app clients, which run no script and want
    no timestamp.
    """
    global _url, _timestamp, _at

    if not force and _timestamp and time.time() - _at < TTL:
        return _timestamp

    try:
        http = await net.session()
        async with http.get(HOME, headers={"User-Agent": BROWSER_UA},
                            timeout=net.timeout(15)) as response:
            html = await response.text()

        found = _JS_URL.search(html)
        if not found:
            return _timestamp

        url = found.group(1)
        if url.startswith("/"):
            url = "https://www.youtube.com" + url

        async with http.get(url, headers={"User-Agent": BROWSER_UA},
                            timeout=net.timeout(25)) as response:
            script = await response.text()
    except Exception:
        return _timestamp

    stamp = _STS.search(script)
    if not stamp:
        return _timestamp

    _url = url
    _timestamp = int(stamp.group(1))
    _at = time.time()
    return _timestamp


async def renew_if_due(ahead=600):
    """Reads the script again before the held timestamp is stale, for the keeper."""
    if _timestamp and time.time() - _at < TTL - ahead:
        return False
    return bool(await signature_timestamp(force=True))


def script_url():
    """Which script the cached timestamp came out of."""
    return _url
