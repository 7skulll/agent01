"""用户会话鉴权中间件。

- 公开路径（健康检查、登录状态、登录、首次初始化）直接放行；
- 其余 ``/api/v1/*`` 必须携带 ``Authorization: Bearer <token>``；
- 服务令牌 ``RAG_API_TOKEN`` 作为脚本/运维使用的管理员令牌；
- 系统还没有任何账号且未配置服务令牌时进入初始化模式，本地匿名用户按管理员处理，
  以便创建首个管理员；创建账号后自动收紧。
"""

import hmac
from uuid import uuid4

import psycopg
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.users import LOCAL_USER, SERVICE_USER

PUBLIC_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/auth/status",
        "/api/v1/auth/login",
        "/api/v1/auth/setup",
        "/api/v1/auth/register",
    }
)
PROTECTED_PREFIX = "/api/v1/"


def bearer_token(scope: Scope) -> str | None:
    for key, value in scope["headers"]:
        if key.lower() != b"authorization":
            continue
        scheme, separator, credentials = value.partition(b" ")
        if separator and scheme.lower() == b"bearer":
            try:
                return credentials.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


class UserAuthMiddleware:
    """按 ``/api/v1`` 前缀校验用户会话，CORS 预检保持开放。"""

    def __init__(self, app: ASGIApp, auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] == "OPTIONS"
            or scope["path"] in PUBLIC_PATHS
            or not scope["path"].startswith(PROTECTED_PREFIX)
        ):
            return await self.app(scope, receive, send)

        state = scope.setdefault("state", {})
        token = bearer_token(scope)
        if token and self.auth.service_token and hmac.compare_digest(
            token.encode("utf-8"), self.auth.service_token.encode("utf-8")
        ):
            state["user"] = SERVICE_USER.model_copy()
            return await self.app(scope, receive, send)

        try:
            resolved = await self.auth.resolve(token) if token else None
        except psycopg.Error:
            return await self._reject(
                scope,
                receive,
                send,
                status=503,
                code="storage_unavailable",
                message="登录状态暂时无法校验，请稍后重试。",
                retryable=True,
            )
        if resolved is not None:
            user, token_hash = resolved
            state["user"] = user
            state["session_hash"] = token_hash
            return await self.app(scope, receive, send)

        try:
            required = await self.auth.required()
        except psycopg.Error:
            return await self._reject(
                scope,
                receive,
                send,
                status=503,
                code="storage_unavailable",
                message="登录状态暂时无法校验，请稍后重试。",
                retryable=True,
            )
        if not required:
            state["user"] = LOCAL_USER.model_copy()
            return await self.app(scope, receive, send)

        message = "登录已失效，请重新登录。" if token else "请先登录。"
        return await self._reject(
            scope, receive, send, status=401, code="unauthorized", message=message
        )

    async def _reject(
        self, scope, receive, send, *, status, code, message, retryable=False
    ) -> None:
        state = scope.get("state") or {}
        request_id = state.get("request_id") or str(uuid4())
        response = JSONResponse(
            status_code=status,
            content={
                "detail": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "request_id": request_id,
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
