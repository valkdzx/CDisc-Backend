"""The two InnerTube calls this backend makes.

<p>InnerTube is the API YouTube's own apps use. There is no key to register and
no documentation; a request is the client saying who it is and what it wants,
and the answer is the same tree of renderers the app would draw. Two calls
cover everything here: /player for a video, /search for a query.
"""
import json

from .. import net

BASE = "https://www.youtube.com/youtubei/v1"


async def player(video_id, client, *, visitor_data=None, cookie_headers=None,
                 signature_timestamp=None, timeout=20, jar=None,
                 po_token=None, oauth_token=None):
    """
    Asks what this client would be served for this video.

    <p>`contentCheckOk` and `racyCheckOk` say the caller has already agreed to
    whatever warning the video carries; without them a merely graphic video is
    refused the same way an age-restricted one is, which makes the two
    impossible to tell apart.

    :param cookie_headers: what CookieJar.headers() returned, or nothing at all
                           for an anonymous ask
    :param jar: the CookieJar those headers came from, so any session Google
                refreshes in the reply is kept rather than dropped
    :param po_token: a proof-of-origin token to present with the request. Only
                     worth sending when it belongs to this session — one minted
                     elsewhere is refused, and refused in a way that reads as
                     the video being unavailable
    :param oauth_token: an access token from the TV sign-in. Only TVHTML5
                        takes one; every other client answers 400 to it
    """
    body = {
        "context": {"client": client.context(visitor_data)},
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    if signature_timestamp and client.scripted:
        body["playbackContext"] = {
            "contentPlaybackContext": {"signatureTimestamp": signature_timestamp}
        }
    if po_token:
        body["serviceIntegrityDimensions"] = {"poToken": po_token}

    headers = client.headers(visitor_data)
    headers.update(cookie_headers or {})
    if oauth_token:
        headers["Authorization"] = "Bearer " + oauth_token
        headers["Origin"] = "https://www.youtube.com"
        headers["Referer"] = "https://www.youtube.com/"

    http = await net.session()
    async with http.post(f"{BASE}/player", data=json.dumps(body).encode(),
                         headers=headers, timeout=net.timeout(timeout)) as response:
        if jar is not None:
            jar.absorb(response)
        text = await response.text()
        status = response.status

    try:
        return status, json.loads(text)
    except ValueError:
        return status, {}


async def search(query, client, *, visitor_data=None, params=None, timeout=20):
    """
    One search. Asked as the browser, because a search carries no stream and
    the clients that carry streams answer this endpoint with renderers nothing
    here knows how to read.

    :param params: the opaque blob the site puts in the URL when a filter chip
                   is clicked. What it means is YouTube's business; that it is
                   sent unchanged is ours. See {@link youtube.search.VIDEOS}.
    """
    body = {
        "context": {"client": client.context(visitor_data)},
        "query": query,
    }
    if params:
        body["params"] = params

    http = await net.session()
    async with http.post(f"{BASE}/search", data=json.dumps(body).encode(),
                         headers=client.headers(visitor_data),
                         timeout=net.timeout(timeout)) as response:
        text = await response.text()
        status = response.status

    try:
        return status, json.loads(text)
    except ValueError:
        return status, {}


def playability(payload):
    """
    What YouTube said about whether this can be played, flattened.

    <p>The subreason is where the useful half lives: `status` is one of a
    handful of words and `reason` is written for a viewer, while the subreason
    is the sentence that distinguishes an age gate from a country block.
    """
    play = payload.get("playabilityStatus") or {}
    subreason = ""

    screen = play.get("errorScreen") or {}
    for holder in screen.values():
        if not isinstance(holder, dict):
            continue
        runs = (holder.get("subreason") or {}).get("runs") or []
        if runs:
            subreason = " ".join(run.get("text", "") for run in runs)
            break
        simple = (holder.get("subreason") or {}).get("simpleText")
        if simple:
            subreason = simple
            break

    return {
        "status": play.get("status"),
        "reason": play.get("reason") or "",
        "subreason": subreason,
    }
