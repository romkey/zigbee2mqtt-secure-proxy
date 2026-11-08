"""Admin login through OpenID Connect (Authentik)."""

from typing import Protocol

from authlib.integrations.starlette_client import (  # type: ignore[import-untyped]  # Authlib ships no type hints
    OAuth,
)
from starlette.requests import Request
from starlette.responses import Response

from zigbee2mqtt_secure_proxy.config import Settings


class OidcClient(Protocol):
    async def authorize_redirect(
        self, request: Request, redirect_uri: str
    ) -> Response: ...

    # A dict with a "userinfo" entry, whose claims depend on the provider.
    async def authorize_access_token(
        self, request: Request
    ) -> dict[str, dict[str, object]]: ...


def make_oidc_client(settings: Settings) -> OidcClient:
    """An Authlib client; discovery happens on first use, not at startup.

    https://docs.authlib.org/en/latest/client/starlette.html
    """
    oauth = OAuth()
    client: OidcClient = oauth.register(
        name="authentik",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=(
            f"{settings.oidc_issuer}/.well-known/openid-configuration"
        ),
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
    )
    return client
