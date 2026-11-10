# Configuration

The app reads these environment variables at startup. Put them in `.env`
(copy `.env.example`); compose passes it to the container. An invalid
value stops the app with a message naming the variable.

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | required | PostgreSQL connection, `postgresql://user:password@host:5432/dbname` |
| `MQTT_URL` | required | The broker zigbee2mqtt uses, `mqtt://host:1883` or `mqtts://host:8883` |
| `MQTT_USERNAME` | none | Broker user name |
| `MQTT_PASSWORD` | none | Broker password |
| `MQTT_BASE_TOPIC` | `zigbee2mqtt` | zigbee2mqtt's `base_topic` |
| `MQTT_REQUEST_TIMEOUT` | `10` | Seconds to wait for an answer from zigbee2mqtt |
| `FRONTEND_URL` | none | Serve zigbee2mqtt's own web UI through the proxy, from this address |
| `OIDC_ISSUER` | required | OpenID Connect issuer URL |
| `OIDC_CLIENT_ID` | required | OpenID Connect client ID |
| `OIDC_CLIENT_SECRET` | required | OpenID Connect client secret |
| `OIDC_REQUIRED_GROUP` | none | Only members of this group may log in. Without it, anyone the provider authenticates is an admin |
| `PUBLIC_URL` | required | External address of the proxy, used for the login callback. `https://` also marks the login cookie secure |
| `SESSION_SECRET` | required | Signs the login cookie; at least 32 characters |
| `REVIEW_TTL` | `24h` | How long a held action waits before it expires: `90s`, `30m`, `24h`, `7d` |
| `ACTION_BATCH_GAP` | `5` | Actions from one key less than this many seconds apart share a batch |
| `ACTION_RETENTION_DAYS` | `90` | Days to keep the action log. Admin events are kept indefinitely |
| `HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Address to listen on |
| `PORT` | `8080` | Port to listen on |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |


## Database service (not read by the app)

`docker-compose.prod.yaml` passes these to the `db` service. They must agree with `DATABASE_URL`.

| Variable | Meaning |
|----------|---------|
| `POSTGRES_USER` | Database user to create |
| `POSTGRES_PASSWORD` | That user's password |
| `POSTGRES_DB` | Database to create |

## Publishing (not read by the app)

| Variable | Meaning |
|----------|---------|
| `UV_PUBLISH_TOKEN` | PyPI API token, only for publishing by hand with `uv publish`. Releases from GitHub don't need it. |
