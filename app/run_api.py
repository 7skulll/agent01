import asyncio
import sqlite3
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from app.errors import ChatError
from app.run_repository import TERMINAL_STATUSES
from app.schemas import RunRequest, RunResponse
from app.streaming import sse_frame

router = APIRouter(prefix="/api/v1", tags=["runs"])


async def call(operation, *, conversation_id=None, run_id=None):
    request_id = str(uuid4())
    try:
        return await operation(request_id)
    except (ChatError, sqlite3.Error) as exc:
        error = (
            exc
            if isinstance(exc, ChatError)
            else ChatError("storage_unavailable", "会话存储暂时不可用。", 503, True)
        )
        error.request_id = request_id
        error.thread_id = conversation_id
        detail = error.detail()
        if run_id:
            detail["run_id"] = run_id
        raise HTTPException(status_code=error.status, detail=detail) from None


@router.post("/conversations/{conversation_id}/runs", response_model=RunResponse, status_code=202)
async def create_run(conversation_id: UUID, body: RunRequest, request: Request):
    return await call(
        lambda request_id: request.app.state.run_service.create(
            str(conversation_id), body, request_id
        ),
        conversation_id=str(conversation_id),
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: UUID, request: Request):
    service = request.app.state.run_service
    repository = service.repository
    row = await call(lambda _: service.inspect(str(run_id)), run_id=str(run_id))
    return repository.response(row)


@router.post("/runs/{run_id}/pause", response_model=RunResponse)
async def pause_run(run_id: UUID, request: Request):
    repository = request.app.state.run_service.repository
    row = await call(lambda _: repository.pause(str(run_id)), run_id=str(run_id))
    return repository.response(row)


@router.post("/runs/{run_id}/resume", response_model=RunResponse, status_code=202)
async def resume_run(run_id: UUID, request: Request):
    return await call(
        lambda _: request.app.state.run_service.resume(str(run_id)), run_id=str(run_id)
    )


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: UUID, request: Request):
    return await call(
        lambda _: request.app.state.run_service.cancel(str(run_id)), run_id=str(run_id)
    )


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    repository = request.app.state.run_service.repository
    row = await call(
        lambda _: request.app.state.run_service.inspect(str(run_id)), run_id=str(run_id)
    )
    if last_event_id:
        try:
            stream_id, sequence = last_event_id.rsplit(":", 1)
            if stream_id != str(run_id) or int(sequence) < 0:
                raise ValueError
            after = int(sequence)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID 无效或属于其他任务。"
            ) from None
    if after > row["event_seq"]:
        raise HTTPException(status_code=409, detail="事件游标超出已保存的事件范围。")

    async def events():
        cursor = after
        heartbeat_at = asyncio.get_running_loop().time()
        try:
            while True:
                batch = await repository.events(str(run_id), cursor)
                for seq, event, data in batch:
                    yield sse_frame(event, data, f"{run_id}:{seq}")
                    cursor = seq
                    heartbeat_at = asyncio.get_running_loop().time()
                current = await request.app.state.run_service.inspect(str(run_id))
                if (
                    current["status"] in TERMINAL_STATUSES | {"paused", "recovering"}
                    and cursor >= current["event_seq"]
                ):
                    return
                clock = asyncio.get_running_loop().time()
                if (
                    clock - heartbeat_at
                    >= request.app.state.runtime.settings.stream_heartbeat_seconds
                ):
                    yield ": heartbeat\n\n"
                    heartbeat_at = clock
                await asyncio.sleep(0.05)
        except sqlite3.Error:
            # This is a subscription failure, not the task's business outcome.
            # No SSE id: keep the client's cursor on the last durable event.
            yield sse_frame(
                "stream_error",
                {
                    "code": "storage_unavailable",
                    "message": "事件读取失败，请重新连接。",
                    "retryable": True,
                    "http_status": 503,
                    "run_id": str(run_id),
                    "conversation_id": row["conversation_id"],
                    "thread_id": row["conversation_id"],
                    "request_id": row["request_id"],
                    "last_event_id": f"{run_id}:{cursor}",
                },
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
