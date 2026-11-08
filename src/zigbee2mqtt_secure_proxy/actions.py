"""Records every action, assigns batches, and runs the review workflow."""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zigbee2mqtt_secure_proxy.config import Settings
from zigbee2mqtt_secure_proxy.models import (
    Action,
    AdminEvent,
    ApiKey,
    Batch,
    ClientSession,
)
from zigbee2mqtt_secure_proxy.mqtt import (
    MqttBridge,
    MqttUnavailableError,
    Subscription,
)
from zigbee2mqtt_secure_proxy.permissions import Operation, Tier, classify

logger = logging.getLogger(__name__)

_SECRET_FIELDS = ("password", "secret", "token", "network_key")
_FULLY_REDACTED_TOPICS = {"bridge/request/install_code/add"}
_RESPONSES_WITHOUT_DATA = {"backup"}
_MAX_STORED_RESULT = 8192
_MAX_SUMMARY_VALUES = 50
_INTERRUPTED_AFTER = timedelta(hours=1)
_REDACTED = "***"


class Status(StrEnum):
    # Authorized and being carried out, by a live request or an admin's approval.
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    DENIED = "denied"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"


class KeyRevokedError(RuntimeError):
    """The key was revoked after the client authenticated."""


@dataclass(frozen=True)
class Finished:
    status: Status
    response: object = None
    error: str | None = None


@dataclass(frozen=True)
class Submission:
    action_id: int
    status: Status
    finished: asyncio.Task[Finished] | None = None


