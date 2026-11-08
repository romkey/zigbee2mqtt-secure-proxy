"""Clients must not be able to tell the proxy from zigbee2mqtt."""

from typing import Any

import pytest
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from tests.conftest import Harness


def connect(admin: Harness, token: str) -> Any:
    return admin.client.websocket_connect(f"/api?token={token}")


def request(
    ws: WebSocketTestSession, action: str, transaction: str = "t1"
) -> dict[str, Any]:
    ws.send_json(
        {"topic": f"bridge/request/{action}", "payload": {"transaction": transaction}}
    )
    reply: dict[str, Any] = ws.receive_json()
    return reply


def test_bad_token_gets_the_same_close_as_zigbee2mqtt(harness: Harness) -> None:
    with (
        harness.client.websocket_connect("/api?token=nope") as ws,
        pytest.raises(WebSocketDisconnect) as closed,
    ):
        ws.receive_json()
    assert closed.value.code == 4401
    assert closed.value.reason == "Unauthorized"


def test_retained_state_is_replayed_then_live_messages_flow(admin: Harness) -> None:
    admin.inject("bridge/devices", [{"friendly_name": "lamp"}])
    admin.inject("lamp", {"state": "ON"})
    with connect(admin, admin.create_key()) as ws:
        assert ws.receive_json() == {
            "topic": "bridge/devices",
            "payload": [{"friendly_name": "lamp"}],
        }
        assert ws.receive_json() == {"topic": "lamp", "payload": {"state": "ON"}}
        admin.inject("lamp", {"state": "OFF"})
        assert ws.receive_json() == {"topic": "lamp", "payload": {"state": "OFF"}}


def test_a_set_reaches_the_broker_and_is_recorded(admin: Harness) -> None:
    with connect(admin, admin.create_key("read-write")) as ws:
        ws.send_json({"topic": "lamp/set", "payload": {"state": "ON"}})
        admin.wait_published(1)
        admin.drain()
    assert admin.broker.published == [("lamp/set", {"state": "ON"})]
    assert admin.rows("SELECT topic, status, transport, device FROM actions") == [
        ("lamp/set", "executed", "ws", "lamp")
    ]


def test_invalid_messages_are_ignored(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as ws:
        ws.send_text("not json")
        ws.send_text("[1]")
        ws.send_bytes(b"\x00")
        reply = request(ws, "permit_join")
    assert reply["payload"]["status"] == "error"
    assert len(admin.rows("SELECT id FROM actions")) == 1


def test_denied_bridge_request_gets_a_zigbee2mqtt_style_error(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as ws:
        reply = request(ws, "permit_join")
    assert reply["topic"] == "bridge/response/permit_join"
    assert reply["payload"]["status"] == "error"
    assert reply["payload"]["data"] == {}
    assert reply["payload"]["transaction"] == "t1"
    assert admin.broker.published == []
    assert admin.rows("SELECT status FROM actions") == [("denied",)]


def test_denied_set_is_silent_like_zigbee2mqtt_but_recorded(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as ws:
        ws.send_json({"topic": "lamp/set", "payload": {"state": "ON"}})
        request(ws, "permit_join")
    assert admin.broker.published == []
    assert admin.rows("SELECT topic, status FROM actions ORDER BY id")[0] == (
        "lamp/set",
        "denied",
    )


def test_unknown_action_is_denied_without_a_reply(admin: Harness) -> None:
    with connect(admin, admin.create_key("read-write-delete")) as ws:
        ws.send_json({"topic": "bridge/request/brand_new", "payload": {}})
        ws.send_json({"topic": "lamp/get", "payload": {"state": ""}})
        admin.wait_published(1)
        admin.drain()
    assert admin.broker.published == [("lamp/get", {"state": ""})]
    assert admin.rows("SELECT verb, status FROM actions ORDER BY id") == [
        ("unrecognised", "denied"),
        ("get", "executed"),
    ]


def test_bridge_response_is_delivered_and_recorded(admin: Harness) -> None:
    admin.broker.answers["health_check"] = {"data": {"healthy": True}, "status": "ok"}
    with connect(admin, admin.create_key()) as ws:
        reply = request(ws, "health_check")
        admin.drain()
    assert reply["payload"]["status"] == "ok"
    assert reply["payload"]["transaction"] == "t1"
    assert admin.rows("SELECT status, result->>'status' FROM actions") == [
        ("executed", "ok")
    ]


def test_zigbee2mqtt_errors_mark_the_action_failed(admin: Harness) -> None:
    admin.broker.answers["device/rename"] = {
        "data": {},
        "status": "error",
        "error": "no such device",
    }
    with connect(admin, admin.create_key("read-write")) as ws:
        request(ws, "device/rename")
        admin.drain()
    assert admin.rows("SELECT status, error FROM actions") == [
        ("failed", "no such device")
    ]


def test_no_response_marks_the_action_failed(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as ws:
        ws.send_json({"topic": "bridge/request/health_check", "payload": {}})
        admin.wait_published(1)
        admin.drain()
    status, error = admin.rows("SELECT status, error FROM actions")[0]
    assert status == "failed"
    assert "no response" in error


def test_backup_response_is_withheld_from_read_keys(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as reader:
        admin.inject("bridge/response/backup", {"data": {"zip": "x"}, "status": "ok"})
        admin.inject("lamp", {"state": "ON"})
        assert reader.receive_json()["topic"] == "lamp"


def test_held_request_replies_and_waits_for_review(admin: Harness) -> None:
    key = admin.create_key("read-write", for_review=True)
    with connect(admin, key) as ws:
        reply = request(ws, "permit_join")
    assert "review" in reply["payload"]["error"]
    assert admin.broker.published == []
    assert admin.rows("SELECT status FROM actions") == [("pending",)]


def test_reads_under_a_review_key_run_immediately(admin: Harness) -> None:
    key = admin.create_key("read-write", for_review=True)
    with connect(admin, key) as ws:
        ws.send_json({"topic": "lamp/get", "payload": {"state": ""}})
        admin.wait_published(1)


def test_session_is_recorded(admin: Harness) -> None:
    admin.inject("lamp", {"state": "ON"})
    with connect(admin, admin.create_key()) as ws:
        ws.receive_json()
    rows = admin.rows("SELECT transport, messages_out, disconnected_at FROM sessions")
    assert rows[0][:2] == ("ws", 1)
    assert rows[0][2] is not None


def test_revoking_a_key_disconnects_its_clients(admin: Harness) -> None:
    with connect(admin, admin.create_key()) as ws:
        admin.client.post("/_proxy/keys/1/revoke", data={"csrf": admin.csrf()})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_broker_outage_is_recorded_as_a_failure(admin: Harness) -> None:
    with connect(admin, admin.create_key("read-write")) as ws:
        admin.proxy.bridge.connection = None
        ws.send_json({"topic": "lamp/set", "payload": {}})
        request(ws, "device/remove")
        admin.drain()
    assert admin.rows("SELECT status, error FROM actions ORDER BY id")[0] == (
        "failed",
        "MQTT broker unavailable",
    )
