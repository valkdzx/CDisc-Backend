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
| `GET /audio/{id}` | Resolves a YouTube audio stream |
| `GET /info/{id}` | YouTube video metadata |
| `GET /streams/{id}` | YouTube format diagnostics; disabled unless a diagnostic token is configured |
| `GET /spotify?url=` | Resolves Spotify track, album, and playlist links |
| `GET /soundcloud?url=` | Resolves a SoundCloud track link |
| `GET /yandex?url=` and `/vk?url=` | Resolves music links and collections |
| `GET /cdisc/public-youtube-data` | Returns the configured or minted YouTube visitor data |

Most track endpoints also have service-specific `info`, `audio`, and `search`
routes. See the route list returned by `GET /` and the handlers under
`cdisc_backend/routes/` for request and response details.

## Development

Run the local checks as scripts:

```sh
python test_range.py
python test_tracks.py
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
