"""Optionally serves zigbee2mqtt's own web UI through the proxy.

These are static files, which zigbee2mqtt also serves without a token; the
UI's WebSocket then authenticates against the proxy with an API key.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter()

_REQUEST_HEADERS = ("accept", "accept-language", "if-none-match", "if-modified-since")
_RESPONSE_HEADERS = ("content-type", "etag", "last-modified", "cache-control")


@router.get("/{path:path}", include_in_schema=False)
async def frontend(request: Request, path: str) -> Response:
    client: httpx.AsyncClient = request.app.state.frontend_client
    # Cookies and credentials are deliberately not forwarded. The base URL is
    # fixed and the path is appended to it, so this can't reach another host.
    headers = {
        name: request.headers[name]
        for name in _REQUEST_HEADERS
        if name in request.headers
    }
    try:
        upstream = await client.get(
            f"{request.app.state.frontend_url}/{path}",
            headers=headers,
            params=request.query_params,
        )
    except httpx.HTTPError as e:
        logger.warning("zigbee2mqtt frontend unreachable: %s", type(e).__name__)
        return Response("zigbee2mqtt frontend unreachable", 502)
    forwarded = {
        name: upstream.headers[name]
        for name in _RESPONSE_HEADERS
        if name in upstream.headers
    }
    return Response(upstream.content, upstream.status_code, forwarded)
