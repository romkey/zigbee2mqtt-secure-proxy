"""Settings, read once at startup from environment variables.

Every variable is documented in docs/configuration.md and listed, with a
safe example value, in .env.example.
"""

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_DURATION = re.compile(r"^(\d+)([smhd])$")
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_MIN_SESSION_SECRET_LENGTH = 32


class ConfigError(ValueError):
    """An environment variable is missing or set to something unusable."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _number(name: str, text: str, minimum: float) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ConfigError(f"{name} must be a number, not {text!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}, not {text}")
    return value


def _duration(name: str, text: str) -> float:
    match = _DURATION.match(text)
    if not match:
        raise ConfigError(f"{name} must look like 90s, 30m, 24h or 7d, not {text!r}")
    return int(match[1]) * _SECONDS_PER_UNIT[match[2]]


def _http_url(name: str, url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{name} must be an http:// or https:// URL")
    return url.rstrip("/")


def _database_url(env: Mapping[str, str]) -> str:
    url = _required(env, "DATABASE_URL")
    # Never echo the value: it contains the database password.
    parts = urlsplit(url)
    if parts.scheme not in {"postgresql", "postgres"} or not parts.hostname:
        raise ConfigError(
            "DATABASE_URL must look like postgresql://user:password@host:5432/dbname"
        )
    return url


def _mqtt_url(env: Mapping[str, str]) -> str:
    url = _required(env, "MQTT_URL")
    parts = urlsplit(url)
    if parts.scheme not in {"mqtt", "mqtts"} or not parts.hostname:
        raise ConfigError("MQTT_URL must look like mqtt://host:1883 or mqtts://host")
    if parts.username or parts.password:
        raise ConfigError("MQTT_URL must not hold credentials; use MQTT_USERNAME")
    return url


def _base_topic(env: Mapping[str, str]) -> str:
    topic = env.get("MQTT_BASE_TOPIC", "zigbee2mqtt")
    if not topic or set(topic) & {"+", "#", "\0"}:
        raise ConfigError("MQTT_BASE_TOPIC must be a plain topic, without wildcards")
    return topic


def _session_secret(env: Mapping[str, str]) -> str:
    secret = _required(env, "SESSION_SECRET")
    if len(secret) < _MIN_SESSION_SECRET_LENGTH:
        raise ConfigError(
            f"SESSION_SECRET must be at least {_MIN_SESSION_SECRET_LENGTH} characters"
        )
    return secret


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    mqtt_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str = field(repr=False)
    public_url: str
    session_secret: str = field(repr=False)
    mqtt_username: str | None = None
    mqtt_password: str | None = field(default=None, repr=False)
    mqtt_base_topic: str = "zigbee2mqtt"
    mqtt_request_timeout: float = 10
    frontend_url: str | None = None
    oidc_required_group: str | None = None
    review_ttl: float = 86400
    action_batch_gap: float = 5
    action_retention_days: int = 90
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def mqtt_host(self) -> str:
        return urlsplit(self.mqtt_url).hostname or ""

    @property
    def mqtt_tls(self) -> bool:
        return urlsplit(self.mqtt_url).scheme == "mqtts"

    @property
    def mqtt_port(self) -> int:
        return urlsplit(self.mqtt_url).port or (8883 if self.mqtt_tls else 1883)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        port_text = env.get("PORT", str(cls.port))
        try:
            port = int(port_text)
        except ValueError:
            raise ConfigError(f"PORT must be a number, not {port_text!r}") from None
        if not 0 < port < 65536:
            raise ConfigError(f"PORT must be 1-65535, not {port}")

        log_level = env.get("LOG_LEVEL", cls.log_level).upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigError(f"LOG_LEVEL {log_level!r} is not a logging level")

        frontend_url = env.get("FRONTEND_URL")
        return cls(
            database_url=_database_url(env),
            mqtt_url=_mqtt_url(env),
            mqtt_username=env.get("MQTT_USERNAME") or None,
            mqtt_password=env.get("MQTT_PASSWORD") or None,
            mqtt_base_topic=_base_topic(env),
            mqtt_request_timeout=_number(
                "MQTT_REQUEST_TIMEOUT",
                env.get("MQTT_REQUEST_TIMEOUT", str(cls.mqtt_request_timeout)),
                0.1,
            ),
            frontend_url=(
                _http_url("FRONTEND_URL", frontend_url) if frontend_url else None
            ),
            oidc_issuer=_http_url("OIDC_ISSUER", _required(env, "OIDC_ISSUER")),
            oidc_client_id=_required(env, "OIDC_CLIENT_ID"),
            oidc_client_secret=_required(env, "OIDC_CLIENT_SECRET"),
            oidc_required_group=env.get("OIDC_REQUIRED_GROUP") or None,
            public_url=_http_url("PUBLIC_URL", _required(env, "PUBLIC_URL")),
            session_secret=_session_secret(env),
            review_ttl=_duration("REVIEW_TTL", env.get("REVIEW_TTL", "24h")),
            action_batch_gap=_number(
                "ACTION_BATCH_GAP",
                env.get("ACTION_BATCH_GAP", str(cls.action_batch_gap)),
                0,
            ),
            action_retention_days=int(
                _number(
                    "ACTION_RETENTION_DAYS",
                    env.get("ACTION_RETENTION_DAYS", str(cls.action_retention_days)),
                    1,
                )
            ),
            host=env.get("HOST", cls.host),
            port=port,
            log_level=log_level,
        )
