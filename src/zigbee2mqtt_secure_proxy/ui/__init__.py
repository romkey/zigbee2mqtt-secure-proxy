"""The admin web interface, under /_proxy/."""

import hmac
import secrets
from datetime import UTC, datetime
from importlib.resources import files
from typing import Annotated
from urllib.parse import parse_qs, urlencode

from authlib.integrations.starlette_client import (  # type: ignore[import-untyped]  # Authlib ships no type hints
    OAuthError,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zigbee2mqtt_secure_proxy.actions import Proxy, Status
from zigbee2mqtt_secure_proxy.auth.apikeys import generate_token, hash_token
from zigbee2mqtt_secure_proxy.auth.oidc import OidcClient
from zigbee2mqtt_secure_proxy.models import (
    Action,
    AdminEvent,
    AdminUser,
    ApiKey,
    Batch,
)
from zigbee2mqtt_secure_proxy.permissions import Tier

router = APIRouter(prefix="/_proxy", include_in_schema=False)
templates = Jinja2Templates(directory=str(files(__package__) / "templates"))

_PAGE_SIZE = 50
_NO_STORE = {"Cache-Control": "no-store"}


class LoginRequiredError(Exception):
    """Handled in app.py by redirecting to the login page."""


def _proxy(request: Request) -> Proxy:
    proxy: Proxy = request.app.state.proxy
    return proxy


def require_admin(request: Request) -> int:
    admin_id = request.session.get("admin_id")
    if admin_id is None:
        raise LoginRequiredError
    return int(admin_id)


Admin = Annotated[int, Depends(require_admin)]


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if token is None:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


async def _form(request: Request) -> dict[str, list[str]]:
    """The submitted form, after checking its CSRF token."""
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    sent = form.get("csrf", [""])[0]
    if not hmac.compare_digest(sent, _csrf_token(request)):
        raise HTTPException(403, "invalid form token; reload the page and try again")
    return form


def _render(
    request: Request,
    template: str,
    context: dict[str, object] | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        template,
        {"csrf": _csrf_token(request), **(context or {})},
        status_code=status_code,
        headers=_NO_STORE,
    )


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"/_proxy{path}", status_code=303)


def _event(admin_id: int, kind: str, **detail: object) -> AdminEvent:
    return AdminEvent(at=datetime.now(UTC), admin_id=admin_id, kind=kind, detail=detail)


@router.get("/login")
async def login(request: Request) -> Response:
    oidc: OidcClient = request.app.state.oidc
    callback = f"{request.app.state.settings.public_url}/_proxy/auth/callback"
    return await oidc.authorize_redirect(request, callback)


@router.get("/auth/callback")
async def auth_callback(request: Request) -> Response:
    oidc: OidcClient = request.app.state.oidc
    try:
        token = await oidc.authorize_access_token(request)
    except OAuthError:
        return _render(request, "error.html", {"message": "Login failed."}, 400)
    claims = token.get("userinfo") or {}
    subject = claims.get("sub")
    required_group = request.app.state.settings.oidc_required_group
    groups = claims.get("groups")
    if not subject or (
        required_group and not (isinstance(groups, list) and required_group in groups)
    ):
        return _render(
            request, "error.html", {"message": "This account may not log in."}, 403
        )
    now = datetime.now(UTC)
    email = claims.get("email")
    name = claims.get("name") or claims.get("preferred_username")
    async with _proxy(request).sessions.begin() as session:
        admin = await session.scalar(
            select(AdminUser).where(AdminUser.oidc_subject == str(subject))
        )
        if admin is None:
            admin = AdminUser(oidc_subject=str(subject), last_login_at=now)
            session.add(admin)
        admin.email = str(email) if email else None
        admin.display_name = str(name) if name else None
        admin.last_login_at = now
        await session.flush()
        session.add(_event(admin.id, "login"))
        admin_id = admin.id
    # A new session on login, so anything set before it can't carry over.
    request.session.clear()
    request.session["admin_id"] = admin_id
    return _redirect("/review")


@router.post("/logout")
async def logout(request: Request, admin: Admin) -> Response:
    await _form(request)
    request.session.clear()
    return RedirectResponse("/_proxy/login", status_code=303)


@router.get("")
@router.get("/")
async def home(admin: Admin) -> Response:
    return _redirect("/review")


async def _key_rows(session: AsyncSession) -> list[ApiKey]:
    return list(
        (await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))).all()
    )


@router.get("/keys")
async def keys(request: Request, admin: Admin) -> Response:
    async with _proxy(request).sessions() as session:
        rows = await _key_rows(session)
    return _render(request, "keys.html", {"keys": rows, "tiers": list(Tier)})


def _tier(form: dict[str, list[str]]) -> Tier:
    try:
        return Tier(form.get("tier", [""])[0])
    except ValueError:
        raise HTTPException(400, "unknown tier") from None


@router.post("/keys")
async def create_key(request: Request, admin: Admin) -> Response:
    form = await _form(request)
    name = form.get("name", [""])[0].strip()
    if not name:
        raise HTTPException(400, "a key needs a name")
    tier = _tier(form)
    for_review = "for_review" in form
    token, prefix = generate_token()
    async with _proxy(request).sessions.begin() as session:
        key = ApiKey(
            name=name,
            prefix=prefix,
            key_hash=hash_token(token),
            tier=tier,
            for_review=for_review,
            created_by=admin,
            created_at=datetime.now(UTC),
        )
        session.add(key)
        await session.flush()
        session.add(
            _event(
                admin,
                "key_created",
                key_id=key.id,
                name=name,
                tier=str(tier),
                for_review=for_review,
            )
        )
    # Rendered directly, never redirected: this is the only time it is shown.
    return _render(request, "key_created.html", {"name": name, "token": token})


