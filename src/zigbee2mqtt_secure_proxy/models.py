"""Database tables. Schema changes are Alembic migrations in migrations/."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from zigbee2mqtt_secure_proxy.db import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    oidc_subject: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(255))
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(8), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(32))
    for_review: Mapped[bool] = mapped_column(default=False)
    # Reserved for per-key device allowlists; always null for now.
    device_allowlist: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("admin_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Batch(Base):
    """Actions from one key that arrived close together."""

    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_key_last_action", "api_key_id", text("last_action_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_action_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action_count: Mapped[int] = mapped_column(default=0)
    summary: Mapped[dict[str, list[str]]] = mapped_column(JSONB)


class ClientSession(Base):
    """One WebSocket or event-stream connection."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    transport: Mapped[str] = mapped_column(String(8))
    client_addr: Mapped[str | None] = mapped_column(String(64))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    messages_out: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        Index(
            "ix_actions_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_actions_key_requested", "api_key_id", text("requested_at DESC")),
        Index("ix_actions_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    transport: Mapped[str] = mapped_column(String(8))
    client_addr: Mapped[str | None] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(Text)
    verb: Mapped[str] = mapped_column(Text)
    payload: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str] = mapped_column(String(16))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(Text)


class AdminEvent(Base):
    __tablename__ = "admin_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id"))
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, object]] = mapped_column(JSONB)
