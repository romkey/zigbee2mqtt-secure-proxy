# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- WebSocket at `/api` compatible with zigbee2mqtt's frontend protocol, gated
  by an API key passed where zigbee2mqtt expects its `auth_token`.
- HTTP mirror of zigbee2mqtt's MQTT topics, and a server-sent event stream.
- Optional pass-through to zigbee2mqtt's own web UI.
- API keys with `read`, `read-write` and `read-write-delete` access.
- Keys can be marked for review, holding their changes until an admin approves
  them singly, by batch or all together.
- A log of every action, grouped into batches by timing.
- Admin interface with OpenID Connect login through Authentik.
- FastAPI service with `GET /healthz`, settings from the environment,
  PostgreSQL with Alembic, and `migrate` and `prune` commands.
