"""Undoing the `n` a streaming URL is handed with.

<p>Every YouTube streaming URL carries a parameter the player is expected to
run a function over and put back. The function lives in the player script,
obfuscated and different every few days, so computing it means running their
JavaScript — which is why this asks a service that already does rather than
shipping an interpreter.

<p>It is not optional. A SABR endpoint asked with the `n` it was given answers
403 and nothing else, which reads as a refusal rather than as a URL that was
never finished. That cost an afternoon; the comment is here so it does not cost
another.
"""
import time
import urllib.parse

from .. import net
from ..web import Failure

#: The public instance the CDisc plugin points at by default. Same protocol as
#: youtube-source's own remote cipher: a POST, a JSON body, a URL back.
DEFAULT = "https://cipher.kikkia.dev"

#: Resolved URLs last as long as the signature in them, which is hours. Kept
#: for a fraction of that: a track replayed twice in a minute should not ask
#: twice, and nothing here is worth serving stale.
TTL = 300

_cache = {}


def n_param(url):
    """The `n` in a URL, or None when there is nothing to undo."""
    query = urllib.parse.urlparse(url).query
    values = urllib.parse.parse_qs(query).get("n")
    return values[0] if values else None


async def resolve(url, player_url, base=DEFAULT, password=None):
    """
    The same URL with its `n` replaced by what the player would have computed.

    <p>Returns the URL untouched when it carries no `n`, which is the ordinary
    case for the direct links the app clients hand out — only the SABR endpoint
    and the browser client's formats need this.
    """
    scrambled = n_param(url)
    if not scrambled:
        return url

    key = (player_url, scrambled)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL:
        return _swap(url, scrambled, cached[1])

    headers = {}
    if password:
        headers["Authorization"] = password

    http = await net.session()
    try:
        async with http.post(f"{base.rstrip('/')}/resolve_url",
                             json={"player_url": player_url,
                                   "stream_url": url,
                                   "n_param": scrambled},
                             headers=headers,
                             timeout=net.timeout(30)) as response:
            if response.status != 200:
                raise Failure(f"The cipher service answered {response.status}",
                              502, "cipher")
            payload = await response.json()
    except Failure:
        raise
    except Exception as e:
        raise Failure(f"Could not reach the cipher service: {e}", 502, "cipher")

    resolved = payload.get("resolved_url")
    if not resolved:
        raise Failure("The cipher service returned no URL", 502, "cipher")

    # Cached as the value rather than the whole URL: the same `n` resolves to
    # the same thing for every URL sharing that player script, and stream URLs
    # differ in a dozen other parameters.
    fresh = n_param(resolved)
    if fresh:
        _cache[key] = (time.monotonic(), fresh)
    return resolved


def _swap(url, old, new):
    return url.replace(f"n={old}", f"n={new}", 1)


async def unlock(fmt, player_url, base=DEFAULT, password=None):
    """
    A format's playable URL, whichever of the two shapes it arrived in.

    <p>Formats come three ways. Some carry a `url` outright, which only needs
    its `n` undone. Some carry a `signatureCipher`: the URL, a scrambled
    signature, and the name of the parameter to put the descrambled one back
    under — that is what an age-gated video is served as, and a URL fetched
    without it answers 403. Some carry neither, and those are SABR's business.

    <p>Both trips go to the same endpoint: `/resolve_url` takes the signature
    and the `n` together, so a format costs one call rather than two.

    :returns: a URL, or None when this format is not a link at all
    """
    if fmt.get("url"):
        return await resolve(fmt["url"], player_url, base, password)

    packed = fmt.get("signatureCipher") or fmt.get("cipher")
    if not packed:
        return None

    parts = urllib.parse.parse_qs(packed)
    url = (parts.get("url") or [None])[0]
    if not url:
        return None

    body = {
        "stream_url": url,
        "player_url": player_url,
        "encrypted_signature": (parts.get("s") or [""])[0],
        # Which parameter the signature goes back under. Usually "sig", but it
        # is named in the packed value rather than fixed, so it is read.
        "signature_key": (parts.get("sp") or ["sig"])[0],
        "n_param": n_param(url) or "",
    }

    headers = {"Authorization": password} if password else {}

    http = await net.session()
    try:
        async with http.post(f"{base.rstrip('/')}/resolve_url", json=body,
                             headers=headers, timeout=net.timeout(30)) as response:
            if response.status != 200:
                raise Failure(f"The cipher service answered {response.status}",
                              502, "cipher")
            payload = await response.json()
    except Failure:
        raise
    except Exception as e:
        raise Failure(f"Could not reach the cipher service: {e}", 502, "cipher")

    resolved = payload.get("resolved_url")
    if not resolved:
        raise Failure("The cipher service returned no URL", 502, "cipher")
    return resolved
