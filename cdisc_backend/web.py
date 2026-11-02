"""Replies, and the one shape errors take."""
import secrets

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
    if not expected or not secrets.compare_digest(given, expected):
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
    except Exception as e:  # noqa: BLE001 - the last line before a stack trace
        request.app.logger.exception("unhandled error on %s", request.path)
        return failed(str(e), 500, "internal")
