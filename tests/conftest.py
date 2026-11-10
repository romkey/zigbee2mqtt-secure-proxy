"""A fake broker at the MQTT boundary and a real PostgreSQL behind the app."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from zigbee2mqtt_secure_proxy import db
from zigbee2mqtt_secure_proxy.actions import Proxy
from zigbee2mqtt_secure_proxy.app import create_app
from zigbee2mqtt_secure_proxy.config import Settings
from zigbee2mqtt_secure_proxy.mqtt import Connection, Connector


class FakeBroker:
    """Stands in for the broker and for zigbee2mqtt behind it."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []
        self.incoming: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self.connected = asyncio.Event()
        self.changed = asyncio.Condition()
        # Bridge action -> response body, sent as zigbee2mqtt would answer.
        self.answers: dict[str, dict[str, Any]] = {}

    def connector(self) -> Connector:
        @asynccontextmanager
        async def connect() -> AsyncIterator[Connection]:
            self.connected.set()
            yield self

        return connect

    async def subscribe(self, topic: str) -> None:
        assert topic == "zigbee2mqtt/#"

    async def publish(self, topic: str, payload: str) -> None:
        relative = topic.removeprefix("zigbee2mqtt/")
        body = json.loads(payload)
        self.published.append((relative, body))
        async with self.changed:
            self.changed.notify_all()
        action = relative.removeprefix("bridge/request/")
        if relative.startswith("bridge/request/") and action in self.answers:
            answer = dict(self.answers[action])
            if isinstance(body, dict) and "transaction" in body:
                answer["transaction"] = body["transaction"]
            self.incoming.put_nowait(
                (f"zigbee2mqtt/bridge/response/{action}", json.dumps(answer).encode())
            )

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self.incoming.get()
            yield item
            self.incoming.task_done()

    async def inject(self, topic: str, payload: object) -> None:
        """Deliver a message from zigbee2mqtt and wait until it is processed."""
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.incoming.put_nowait((f"zigbee2mqtt/{topic}", raw))
        await self.incoming.join()

    async def wait_published(self, count: int) -> None:
        with anyio.fail_after(5):
            async with self.changed:
                await self.changed.wait_for(lambda: len(self.published) >= count)


class FakeOidc:
    def __init__(self) -> None:
        self.claims: dict[str, object] = {"sub": "alice", "email": "a@example.test"}
        self.error: Exception | None = None

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> Response:
        return RedirectResponse(f"https://idp.test/auth?cb={redirect_uri}", 302)

    async def authorize_access_token(
        self, request: Request
    ) -> dict[str, dict[str, object]]:
        if self.error:
            raise self.error
        return {"userinfo": self.claims}


def base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "MQTT_URL": "mqtt://broker.test",
        "OIDC_ISSUER": "https://idp.test/application/o/proxy/",
        "OIDC_CLIENT_ID": "proxy",
        "OIDC_CLIENT_SECRET": "client-secret",  # not-a-secret
        "PUBLIC_URL": "http://proxy.test",
        "SESSION_SECRET": "s" * 40,  # not-a-secret
        "ACTION_BATCH_GAP": "5",
        "MQTT_REQUEST_TIMEOUT": "0.2",
    }


@pytest.fixture(scope="session")
def migrated() -> str:
    url = os.environ["DATABASE_URL"]
    db.migrate(url)
    return url


@pytest.fixture
def settings(migrated: str) -> Settings:
    return Settings.from_env(base_env())


@dataclass
class Harness:
    client: TestClient
    broker: FakeBroker
    oidc: FakeOidc
    proxy: Proxy

    def inject(self, topic: str, payload: object) -> None:
        assert self.client.portal is not None
        self.client.portal.call(self.broker.inject, topic, payload)

    def wait_published(self, count: int) -> None:
        assert self.client.portal is not None
        self.client.portal.call(self.broker.wait_published, count)

    def drain(self) -> None:
        assert self.client.portal is not None
        self.client.portal.call(self.proxy.drain)

    def login(self) -> None:
        response = self.client.get("/_proxy/auth/callback", follow_redirects=False)
        assert response.status_code == 303

    def csrf(self) -> str:
        page = self.client.get("/_proxy/keys").text
        return page.split('name="csrf" value="')[1].split('"')[0]

    def create_key(self, tier: str = "read", *, for_review: bool = False) -> str:
        data = {"csrf": self.csrf(), "name": f"{tier} key", "tier": tier}
        if for_review:
            data["for_review"] = "on"
        page = self.client.post("/_proxy/keys", data=data).text
        return page.split('class="token"><code>')[1].split("<")[0]

    def rows(self, sql: str) -> list[Any]:
        assert self.client.portal is not None

        async def fetch() -> list[Any]:
            async with self.proxy.sessions() as session:
                return list((await session.execute(text(sql))).all())

        return self.client.portal.call(fetch)


def _run_app(settings: Settings, **kwargs: Any) -> Iterator[Harness]:
    broker = FakeBroker()
    oidc = FakeOidc()
    app = create_app(settings, connector=broker.connector(), oidc=oidc, **kwargs)
    # base_url is http: a test client talks to the proxy like any other client.
    with TestClient(app, base_url="http://proxy.test") as client:
        assert client.portal is not None
        client.portal.call(broker.connected.wait)
        yield Harness(client, broker, oidc, app.state.proxy)
    engine = db.make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE actions, batches, sessions, admin_events, api_keys,"
                " admin_users RESTART IDENTITY CASCADE"
            )
        )
    engine.dispose()


@pytest.fixture
def harness(settings: Settings) -> Iterator[Harness]:
    yield from _run_app(settings)


@pytest.fixture
def admin(harness: Harness) -> Harness:
    harness.login()
    return harness


def with_settings(settings: Settings, **changes: Any) -> Settings:
    return replace(settings, **changes)
