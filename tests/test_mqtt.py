import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiomqtt
import pytest

from zigbee2mqtt_secure_proxy.mqtt import (
    Connection,
    Message,
    MqttBridge,
    MqttUnavailableError,
    Subscription,
    is_client_command,
    parse_payload,
)


@asynccontextmanager
async def no_connection() -> AsyncIterator[Connection]:
    raise aiomqtt.MqttError
    yield  # pragma: no cover


def bridge() -> MqttBridge:
    return MqttBridge(no_connection, "z2m", retry_delay=0)


def test_parse_payload() -> None:
    assert parse_payload(b'{"a":1}') == {"a": 1}
    assert parse_payload(b"online") == "online"


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("lamp/set", True),
        ("lamp/get/x", True),
        ("bridge/request/restart", True),
        ("lamp", False),
        ("lamp/availability", False),
        ("bridge/response/restart", False),
        ("bridge/devices", False),
    ],
)
def test_client_commands(topic: str, *, expected: bool) -> None:
    assert is_client_command(topic) is expected


def test_cache_keeps_state_but_not_events_or_commands() -> None:
    b = bridge()
    b.handle("z2m/lamp", b'{"state":"ON"}')
    b.handle("z2m/bridge/logging", b'{"message":"x"}')
    b.handle("z2m/bridge/event", b"{}")
    b.handle("z2m/bridge/response/restart", b"{}")
    b.handle("z2m/lamp/set", b"{}")
    b.handle("other/lamp", b"{}")
    assert b.cache == {"lamp": {"state": "ON"}}


def test_empty_payload_clears_a_retained_topic() -> None:
    b = bridge()
    b.handle("z2m/lamp", b"{}")
    b.handle("z2m/lamp", b"")
    assert b.cache == {}


def test_replay_puts_bridge_topics_first() -> None:
    b = bridge()
    b.handle("z2m/lamp", b"{}")
    b.handle("z2m/bridge/info", b"{}")
    _, replay = b.subscribe()
    assert [m.topic for m in replay] == ["bridge/info", "lamp"]


def test_attribute_topics_are_dropped_once_names_are_known() -> None:
    b = bridge()
    b.handle("z2m/bridge/devices", b'[{"friendly_name":"lamp"}]')
    b.handle("z2m/lamp/state", b"ON")
    b.handle("z2m/lamp/availability", b"online")
    b.handle("z2m/lamp", b"{}")
    assert set(b.cache) == {"bridge/devices", "lamp/availability", "lamp"}


async def test_full_queue_closes_the_slow_client() -> None:
    subscription = Subscription()
    for i in range(subscription.queue.maxsize + 1):
        subscription.put(Message("t", i))
    items = [subscription.queue.get_nowait() for _ in range(subscription.queue.qsize())]
    assert items[-1] is None


async def test_lost_connection_tells_clients_the_bridge_is_offline() -> None:
    b = bridge()
    subscription, _ = b.subscribe()
    task = asyncio.create_task(b.run())
    message = await asyncio.wait_for(subscription.queue.get(), 5)
    task.cancel()
    assert message == Message("bridge/state", {"state": "offline"})
    assert b.cache["bridge/state"] == {"state": "offline"}


async def test_send_without_a_connection_raises() -> None:
    with pytest.raises(MqttUnavailableError):
        await bridge().send("lamp/set", {})


class _FailingConnection:
    async def subscribe(self, topic: str) -> None: ...

    async def publish(self, topic: str, payload: str) -> None:
        raise aiomqtt.MqttError

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        yield ("", b"")  # pragma: no cover


async def test_failed_publish_raises_and_forgets_the_waiter() -> None:
    b = bridge()
    b.connection = _FailingConnection()
    with pytest.raises(MqttUnavailableError):
        await b.send("bridge/request/restart", {}, expects_response="restart")
    assert b._responses["bridge/response/restart", None] == []


async def test_responses_match_by_transaction_then_in_order() -> None:
    b = bridge()
    b.connection = _FailingConnection()
    b.connection.publish = _noop  # type: ignore[method-assign]  # keep the connection, skip the failure
    first = await b.send("x", {"transaction": "a"}, expects_response="health_check")
    second = await b.send("x", {"transaction": "b"}, expects_response="health_check")
    plain = await b.send("x", {}, expects_response="health_check")
    assert first is not None
    assert second is not None
    assert plain is not None
    b.handle("z2m/bridge/response/health_check", b'{"transaction":"b"}')
    assert second.done()
    assert not first.done()
    b.handle("z2m/bridge/response/health_check", b'{"status":"ok"}')
    assert plain.done()


async def _noop(topic: str, payload: str) -> None:
    return None


async def test_next_message_can_be_discarded() -> None:
    b = bridge()
    future = b.next_message("lamp")
    b.discard_next("lamp", future)
    b.handle("z2m/lamp", b"{}")
    assert not future.done()
