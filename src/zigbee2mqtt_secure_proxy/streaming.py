"""Delivery shared by the WebSocket and the event stream."""

import json

import anyio

from zigbee2mqtt_secure_proxy.actions import Proxy
from zigbee2mqtt_secure_proxy.auth.apikeys import AuthenticatedKey
from zigbee2mqtt_secure_proxy.mqtt import Message, Subscription
from zigbee2mqtt_secure_proxy.permissions import outgoing_tier


def may_receive(key: AuthenticatedKey, message: Message) -> bool:
    return key.tier.allows(outgoing_tier(message.topic))


def frame(message: Message) -> str:
    return json.dumps(
        {"topic": message.topic, "payload": message.payload},
        separators=(",", ":"),
        ensure_ascii=False,
    )


async def finish_stream(
    proxy: Proxy,
    key: AuthenticatedKey,
    subscription: Subscription,
    session_id: int,
    messages: int,
    size: int,
) -> None:
    """Close the books on a connection, even while it is being cancelled."""
    proxy.bridge.unsubscribe(subscription)
    proxy.unregister(key.id, subscription)
    with anyio.CancelScope(shield=True):
        await proxy.close_session(session_id, messages, size)
