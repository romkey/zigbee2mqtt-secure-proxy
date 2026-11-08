# zigbee2mqtt-secure-proxy — Plan

Status: implemented, 2026-11-08. Section 14 lists where the build differs from this design.

## 1. Summary

A small web service that sits in front of a zigbee2mqtt instance and provides:

- An admin web UI, login via OpenID Connect (Authentik). Every logged-in user is an admin.
- A client-facing API that is indistinguishable from zigbee2mqtt's own, except that it
  requires an API key. The primary surface is a WebSocket speaking the zigbee2mqtt frontend
  protocol; an HTTP mirror of the MQTT topics is offered alongside for scripts.
- API keys with one of three tiers: `read`, `read-write`, `read-write-delete`.
- API keys optionally flagged **for review**: mutating actions under such a key are queued,
  and an admin approves or rejects them (individually, per batch, or all) before they run.
- A complete record of every action taken through the proxy, allowed or not, grouped into
  batches by timing so the admin sees "key X changed 6 lights at 19:02" rather than 6 rows.

The proxy itself talks to zigbee2mqtt over MQTT, exactly as the zigbee2mqtt frontend and
Home Assistant do. Nothing else reaches the broker.

## 2. Decisions

| Decision | Choice |
|---|---|
| Language | Python (FastAPI), following python-ai-rules |
| Database | PostgreSQL |
| License | MIT |
| Risky non-delete actions (permit_join, restart, OTA, options) | `read-write` tier |
| Reads under for-review keys | execute immediately, only mutations are held |
| Per-key device allowlists | later release, schema leaves room |
| Frontend-compatible WebSocket | in scope, first release; transparency is a requirement |

### Why Python, not Rails

"Lightweight" rules Rails out: a FastAPI app is one small image with no asset pipeline and
no job runner, and PostgreSQL is the only other service. python-ai-rules are concrete; there
is no Ruby equivalent of comparable precision.

### Stack

| Concern | Choice | Why |
|---|---|---|
| Web framework | FastAPI + uvicorn | async; WebSockets, SSE and HTTP in one process |
| MQTT | aiomqtt (asyncio wrapper over paho-mqtt 2) | one long-lived connection shared by all clients |
| OIDC | Authlib (`authlib.integrations.starlette_client`) | standard OIDC discovery + PKCE against Authentik |
| Sessions | Starlette `SessionMiddleware` (signed cookie) | no server-side session store |
| Templates | Jinja2, plain HTML forms, no JS build step | lightweight |
| Database | PostgreSQL (current major, `postgres:N-trixie` image), psycopg 3 | rules default, owner preference |
| ORM / migrations | SQLAlchemy 2 (async, psycopg) + Alembic via an explicit `migrate` command | required by the rules once models exist |
| Settings | frozen dataclass read from env once at startup | rules §2 |

Exact versions are looked up at implementation time, never from memory (rules §0, §8).

## 3. Client-facing API

### 3.1 Transparency requirement

A client written for zigbee2mqtt must work against the proxy unchanged apart from supplying
an API key. Concretely:

- The WebSocket lives at the same path the zigbee2mqtt frontend uses (`/api`) and speaks the
  same JSON message format (`{"topic": ..., "payload": ...}` in both directions, topics
  relative to the base topic). Message shapes, retained-state replay on connect, and
  `bridge/response` correlation by `transaction` are copied from zigbee2mqtt's frontend
  implementation and docs, cited in comments (rules §0). This is verified at implementation
  time against the current zigbee2mqtt source, not from memory.
- zigbee2mqtt already supports a frontend `auth_token`, passed by clients on the WebSocket
  URL as `?token=...`. The proxy accepts the API key in exactly that place, so clients that
  already know how to authenticate to zigbee2mqtt need no change at all. `Authorization:
  Bearer` is accepted as well for HTTP clients.
