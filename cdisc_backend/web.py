"""Replies, the one shape errors take, and who is let in at all."""
import secrets
import time

from aiohttp import web


class Failure(Exception):
    """
    Something a caller should be told about, with the status to tell them in.

    <p>A code as well as a sentence, because the plugin reads these: "the video
    is age restricted and there is no account here" and "no such video" are
    both 404-ish to a human and completely different to a program.
    """

    def __init__(self, message, status=500, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or "error"


def require_diagnostics(request):
    """
    Lets the request through only with the diagnostics token.

    <p>A 404 without one rather than a 401: an endpoint that says "wrong
    token" has admitted it exists. With no token configured the route is
    switched off entirely.
    """
    expected = request.app["config"].diagnostics_token
    given = (request.rel_url.query.get("token")
             or request.headers.get("X-Diag-Token") or "")
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        raise web.HTTPNotFound()


def json(payload, status=200):
    return web.json_response(payload, status=status)


def failed(message, status=500, code="error"):
    return web.json_response({"error": message, "code": code}, status=status)


@web.middleware
async def errors(request, handler):
    """Turns a Failure into its reply, and anything else into a 500."""
    try:
        return await handler(request)
    except Failure as e:
        return failed(e.message, e.status, e.code)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001 - the last line before a stack trace
        # The exception goes to the log and stays there. Its text names files,
        # hosts and whatever else the failing line was holding, and none of
        # that is the caller's to read.
        request.app.logger.exception("unhandled error on %s", request.path)
        return failed("Internal error", 500, "internal")


# ------------------------------------------------------------------ the door

def client_address(request):
    """
    Who is asking, as far as that can be known.

    <p>The socket's address unless the config names a header to trust. Behind
    a reverse proxy the socket is the proxy, for everyone, and a limit counted
    against it is a limit on the whole internet at once; without one, the
    header is whatever the caller typed, and trusting it is no limit at all.
    So it is read only when asked for. The last value, because that is the
    one the nearest proxy wrote.
    """
    header = request.app["config"].client_ip_header
    if header:
        given = request.headers.get(header, "").split(",")[-1].strip()
        if given:
            return given
    return request.remote or "unknown"


def _presented(request):
    """The access token a request carries, wherever it put it."""
    given = request.headers.get("Authorization", "").strip()
    if given.lower().startswith("bearer "):
        given = given[7:].strip()
    return (given or request.headers.get("X-CDisc-Token", "").strip()
            or request.rel_url.query.get("key", ""))


#: Headers that say a proxy stands in front of this process.
FORWARDED = ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded")


class Door:
    """Counts requests per address: how many a minute, how many at once."""

    #: Addresses remembered before the idle ones are swept out.
    SWEEP_AT = 4096

    def __init__(self):
        #: address -> [window started at, requests in it]
        self.windows = {}
        #: address -> requests being answered right now
        self.open = {}
        self.warned = False

    def admit(self, address, per_minute, at_once):
        """None when the request may pass, else the reason it may not."""
        now = time.monotonic()
        if per_minute:
            window = self.windows.get(address)
            if window is None or now - window[0] >= 60:
                window = self.windows[address] = [now, 0]
                if len(self.windows) > self.SWEEP_AT:
                    self._sweep(now)
            window[1] += 1
            if window[1] > per_minute:
                return "rate_limited", max(1, int(60 - (now - window[0])))
        if at_once and self.open.get(address, 0) >= at_once:
            return "too_many_at_once", 1
        self.open[address] = self.open.get(address, 0) + 1
        return None

    def leave(self, address):
        left = self.open.get(address, 0) - 1
        if left > 0:
            self.open[address] = left
        else:
            self.open.pop(address, None)

    def _sweep(self, now):
        for address in [a for a, (at, _) in self.windows.items() if now - at >= 60]:
            self.windows.pop(address, None)


@web.middleware
async def door(request, handler):
    """
    The access token, when one is configured, and the per-address limits.

    <p>The index is left outside the token: it is what a plugin probes to
    choose a node, it says nothing a README does not, and a node that answers
    401 to "are you there" looks like one that is not.
    """
    config = request.app["config"]

    expected = config.access_token
    if expected and request.path != "/":
        if not secrets.compare_digest(_presented(request).encode(), expected.encode()):
            return failed("This backend wants an access token", 401, "unauthorized")

    keeper = request.app["door"]
    if not config.client_ip_header and any(h in request.headers for h in FORWARDED):
        # Behind a proxy nobody told us about, every caller shares the proxy's
        # address, and a limit per address would be a limit on all of them
        # together. Counting nothing is the lesser fault — said once, loudly.
        if not keeper.warned:
            keeper.warned = True
            request.app.logger.warning(
                "Requests arrive through a proxy, but limits.client-ip-header "
                "is not set: per-address limits are OFF until it names the "
                "header to trust (CF-Connecting-IP behind Cloudflare).")
        return await handler(request)

    address = client_address(request)
    refused = keeper.admit(address, config.requests_per_minute,
                           config.concurrent_requests)
    if refused is not None:
        code, wait = refused
        response = failed("Too many requests from this address", 429, code)
        response.headers["Retry-After"] = str(wait)
        return response
    try:
        return await handler(request)
    finally:
        keeper.leave(address)
