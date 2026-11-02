"""One HTTP session for the whole process.

<p>A new session per request is a new connection pool per request, which throws
away every keep-alive to YouTube and every DNS answer. Everything here goes to
two hosts, so one shared session is both faster and kinder.

<p>Cookies are handled by hand rather than by aiohttp. The account's session is
a file on disk that outlives the process, and half of it living in a jar in
memory would mean two sources of truth and a `Cookie` header assembled from
both. See {@link youtube.cookies}.
"""
import aiohttp

_session = None


def timeout(seconds=20):
    return aiohttp.ClientTimeout(total=seconds)


def stream_timeout(read=30, connect=15):
    """
    A timeout for reading a whole track, rather than for one small answer.

    <p>`total` counts the body as well as the reply, so a total of sixty
    seconds does not mean "give up if it stalls" — it means "give up on any
    track that takes longer than a minute to transfer", which is most of them.
    The failure looks like a CDN that hung up mid-stream, which is a bad place
    to start debugging from. What we actually want to bound is silence.
    """
    return aiohttp.ClientTimeout(total=None, sock_connect=connect,
                                 sock_read=read)


#: How long a looked-up address is kept. aiohttp's default is ten seconds,
#: and a lookup here is not cheap: this host's resolver loses a few percent
#: of its answers — every public resolver alike, so it is the path — and
#: glibc waits five seconds before asking again. That was a five-second
#: silence before the first note, now and then, for no reason in the code.
#: The CDN's hosts per region are few and their addresses stable, so most
#: plays find theirs already known.
DNS_CACHE = 600

#: The longest response header accepted. aiohttp's default is 8 KiB, and the
#: YouTube TV page's Content-Security-Policy alone is longer than that — the
#: whole page is refused as a malformed reply before a byte of it is read.
MAX_HEADER = 64 * 1024


async def session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=timeout(), cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(ttl_dns_cache=DNS_CACHE),
            max_field_size=MAX_HEADER)
    return _session


async def close():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
