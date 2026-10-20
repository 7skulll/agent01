"""Optional shared bearer-token gate for the HTTP API.

This is a single-workspace deployment guard, not a user/tenant authorization model.
It is disabled when ``RAG_API_TOKEN`` is empty, so local development and existing
installations keep working until an operator explicitly enables it.
"""

import hmac
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = frozenset({"/api/v1/health"})
PROTECTED_PREFIX = "/api/v1/"


def _bearer(scope: Scope) -> bytes | None:
    for key, value in scope["headers"]:
        if key.lower() != b"authorization":
            continue
        scheme, separator, credentials = value.partition(b" ")
        if separator and scheme.lower() == b"bearer":
            return credentials
    return None


class BearerTokenMiddleware:
    """Reject API requests without the configured token; CORS preflight stays open."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.token
            or scope["method"] == "OPTIONS"
            or scope["path"] in PUBLIC_PATHS
            or not scope["path"].startswith(PROTECTED_PREFIX)
        ):
            return await self.app(scope, receive, send)

        supplied = _bearer(scope)
        if supplied is not None and hmac.compare_digest(supplied, self.token):
            return await self.app(scope, receive, send)

        request_id = (scope.get("state") or {}).get("request_id") or str(uuid4())
        response = JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "code": "unauthorized",
                    "message": "需要有效的访问令牌。",
                    "retryable": False,
                    "request_id": request_id,
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
