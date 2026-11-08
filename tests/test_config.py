from dataclasses import replace

import pytest

from zigbee2mqtt_secure_proxy.config import ConfigError, Settings

URL = "postgresql://user:hunter2@db:5432/proxy"  # not-a-secret
ENV = {
    "DATABASE_URL": URL,
    "MQTT_URL": "mqtt://broker:1884",
    "OIDC_ISSUER": "https://auth.test/application/o/z2m/",
    "OIDC_CLIENT_ID": "id",
    "OIDC_CLIENT_SECRET": "hunter3",  # not-a-secret
    "PUBLIC_URL": "https://proxy.test/",
    "SESSION_SECRET": "x" * 32,  # not-a-secret
}


def test_defaults() -> None:
    settings = Settings.from_env(ENV)
    assert (settings.host, settings.port, settings.log_level) == (
        "127.0.0.1",
        8080,
        "INFO",
    )
    assert settings.mqtt_base_topic == "zigbee2mqtt"
    assert settings.review_ttl == 86400
    assert settings.action_batch_gap == 5
    assert settings.action_retention_days == 90
    assert settings.public_url == "https://proxy.test"
    assert settings.oidc_issuer == "https://auth.test/application/o/z2m"


def test_reads_environment() -> None:
    env = ENV | {
        "HOST": "0.0.0.0",  # noqa: S104
        "PORT": "9000",
        "LOG_LEVEL": "debug",
        "REVIEW_TTL": "30m",
        "FRONTEND_URL": "http://z2m:8080/",
        "OIDC_REQUIRED_GROUP": "staff",
        "MQTT_USERNAME": "u",
        "MQTT_PASSWORD": "p",  # not-a-secret
    }
    settings = Settings.from_env(env)
    assert settings.port == 9000
    assert settings.log_level == "DEBUG"
    assert settings.review_ttl == 1800
    assert settings.frontend_url == "http://z2m:8080"
    assert settings.oidc_required_group == "staff"
    assert settings.mqtt_username == "u"


def test_mqtt_url_parts() -> None:
    settings = Settings.from_env(ENV)
    assert (settings.mqtt_host, settings.mqtt_port, settings.mqtt_tls) == (
        "broker",
        1884,
        False,
    )
    tls = replace(settings, mqtt_url="mqtts://broker")
    assert (tls.mqtt_port, tls.mqtt_tls) == (8883, True)


@pytest.mark.parametrize("port", ["eighty", "0", "65536"])
def test_rejects_bad_port(port: str) -> None:
    with pytest.raises(ConfigError, match="PORT"):
        Settings.from_env(ENV | {"PORT": port})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "loud"),
        ("MQTT_URL", "http://broker"),
        ("MQTT_URL", "mqtt://user:pw@broker"),
        ("MQTT_BASE_TOPIC", "a/#"),
        ("MQTT_REQUEST_TIMEOUT", "soon"),
        ("MQTT_REQUEST_TIMEOUT", "0"),
        ("REVIEW_TTL", "tomorrow"),
        ("ACTION_BATCH_GAP", "-1"),
        ("ACTION_RETENTION_DAYS", "0"),
        ("OIDC_ISSUER", "ftp://x"),
        ("PUBLIC_URL", "nope"),
        ("FRONTEND_URL", "nope"),
        ("SESSION_SECRET", "short"),
    ],
)
def test_rejects_bad_values_naming_the_variable(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(ENV | {name: value})


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "MQTT_URL",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "PUBLIC_URL",
        "SESSION_SECRET",
    ],
)
def test_required_variables(name: str) -> None:
    env = {k: v for k, v in ENV.items() if k != name}
    with pytest.raises(ConfigError, match=f"{name} is required"):
        Settings.from_env(env)


def test_bad_database_url_error_hides_the_value() -> None:
    bad = "mysql://user:hunter2@db/proxy"  # not-a-secret
    with pytest.raises(ConfigError, match="DATABASE_URL") as error:
        Settings.from_env(ENV | {"DATABASE_URL": bad})
    assert "hunter2" not in str(error.value)


def test_repr_hides_secrets() -> None:
    text = repr(Settings.from_env(ENV))
    assert "hunter2" not in text
    assert "hunter3" not in text
    assert "x" * 32 not in text
