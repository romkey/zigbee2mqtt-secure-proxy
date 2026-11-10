import asyncio
from typing import Any

from tests.conftest import Harness
from zigbee2mqtt_secure_proxy.api import event_stream
from zigbee2mqtt_secure_proxy.auth.apikeys import AuthenticatedKey
from zigbee2mqtt_secure_proxy.mqtt import Message
from zigbee2mqtt_secure_proxy.permissions import Tier


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_requires_a_valid_key(harness: Harness) -> None:
    assert harness.client.get("/api/bridge/info").status_code == 401
    bad = bearer("z2m_00000000_nope")
    assert harness.client.get("/api/bridge/info", headers=bad).status_code == 401


def test_session_cookie_does_not_grant_api_access(admin: Harness) -> None:
    assert admin.client.get("/api/bridge/info").status_code == 401


def test_reads_cached_topics_and_records_them(admin: Harness) -> None:
    admin.inject("bridge/info", {"version": "2.14.3"})
    admin.inject("room/lamp", {"state": "ON"})
    headers = bearer(admin.create_key())
    bridge = admin.client.get("/api/bridge/info", headers=headers)
    assert bridge.json() == {"version": "2.14.3"}
    device = admin.client.get("/api/devices/room/lamp", headers=headers)
    assert device.json() == {"state": "ON"}
    assert admin.client.get("/api/devices/none", headers=headers).status_code == 404
    assert admin.rows("SELECT topic, status, transport FROM actions ORDER BY id")[
        :2
    ] == [
        ("bridge/info", "executed", "http"),
        ("room/lamp", "executed", "http"),
    ]


def test_token_query_parameter_works_too(admin: Harness) -> None:
    admin.inject("bridge/info", {})
    token = admin.create_key()
    assert admin.client.get(f"/api/bridge/info?token={token}").status_code == 200


def test_set_returns_the_next_state(admin: Harness) -> None:
    headers = bearer(admin.create_key("read-write"))
    assert admin.client.portal is not None

    async def answer() -> None:
        await admin.broker.wait_published(1)
        await admin.broker.inject("lamp", {"state": "ON"})

    admin.client.portal.start_task_soon(answer)
    response = admin.client.post(
        "/api/devices/lamp/set", json={"state": "ON"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json() == {"state": "ON"}


def test_set_without_a_state_update_reports_published(admin: Harness) -> None:
    headers = bearer(admin.create_key("read-write"))
    response = admin.client.post("/api/devices/lamp/set", json={}, headers=headers)
    assert (response.status_code, response.json()) == (202, {"status": "published"})


def test_get_is_allowed_for_read_keys(admin: Harness) -> None:
    headers = bearer(admin.create_key())
    response = admin.client.post(
        "/api/devices/lamp/get", content="plain", headers=headers
    )
    assert response.status_code == 202
    assert admin.broker.published == [("lamp/get", "plain")]


def test_set_is_denied_for_read_keys(admin: Harness) -> None:
    headers = bearer(admin.create_key())
    assert admin.client.post("/api/devices/l/set", headers=headers).status_code == 403
    assert admin.broker.published == []


def test_set_with_the_broker_down_is_503(admin: Harness) -> None:
    headers = bearer(admin.create_key("read-write"))
    admin.proxy.bridge.connection = None
    assert admin.client.post("/api/devices/l/set", headers=headers).status_code == 503


def test_bridge_request_round_trip_hides_the_injected_transaction(
    admin: Harness,
) -> None:
    admin.broker.answers["health_check"] = {"data": {"healthy": True}, "status": "ok"}
    response = admin.client.post(
        "/api/bridge/request/health_check", headers=bearer(admin.create_key())
    )
    assert response.json() == {"data": {"healthy": True}, "status": "ok"}


def test_bridge_request_keeps_the_callers_transaction(admin: Harness) -> None:
    admin.broker.answers["health_check"] = {"data": {}, "status": "ok"}
    response = admin.client.post(
        "/api/bridge/request/health_check",
        json={"transaction": "mine"},
        headers=bearer(admin.create_key()),
    )
    assert response.json()["transaction"] == "mine"


def test_bridge_request_timeout_is_504(admin: Harness) -> None:
    response = admin.client.post(
        "/api/bridge/request/health_check", headers=bearer(admin.create_key())
    )
    assert response.status_code == 504


def test_bridge_request_with_the_broker_down_is_503(admin: Harness) -> None:
    headers = bearer(admin.create_key())
    admin.proxy.bridge.connection = None
    response = admin.client.post("/api/bridge/request/health_check", headers=headers)
    assert response.status_code == 503


def test_held_request_returns_202_and_a_status_url(admin: Harness) -> None:
    headers = bearer(admin.create_key("read-write", for_review=True))
    response = admin.client.post("/api/devices/lamp/set", json={}, headers=headers)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    status = admin.client.get(body["status_url"], headers=headers).json()
    assert status["status"] == "pending"
    assert status["topic"] == "lamp/set"


def test_a_key_cannot_read_another_keys_action(admin: Harness) -> None:
    first = bearer(admin.create_key("read-write", for_review=True))
    second = bearer(admin.create_key())
    action = admin.client.post("/api/devices/l/set", headers=first).json()["action_id"]
    assert admin.client.get(f"/api/actions/{action}", headers=second).status_code == 404


def test_event_stream_filters_and_finishes(admin: Harness) -> None:
    proxy = admin.proxy
    assert admin.client.portal is not None
    key = admin.create_key()
    assert key
    reader = AuthenticatedKey(1, Tier.READ)

    async def collect() -> list[str]:
        session_id = await proxy.open_session(1, "sse", None)
        subscription, _ = proxy.bridge.subscribe()
        proxy.register(1, subscription)
        replay = [
            Message("lamp", {"state": "ON"}),
            Message("bridge/response/backup", {"data": {}}),
        ]
        chunks: list[str] = []
        stream: Any = event_stream(
            proxy, reader, subscription, replay, session_id, keepalive=0.01
        )
        chunks.append(await anext(stream))
        chunks.append(await anext(stream))  # the keepalive
        subscription.put(Message("bridge/response/backup", {"data": {}}))
        subscription.put(Message("lamp", {"state": "OFF"}))
        chunks.append(await anext(stream))
        subscription.close()
        async for chunk in stream:
            chunks.append(chunk)
        await asyncio.sleep(0)
        return chunks

    chunks = admin.client.portal.call(collect)
    assert chunks == [
        'data: {"topic":"lamp","payload":{"state":"ON"}}\n\n',
        ": keepalive\n\n",
        'data: {"topic":"lamp","payload":{"state":"OFF"}}\n\n',
    ]
    assert admin.rows("SELECT messages_out FROM sessions") == [(2,)]
