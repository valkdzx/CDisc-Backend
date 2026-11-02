"""Who we claim to be when we ask InnerTube for something.

<p>YouTube answers the same question very differently depending on which of its
own apps is asking, and the differences are not cosmetic. The one that matters
here is whether the answer carries playable URLs at all: most clients now get a
`serverAbrStreamingUrl` and formats with no `url` on them, which is a protocol
rather than a link. A few still get direct URLs, and those are worth having.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Client:
    """One InnerTube client, and everything a request as it needs."""

    name: str
    version: str
    client_id: str
    user_agent: str
    extra: dict = field(default_factory=dict)

    #: Whether this client runs a player script, and therefore should send a
    #: signature timestamp. Saying so when it is untrue invites questions the
    #: client cannot answer.
    scripted: bool = False

    def context(self, visitor_data=None):
        context = {
            "clientName": self.name,
            "clientVersion": self.version,
            "hl": "en",
            "gl": "US",
        }
        context.update(self.extra)
        if visitor_data:
            context["visitorData"] = visitor_data
        return context

    def headers(self, visitor_data=None):
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "X-Youtube-Client-Name": self.client_id,
            "X-Youtube-Client-Version": self.version,
        }
        if visitor_data:
            headers["X-Goog-Visitor-Id"] = visitor_data
        return headers


#: Apple's headset, and the reason any of this is simple.
#:
#: YouTube still answers this client the old way: every format carries a direct
#: URL, there is no SABR endpoint to fall back on and no `n` parameter to
#: descramble. What it wants is a visitor id — any visitor id, including one an
#: ordinary page fetch hands out, because nothing checks whether a browser
#: earned it. Not universal: videos marked as made for kids are not served to
#: it, so the ladder below it stays necessary.
VISIONOS = Client(
    name="VISIONOS",
    version="1.02",
    client_id="101",
    user_agent=("com.google.ios.youtube/1.02 (RealityDevice17,1; U; "
                "CPU visionOS 26_5 like Mac OS X)"),
    extra={
        "deviceMake": "Apple",
        "deviceModel": "RealityDevice17,1",
        "osName": "visionOS",
        "osVersion": "26.5.23O471",
    },
)

#: The Quest app. Also answers with direct URLs, and is served some things
#: visionOS is not, which is the whole reason to keep a second one.
ANDROID_VR = Client(
    name="ANDROID_VR",
    version="1.62.27",
    client_id="28",
    user_agent=("com.google.android.apps.youtube.vr.oculus/1.62.27 "
                "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"),
    extra={
        "deviceMake": "Oculus",
        "deviceModel": "Quest 3",
        "osName": "Android",
        "osVersion": "12L",
        "androidSdkVersion": 32,
    },
)

#: The iPhone app.
IOS = Client(
    name="IOS",
    version="20.10.4",
    client_id="5",
    user_agent="com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)",
    extra={
        "deviceMake": "Apple",
        "deviceModel": "iPhone16,2",
        "osName": "iOS",
        "osVersion": "18.3.2.22D82",
    },
)


def web(version):
    """
    The desktop site.

    <p>Its formats arrive without URLs these days, so it is no use for playing
    anything — but it is the client a browser's cookies belong to, which makes
    it the one to ask when the question is "what does this account see". And it
    is what search is asked as, since a search carries no stream.
    """
    return Client(
        name="WEB",
        version=version,
        client_id="1",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        extra={"osName": "Windows", "osVersion": "10.0"},
        scripted=True,
    )


#: YouTube Studio's client, and the way an age gate is actually passed.
#:
#: Signed in, the ordinary browser client is answered with a SABR endpoint and
#: formats carrying nothing to fetch. This one is answered with the same
#: formats carrying a `signatureCipher`: a URL, a scrambled signature, and the
#: parameter to put the descrambled one back under. That is a link, once the
#: player script has been run over it - see {@link cipher.unlock}.
#:
#: Measured, so that none of it has to be re-derived later: the cookies are
#: what opens the gate (without them every client answers LOGIN_REQUIRED), the
#: visitor id makes no difference at all, and the version only has to be
#: well-formed - a date-shaped one from years ago is served exactly like
#: today's, while "1.0" is a 404. No po-token is involved anywhere.
WEB_CREATOR = Client(
    name="WEB_CREATOR",
    version="1.20250101.00.00",
    client_id="62",
    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    extra={"osName": "Windows", "osVersion": "10.0"},
    scripted=True,
)


#: What to try, in order, for a stream. Direct-URL clients first: when one of
#: them answers there is nothing left to do, and when none does the answer is
#: usually about the video rather than about the client.
STREAM_CLIENTS = (VISIONOS, ANDROID_VR, IOS)
