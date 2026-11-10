# zigbee2mqtt-secure-proxy

[![CI](https://github.com/romkey/zigbee2mqtt-secure-proxy/actions/workflows/ci.yaml/badge.svg)](https://github.com/romkey/zigbee2mqtt-secure-proxy/actions/workflows/ci.yaml)
[![Lint](https://github.com/romkey/zigbee2mqtt-secure-proxy/actions/workflows/lint.yaml/badge.svg)](https://github.com/romkey/zigbee2mqtt-secure-proxy/actions/workflows/lint.yaml)
[![Python 3.15](https://img.shields.io/badge/python-3.15-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A proxy that sits in front of [zigbee2mqtt](https://www.zigbee2mqtt.io/) and gates access with API keys. Keys are read-only, read-write or read-write-delete, and a key can be held for review so an admin approves its actions before they run. Every action is recorded. Clients written for zigbee2mqtt work unchanged apart from supplying a key. Admins log in with OpenID Connect through Authentik.

> **AI disclosure:** This project was written by AI under human oversight,
> following [these rules](https://github.com/romkey/python-ai-rules/blob/main/RULES.md).

## What's here

| Path | What it is |
|------|------------|
| `src/zigbee2mqtt_secure_proxy/` | The app; [docs/overview.md](docs/overview.md) describes each module |
| `tests/` | pytest tests, using a fake broker and a real PostgreSQL |
| `docs/configuration.md` | Every environment variable |
| `PLAN.md` | The design |

## Technical details

A FastAPI service that holds one MQTT connection to the broker zigbee2mqtt uses, and stores keys and its log in PostgreSQL. Lock the broker down so that zigbee2mqtt and this proxy are the only things that can reach it, or clients can simply go around the proxy.

### For clients

**WebSocket.** `/api` speaks the same protocol as zigbee2mqtt's own frontend. Pass the API key where you would pass zigbee2mqtt's `auth_token`:

```text
ws://proxy:8080/api?token=z2m_xxxxxxxx_...
```

Set `FRONTEND_URL` and the stock zigbee2mqtt web UI works through the proxy too; give it the API key as its token.

**HTTP.** For scripts, send the key as `Authorization: Bearer <key>`:

| Request | What it does |
|---------|--------------|
| `GET /api/bridge/{info,devices,groups,state,...}` | Latest message on that bridge topic |
| `GET /api/devices/{name}` | Latest state of a device |
| `POST /api/devices/{name}/set` | Publish to `.../set` and return the next state |
| `POST /api/devices/{name}/get` | Publish to `.../get` and return the next state |
| `POST /api/bridge/request/{action}` | Make a bridge request and return its response |
| `GET /api/events` | Server-sent events of every message |
| `GET /api/actions/{id}` | Status of one of this key's held actions |

### API key access

| Access | Allows |
|--------|--------|
| `read` | State, `/get`, health checks, the network map |
| `read-write` | Also `/set`, permit join, restart, options, renames, OTA, backups, and installing extensions or converters |
| `read-write-delete` | Also removing devices, groups, group members, bindings, extensions and converters, and factory resets |

Anything the proxy does not recognise is denied. `read-write` allows zigbee2mqtt's `extension/save` and `converter/save`, which run code on the zigbee2mqtt host, and `backup`, which contains the Zigbee network key. Give it out accordingly.

### Review

Mark a key **for review** and its reads still run at once, but anything that changes state is held. HTTP clients get `202` and a status URL. WebSocket clients get zigbee2mqtt's normal error response for bridge requests and no reply for `/set`, which zigbee2mqtt never answers either. Admins approve or reject each action, each batch, or all of them from the Review page. Held actions expire after `REVIEW_TTL`.

### Action log

Every request a key makes is recorded with its outcome, whether it ran, failed, was denied or was held. Actions from one key that arrive within `ACTION_BATCH_GAP` seconds of each other form a batch. Passwords, tokens, network keys and backups are not kept. A WebSocket or event-stream connection is recorded once, rather than once per message delivered.

### Authentik setup

Create an OAuth2/OpenID provider and an application in Authentik, with redirect URI `<PUBLIC_URL>/_proxy/auth/callback`. Put its issuer URL, client ID and client secret in `.env`. Everyone who can log in is an admin, so bind the application to a group or set `OIDC_REQUIRED_GROUP`. The admin interface is at `<PUBLIC_URL>/_proxy/`.

### Commands

| Command | What it does |
|---------|--------------|
| `zigbee2mqtt-secure-proxy` or `... serve` | Runs the web service |
| `zigbee2mqtt-secure-proxy migrate` | Brings the database schema up to date. The service never migrates on its own |
| `zigbee2mqtt-secure-proxy prune` | Expires held actions and deletes old log entries. The service also does this by itself |

`GET /healthz` reports that the process is up, without authentication.

## Running it

Requires Docker. Configuration comes from environment variables, kept in
`.env` (see [docs/configuration.md](docs/configuration.md)):

```bash
cp .env.example .env   # then edit it
docker compose -f docker-compose.prod.yaml up -d
```

Set matching database credentials in `DATABASE_URL` and the `POSTGRES_*`
variables. Compose runs `migrate` before starting the app. Back up the
`db-data` volume.

That runs the latest release from `ghcr.io/romkey/zigbee2mqtt-secure-proxy`; set
`APP_VERSION=X.Y.Z` to pin one. The app answers on http://localhost:8080.

### With pip

Needs Python 3.15 or newer. Install into a virtual environment, from a
release's wheel (or `pip install zigbee2mqtt-secure-proxy` if it's published on PyPI):

```bash
python3 -m venv ~/.venvs/zigbee2mqtt-secure-proxy
~/.venvs/zigbee2mqtt-secure-proxy/bin/pip install https://github.com/romkey/zigbee2mqtt-secure-proxy/releases/download/vX.Y.Z/zigbee2mqtt_secure_proxy-X.Y.Z-py3-none-any.whl
~/.venvs/zigbee2mqtt-secure-proxy/bin/zigbee2mqtt-secure-proxy --version
```

Set its environment variables yourself (see
[docs/configuration.md](docs/configuration.md)); it doesn't read `.env`. You need a PostgreSQL server, and you run
`zigbee2mqtt-secure-proxy migrate` before starting the service and after
each upgrade.

## Development

With Docker, nothing else to install:

```bash
docker compose run --rm app uv run --locked zigbee2mqtt-secure-proxy migrate   # set up the dev database
docker compose up --build                                  # run from the working tree
docker compose -f docker-compose.test.yaml run --rm test   # tests with coverage
docker compose -f docker-compose.lint.yaml run --rm lint   # every lint check, as CI runs them
```

With [uv](https://docs.astral.sh/uv/), which installs the exact versions
CI tests (`uv.lock`):

```bash
uv sync
DATABASE_URL=postgresql://proxy:proxy@localhost:5432/proxy uv run pytest  # not-a-secret: needs `docker compose up db`
uv run mypy
```

With pip (25.1 or newer, for `--group`). pip doesn't read `uv.lock`, so it
may pick newer dependency versions than CI tested:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e . --group dev
DATABASE_URL=postgresql://proxy:proxy@localhost:5432/proxy pytest  # not-a-secret: needs `docker compose up db`
mypy
```

Either way, `uvx pre-commit install` (or `pip install pre-commit &&
pre-commit install`) runs the lint checks on every commit.

## License

[MIT](LICENSE)
