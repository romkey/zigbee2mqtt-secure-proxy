from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import Harness
from zigbee2mqtt_secure_proxy.actions import KeyRevokedError, redact
from zigbee2mqtt_secure_proxy.permissions import classify

START = datetime(2026, 1, 1, 12, tzinfo=UTC)


def record(
    admin: Harness, key_id: int, seconds: float, topic: str = "lamp/set"
) -> None:
    assert admin.client.portal is not None

    async def run() -> None:
        await admin.proxy.record(
            key_id=key_id,
            transport="ws",
            client_addr=None,
            topic=topic,
            payload={"state": "ON"},
            operation=classify(topic),
            now=START + timedelta(seconds=seconds),
        )

    admin.client.portal.call(run)


def batches(admin: Harness) -> list[tuple[int, int]]:
    return [
        (row[0], row[1])
        for row in admin.rows(
            "SELECT api_key_id, action_count FROM batches ORDER BY id"
        )
    ]


def test_actions_within_the_gap_share_a_batch(admin: Harness) -> None:
    admin.create_key("read-write")
    record(admin, 1, 0)
    record(admin, 1, 4.9)
    record(admin, 1, 9.8)
    assert batches(admin) == [(1, 3)]


def test_a_gap_of_exactly_the_limit_starts_a_new_batch(admin: Harness) -> None:
    admin.create_key("read-write")
    record(admin, 1, 0)
    record(admin, 1, 5)
    assert batches(admin) == [(1, 1), (1, 1)]


def test_two_keys_interleaved_never_merge(admin: Harness) -> None:
    admin.create_key("read-write")
    admin.create_key("read-write")
    record(admin, 1, 0)
    record(admin, 2, 1)
    record(admin, 1, 2)
    assert batches(admin) == [(1, 2), (2, 1)]


def test_batch_summary_collects_devices_topics_and_verbs(admin: Harness) -> None:
    admin.create_key("read-write")
    record(admin, 1, 0, "lamp/set")
    record(admin, 1, 1, "fan/get")
    assert admin.rows("SELECT summary FROM batches")[0][0] == {
        "devices": ["lamp", "fan"],
        "topics": ["lamp/set", "fan/get"],
        "verbs": ["set", "get"],
    }


def test_recording_for_a_revoked_key_fails(admin: Harness) -> None:
    admin.create_key()
    admin.client.post("/_proxy/keys/1/revoke", data={"csrf": admin.csrf()})
    with pytest.raises(KeyRevokedError):
        record(admin, 1, 0)


def test_redact() -> None:
    payload = {
        "options": {"mqtt": {"password": "x"}, "n": [{"auth_token": "y"}]},
        "state": "ON",
    }
    assert redact("bridge/request/options", payload) == {
        "options": {"mqtt": {"password": "***"}, "n": [{"auth_token": "***"}]},
        "state": "ON",
    }
    assert redact("bridge/request/install_code/add", {"value": "x"}) == "***"


def test_backup_data_is_never_stored(admin: Harness) -> None:
    admin.broker.answers["backup"] = {"data": {"zip": "secret"}, "status": "ok"}
    token = admin.create_key("read-write")
    response = admin.client.post(
        "/api/bridge/request/backup", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.json()["data"] == {"zip": "secret"}
    admin.drain()
    result = admin.rows("SELECT result FROM actions")[0][0]
    assert "data" not in result


def test_large_results_are_truncated(admin: Harness) -> None:
    admin.broker.answers["networkmap"] = {"data": {"v": "x" * 9000}, "status": "ok"}
    token = admin.create_key()
    admin.client.post(
        "/api/bridge/request/networkmap", headers={"Authorization": f"Bearer {token}"}
    )
    admin.drain()
    result = admin.rows("SELECT result FROM actions")[0][0]
    assert result["truncated"] is True
    assert "data" not in result


def test_expire_and_prune(admin: Harness) -> None:
    assert admin.client.portal is not None
    admin.create_key("read-write", for_review=True)
    record(admin, 1, 0)
    now = START + timedelta(days=2)
    expired = admin.client.portal.call(admin.proxy.expire, now)
    assert expired == 1
    assert admin.rows("SELECT status FROM actions") == [("expired",)]
    admin.client.portal.call(admin.proxy.prune, START + timedelta(days=30))
    assert len(admin.rows("SELECT id FROM actions")) == 1
    admin.client.portal.call(admin.proxy.prune, START + timedelta(days=100))
    assert admin.rows("SELECT id FROM actions") == []
    assert admin.rows("SELECT id FROM batches") == []
    assert admin.rows("SELECT id FROM admin_events") != []


def test_interrupted_actions_are_closed_out(admin: Harness) -> None:
    assert admin.client.portal is not None
    admin.create_key("read-write")
    record(admin, 1, 0)
    admin.client.portal.call(admin.proxy.expire, START + timedelta(hours=2))
    assert admin.rows("SELECT status, error FROM actions") == [
        ("failed", "interrupted before it finished")
    ]
