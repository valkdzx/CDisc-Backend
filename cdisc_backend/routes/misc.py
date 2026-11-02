"""Search, the token pair, and the index of routes."""
from ..web import Failure, json
from ..youtube import search, visitor


async def handle_index(request):
    return json([
        "/audio/{id}",
        "/info/{id}",
        "/search?q=",
        "/spotify?url=",
        "/soundcloud?url=",
        "/yandex?url=",
        "/yandex/audio/{id}",
        "/yandex/info/{id}",
        "/yandex/search?q=",
        "/vk?url=",
        "/vk/audio/{owner_id}_{id}[_{key}]",
        "/vk/info/{owner_id}_{id}[_{key}]",
        "/vk/search?q=",
        "/cdisc/public-youtube-data",
    ])


async def handle_search(request):
    query = request.rel_url.query.get("q", "").strip()
    if not query:
        raise Failure("Missing q", 400, "no_query")

    try:
        limit = max(1, min(20, int(request.rel_url.query.get("limit", 5))))
    except ValueError:
        limit = 5

    config = request.app["config"]

    age = search.cached(query)
    results = await search.find(query, config.web_client_version, limit)

    response = json({"query": query, "results": results})
    # So the second identical search never reaches this process at all. The
    # window is what is left of the entry's life: saying 300 on an entry with
    # forty seconds to go would pin a browser to a stale list for far longer
    # than this backend would serve one.
    fresh = search.TTL if age is None else max(0, int(search.TTL - age))
    response.headers["Cache-Control"] = f"public, max-age={fresh}"
    return response


async def handle_public_data(request):
    """
    The pair the plugin asks for when it has none of its own.

    <p>The visitor id is minted here rather than configured, because minting
    one is a page fetch and a fresh one is better than a stale one. The
    po-token cannot be minted without a browser, so that half is whatever the
    config was given — and when it was given nothing, this says so rather than
    handing out an empty string that looks like a token.
    """
    config = request.app["config"]

    visitor_data = config.visitor_data or await visitor.current()
    if not visitor_data:
        raise Failure("Could not mint a visitor id", 502, "no_visitor")

    return json({
        "visitor-data": visitor_data,
        "visitorData": visitor_data,
        "po-token": config.po_token,
        "poToken": config.po_token,
        "source": "config" if config.visitor_data else "minted",
    })
