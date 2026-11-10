"""The proxy's one connection to the broker, shared by every client.

Keeps the latest message per topic to replay to new clients, fans live
messages out to them, and matches bridge requests to their responses.
"""

import asyncio
import contextlib
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Protocol  # noqa: UP035

import aiomqtt

from zigbee2mqtt_secure_proxy.config import Settings
from zigbee2mqtt_secure_proxy.permissions import GET_SET

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 10_000
_MAX_RETRY_DELAY = 30.0
_MQTT_QOS = 1
# Non-replayable zigbee2mqtt topics: events, not state.
_TRANSIENT_BRIDGE_TOPICS = {"bridge/logging", "bridge/event"}


class MqttUnavailableError(RuntimeError):
    """The broker cannot be reached right now."""


def parse_payload(raw: bytes) -> Any:
    """JSON if it parses, else text: what zigbee2mqtt's frontend does."""
    text = raw.decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def dump_payload(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def is_client_command(topic: str) -> bool:
    """Requests travelling towards zigbee2mqtt, which clients must not be sent."""
    if topic.startswith("bridge/"):
        return topic.startswith("bridge/request/")
    return GET_SET.match(topic) is not None


class Connection(Protocol):
    async def subscribe(self, topic: str) -> None: ...

    async def publish(self, topic: str, payload: str) -> None: ...

    def messages(self) -> AsyncIterator[tuple[str, bytes]]: ...


type Connector = Callable[[], AsyncContextManager[Connection]]


class _AiomqttConnection:
    def __init__(self, client: aiomqtt.Client) -> None:
        self.client = client

    async def subscribe(self, topic: str) -> None:
        await self.client.subscribe(topic, qos=_MQTT_QOS)

    async def publish(self, topic: str, payload: str) -> None:
        await self.client.publish(topic, payload, qos=_MQTT_QOS)

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        async for message in self.client.messages:
            payload = message.payload
            yield (
                message.topic.value,
                payload if isinstance(payload, bytes) else str(payload).encode(),
            )


def aiomqtt_connector(settings: Settings) -> Connector:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Connection]:
        async with aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            tls_context=ssl.create_default_context() if settings.mqtt_tls else None,
            timeout=settings.mqtt_request_timeout,
        ) as client:
            yield _AiomqttConnection(client)

    return connect


@dataclass(frozen=True)
class Message:
    topic: str
    payload: object


