import asyncio
import hashlib
import logging
import re
import unicodedata
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from pydantic import BaseModel
from starlette.datastructures import UploadFile

from app.clock import utc_now
from app.database import Database
from app.errors import ChatError
from app.knowledge_bases import require_knowledge_base
from app.rag.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_BYTES,
    EmptyFileError,
    FileTooLargeError,
    Storage,
)

logger = logging.getLogger(__name__)


class FileRecord(BaseModel):
    file_id: str
    kb_id: str
    name: str
    extension: str
    size: int
    content_hash: str
    storage_path: str
    storage_backend: str
    index_status: Literal["not_indexed", "recorded", "parsing", "indexing", "indexed", "failed"]
    index_error: str | None = None
    created_at: str
    updated_at: str


class FileSummary(BaseModel):
    file_id: str
    kb_id: str
    name: str
    extension: str
    size: int
    index_status: str
    index_error: str | None = None
    chunk_count: int = 0
    keyword_ready: bool = False
    keyword_error: str | None = None
    created_at: str
    updated_at: str


class FileListResponse(BaseModel):
    items: list[FileSummary]
    total: int
    limit: int
    offset: int


class IndexResponse(BaseModel):
    kb_id: str
    scheduled: list[str]
    count: int


def safe_filename(filename: str | None) -> tuple[str, str]:
    # Like RAGFlow's filename_type: validate type, basename and length before suffix matching.
    if not isinstance(filename, str) or not filename or len(filename) > 4096:
        raise ChatError("invalid_filename", "文件名无效。", 422)
    try:
        name = unquote(filename, encoding="utf-8", errors="strict")
        # Accommodate clients that encoded UTF-8 header bytes as Latin-1 text.
        try:
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        # Reject control characters before trimming or taking the basename.
        if any(unicodedata.category(c) == "Cc" for c in name):
            raise ChatError("invalid_filename", "文件名不能包含控制字符。", 422)
        name = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
        name = name.strip()
        if not name or len(name.encode("utf-8")) > 255:
            raise ValueError
    except (UnicodeError, ValueError):
        raise ChatError(
            "invalid_filename", "文件名必须为有效 UTF-8，且不超过 255 字节。", 422
        ) from None
    # Display names may contain punctuation, symbols and emoji. Storage keys use
    # a UUID and the validated extension, never this basename.
    match = re.search(r"(\.[a-z0-9]+)$", name.lower())
    extension = match.group(1) if match else ""
    if extension not in ALLOWED_EXTENSIONS or name.lower() == extension:
        raise ChatError("unsupported_file_type", "仅支持 PDF、Word、MD、TXT 和 Excel 文件。", 415)
    return name, extension


class FileRepository:
    def __init__(self, database: Database):
        self.database = database

    async def by_key(self, kb_id, key):
        # Called under the shared transaction lock by FileService.
        cursor = await self.database.connection.execute(
            "SELECT * FROM files WHERE kb_id=%s AND idempotency_key=%s", (kb_id, key)
        )
        row = await cursor.fetchone()
        return row

    async def insert(self, record: FileRecord, key: str):
        values = {**record.model_dump(), "idempotency_key": key}
        columns = ",".join(values)
        placeholders = ",".join("%s" for _ in values)
        await self.database.connection.execute(
            f"INSERT INTO files ({columns}) VALUES ({placeholders})", tuple(values.values())
        )

    async def get(self, file_id: str) -> dict | None:
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM files WHERE file_id=%s", (file_id,)
            )
            row = await cursor.fetchone()
        return row

    async def list_by_kb(self, kb_id: str, limit: int, offset: int) -> tuple[list[dict], int]:
        await require_knowledge_base(self.database, kb_id)
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT COUNT(*) AS count FROM files WHERE kb_id=%s", (kb_id,)
            )
            total = (await cursor.fetchone())["count"]
            cursor = await self.database.connection.execute(
                "SELECT * FROM files WHERE kb_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (kb_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return rows, total

    async def pending(self, kb_id: str) -> list[dict]:
        """待解析或上次失败的文件；已 recorded/indexed 的不重复处理。"""
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM files WHERE kb_id=%s AND index_status IN ('not_indexed','failed') "
                "ORDER BY created_at, id",
                (kb_id,),
            )
            return await cursor.fetchall()

    async def set_status(self, file_id: str, status: str, error: str | None = None) -> None:
        async with self.database.transaction():
            await self.set_status_within(file_id, status, error)

    async def set_status_within(self, file_id: str, status: str, error: str | None = None) -> None:
        """同一事务内调用，与切片写入原子提交。"""
        await self.database.connection.execute(
            "UPDATE files SET index_status=%s, index_error=%s, updated_at=%s WHERE file_id=%s",
            (status, error, utc_now(), file_id),
        )

    async def reset_stuck(self) -> int:
        """启动对账：进程中断遗留的 parsing 状态回到 not_indexed。"""
        async with self.database.transaction():
            cursor = await self.database.connection.execute(
                "UPDATE files SET index_status='not_indexed', index_error=NULL, updated_at=%s "
                "WHERE index_status='parsing'",
                (utc_now(),),
            )
        return cursor.rowcount


