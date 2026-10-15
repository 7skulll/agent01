import sqlite3
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.errors import ChatError
from app.rag.files import DEFAULT_KB_ID, FileListResponse, FileRecord, IndexResponse
from app.rag.storage import MAX_FILE_BYTES, EmptyFileError, FileTooLargeError

router = APIRouter(prefix="/api/v1", tags=["files"])


class LimitedMultipartParser(MultiPartParser):
    """Apply the file limit while receiving, including chunked requests."""

    def __init__(self, headers, stream):
        super().__init__(headers, stream, max_files=1, max_fields=0)
        self.file_bytes = 0
        self.complete = False

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_FILE_BYTES:
                raise FileTooLargeError
        super().on_part_data(data, start, end)

    def on_end(self):
        self.complete = True
        super().on_end()


@router.post(
    "/knowledge-bases/{kb_id}/files",
    response_model=FileRecord,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload_file(
    kb_id: str,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    def fail(status, code, message):
        return HTTPException(status_code=status, detail={"code": code, "message": message})

    if kb_id != DEFAULT_KB_ID:
        raise fail(404, "knowledge_base_not_found", "知识库不存在；当前仅支持 default。")
    key = idempotency_key.strip()
    if not key or not key.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise fail(422, "invalid_idempotency_key", "幂等键必须为非空 ASCII 文本。")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise fail(415, "invalid_content_type", "请使用 multipart/form-data 上传文件。")

    async def bounded_body():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            # Bound multipart headers/preamble as well as the file itself.
            if received > MAX_FILE_BYTES + 64 * 1024:
                raise FileTooLargeError
            yield chunk

    parser = LimitedMultipartParser(request.headers, bounded_body())
    try:
        form = await parser.parse()
        file = form.get("file")
        if not parser.complete or len(form.multi_items()) != 1 or not isinstance(file, UploadFile):
            raise fail(422, "invalid_upload", "请完整上传一个名为 file 的文件字段。")
        return await request.app.state.file_service.upload(kb_id, file, key)
    except ChatError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
    except FileTooLargeError:
        raise fail(
            413, "file_too_large", f"单文件不能超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB。"
        ) from None
    except EmptyFileError:
        raise fail(422, "empty_file", "不能上传空文件。") from None
    except MultiPartException:
        raise fail(422, "invalid_upload", "multipart 数据无效，仅接受一个文件。") from None
    except ClientDisconnect:
        raise fail(499, "client_disconnected", "上传连接已断开。") from None
    except sqlite3.Error:
        raise fail(503, "storage_unavailable", "文件记录保存失败，请重试。") from None
    except OSError:
        raise fail(503, "file_storage_unavailable", "文件存储失败，请重试。") from None
    finally:
        # Close spooled files on success, invalid multipart, disconnect and cancellation.
        for temporary in parser._files_to_close_on_error:
            temporary.close()


def _unknown_kb() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "knowledge_base_not_found",
            "message": "知识库不存在；当前仅支持 default。",
        },
    )


@router.get("/knowledge-bases/{kb_id}/files", response_model=FileListResponse)
async def list_files(
    kb_id: str,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    if kb_id != DEFAULT_KB_ID:
        raise _unknown_kb()
    items, total = await request.app.state.index_service.list_files(kb_id, limit, offset)
    return FileListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("/knowledge-bases/{kb_id}/index", response_model=IndexResponse, status_code=202)
async def build_index(kb_id: str, request: Request):
    """批量触发该知识库下所有待解析文件的解析与切片。"""
    if kb_id != DEFAULT_KB_ID:
        raise _unknown_kb()
    try:
        scheduled = await request.app.state.index_service.schedule(kb_id)
    except ChatError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
    return IndexResponse(kb_id=kb_id, scheduled=scheduled, count=len(scheduled))