async def _live_key(session: AsyncSession, key_id: int) -> ApiKey:
    key = await session.scalar(
        select(ApiKey).where(ApiKey.id == key_id).with_for_update()
    )
    if key is None or key.revoked_at is not None:
        raise HTTPException(404, "no such key")
    return key


@router.post("/keys/{key_id}")
async def update_key(request: Request, admin: Admin, key_id: int) -> Response:
    form = await _form(request)
    tier = _tier(form)
    for_review = "for_review" in form
    proxy = _proxy(request)
    async with proxy.sessions.begin() as session:
        key = await _live_key(session, key_id)
        key.tier = tier
        key.for_review = for_review
        session.add(
            _event(
                admin,
                "key_updated",
                key_id=key_id,
                tier=str(tier),
                for_review=for_review,
            )
        )
    proxy.drop_connections(key_id)
    return _redirect("/keys")


@router.post("/keys/{key_id}/revoke")
async def revoke_key(request: Request, admin: Admin, key_id: int) -> Response:
    await _form(request)
    proxy = _proxy(request)
    async with proxy.sessions.begin() as session:
        key = await _live_key(session, key_id)
        key.revoked_at = datetime.now(UTC)
        session.add(_event(admin, "key_revoked", key_id=key_id))
    proxy.drop_connections(key_id)
    return _redirect("/keys")


@router.get("/review")
async def review(request: Request, admin: Admin) -> Response:
    async with _proxy(request).sessions() as session:
        rows = (
            await session.execute(
                select(Action, ApiKey.name)
                .join(ApiKey, ApiKey.id == Action.api_key_id)
                .where(Action.status == Status.PENDING)
                .order_by(Action.requested_at, Action.id)
            )
        ).all()
    batches: dict[int, dict[str, object]] = {}
    for action, key_name in rows:
        batch = batches.setdefault(
            action.batch_id, {"key_name": key_name, "actions": []}
        )
        actions = batch["actions"]
        assert isinstance(actions, list)  # noqa: S101 - narrows the type for mypy
        actions.append(action)
    return _render(
        request,
        "review.html",
        {
            "batches": list(batches.values()),
            "all_ids": [action.id for action, _ in rows],
        },
    )


@router.post("/review")
async def decide(request: Request, admin: Admin) -> Response:
    form = await _form(request)
    # Only the ids the admin was shown: a request arriving later isn't swept up.
    ids = [int(value) for value in form.get("action_id", []) if value.isdigit()]
    verdict = form.get("verdict", [""])[0]
    if verdict not in {"approve", "reject"}:
        raise HTTPException(400, "unknown verdict")
    await _proxy(request).decide(ids, admin, approve=verdict == "approve")
    return _redirect("/review")


def _parse_time(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get("/log")
async def log(request: Request, admin: Admin) -> Response:
    query = request.query_params
    page = max(
        int(query.get("page", "1")) if query.get("page", "1").isdigit() else 1, 1
    )
    conditions = []
    key_id = query.get("key", "")
    if key_id.isdigit():
        conditions.append(Batch.api_key_id == int(key_id))
    status = query.get("status", "")
    if status in set(Status):
        conditions.append(
            exists().where(Action.batch_id == Batch.id, Action.status == status)
        )
    device = query.get("device", "").strip()
    if device:
        conditions.append(
            exists().where(Action.batch_id == Batch.id, Action.device == device)
        )
    start = _parse_time(query.get("from", ""))
    if start:
        conditions.append(Batch.last_action_at >= start)
    end = _parse_time(query.get("to", ""))
    if end:
        conditions.append(Batch.started_at <= end)

    async with _proxy(request).sessions() as session:
        total = await session.scalar(
            select(func.count()).select_from(Batch).where(*conditions)
        )
        rows = (
            await session.execute(
                select(Batch, ApiKey.name)
                .join(ApiKey, ApiKey.id == Batch.api_key_id)
                .where(*conditions)
                .order_by(Batch.last_action_at.desc(), Batch.id.desc())
                .limit(_PAGE_SIZE)
                .offset((page - 1) * _PAGE_SIZE)
            )
        ).all()
        actions = (
            await session.scalars(
                select(Action)
                .where(Action.batch_id.in_([batch.id for batch, _ in rows]))
                .order_by(Action.requested_at, Action.id)
            )
        ).all()
        all_keys = await _key_rows(session)
    by_batch: dict[int, list[Action]] = {}
    for action in actions:
        by_batch.setdefault(action.batch_id, []).append(action)
    filters = {
        name: query[name]
        for name in ("key", "status", "device", "from", "to")
        if query.get(name)
    }
    return _render(
        request,
        "log.html",
        {
            "batches": [
                {
                    "batch": batch,
                    "key_name": name,
                    "actions": by_batch.get(batch.id, []),
                }
                for batch, name in rows
            ],
            "keys": all_keys,
            "statuses": list(Status),
            "filters": filters,
            "page": page,
            "has_next": (total or 0) > page * _PAGE_SIZE,
            "query_string": urlencode(filters),
        },
    )


@router.get("/events")
async def events(request: Request, admin: Admin) -> Response:
    async with _proxy(request).sessions() as session:
        rows = (
            await session.execute(
                select(AdminEvent, AdminUser)
                .join(AdminUser, AdminUser.id == AdminEvent.admin_id)
                .order_by(AdminEvent.at.desc())
                .limit(200)
            )
        ).all()
    return _render(request, "events.html", {"events": rows})