def redact(topic: str, payload: object) -> object:
    """A copy of the payload that is safe to keep in the log."""
    if topic in _FULLY_REDACTED_TOPICS:
        return _REDACTED
    if isinstance(payload, dict):
        return {
            key: (
                _REDACTED
                if any(field in str(key).lower() for field in _SECRET_FIELDS)
                else redact(topic, value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(topic, value) for value in payload]
    return payload


def _storable_result(action: str | None, response: object) -> object:
    if not isinstance(response, dict):
        return None
    summary = {key: response[key] for key in response if key != "data"}
    if action in _RESPONSES_WITHOUT_DATA:
        return summary
    redacted = redact("", response)
    if len(str(redacted)) > _MAX_STORED_RESULT:
        return {**summary, "truncated": True}
    return redacted


def _merged(values: list[str], value: str | None) -> list[str]:
    if value is None or value in values or len(values) >= _MAX_SUMMARY_VALUES:
        return values
    return [*values, value]


class Proxy:
    """Everything a client request or an admin decision goes through."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        bridge: MqttBridge,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.bridge = bridge
        self.settings = settings
        self.tasks: set[asyncio.Task[Finished]] = set()
        self.connections: dict[int, set[Subscription]] = {}

    def register(self, key_id: int, subscription: Subscription) -> None:
        self.connections.setdefault(key_id, set()).add(subscription)

    def unregister(self, key_id: int, subscription: Subscription) -> None:
        connections = self.connections.get(key_id, set())
        connections.discard(subscription)
        if not connections:
            self.connections.pop(key_id, None)

    def drop_connections(self, key_id: int) -> None:
        """Disconnect a key's clients, so a change of its rights takes effect."""
        for subscription in self.connections.get(key_id, set()):
            subscription.close()

    async def drain(self) -> None:
        """Wait for actions in progress to finish."""
        while self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def record(
        self,
        *,
        key_id: int,
        transport: str,
        client_addr: str | None,
        topic: str,
        payload: object,
        operation: Operation | None,
        session_id: int | None = None,
        now: datetime | None = None,
    ) -> Action:
        now = now or datetime.now(UTC)
        async with self.sessions.begin() as session:
            # The row lock makes batch assignment safe across concurrent requests.
            key = await session.scalar(
                select(ApiKey).where(ApiKey.id == key_id).with_for_update()
            )
            if key is None or key.revoked_at is not None:
                raise KeyRevokedError
            status = self._initial_status(key, operation)
            batch = await self._batch_for(session, key_id, now)
            device = operation.device if operation else None
            verb = operation.verb if operation else "unrecognised"
            batch.last_action_at = now
            batch.action_count += 1
            batch.summary = {
                "devices": _merged(batch.summary["devices"], device),
                "topics": _merged(batch.summary["topics"], topic),
                "verbs": _merged(batch.summary["verbs"], verb),
            }
            key.last_used_at = now
            action = Action(
                api_key_id=key_id,
                batch_id=batch.id,
                session_id=session_id,
                transport=transport,
                client_addr=client_addr,
                topic=topic,
                device=device,
                verb=verb,
                # A held action needs its real payload until it is decided.
                payload=payload if status is Status.PENDING else redact(topic, payload),
                status=status,
                requested_at=now,
                executed_at=now if status is Status.EXECUTED else None,
            )
            session.add(action)
            await session.flush()
            return action

    @staticmethod
    def _initial_status(key: ApiKey, operation: Operation | None) -> Status:
        if operation is None or not Tier(key.tier).allows(operation.tier):
            return Status.DENIED
        if operation.cached:
            return Status.EXECUTED
        if key.for_review and operation.tier is not Tier.READ:
            return Status.PENDING
        return Status.APPROVED

    async def _batch_for(
        self, session: AsyncSession, key_id: int, now: datetime
    ) -> Batch:
        batch = await session.scalar(
            select(Batch)
            .where(Batch.api_key_id == key_id)
            .order_by(Batch.last_action_at.desc(), Batch.id.desc())
            .limit(1)
        )
        gap = timedelta(seconds=self.settings.action_batch_gap)
        if batch is None or now - batch.last_action_at >= gap:
            batch = Batch(
                api_key_id=key_id,
                started_at=now,
                last_action_at=now,
                action_count=0,
                summary={"devices": [], "topics": [], "verbs": []},
            )
            session.add(batch)
            await session.flush()
        return batch

    async def submit(
        self,
        *,
        key_id: int,
        transport: str,
        client_addr: str | None,
        topic: str,
        payload: object,
        session_id: int | None = None,
    ) -> Submission:
        operation = classify(topic)
        action = await self.record(
            key_id=key_id,
            transport=transport,
            client_addr=client_addr,
            topic=topic,
            payload=payload,
            operation=operation,
            session_id=session_id,
        )
        status = Status(action.status)
        if status is not Status.APPROVED or operation is None:
            return Submission(action.id, status)
        task = await self._dispatch(action.id, topic, payload, operation)
        return Submission(action.id, status, task)

    async def _dispatch(
        self, action_id: int, topic: str, payload: object, operation: Operation
    ) -> asyncio.Task[Finished]:
        waiter = None
        unavailable = False
        try:
            waiter = await self.bridge.send(
                topic, payload, expects_response=operation.bridge_action
            )
        except MqttUnavailableError:
            unavailable = True
        task = asyncio.create_task(
            self._finish(action_id, payload, operation, waiter, unavailable=unavailable)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _finish(
        self,
        action_id: int,
        payload: object,
        operation: Operation,
        waiter: asyncio.Future[object] | None,
        *,
        unavailable: bool,
    ) -> Finished:
        outcome = await self._await_outcome(payload, operation, waiter, unavailable)
        async with self.sessions.begin() as session:
            action = await session.get(Action, action_id)
            if action is not None:
                action.status = outcome.status
                action.executed_at = datetime.now(UTC)
                action.error = outcome.error
                action.result = _storable_result(
                    operation.bridge_action, outcome.response
                )
        return outcome

    async def _await_outcome(
        self,
        payload: object,
        operation: Operation,
        waiter: asyncio.Future[object] | None,
        unavailable: bool,
    ) -> Finished:
        if unavailable:
            return Finished(Status.FAILED, error="MQTT broker unavailable")
        if waiter is None:
            return Finished(Status.EXECUTED)
        timeout = self.settings.mqtt_request_timeout
        try:
            response = await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            transaction = (
                payload.get("transaction") if isinstance(payload, dict) else None
            )
            self.bridge.forget_response(
                operation.bridge_action or "", transaction, waiter
            )
            return Finished(Status.FAILED, error=f"no response within {timeout:g}s")
        if isinstance(response, dict) and response.get("status") == "error":
            return Finished(Status.FAILED, response, str(response.get("error")))
        return Finished(Status.EXECUTED, response)

    async def decide(
        self, action_ids: Iterable[int], admin_id: int, *, approve: bool
    ) -> int:
        """Approve or reject exactly these actions; returns how many were pending."""
        now = datetime.now(UTC)
        to_run: list[tuple[int, str, object, Operation]] = []
        decided = 0
        ttl = timedelta(seconds=self.settings.review_ttl)
        async with self.sessions.begin() as session:
            actions = (
                await session.scalars(
                    select(Action)
                    .where(
                        Action.id.in_(list(action_ids)),
                        Action.status == Status.PENDING,
                    )
                    .order_by(Action.requested_at, Action.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for action in actions:
                decided += 1
                full_payload = action.payload
                action.payload = redact(action.topic, full_payload)
                action.decided_at = now
                action.decided_by = admin_id
                if now - action.requested_at >= ttl:
                    action.status = Status.EXPIRED
                    continue
                if not approve:
                    action.status = Status.REJECTED
                    continue
                key = await session.get(ApiKey, action.api_key_id)
                operation = classify(action.topic)
                if (
                    key is None
                    or key.revoked_at is not None
                    or operation is None
                    or not Tier(key.tier).allows(operation.tier)
                ):
                    action.status = Status.REJECTED
                    action.error = "the key no longer permits this action"
                    continue
                action.status = Status.APPROVED
                to_run.append((action.id, action.topic, full_payload, operation))
            if decided:
                session.add(
                    AdminEvent(
                        at=now,
                        admin_id=admin_id,
                        kind="review_approved" if approve else "review_rejected",
                        detail={"action_ids": [action.id for action in actions]},
                    )
                )
        for action_id, topic, payload, operation in to_run:
            await self._dispatch(action_id, topic, payload, operation)
        return decided

    async def expire(self, now: datetime | None = None) -> int:
        """Expire stale held actions, and close out interrupted ones."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(seconds=self.settings.review_ttl)
        async with self.sessions.begin() as session:
            stale = (
                await session.scalars(
                    select(Action)
                    .where(
                        Action.status == Status.PENDING, Action.requested_at <= cutoff
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for action in stale:
                action.status = Status.EXPIRED
                action.decided_at = now
                action.payload = redact(action.topic, action.payload)
            interrupted = (
                await session.scalars(
                    select(Action)
                    .where(
                        Action.status == Status.APPROVED,
                        Action.requested_at <= now - _INTERRUPTED_AFTER,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for action in interrupted:
                action.status = Status.FAILED
                action.error = "interrupted before it finished"
        return len(stale)

    async def prune(self, now: datetime | None = None) -> None:
        """Delete log rows past the retention period. Admin events are kept."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.settings.action_retention_days)
        async with self.sessions.begin() as session:
            await session.execute(
                delete(Action).where(
                    Action.requested_at < cutoff, Action.status != Status.PENDING
                )
            )
            await session.execute(
                delete(Batch).where(
                    Batch.last_action_at < cutoff,
                    ~select(Action.id).where(Action.batch_id == Batch.id).exists(),
                )
            )
            await session.execute(
                delete(ClientSession).where(
                    ClientSession.disconnected_at < cutoff,
                    ~select(Action.id)
                    .where(Action.session_id == ClientSession.id)
                    .exists(),
                )
            )

    async def open_session(
        self, key_id: int, transport: str, client_addr: str | None
    ) -> int:
        async with self.sessions.begin() as session:
            row = ClientSession(
                api_key_id=key_id,
                transport=transport,
                client_addr=client_addr,
                connected_at=datetime.now(UTC),
            )
            session.add(row)
            await session.flush()
            return row.id

    async def close_session(self, session_id: int, messages: int, size: int) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(ClientSession, session_id)
            if row is not None:
                row.disconnected_at = datetime.now(UTC)
                row.messages_out = messages
                row.bytes_out = size

    async def maintain_forever(self, interval: float = 60) -> None:
        """Expire held actions every interval; prune the log about once a day."""
        last_prune: datetime | None = None
        while True:
            await self.expire()
            now = datetime.now(UTC)
            if last_prune is None or now - last_prune >= timedelta(days=1):
                await self.prune(now)
                last_prune = now
            await asyncio.sleep(interval)
