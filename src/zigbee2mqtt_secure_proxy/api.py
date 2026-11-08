"""An HTTP mirror of zigbee2mqtt's MQTT topics, for scripts."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from zigbee2mqtt_secure_proxy.actions import KeyRevokedError, Proxy, Status, Submission
from zigbee2mqtt_secure_proxy.auth.apikeys import (
    AuthenticatedKey,
    authenticate,
    presented_token,
)
from zigbee2mqtt_secure_proxy.models import Action
from zigbee2mqtt_secure_proxy.mqtt import Message, Subscription, new_transaction
from zigbee2mqtt_secure_proxy.permissions import cached_read
from zigbee2mqtt_secure_proxy.streaming import finish_stream, frame, may_receive

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_KEEPALIVE_SECONDS = 15


def _proxy(request: Request) -> Proxy:
    proxy: Proxy = request.app.state.proxy
    return proxy


async def require_key(request: Request) -> AuthenticatedKey:
    key = await authenticate(_proxy(request).sessions, presented_token(request))
    if key is None:
        logger.warning("rejected an HTTP request with a bad API key")
        raise HTTPException(401, "a valid API key is required")
    return key


Key = Annotated[AuthenticatedKey, Depends(require_key)]


def _address(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _body(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return {}
    text = raw.decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _read_cached(
    request: Request, key: AuthenticatedKey, topic: str, device: str | None
) -> JSONResponse:
    proxy = _proxy(request)
    try:
        await proxy.record(
            key_id=key.id,
            transport="http",
            client_addr=_address(request),
            topic=topic,
            payload=None,
            operation=cached_read(topic, device),
        )
    except KeyRevokedError:
        raise HTTPException(401, "a valid API key is required") from None
    if topic not in proxy.bridge.cache:
        raise HTTPException(404, f"nothing known yet for {topic}")
    return JSONResponse(proxy.bridge.cache[topic])


@router.get("/bridge/{name:path}")
async def bridge_topic(request: Request, key: Key, name: str) -> Response:
    return await _read_cached(request, key, f"bridge/{name}", None)


@router.get("/devices/{name:path}")
async def device_state(request: Request, key: Key, name: str) -> Response:
    return await _read_cached(request, key, name, name)


async def _submit(
    request: Request, key: AuthenticatedKey, topic: str, payload: object
) -> Submission:
    try:
        return await _proxy(request).submit(
            key_id=key.id,
            transport="http",
            client_addr=_address(request),
            topic=topic,
            payload=payload,
        )
    except KeyRevokedError:
        raise HTTPException(401, "a valid API key is required") from None


def _refusal(submission: Submission) -> Response | None:
    if submission.status is Status.DENIED:
        return JSONResponse({"error": "this API key does not permit this"}, 403)
    if submission.status is Status.PENDING:
        return JSONResponse(
            {
                "status": "pending",
                "action_id": submission.action_id,
                "status_url": f"/api/actions/{submission.action_id}",
            },
            202,
        )
    return None


async def _device_command(
    request: Request, key: AuthenticatedKey, name: str, verb: str
) -> Response:
    proxy = _proxy(request)
    # Registered first, so a reply that arrives quickly isn't missed.
    state = proxy.bridge.next_message(name)
    try:
        submission = await _submit(request, key, f"{name}/{verb}", await _body(request))
        refusal = _refusal(submission)
        if refusal is not None:
            return refusal
        finished = await submission.finished if submission.finished else None
        if finished is not None and finished.status is Status.FAILED:
            return JSONResponse({"error": finished.error}, 503)
        try:
            timeout = proxy.settings.mqtt_request_timeout
            return JSONResponse(await asyncio.wait_for(state, timeout))
        except TimeoutError:
            return JSONResponse({"status": "published"}, 202)
    finally:
        proxy.bridge.discard_next(name, state)


@router.post("/devices/{name:path}/set")
async def device_set(request: Request, key: Key, name: str) -> Response:
    return await _device_command(request, key, name, "set")


@router.post("/devices/{name:path}/get")
async def device_get(request: Request, key: Key, name: str) -> Response:
    return await _device_command(request, key, name, "get")


@router.post("/bridge/request/{action:path}")
async def bridge_request(request: Request, key: Key, action: str) -> Response:
    payload = await _body(request)
    transaction = new_transaction()
    injected = False
    if payload == "":
        payload = {}
    if isinstance(payload, dict) and "transaction" not in payload:
        payload = {**payload, "transaction": transaction}
        injected = True
    submission = await _submit(request, key, f"bridge/request/{action}", payload)
    refusal = _refusal(submission)
    if refusal is not None:
        return refusal
    finished = await submission.finished if submission.finished else None
    if finished is None or finished.response is None:
        error = finished.error if finished else "not executed"
        status = 504 if error and error.startswith("no response") else 503
        return JSONResponse({"error": error}, status)
    response = finished.response
    if injected and isinstance(response, dict):
        response = {k: v for k, v in response.items() if k != "transaction"}
    return JSONResponse(response)


@router.get("/actions/{action_id}")
async def action_status(request: Request, key: Key, action_id: int) -> Response:
    async with _proxy(request).sessions() as session:
        action = await session.scalar(
            select(Action).where(Action.id == action_id, Action.api_key_id == key.id)
        )
    if action is None:
        raise HTTPException(404, "no such action")
    return JSONResponse(
        {
            "id": action.id,
            "status": action.status,
            "topic": action.topic,
            "result": action.result,
            "error": action.error,
        }
    )


async def event_stream(
    proxy: Proxy,
    key: AuthenticatedKey,
    subscription: Subscription,
    replay: list[Message],
    session_id: int,
    keepalive: float = _KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    messages = size = 0
    try:
        for message in replay:
            if may_receive(key, message):
                text = f"data: {frame(message)}\n\n"
                messages += 1
                size += len(text)
                yield text
        while True:
            try:
                item = await asyncio.wait_for(subscription.queue.get(), keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                return
            if may_receive(key, item):
                text = f"data: {frame(item)}\n\n"
                messages += 1
                size += len(text)
                yield text
    finally:
        await finish_stream(proxy, key, subscription, session_id, messages, size)


@router.get("/events")
async def events(request: Request, key: Key) -> StreamingResponse:
    proxy = _proxy(request)
    session_id = await proxy.open_session(key.id, "sse", _address(request))
    subscription, replay = proxy.bridge.subscribe()
    proxy.register(key.id, subscription)
    return StreamingResponse(
        event_stream(proxy, key, subscription, replay, session_id),
        media_type="text/event-stream",
    )
