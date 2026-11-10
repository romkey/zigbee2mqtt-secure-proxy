import logging
import signal
from collections.abc import Callable
from typing import Any

import pytest
import uvicorn
from sqlalchemy.exc import OperationalError

from tests.conftest import base_env
from zigbee2mqtt_secure_proxy import __main__ as cli
from zigbee2mqtt_secure_proxy import __version__, db
from zigbee2mqtt_secure_proxy.actions import Proxy

URL = "postgresql://user:hunter2@db:5432/proxy"  # not-a-secret


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in base_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", URL)


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"zigbee2mqtt-secure-proxy {__version__}\n"


@pytest.mark.usefixtures("configured")
def test_bad_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PORT", "eighty")
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
    assert "PORT must be a number" in capsys.readouterr().err


@pytest.mark.usefixtures("configured")
def test_serve_installs_a_sigterm_handler_and_returns_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[bool] = []
    handlers: dict[int, Callable[..., Any]] = {}

    class FakeServer:
        def __init__(self, config: object) -> None:
            pass

        def run(self) -> None:
            ran.append(True)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(signal, "signal", handlers.__setitem__)

    assert cli.main([]) == 0
    assert ran == [True]
    handlers[signal.SIGTERM](signal.SIGTERM, None)


@pytest.mark.usefixtures("configured")
def test_migrate_runs_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    monkeypatch.setattr(db, "migrate", urls.append)
    assert cli.main(["migrate"]) == 0
    assert urls == [URL]


@pytest.mark.usefixtures("configured")
def test_migrate_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(url: str) -> None:
        raise OperationalError(URL, {}, Exception(URL))

    monkeypatch.setattr(db, "migrate", fail)
    assert cli.main(["migrate"]) == 1
    assert "hunter2" not in caplog.text


@pytest.mark.usefixtures("configured")
def test_prune_command_needs_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    pruned: list[bool] = []

    async def fake_expire(self: object) -> int:
        return 0

    async def fake_prune(self: object) -> None:
        pruned.append(True)

    monkeypatch.setattr(Proxy, "expire", fake_expire)
    monkeypatch.setattr(Proxy, "prune", fake_prune)
    assert cli.main(["prune"]) == 0
    assert pruned == [True]


def test_tokens_are_redacted_from_logs() -> None:
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" [accepted]',
        ("1.2.3.4", "/api?token=z2m_abc_secret&x=1"),
        None,
    )
    assert cli.RedactTokens().filter(record)
    assert "secret" not in record.getMessage()
    assert "token=***" in record.getMessage()
