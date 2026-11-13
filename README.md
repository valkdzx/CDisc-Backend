# CDisc Backend

An HTTP backend for the [CDisc Music](https://github.com/valkdzx/CDisc-Music)
Minecraft plugin. It resolves audio and metadata from YouTube, Spotify,
SoundCloud, Yandex Music, and VK.

## Requirements

- Python 3.11 or newer
- A network connection to the supported services
- `aiohttp` (installed from `requirements.txt`)

## Run locally

```sh
python -m venv .venv
```

Activate the virtual environment, then run:

```sh
python -m pip install -r requirements.txt
cp config.example.json config.json
python run.py
```

On Windows PowerShell, replace the `cp` command with:

```powershell
Copy-Item config.example.json config.json
```

The default listener is `127.0.0.1:8080`. Set `host`, `port`, and `public-url`
in the untracked `config.json`, or use the corresponding environment variables
(`CDISC_HOST`, `CDISC_PORT`, and `CDISC_PUBLIC_URL`). When running behind a
reverse proxy, keep the application on a private interface and let the proxy
handle public HTTPS.

## Access and limits

Out of the box every route answers anyone who can reach the address. With
account tokens configured, that means anyone can play through those accounts,
so decide which of the two this backend is:

- **A private backend.** Set `access-token` (`CDISC_ACCESS_TOKEN`). Every route
  except `GET /` then wants it as `X-CDisc-Token: <token>`,
  `Authorization: Bearer <token>`, or `?key=<token>`.
- **A public node.** Leave `access-token` empty, turn on `keys`, and rely on
  the limits.

### Keys: one per Minecraft server

The plugin is open source, so it cannot carry a secret: anything compiled into
it is published with it. A key here does a different job — it names a caller.
With `keys.enabled`, a plugin asks the node for a key the first time it starts
(`GET /register` for a challenge, `POST /register` with the solution), stores
it in `plugins/CDisc/backend-keys.properties`, and sends it as `X-CDisc-Token`
with every request to that node. Nobody configures anything.

What that buys:

- **Limits per server, not per address.** A server with a hundred players and
  a dozen jukeboxes is a dozen streams from one address. Counted per key it
  gets `keys.requests-per-minute` (3000) and `keys.concurrent-requests` (128);
  callers without a key keep the tighter per-address `limits`.
- **Revocation.** `GET /keys?token=<diagnostics-token>` lists every key with
  its request count, last use and requests in flight;
  `POST /keys/{id}/revoke?token=...` (and `/restore`) turns one off without
  touching any other server.
- **A cost to asking.** A registration takes about a second of hashing
  (`keys.pow-bits`) and an address gets `keys.registrations-per-day`. That is
  nothing once per server and a nuisance at a thousand keys.

It does not stop a determined person from registering a key by hand; nothing
can, for an open client. It bounds what one caller can take and lets you cut
one off.

`keys.required: true` refuses callers without a key (`401 key_required`).
Leave it off until the plugins using the node are new enough to register;
until then keyed and anonymous callers work side by side. Only hashes of keys
are stored, in `keys.file` (`data/keys.json`, mode 600).

`limits` in `config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `requests-per-minute` | 600 | Per client address without a key; `0` disables |
| `concurrent-requests` | 32 | Requests in flight per address without a key — a proxied track counts for as long as it is being sent; `0` disables |
| `live-streams` | 24 | Broadcasts read at once, for all clients together |
| `client-ip-header` | empty | Header a trusted reverse proxy puts the client address in |

Behind a reverse proxy, **set `client-ip-header`** (`CF-Connecting-IP` behind
Cloudflare, `X-Forwarded-For` behind nginx or Caddy). Without it every client
shares the proxy's address; the backend notices the forwarding headers, logs a
warning and leaves the per-address limits off rather than throttling everyone
together. Only name a header your proxy overwrites — on a directly exposed
port the header is whatever the caller sends.

Memory is bounded separately: proxied audio is held in a 256 MB buffer, a read
in flight counts at its full length, a track that does not fit is passed
straight through (or refused with `503 busy` when it can only be buffered),
and a single buffered track is capped at 64 MB.

## Configuration and credentials

`config.example.json` contains safe defaults and blank credential fields.
The real `config.json`, environment files, credentials, account exports, and
TLS materials are ignored by Git. Do not commit them.

Optional integrations read credentials from environment variables or the local
`config.json`: Spotify (`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`), Yandex
Music (`YANDEX_MUSIC_TOKEN`), VK (`VK_TOKEN`), and the diagnostic route
(`CDISC_DIAG_TOKEN`). YouTube cookies and account refresh tokens are disabled
by default. To enable them, configure private files outside the repository or
use the supported `CDISC_YOUTUBE_COOKIES`, `CDISC_YOUTUBE_OAUTH_FILE`, and
`CDISC_YOUTUBE_REFRESH_TOKEN` environment variables.

TLS can be terminated by a reverse proxy. If the application itself should
serve TLS, provide deployment-specific certificate paths through
`CDISC_TLS_CERT` and `CDISC_TLS_KEY`; no certificate or private key is included
in this repository.

## Endpoints

| Route | Purpose |
| --- | --- |
| `GET /` | Lists the available routes |
| `GET /search?q=` | Searches YouTube |
| `GET /audio/{id}` | Resolves a YouTube audio stream (`?mode=sabr` needs the diagnostic token) |
| `GET /info/{id}` | YouTube video metadata |
| `GET /streams/{id}` | YouTube format diagnostics; disabled unless a diagnostic token is configured |
| `GET /spotify?url=` | Resolves Spotify track, album, and playlist links |
| `GET /soundcloud?url=` | Resolves a SoundCloud track link |
| `GET /yandex?url=` and `/vk?url=` | Resolves music links and collections |
| `GET /cdisc/public-youtube-data` | Returns the configured or minted YouTube visitor data |
| `GET` and `POST /register` | Issues a per-server key, when `keys.enabled` |
| `GET /keys`, `POST /keys/{id}/revoke` | Lists and revokes keys; diagnostic token required |

Most track endpoints also have service-specific `info`, `audio`, and `search`
routes. See the route list returned by `GET /` and the handlers under
`cdisc_backend/routes/` for request and response details.

## Development

Run the local checks as scripts:

```sh
python test_range.py
python test_tracks.py
python test_access.py
python test_live.py
python test_e2e.py
```

`smoke.py` makes requests to real services and is not a local test.

## Project layout

```text
cdisc_backend/
  app.py          application and route registration
  config.py       settings and environment-variable handling
  routes/         HTTP handlers
  youtube/        YouTube clients, search, account and stream resolution
  spotify/        Spotify API and track matching
  soundcloud/     SoundCloud API
  yandex/         Yandex Music API
  vk/             VK Music API
```

## License

MIT. See [LICENSE](LICENSE).
