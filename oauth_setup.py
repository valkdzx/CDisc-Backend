"""Links a YouTube account to this backend, the way a TV is signed in.

<p>Prints a code; type it at the address shown on a device signed into the
YouTube account to use. Configure `CDISC_YOUTUBE_OAUTH_FILE` or
`youtube.oauth-token-file` in the local config to store the refresh token
outside the source tree. Without a token-file setting, the token is not saved.

    python oauth_setup.py

<p>Use an account you would not mind losing: playing audio from a server
under a login is the pattern YouTube restricts accounts for. The grant shows
in that account's security page as a TV, and removing it there is how the
backend is signed out — nothing else about the account is touched.
"""
import asyncio
import sys

from cdisc_backend import net
from cdisc_backend.config import Config
from cdisc_backend.youtube.account import Account, SignedOut


def show(code, url, seconds):
    print()
    print("  Open    %s" % url)
    print("  Enter   %s" % code)
    print()
    print("  Waiting for it (the code lasts %d minutes)..." % (seconds // 60))
    sys.stdout.flush()


async def main():
    config = Config.load()
    account = Account(None, config.oauth_token_file)
    try:
        await account.link(show)
        # Proves the token works rather than only that it was handed over.
        account._access = None
        await account.token()
    except SignedOut as e:
        print("  Failed: %s" % e)
        return 1
    finally:
        await net.close()

    print("  Linked. Refresh token written to %s" % (config.oauth_token_file or "nowhere"))
    print("  Restart the backend to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
