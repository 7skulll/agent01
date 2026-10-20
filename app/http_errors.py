"""统一 HTTP 错误协议；流已开始后的故障仍由 SSE 处理。"""

from uuid import UUID, uuid4

import psycopg
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.errors import ChatError


class ValidationIssue(BaseModel):
    loc: list[str | int]
    type: str
    msg: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    request_id: str
    thread_id: str | None = None
    run_id: str | None = None
    errors: list[ValidationIssue] | None = None


class ErrorResponse(BaseModel):
    detail: ErrorDetail


def respond(request, status, detail, headers=None):
    payload = {
        **detail,
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
    }
    for field, names in (
        ("thread_id", ("conversation_id", "thread_id")),
        ("run_id", ("run_id",)),
    ):
        value = next(
            (request.path_params[name] for name in names if name in request.path_params), None
        )
        if value is not None and not payload.get(field):
            try:
                payload[field] = str(UUID(str(value)))
            except ValueError:
                # 无效路径参数不作为资源 ID 回显，保留结构化校验位置即可。
                pass
    body = ErrorResponse(detail=ErrorDetail(**payload))
    return JSONResponse(
        status_code=status,
        content=body.model_dump(exclude_none=True),
        headers=headers,
    )


def register_error_handlers(app):
    @app.exception_handler(ChatError)
    async def business_error(request: Request, exc: ChatError):
        return respond(request, exc.status, exc.detail())

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error):
        # 不能向客户端返回 SQL、连接字符串或数据库异常正文。
        return respond(
            request,
            503,
            {
                "code": "storage_unavailable",
                "message": "数据存储暂时不可用，请稍后重试。",
                "retryable": True,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return respond(
            request,
            422,
            {
                "code": "validation_error",
                "message": "请求参数无效。",
                "retryable": False,
                "errors": [
                    {key: item[key] for key in ("loc", "type", "msg")} for item in exc.errors()
                ],
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # 兼容已有路由的 detail 对象，同时规范化框架和旧游标接口的字符串错误。
        detail: dict[str, object] = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {
                "code": {404: "not_found", 405: "method_not_allowed"}.get(
                    exc.status_code, "http_error"
                ),
                "message": str(exc.detail),
            }
        )
        detail = {"retryable": exc.status_code in (429, 502, 503, 504), **detail}
        return respond(request, exc.status_code, detail, exc.headers)
