import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from pydantic import BaseModel
from starlette.datastructures import UploadFile

from app.conversations import ConversationRepository
from app.errors import ChatError
from app.rag.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_BYTES,
    MAX_STORED_FILE_BYTES,
    EmptyFileError,
    FileTooLargeError,
    Storage,
)

DEFAULT_KB_ID = "default"

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
        name = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
        name = name.strip()
        if not name or len(name.encode("utf-8")) > 255:
            raise ValueError
    except (UnicodeError, ValueError):
        raise ChatError(
            "invalid_filename", "文件名必须为有效 UTF-8，且不超过 255 字节。", 422
        ) from None
    # Allow Unicode letters/numbers, spaces and a small set of filename punctuation.
    if not all(unicodedata.category(c)[0] in {"L", "N", "M"} or c in " ._-()（）" for c in name):
        raise ChatError("invalid_filename", "文件名包含不允许的字符。", 422)
    match = re.search(r"(\.[a-z0-9]+)$", name.lower())
    extension = match.group(1) if match else ""
    if extension not in ALLOWED_EXTENSIONS or name.lower() == extension:
        raise ChatError("unsupported_file_type", "仅支持 PDF、Word、MD、TXT 和 Excel 文件。", 415)
    return name, extension


class FileRepository:
    def __init__(self, database: ConversationRepository):
        self.database = database

    @staticmethod
    def _create_files_sql() -> str:
        # 数据库只做宽松兜底（MAX_STORED_FILE_BYTES），真实 50 MiB 限额在接收期用
        # MAX_FILE_BYTES 控制，避免以后调整上限都要重建带 CHECK 的表。
        return (
            "CREATE TABLE IF NOT EXISTS files ("
            "file_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, name TEXT NOT NULL, "
            "extension TEXT NOT NULL, "
            f"size INTEGER NOT NULL CHECK(size > 0 AND size <= {MAX_STORED_FILE_BYTES}), "
            "content_hash TEXT NOT NULL, storage_path TEXT NOT NULL UNIQUE, "
            "storage_backend TEXT NOT NULL, index_status TEXT NOT NULL CHECK(index_status IN "
            "('not_indexed','recorded','parsing','indexing','indexed','failed')), "
            "index_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL, UNIQUE(kb_id, idempotency_key))"
        )

    async def setup(self):
        async with self.database.transaction():
            await self._create_or_migrate()
            await self.database.connection.execute(
                "CREATE INDEX IF NOT EXISTS files_kb_name ON files(kb_id, name)"
            )
            await self.database.connection.execute(
                "CREATE INDEX IF NOT EXISTS files_content_hash ON files(content_hash)"
            )

    async def _create_or_migrate(self):
        connection = self.database.connection
        async with connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
        ) as cursor:
            row = await cursor.fetchone()
        sql = row["sql"] if row else None
        if sql is None:
            await connection.execute(self._create_files_sql())
            return
        if "20971520)" not in sql:
            return
        # 旧表把 20 MiB 上限写死在 CHECK 里；SQLite 无法修改 CHECK，只能重建表并迁移数据。
        columns = (
            "file_id, kb_id, name, extension, size, content_hash, storage_path, "
            "storage_backend, index_status, index_error, created_at, updated_at, "
            "idempotency_key"
        )
        await connection.execute("ALTER TABLE files RENAME TO files_old")
        await connection.execute(self._create_files_sql())
        await connection.execute(f"INSERT INTO files ({columns}) SELECT {columns} FROM files_old")
        await connection.execute("DROP TABLE files_old")

    async def by_key(self, kb_id, key):
        # Called under the shared transaction lock by FileService.
        async with self.database.connection.execute(
            "SELECT * FROM files WHERE kb_id=? AND idempotency_key=?", (kb_id, key)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def insert(self, record: FileRecord, key: str):
        values = {**record.model_dump(), "idempotency_key": key}
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        await self.database.connection.execute(
            f"INSERT INTO files ({columns}) VALUES ({placeholders})", tuple(values.values())
        )

    async def get(self, file_id: str) -> dict | None:
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT * FROM files WHERE file_id=?", (file_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_by_kb(self, kb_id: str, limit: int, offset: int) -> tuple[list[dict], int]:
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT COUNT(*) FROM files WHERE kb_id=?", (kb_id,)
            ) as cursor:
                total = (await cursor.fetchone())[0]
            async with self.database.connection.execute(
                "SELECT * FROM files WHERE kb_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (kb_id, limit, offset),
            ) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
        return rows, total

    async def pending(self, kb_id: str) -> list[dict]:
        """待解析或上次失败的文件；已 recorded/indexed 的不重复处理。"""
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT * FROM files WHERE kb_id=? AND index_status IN ('not_indexed','failed') "
                "ORDER BY created_at, rowid",
                (kb_id,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def set_status(self, file_id: str, status: str, error: str | None = None) -> None:
        async with self.database.transaction():
            await self.set_status_within(file_id, status, error)

    async def set_status_within(self, file_id: str, status: str, error: str | None = None) -> None:
        """同一事务内调用，与切片写入原子提交。"""
        await self.database.connection.execute(
            "UPDATE files SET index_status=?, index_error=?, updated_at=? WHERE file_id=?",
            (status, error, datetime.now(timezone.utc).isoformat(), file_id),
        )

    async def reset_stuck(self) -> int:
        """启动对账：进程中断遗留的 parsing 状态回到 not_indexed。"""
        async with self.database.transaction():
            cursor = await self.database.connection.execute(
                "UPDATE files SET index_status='not_indexed', index_error=NULL, updated_at=? "
                "WHERE index_status='parsing'",
                (datetime.now(timezone.utc).isoformat(),),
            )
        return cursor.rowcount


class FileService:
    def __init__(self, repository: FileRepository, storage: Storage):
        self.repository = repository
        self.storage = storage

    async def upload(self, kb_id: str, file: UploadFile, key: str) -> FileRecord:
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

        async def publish():
            file_id = str(uuid4())
            stored = None
            try:
                # Serialize key lookup and insert with existing DB writes; no network calls here.
                async with self.repository.database.transaction():
                    existing = await self.repository.by_key(kb_id, key)
                    if existing:
                        if (existing["name"], existing["content_hash"]) != (name, content_hash):
                            raise ChatError("idempotency_conflict", "幂等键已用于其他文件。", 409)
                        return FileRecord(**existing)

                    storage_key = f"{kb_id}/{file_id}{extension}"

                    async def chunks():
                        while chunk := await file.read(256 * 1024):
                            yield chunk

                    stored = await self.storage.put(storage_key, chunks())
                    now = datetime.now(timezone.utc).isoformat()
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
                        created_at=now,
                        updated_at=now,
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
