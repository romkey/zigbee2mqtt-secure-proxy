"""The ASGI application."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from zigbee2mqtt_secure_proxy import __version__, api, db, frontend_proxy, ui, ws
from zigbee2mqtt_secure_proxy.actions import Proxy
from zigbee2mqtt_secure_proxy.auth.oidc import OidcClient, make_oidc_client
from zigbee2mqtt_secure_proxy.config import Settings
from zigbee2mqtt_secure_proxy.mqtt import Connector, MqttBridge, aiomqtt_connector

logger = logging.getLogger(__name__)

_SESSION_SECONDS = 12 * 3600
_FRONTEND_TIMEOUT_SECONDS = 30
_UI_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' "
        "https: http:; frame-ancestors 'none'; base-uri 'none'"
    ),
}


def create_app(
    settings: Settings,
    *,
    connector: Connector | None = None,
    oidc: OidcClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = db.make_async_engine(settings.database_url)
        bridge = MqttBridge(
            connector or aiomqtt_connector(settings), settings.mqtt_base_topic
        )
        proxy = Proxy(db.make_sessions(engine), bridge, settings)
        app.state.proxy = proxy
        app.state.frontend_client = httpx.AsyncClient(
            timeout=_FRONTEND_TIMEOUT_SECONDS, transport=transport
        )
        background = [
            asyncio.create_task(bridge.run()),
            asyncio.create_task(proxy.maintain_forever()),
        ]
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await proxy.drain()
            await app.state.frontend_client.aclose()
            await engine.dispose()

    # The docs routes are off: the proxy must look like zigbee2mqtt to clients.
    app = FastAPI(
        title="zigbee2mqtt-secure-proxy",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.oidc = oidc or make_oidc_client(settings)
    app.state.frontend_url = settings.frontend_url
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="z2m_proxy_session",
        max_age=_SESSION_SECONDS,
        path="/_proxy",
        same_site="lax",
        https_only=settings.public_url.startswith("https://"),
    )

    @app.middleware("http")
    async def ui_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/_proxy"):
            response.headers.update(_UI_HEADERS)
        return response

    @app.exception_handler(ui.LoginRequiredError)
    async def login_required(
        request: Request, exc: ui.LoginRequiredError
    ) -> RedirectResponse:
        return RedirectResponse("/_proxy/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Report that the process is up. No auth, no dependencies (rules §6)."""
        return {"status": "ok", "version": __version__}

    app.include_router(ws.router)
    app.include_router(api.router)
    app.include_router(ui.router)
    if settings.frontend_url:
        app.include_router(frontend_proxy.router)
    return app
