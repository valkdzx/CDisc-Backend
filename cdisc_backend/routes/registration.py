"""Asking for a key, and the owner's view of the ones given out."""
from ..web import Failure, honest_address, json, require_diagnostics

_REASONS = {
    "bad_solution": ("That challenge was not solved, or has expired; ask for "
                     "another", 400),
    "too_many_registrations": ("This address has registered enough keys for "
                               "today", 429),
    "full": ("This node is not issuing keys at the moment", 503),
}


async def handle_challenge(request):
    return json(request.app["keys"].challenge())


async def handle_register(request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise Failure("Send JSON: {\"challenge\": ..., \"counter\": ...}", 400, "bad_request")

    key, problem = request.app["keys"].register(
        body.get("challenge"), body.get("counter"),
        address=honest_address(request), note=body.get("name") or "")
    if problem:
        message, status = _REASONS[problem]
        raise Failure(message, status, problem)
    return json({"key": key,
                 "send_as": "X-CDisc-Token: <key>  (or Authorization: Bearer <key>)"})


async def handle_list(request):
    require_diagnostics(request)
    door = request.app["door"]
    rows = request.app["keys"].describe()
    for row in rows:
        row["in_flight"] = door.open.get("key:" + row["id"], 0)
    return json({"count": len(rows), "keys": rows})


async def handle_revoke(request):
    require_diagnostics(request)
    ident = request.match_info["id"]
    banned = request.match_info["action"] == "revoke"
    if not request.app["keys"].revoke(ident, banned, request.rel_url.query.get("note")):
        raise Failure("No such key", 404, "not_found")
    return json({"id": ident, "banned": banned})
