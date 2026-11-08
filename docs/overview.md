# zigbee2mqtt-secure-proxy overview

**What:** an API-key gated, auditable proxy for zigbee2mqtt. Design and milestones are in PLAN.md.
**Runs as:** HTTP service on port 8080, plus a one-shot `migrate` command.
**Talks to:** PostgreSQL, the MQTT broker zigbee2mqtt uses, Authentik; optionally zigbee2mqtt's web UI.

## Files

- `src/zigbee2mqtt_secure_proxy/__main__.py`: command line (`serve`, `migrate`, `prune`), logging, signal handling.
- `src/zigbee2mqtt_secure_proxy/config.py`: `Settings` from environment variables.
- `src/zigbee2mqtt_secure_proxy/app.py`: FastAPI application, startup and shutdown; `GET /healthz`.
- `src/zigbee2mqtt_secure_proxy/mqtt.py`: broker connection, message cache, fan-out, request/response matching.
- `src/zigbee2mqtt_secure_proxy/permissions.py`: which tier each zigbee2mqtt operation needs.
- `src/zigbee2mqtt_secure_proxy/actions.py`: recording, batching, running actions, review workflow, pruning.
- `src/zigbee2mqtt_secure_proxy/ws.py`: zigbee2mqtt-compatible WebSocket.
- `src/zigbee2mqtt_secure_proxy/api.py`: HTTP mirror and event stream.
- `src/zigbee2mqtt_secure_proxy/streaming.py`: delivery shared by the WebSocket and the event stream.
- `src/zigbee2mqtt_secure_proxy/frontend_proxy.py`: optional pass-through to zigbee2mqtt's web UI.
- `src/zigbee2mqtt_secure_proxy/auth/`: API keys (`apikeys.py`), admin login (`oidc.py`).
- `src/zigbee2mqtt_secure_proxy/ui/`: admin routes and templates, under `/_proxy/`.
- `src/zigbee2mqtt_secure_proxy/models.py`: tables.
- `src/zigbee2mqtt_secure_proxy/db.py`: engines, running migrations.
- `src/zigbee2mqtt_secure_proxy/migrations/`: Alembic environment and versions.
- `tests/`: pytest; one file per module. `test_db.py` needs real PostgreSQL.
- `Dockerfile`: `dev` and `production` stages.
- `docker-compose.yaml` (dev), `.test`, `.lint`, `.prod`.

## Conventions

- All configuration from environment variables; see docs/configuration.md.
- Schema changes are Alembic migrations, applied only by the `migrate` command.
- Never log or echo `DATABASE_URL`: it holds a password. Never log request URLs: they can hold API keys.
- A new zigbee2mqtt bridge request is denied until it is added to `permissions.py`.
- Rules: https://github.com/romkey/python-ai-rules/blob/main/RULES.md