class Subscription:
    """One client's queue of outgoing messages."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Message | None] = asyncio.Queue(_QUEUE_LIMIT)

    def put(self, message: Message) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("dropping a client that stopped reading")
            self.close()

    def close(self) -> None:
        """Make the consumer stop, even if the queue is full."""
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class MqttBridge:
    def __init__(
        self, connector: Connector, base_topic: str, retry_delay: float = 1.0
    ) -> None:
        self.connector = connector
        self.base_topic = base_topic
        self.retry_delay = retry_delay
        self.cache: dict[str, object] = {}
        self.subscriptions: set[Subscription] = set()
        self.connection: Connection | None = None
        self._responses: dict[tuple[str, str | None], list[asyncio.Future[object]]] = {}
        self._next: dict[str, list[asyncio.Future[object]]] = {}
        self._entity_names: set[str] = set()

    @property
    def connected(self) -> bool:
        return self.connection is not None

    async def run(self) -> None:
        delay = self.retry_delay
        while True:
            try:
                async with self.connector() as connection:
                    await connection.subscribe(f"{self.base_topic}/#")
                    self.connection = connection
                    delay = self.retry_delay
                    logger.info("connected to the MQTT broker")
                    async for full_topic, raw in connection.messages():
                        self.handle(full_topic, raw)
            except aiomqtt.MqttError as e:
                logger.warning("MQTT connection lost: %s", type(e).__name__)
            self._connection_lost()
            await asyncio.sleep(delay)
            delay = min(max(delay, 0.001) * 2, _MAX_RETRY_DELAY)

    def _connection_lost(self) -> None:
        self.connection = None
        # What zigbee2mqtt tells its clients when it goes away.
        self._dispatch("bridge/state", {"state": "offline"})

    def handle(self, full_topic: str, raw: bytes) -> None:
        prefix = f"{self.base_topic}/"
        if not full_topic.startswith(prefix):
            return
        topic = full_topic.removeprefix(prefix)
        if is_client_command(topic):
            return
        if not raw:
            # zigbee2mqtt clears a retained topic with an empty message.
            self.cache.pop(topic, None)
            return
        self._dispatch(topic, parse_payload(raw))

    def _dispatch(self, topic: str, payload: object) -> None:
        self._learn_entity_names(topic, payload)
        if self._is_attribute_topic(topic):
            return
        if self._replayable(topic):
            self.cache[topic] = payload
        self._resolve_waiters(topic, payload)
        message = Message(topic, payload)
        for subscription in self.subscriptions:
            subscription.put(message)

    def _replayable(self, topic: str) -> bool:
        return topic not in _TRANSIENT_BRIDGE_TOPICS and not topic.startswith(
            "bridge/response/"
        )

    def _learn_entity_names(self, topic: str, payload: object) -> None:
        if topic not in {"bridge/devices", "bridge/groups"} or not isinstance(
            payload, list
        ):
            return
        names = {
            entity["friendly_name"]
            for entity in payload
            if isinstance(entity, dict) and "friendly_name" in entity
        }
        self._entity_names |= names

    def _is_attribute_topic(self, topic: str) -> bool:
        """zigbee2mqtt's per-attribute output, which its frontend never receives."""
        if topic in self._entity_names or topic.endswith("/availability"):
            return False
        return any(topic.startswith(f"{name}/") for name in self._entity_names)

    def _resolve_waiters(self, topic: str, payload: object) -> None:
        for future in self._next.pop(topic, []):
            if not future.done():
                future.set_result(payload)
        if not topic.startswith("bridge/response/"):
            return
        transaction = payload.get("transaction") if isinstance(payload, dict) else None
        for candidate in (transaction, None):
            waiters = self._responses.get((topic, candidate))
            while waiters:
                future = waiters.pop(0)
                if not future.done():
                    future.set_result(payload)
                    return

    def subscribe(self) -> tuple[Subscription, list[Message]]:
        """A new subscription and the state to replay, with no gap between them."""
        subscription = Subscription()
        self.subscriptions.add(subscription)
        # zigbee2mqtt replays retained bridge state before device state.
        ordered = sorted(
            self.cache.items(), key=lambda i: not i[0].startswith("bridge/")
        )
        return subscription, [Message(topic, payload) for topic, payload in ordered]

    def unsubscribe(self, subscription: Subscription) -> None:
        self.subscriptions.discard(subscription)

    def next_message(self, topic: str) -> asyncio.Future[object]:
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._next.setdefault(topic, []).append(future)
        return future

    def discard_next(self, topic: str, future: asyncio.Future[object]) -> None:
        with contextlib.suppress(KeyError, ValueError):
            self._next[topic].remove(future)
            if not self._next[topic]:
                del self._next[topic]

    async def send(
        self, topic: str, payload: object, *, expects_response: str | None = None
    ) -> asyncio.Future[object] | None:
        """Publish; for a bridge request, also return a future for its response."""
        connection = self.connection
        if connection is None:
            raise MqttUnavailableError
        waiter = None
        key = None
        if expects_response is not None:
            transaction = (
                payload.get("transaction") if isinstance(payload, dict) else None
            )
            key = (f"bridge/response/{expects_response}", transaction)
            waiter = asyncio.get_running_loop().create_future()
            self._responses.setdefault(key, []).append(waiter)
        try:
            await connection.publish(
                f"{self.base_topic}/{topic}", dump_payload(payload)
            )
        except aiomqtt.MqttError as e:
            if key is not None and waiter is not None:
                self._responses[key].remove(waiter)
            raise MqttUnavailableError from e
        return waiter

    def forget_response(
        self, action: str, transaction: str | None, waiter: asyncio.Future[object]
    ) -> None:
        key = (f"bridge/response/{action}", transaction)
        with contextlib.suppress(KeyError, ValueError):
            self._responses[key].remove(waiter)
            if not self._responses[key]:
                del self._responses[key]


def new_transaction() -> str:
    return uuid.uuid4().hex
