"""The WebSocket zigbee2mqtt's frontend speaks, at the same path.

Mirrors lib/extension/frontend.ts in zigbee2mqtt 2.14.3: the connection is
accepted and then closed with 4401 for a bad token; messages are
{"topic", "payload"} with topics relative to the base topic; and invalid
messages are ignored.
"""

import json
import logging
from collections.abc import Coroutine
from typing import Any

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from zigbee2mqtt_secure_proxy.actions import KeyRevokedError, Proxy, Status
from zigbee2mqtt_secure_proxy.auth.apikeys import (
    AuthenticatedKey,
    authenticate,
    presented_token,
)
from zigbee2mqtt_secure_proxy.mqtt import Message, Subscription
from zigbee2mqtt_secure_proxy.permissions import BRIDGE_REQUESTS, bridge_action
from zigbee2mqtt_secure_proxy.streaming import finish_stream, frame, may_receive

logger = logging.getLogger(__name__)
router = APIRouter()

_UNAUTHORIZED = 4401
_DENIED_ERROR = "This API key does not permit this action"
_HELD_ERROR = "Held for review; it runs if an admin approves it"


def error_response(topic: str, payload: object, error: str) -> Message | None:
    """zigbee2mqtt's error response to a bridge request, or None if it has none."""
    action = bridge_action(topic)
    if action is None or action not in BRIDGE_REQUESTS:
        # zigbee2mqtt answers neither /set, /get nor actions it does not know.
        return None
    body: dict[str, object] = {"data": {}, "status": "error", "error": error}
    if isinstance(payload, dict) and "transaction" in payload:
        body["transaction"] = payload["transaction"]
    return Message(f"bridge/response/{action}", body)


def parse_message(text: str) -> tuple[str, object] | None:
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or not isinstance(message.get("topic"), str):
        return None
    return message["topic"], message.get("payload")


class _Counter:
    def __init__(self) -> None:
        self.messages = 0
        self.size = 0


async def _send_loop(
    websocket: WebSocket,
    key: AuthenticatedKey,
    subscription: Subscription,
    replay: list[Message],
    sent: _Counter,
) -> None:
    async def deliver(message: Message) -> None:
        if may_receive(key, message):
            text = frame(message)
            await websocket.send_text(text)
            sent.messages += 1
            sent.size += len(text)

    for message in replay:
        await deliver(message)
    while (item := await subscription.queue.get()) is not None:
        await deliver(item)
    await websocket.close(_UNAUTHORIZED, "Unauthorized")


async def _receive_loop(
    websocket: WebSocket,
    proxy: Proxy,
    key: AuthenticatedKey,
    subscription: Subscription,
    session_id: int,
) -> None:
    address = websocket.client.host if websocket.client else None
    while True:
        received = await websocket.receive()
        if received["type"] == "websocket.disconnect":
            return
        text = received.get("text")
        parsed = parse_message(text) if text else None
        if parsed is None:
            logger.debug("ignoring an invalid WebSocket message")
            continue
        topic, payload = parsed
        submission = await proxy.submit(
            key_id=key.id,
            transport="ws",
            client_addr=address,
            topic=topic,
            payload=payload,
            session_id=session_id,
        )
        error = {Status.DENIED: _DENIED_ERROR, Status.PENDING: _HELD_ERROR}.get(
            submission.status
        )
        reply = error_response(topic, payload, error) if error else None
        if reply is not None:
            subscription.put(reply)


@router.websocket("/api")
async def websocket_api(websocket: WebSocket) -> None:
    proxy: Proxy = websocket.app.state.proxy
    await websocket.accept()
    key = await authenticate(proxy.sessions, presented_token(websocket))
    if key is None:
        logger.warning("rejected a WebSocket connection with a bad API key")
        await websocket.close(_UNAUTHORIZED, "Unauthorized")
        return

    address = websocket.client.host if websocket.client else None
    session_id = await proxy.open_session(key.id, "ws", address)
    subscription, replay = proxy.bridge.subscribe()
    proxy.register(key.id, subscription)
    sent = _Counter()

    async def stop_after(loop: Coroutine[Any, Any, None]) -> None:
        """Run one direction; when it ends, for any reason, end the other too."""
        try:
            await loop
        except KeyRevokedError:
            await websocket.close(_UNAUTHORIZED, "Unauthorized")
        except WebSocketDisconnect, RuntimeError:
            logger.debug("WebSocket closed while sending")
        finally:
            group.cancel_scope.cancel()

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(
                stop_after, _send_loop(websocket, key, subscription, replay, sent)
            )
            group.start_soon(
                stop_after,
                _receive_loop(websocket, proxy, key, subscription, session_id),
            )
    finally:
        await finish_stream(
            proxy, key, subscription, session_id, sent.messages, sent.size
        )