class FileService:
    def __init__(self, repository: FileRepository, storage: Storage):
        self.repository = repository
        self.storage = storage
        self.publication_lock = asyncio.Lock()

    async def upload(self, kb_id: str, file: UploadFile, key: str) -> FileRecord:
        await require_knowledge_base(self.repository.database, kb_id)
        name, extension = safe_filename(file.filename)
        digest, size = hashlib.sha256(), 0
        while chunk := await file.read(256 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise FileTooLargeError
            digest.update(chunk)
        if size == 0:
            raise EmptyFileError
        await file.seek(0)
        content_hash = digest.hexdigest()

        async with self.repository.database.read():
            existing = await self.repository.by_key(kb_id, key)
        if existing:
            if (existing["name"], existing["content_hash"]) != (name, content_hash):
                raise ChatError("idempotency_conflict", "幂等键已用于其他文件。", 409)
            return FileRecord(**existing)

        async def publish():
            async with self.publication_lock:
                return await publish_locked()

        async def publish_locked():
            file_id = str(uuid4())
            stored = None
            try:
                storage_key = f"{kb_id}/{file_id}{extension}"

                async def chunks():
                    while chunk := await file.read(256 * 1024):
                        yield chunk

                stored = await self.storage.put(storage_key, chunks())
                async with self.repository.database.transaction():
                    await require_knowledge_base(self.repository.database, kb_id, lock=True)
                    await self.repository.database.connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (kb_id + ":" + key,)
                    )
                    existing = await self.repository.by_key(kb_id, key)
                    if existing:
                        if (existing["name"], existing["content_hash"]) != (name, content_hash):
                            raise ChatError("idempotency_conflict", "幂等键已用于其他文件。", 409)
                        # 竞争失败的暂存对象不属于已提交记录，可以移除。
                        await self.storage.delete(stored.path)
                        stored = None
                        return FileRecord(**existing)

                    timestamp = utc_now()
                    record = FileRecord(
                        file_id=file_id,
                        kb_id=kb_id,
                        name=name,
                        extension=extension,
                        size=stored.size,
                        content_hash=stored.content_hash,
                        storage_path=stored.path,
                        storage_backend=self.storage.backend,
                        index_status="not_indexed",
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    await self.repository.insert(record, key)
                return record
            except BaseException:
                if stored is not None:
                    # 提交可能在报错前已生效：先确认记录是否落库，再决定是否删除对象，
                    # 避免删掉已被记录引用的文件，导致记录与对象不一致。
                    try:
                        committed = await self.repository.get(file_id)
                    except Exception as exc:
                        logger.warning(
                            "upload_compensation_probe_failed file_id=%s type=%s",
                            file_id,
                            type(exc).__name__,
                        )
                        # 查询失败时提交状态未知：保留对象，并向调用方报告原始失败。
                        # 只有成功查询且确认没有记录时，才允许删除对象。
                    else:
                        if committed is not None:
                            return FileRecord(**committed)
                        try:
                            await self.storage.delete(stored.path)
                        except Exception as exc:
                            logger.warning(
                                "upload_compensation_delete_failed file_id=%s type=%s",
                                file_id,
                                type(exc).__name__,
                            )
                raise

        # Once the complete body has arrived, finish publication/compensation before closing it.
        task = asyncio.create_task(publish())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
