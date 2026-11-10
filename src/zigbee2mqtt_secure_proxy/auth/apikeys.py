"""API key format, hashing and lookup."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import HTTPConnection

from zigbee2mqtt_secure_proxy.models import ApiKey
from zigbee2mqtt_secure_proxy.permissions import Tier

_PREFIX_LENGTH = 8
_TOKEN_START = "z2m_"  # noqa: S105 - the fixed start of every key, not a secret


@dataclass(frozen=True)
class AuthenticatedKey:
    id: int
    tier: Tier


def hash_token(token: str) -> str:
    # Tokens are long and random, so a plain digest is enough; a slow password
    # hash would only make every WebSocket message and request slower.
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str]:
    """A new token and its prefix."""
    prefix = secrets.token_hex(_PREFIX_LENGTH // 2)
    return f"{_TOKEN_START}{prefix}_{secrets.token_urlsafe(32)}", prefix


def token_prefix(token: str) -> str | None:
    separator = len(_TOKEN_START) + _PREFIX_LENGTH
    if not token.startswith(_TOKEN_START) or token[separator : separator + 1] != "_":
        return None
    return token[len(_TOKEN_START) : separator]


def presented_token(connection: HTTPConnection) -> str | None:
    """The key from ?token= (as zigbee2mqtt clients send it) or a Bearer header."""
    token = connection.query_params.get("token")
    if token:
        return token
    scheme, _, value = connection.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value else None


async def authenticate(
    sessions: async_sessionmaker[AsyncSession], token: str | None
) -> AuthenticatedKey | None:
    prefix = token_prefix(token) if token else None
    if token is None or prefix is None:
        return None
    async with sessions() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if (
        key is None
        or key.revoked_at is not None
        or not hmac.compare_digest(key.key_hash, hash_token(token))
    ):
        return None
    return AuthenticatedKey(key.id, Tier(key.tier))
