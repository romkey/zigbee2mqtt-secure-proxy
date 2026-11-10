import pytest

from zigbee2mqtt_secure_proxy.permissions import (
    BRIDGE_REQUESTS,
    Tier,
    classify,
    outgoing_tier,
)


def test_tiers_are_cumulative() -> None:
    assert Tier.READ.allows(Tier.READ)
    assert not Tier.READ.allows(Tier.READ_WRITE)
    assert Tier.READ_WRITE.allows(Tier.READ)
    assert not Tier.READ_WRITE.allows(Tier.READ_WRITE_DELETE)
    assert Tier.READ_WRITE_DELETE.allows(Tier.READ_WRITE)


@pytest.mark.parametrize("action", sorted(BRIDGE_REQUESTS))
def test_every_bridge_request_has_its_listed_tier(action: str) -> None:
    operation = classify(f"bridge/request/{action}")
    assert operation is not None
    assert operation.tier is BRIDGE_REQUESTS[action]
    assert operation.bridge_action == action


@pytest.mark.parametrize(
    ("action", "tier"),
    [
        ("health_check", Tier.READ),
        ("permit_join", Tier.READ_WRITE),
        ("restart", Tier.READ_WRITE),
        ("options", Tier.READ_WRITE),
        ("device/ota_update/update", Tier.READ_WRITE),
        ("backup", Tier.READ_WRITE),
        ("device/remove", Tier.READ_WRITE_DELETE),
        ("group/members/remove", Tier.READ_WRITE_DELETE),
        ("touchlink/factory_reset", Tier.READ_WRITE_DELETE),
    ],
)
def test_agreed_classifications(action: str, tier: Tier) -> None:
    operation = classify(f"bridge/request/{action}")
    assert operation is not None
    assert operation.tier is tier


@pytest.mark.parametrize(
    ("topic", "tier", "device"),
    [
        ("lamp/set", Tier.READ_WRITE, "lamp"),
        ("lamp/set/state", Tier.READ_WRITE, "lamp"),
        ("lamp/l1/set", Tier.READ_WRITE, "lamp/l1"),
        ("lamp/get", Tier.READ, "lamp"),
        ("room/lamp/get/brightness", Tier.READ, "room/lamp"),
    ],
)
def test_device_commands(topic: str, tier: Tier, device: str) -> None:
    operation = classify(topic)
    assert operation is not None
    assert (operation.tier, operation.device) == (tier, device)


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "lamp",
        "lamp/availability",
        "bridge/state",
        "bridge/request/",
        "bridge/request/brand_new_action",
        "bridge/set",
        "lamp/#/set",
        "lamp/+/set",
        "/lamp/set",
        "lamp\0/set",
    ],
)
def test_anything_unrecognised_is_denied(topic: str) -> None:
    assert classify(topic) is None


def test_backup_responses_need_write_access() -> None:
    assert outgoing_tier("bridge/response/backup") is Tier.READ_WRITE
    assert outgoing_tier("bridge/devices") is Tier.READ