- Optional `FRONTEND_URL`: when set, HTTP requests that are not the proxy's own routes
  (the stock frontend's HTML, JS, CSS) are reverse-proxied to zigbee2mqtt's frontend, so the
  unmodified zigbee2mqtt web UI can be served through the proxy. The proxy's admin UI lives
  under `/_proxy/` to stay out of the way.
- Errors the proxy introduces (denied by tier, held for review) are reported in-protocol as
  a `bridge/response/...` with `status: "error"` and an `error` string, which is how
  zigbee2mqtt itself reports failures. A `/set` from a for-review key produces no response
  until approved (zigbee2mqtt does not acknowledge `/set` either); on approval the resulting
  state message flows to the client naturally.

### 3.2 HTTP mirror

For scripts that prefer HTTP, topic names are preserved so the mapping is obvious:

| HTTP | MQTT |
|---|---|
| `GET /api/bridge/{info,devices,groups,state,...}` | cached retained message |
| `GET /api/devices/{name}` | cached retained state |
| `POST /api/devices/{name}/get` body=payload | publish `.../get`, return the next state message |
| `POST /api/devices/{name}/set` body=payload | publish `.../set`, return the next state message |
| `POST /api/bridge/request/{action...}` body=payload | publish request, await matching `bridge/response/...` (timeout) |
| `GET /api/events` (SSE) | stream of all messages under the base topic |
| `GET /api/actions/{id}` | status of a held action (proxy-specific, documented as such) |

The topic list and payload shapes come from
https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html at implementation time.

## 4. Permission model

Tiers are cumulative. Each MQTT operation is classified in one table in code; anything not in
the table is denied, so new zigbee2mqtt actions fail closed until classified.

| Tier | Allowed |
|---|---|
| `read` | retained state replay, all `GET`s, `/get`, SSE / WebSocket subscription, read-only bridge requests (e.g. `health_check`, `backup`, config queries) |
| `read-write` | `/set`, `permit_join`, rename, group membership add, `options` changes, OTA check/update, `restart`, and every other state-changing action that does not remove something |
| `read-write-delete` | `device/remove`, `group/remove`, `group/members/remove`, `touchlink/factory_reset` and other removal or reset actions |

Per-key device allowlists are deferred. The `api_keys.device_allowlist` column exists from
the first migration (nullable JSONB, always null for now) so adding the feature is code only.

## 5. Action record and batching

Every request a key makes is recorded as an `action` row, whatever happens to it:

| status | meaning |
|---|---|
| `executed` | allowed, published, response (if any) captured |
| `failed` | allowed, published, but zigbee2mqtt returned an error or timed out |
| `denied` | tier insufficient or topic unclassified; nothing published |
| `pending` / `approved` / `rejected` / `expired` | the review workflow, §6 |

Recorded per action: key, transport (`ws`, `http`, `sse`), client address, topic, payload,
timestamps, result or error, batch id. Read actions are recorded too (`/get`, HTTP `GET`,
read-only bridge requests). Passive subscriptions are not one action per message; a
WebSocket or SSE connection is recorded once as a `session` row (connect, disconnect, bytes,
message count), which keeps the table proportional to what clients *do*, not what the
network emits.

**Batching by timing.** Actions from the same key are grouped into a batch while they arrive
less than `ACTION_BATCH_GAP` (default 5 s) apart; the first action after a longer gap starts
a new batch. The batch id is assigned at insert time from the key's most recent action, so
grouping is a cheap column, not a query-time heuristic. Batches carry a derived summary
(span, count, distinct devices/topics, verbs) for the UI. The gap is per key so two keys
acting at once never merge.

The admin UI shows the action log as batches, newest first, expandable to rows, filterable
by key, status, device and time. A batch whose rows are all `pending` is what the review page
shows, so "approve this batch" is the natural unit.

Separately, `admin_events` records admin-side activity: logins, key created / revoked /
retiered, review decisions. Append-only; the app never updates or deletes rows in either
table. Payloads are stored (they are small device commands); secrets never.

Retention: `ACTION_RETENTION_DAYS` (default 90); a daily task deletes older `actions`,
`sessions` and empties batches. Admin events are kept indefinitely.

## 6. Review queue

- A key flagged `for_review` behaves as `read` immediately; every mutating request within its
  tier is inserted as an action with status `pending`. HTTP responds `202 Accepted` with the
  action id; WebSocket bridge requests get an in-protocol error response (§3.1).
- Review page lists pending batches grouped by key with **Approve / Reject** per batch,
  **Approve all / Reject all** at the top, and per-row buttons inside an expanded batch.
- Approving executes the action exactly as if submitted live, records result and error on the
  same row, and moves it to `executed` or `failed`.
- Pending actions expire after `REVIEW_TTL` (default 24 h). Stale commands never fire late.
- Approve-all and approve-batch apply only to the action ids posted with the form, so a
  request arriving while the admin is clicking is not swept up silently.

## 7. Data model

```
admin_users    id, oidc_subject (unique), email, display_name, last_login_at
api_keys       id, name, prefix (8 chars, shown in UI), key_hash (sha256), tier,
               for_review bool, device_allowlist JSONB null, created_by, created_at,
               last_used_at, revoked_at
batches        id, api_key_id, started_at, last_action_at, action_count,
               summary JSONB (devices, topics, verbs)
actions        id, api_key_id, batch_id, session_id null, transport, client_addr,
               topic, payload JSONB, status, requested_at, decided_at, decided_by null,
               executed_at, result JSONB, error text
sessions       id, api_key_id, transport, client_addr, connected_at, disconnected_at,
               messages_out, bytes_out
admin_events   id, at, admin_id, kind, detail JSONB
```

- JSONB for payloads/results/summaries, TIMESTAMPTZ everywhere, partial index on
  `actions(status) WHERE status = 'pending'`, index on `(api_key_id, requested_at desc)`
  for batch assignment and the log view.
- Keys are random (`secrets.token_urlsafe(32)`), shown once at creation, stored hashed.
  Format `z2m_<prefix>_<secret>`; the prefix lets the UI identify a key without the secret.

## 8. Authentication and authorization

- `/_proxy/login` starts an OIDC Authorization Code + PKCE flow against the Authentik issuer
  from `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`; discovery fetched at startup.
- On callback, the user row is created/updated and a signed session cookie is set.
- Everyone who completes login is an admin. Optional `OIDC_REQUIRED_GROUP` restricts login to
  members of one Authentik group (from the `groups` claim), so Authentik decides who is staff.
- Admin UI routes require the session; `/api` and the WebSocket require an API key only.
  Session cookies never grant API access and API keys never grant UI access.
- CSRF token on UI forms, bound to the session. `/api` is bearer-only so needs none.
- `GET /healthz`: unauthenticated, process-only, as the rules require. `bridge/state` over
  the API tells a key holder whether the MQTT link is up, as it would with zigbee2mqtt.

## 9. Configuration (environment)

```
MQTT_URL                 mqtt://host:1883 or mqtts://host:8883        (required)
MQTT_USERNAME / MQTT_PASSWORD                                         (optional)
MQTT_BASE_TOPIC          default zigbee2mqtt
MQTT_REQUEST_TIMEOUT     seconds, default 10
FRONTEND_URL             optional; reverse-proxy the stock frontend's HTTP from here
OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET                       (required)
OIDC_REQUIRED_GROUP      optional
PUBLIC_URL               external base URL for the OIDC redirect URI  (required)
SESSION_SECRET           required
DATABASE_URL             postgresql://user:pass@db:5432/proxy         (required)
POSTGRES_PASSWORD        used by the compose db service only
REVIEW_TTL               default 24h
ACTION_BATCH_GAP         seconds, default 5
ACTION_RETENTION_DAYS    default 90
HOST / PORT              default 0.0.0.0 / 8080
LOG_LEVEL                default INFO
```

Every variable is documented in `docs/configuration.md` and listed in `.env.example` with
dummy values, per rules §2.

## 10. Project layout (per python-ai-rules §1)

```
zigbee2mqtt-secure-proxy/
  src/zigbee2mqtt_secure_proxy/
    __main__.py        main(): --version, serve / migrate / prune subcommands, SIGTERM
    config.py          frozen Settings from env
    app.py             FastAPI factory, middleware, lifespan (MQTT + DB)
    mqtt.py            MQTT client: retained cache, request/response correlation, fan-out
    permissions.py     operation -> tier classification table
    actions.py         action recording, batch assignment, review state machine
    auth/oidc.py       Authlib OIDC flow and session
    auth/apikeys.py    token parsing (?token= and Bearer), hashing, route dependency
    ws.py              zigbee2mqtt-frontend-compatible WebSocket endpoint
    api/               HTTP mirror, SSE, action status
    frontend_proxy.py  optional pass-through to FRONTEND_URL
    ui/                admin routes + Jinja2 templates under /_proxy/
    db/                SQLAlchemy models, session, Alembic env
    migrations/
  tests/
  docs/configuration.md, docs/overview.md
  scripts/             from the rules template
  Dockerfile, docker-compose{,.test,.lint,.prod}.yaml, pyproject.toml, uv.lock,
  .python-version, .env.example, .pre-commit-config.yaml, renovate.json, .gitleaks.toml,
  .yamllint.yaml, .gitignore, README.md, CHANGELOG.md, LICENSE (MIT), SECURITY.md, AGENTS.md
```

Tooling as the rules dictate: uv, ruff, mypy --strict, pytest with 80% coverage floor,
pre-commit, the three GitHub workflows, protected main, squash-merge PRs. The compose, CI,
migration and test setup for PostgreSQL comes from the rules' `templates/optional/postgres/`:
a `db` service in the dev and prod compose files and a Postgres service container in CI. The
README documents running `migrate` before `serve`; the app never migrates implicitly.
`AGENTS.md` records the python-ai-rules version followed, with `CLAUDE.md` as a symlink.

## 11. Testing approach

- MQTT is faked at the boundary: an in-process fake broker the `mqtt.py` client talks to, so
  tests publish retained messages and assert what the proxy publishes. No real network.
- WebSocket compatibility is tested with recorded real zigbee2mqtt frontend traffic
  (captured once from a live instance, committed as fixtures) replayed through the proxy,
  asserting byte-for-byte message equivalence apart from ordering guarantees.
- OIDC is faked by stubbing Authlib's token exchange and userinfo; one test exercises the real
  discovery parsing against a saved Authentik discovery document.
- Permission table: every classified operation under every tier, plus "unknown is denied".
- Action recording: every status path produces exactly one row; batching tests cover the gap
  boundary, two keys interleaved, and the review page's batch grouping.
- Review queue tests cover the full state machine including expiry and approve-all scoping.
- Database tests run against real PostgreSQL: a service in `docker-compose.test.yaml` locally
  and a service container in CI, Alembic upgrade once per session, rollback per test.

## 12. Milestones

1. **Skeleton** — repo from the rules template plus the optional Postgres template, MIT
   license, settings, `/healthz`, `migrate` with an empty first migration, Docker and CI
   green, first PR.
2. **MQTT core** — connection, retained cache, request/response correlation, fan-out.
3. **API keys, action recording, read tier** — key model, token parsing, `actions` /
   `batches` / `sessions` tables, HTTP `GET`s and SSE.
4. **WebSocket compatibility** — `/api` endpoint, retained replay, fixture-based equivalence
   tests, optional frontend pass-through.
5. **OIDC + admin UI** — login, key management, action log with batches.
6. **Write and delete tiers** — `/set`, bridge requests, permission table complete.
7. **Review queue** — held actions, review page with batch approval, expiry, status endpoint.
8. **Polish** — retention pruning, docs, README per rules §10, release 0.1.0.

Each milestone is one or a few PRs, each with tests, changelog entry and docs.

## 13. Later

- Per-key device allowlists (column already present).
- Rate limiting beyond what a reverse proxy in front provides.
- Multi-tenant or non-admin UI users (not planned).
- Managing zigbee2mqtt's own configuration file (not planned).

## 14. Differences from this plan in the build

- `backup` needs `read-write`, not `read`, and its response is withheld from `read` keys,
  because it contains the Zigbee network key.
- OTA checks need `read-write`; `device/unbind` and `device/binds/clear` need `read-write-delete`.
- OIDC discovery happens on first login, not at startup, so an Authentik outage can't stop the API.
- Database tests truncate tables between tests instead of rolling back, because the code under
  test commits several transactions per request.
- The WebSocket compatibility tests are built from zigbee2mqtt 2.14.3's source, not from
  recorded live traffic.
- Secrets in payloads are redacted when an action is recorded, or when a held action is decided.
- Rows in `actions` are updated as an action moves through its lifecycle; `admin_events` is append-only.
