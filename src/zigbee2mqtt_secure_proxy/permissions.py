"""Which API key tier each zigbee2mqtt operation needs.

Topics are relative to the MQTT base topic. Anything not recognised here is
denied, so an action added by a future zigbee2mqtt fails closed until it is
classified.

The request list comes from zigbee2mqtt 2.14.3 (`requestLookup` and the
`topicRegex` values in lib/extension/*.ts), and
https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# The same pattern zigbee2mqtt uses in lib/extension/publish.ts.
GET_SET = re.compile(r"^(?!bridge)(.+?)/(get|set)(?:/(.+))?$")
_BRIDGE_REQUEST = "bridge/request/"
_FORBIDDEN_TOPIC_CHARACTERS = {"+", "#", "\0"}


class Tier(StrEnum):
    READ = "read"
    READ_WRITE = "read-write"
    READ_WRITE_DELETE = "read-write-delete"

    def allows(self, required: Tier) -> bool:
        order = list(Tier)
        return order.index(self) >= order.index(required)


R, W, D = Tier.READ, Tier.READ_WRITE, Tier.READ_WRITE_DELETE

BRIDGE_REQUESTS: dict[str, Tier] = {
    "health_check": R,
    "coordinator_check": R,
    "networkmap": R,
    "device/reporting/read": R,
    "device/generate_external_definition": R,
    # The backup holds the Zigbee network key, so it is not a plain read.
    "backup": W,
    "permit_join": W,
    "restart": W,
    "options": W,
    "action": W,
    "install_code/add": W,
    "extension/save": W,
    "converter/save": W,
    "touchlink/scan": W,
    "touchlink/identify": W,
    "device/options": W,
    "device/rename": W,
    "device/configure": W,
    "device/interview": W,
    "device/bind": W,
    "device/configure_reporting": W,
    "device/reporting/configure": W,
    "device/ota_update/check": W,
    "device/ota_update/check/downgrade": W,
    "device/ota_update/update": W,
    "device/ota_update/update/downgrade": W,
    "device/ota_update/update/abort": W,
    "device/ota_update/schedule": W,
    "device/ota_update/schedule/downgrade": W,
    "device/ota_update/unschedule": W,
    "group/add": W,
    "group/options": W,
    "group/rename": W,
    "group/members/add": W,
    "extension/remove": D,
    "converter/remove": D,
    "touchlink/factory_reset": D,
    "device/remove": D,
    "device/unbind": D,
    "device/binds/clear": D,
    "group/remove": D,
    "group/members/remove": D,
    "group/members/remove_all": D,
}


@dataclass(frozen=True)
class Operation:
    tier: Tier
    verb: str
    device: str | None = None
    # Answered from the proxy's cache; nothing is published.
    cached: bool = False
    # A bridge request, which zigbee2mqtt answers on bridge/response/<action>.
    bridge_action: str | None = None


def cached_read(topic: str, device: str | None = None) -> Operation:
    return Operation(Tier.READ, "read", device=device or None, cached=True)


def is_valid_topic(topic: str) -> bool:
    return bool(topic) and not (
        set(topic) & _FORBIDDEN_TOPIC_CHARACTERS or topic.startswith("/")
    )


def bridge_action(topic: str) -> str | None:
    """The action of a bridge/request/<action> topic, whether known or not."""
    return topic.removeprefix(_BRIDGE_REQUEST) if _is_bridge_request(topic) else None


def _is_bridge_request(topic: str) -> bool:
    return topic.startswith(_BRIDGE_REQUEST) and len(topic) > len(_BRIDGE_REQUEST)


def classify(topic: str) -> Operation | None:
    """How a client publish to `topic` is treated; None means unrecognised."""
    if not is_valid_topic(topic):
        return None
    action = bridge_action(topic)
    if action is not None:
        tier = BRIDGE_REQUESTS.get(action)
        if tier is None:
            return None
        return Operation(tier, action, bridge_action=action)
    match = GET_SET.match(topic)
    if not match:
        return None
    return Operation(R if match[2] == "get" else W, match[2], device=match[1])


def outgoing_tier(topic: str) -> Tier:
    """The tier a client needs to receive a message on `topic`."""
    return W if topic == "bridge/response/backup" else R
