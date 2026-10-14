"""索引层：把已上传文件解析、切片并落库，推进 files.index_status 状态机。

状态流（复用现有枚举，不改表结构）：
    not_indexed --解析中--> parsing --成功--> recorded
                                   --失败--> failed(index_error)

只做文本产出与切片落库，不触发向量化与检索。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel

from app.config import Settings
from app.errors import ChatError
from app.rag.chunking import chunk_blocks
from app.rag.files import FileRepository, FileSummary
from app.rag.parsers import ParseError, parse
from app.rag.storage import Storage

logger = logging.getLogger(__name__)


class ChunkRecord(BaseModel):
    chunk_id: str
    kb_id: str
    file_id: str
    chunk_index: int
    content: str
    tokens: int
    heading: str | None
    kind: str
    page: int | None
    created_at: str


class IndexRepository:
    """chunks 表；与 files 共用同一连接与事务锁。"""

    def __init__(self, database):
        self.database = database

    async def setup(self):
        async with self.database.transaction():
            await self.database.connection.execute(
                "CREATE TABLE IF NOT EXISTS chunks ("
                "chunk_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, file_id TEXT NOT NULL, "
                "chunk_index INTEGER NOT NULL, content TEXT NOT NULL, tokens INTEGER NOT NULL, "
                "heading TEXT, kind TEXT NOT NULL CHECK(kind IN ('text','table')), page INTEGER, "
                "created_at TEXT NOT NULL, UNIQUE(file_id, chunk_index))"
            )
            await self.database.connection.execute(
                "CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id)"
            )

    async def replace(self, file_id: str, records: list[ChunkRecord]) -> None:
        """整体替换一个文件的切片，保证重解析不残留旧块。"""
        async with self.database.transaction():
            await self.replace_within(file_id, records)

    async def replace_within(self, file_id: str, records: list[ChunkRecord]) -> None:
        """同一事务内调用：先删旧块再插新块。"""
        await self.database.connection.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        if not records:
            return
        await self.database.connection.executemany(
            "INSERT INTO chunks (chunk_id, kb_id, file_id, chunk_index, content, tokens, "
            "heading, kind, page, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    record.chunk_id,
                    record.kb_id,
                    record.file_id,
                    record.chunk_index,
                    record.content,
                    record.tokens,
                    record.heading,
                    record.kind,
                    record.page,
                    record.created_at,
                )
                for record in records
            ],
        )

    async def counts(self, file_ids: list[str]) -> dict[str, int]:
        if not file_ids:
            return {}
        placeholders = ",".join("?" for _ in file_ids)
        async with self.database.lock:
            async with self.database.connection.execute(
                f"SELECT file_id, COUNT(*) AS count FROM chunks "
                f"WHERE file_id IN ({placeholders}) GROUP BY file_id",
                tuple(file_ids),
            ) as cursor:
                return {row["file_id"]: row["count"] for row in await cursor.fetchall()}

    async def list_by_file(self, file_id: str) -> list[dict]:
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT * FROM chunks WHERE file_id=? ORDER BY chunk_index", (file_id,)
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]


class IndexService:
    """单进程单 worker：批量任务在后台串行处理一个知识库的待解析文件。"""

    def __init__(
        self,
        files: FileRepository,
        index: IndexRepository,
        storage: Storage,
        settings: Settings,
    ):
        self.files = files
        self.index = index
        self.storage = storage
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}
        self.schedule_lock = asyncio.Lock()
        self.closing = False

    async def reconcile(self) -> int:
        return await self.files.reset_stuck()

    async def list_files(
        self, kb_id: str, limit: int, offset: int
    ) -> tuple[list[FileSummary], int]:
        rows, total = await self.files.list_by_kb(kb_id, limit, offset)
        counts = await self.index.counts([row["file_id"] for row in rows])
        items = [
            FileSummary(
                file_id=row["file_id"],
                kb_id=row["kb_id"],
                name=row["name"],
                extension=row["extension"],
                size=row["size"],
                index_status=row["index_status"],
                index_error=row["index_error"],
                chunk_count=counts.get(row["file_id"], 0),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
        return items, total

    async def schedule(self, kb_id: str) -> list[str]:
        # 检查、查询待处理文件与注册任务必须串行，否则并发请求会各自创建 worker，
        # 而任务字典只保留最后一个，导致孤儿 worker 无法取消。
        async with self.schedule_lock:
            if self.closing:
                raise ChatError("service_stopping", "服务正在关闭。", 503, True)
            task = self.tasks.get(kb_id)
            if task is not None and not task.done():
                raise ChatError(
                    "index_in_progress", "该知识库正在建立索引，请稍后重试。", 409, True
                )
            pending = await self.files.pending(kb_id)
            file_ids = [row["file_id"] for row in pending]
            if not file_ids:
                return []
            batch = asyncio.create_task(self._run(file_ids), name=f"index:{kb_id}")
            self.tasks[kb_id] = batch
            batch.add_done_callback(lambda done: self._finished(kb_id, done))
            return file_ids

    async def _run(self, file_ids: list[str]) -> None:
        for file_id in file_ids:
            try:
                await self.parse_file(file_id)
            except Exception as exc:
                # 单个文件的意外错误不应中断整批，也不应让批次任务静默失败。
                logger.warning("index_file_failed file_id=%s type=%s", file_id, type(exc).__name__)

    async def parse_file(self, file_id: str) -> None:
        row = await self.files.get(file_id)
        if row is None:
            return
        await self.files.set_status(file_id, "parsing")
        try:
            data = await self.storage.get(row["storage_path"])
            # 解析与切片是 CPU 密集的同步操作，放到线程执行，避免阻塞事件循环上的
            # 聊天流式输出、心跳和取消处理。
            blocks = await asyncio.to_thread(parse, row["extension"], data)
            chunks = await asyncio.to_thread(
                chunk_blocks,
                blocks,
                self.settings.chunk_size_tokens,
                self.settings.chunk_overlap_tokens,
            )
        except ParseError as exc:
            await self._fail(file_id, str(exc))
            return
        except Exception as exc:
            logger.warning("parse_failed file_id=%s type=%s", file_id, type(exc).__name__)
            await self._fail(file_id, "文件解析失败。")
            return

        now = datetime.now(timezone.utc).isoformat()
        records = [
            ChunkRecord(
                chunk_id=str(uuid4()),
                kb_id=row["kb_id"],
                file_id=file_id,
                chunk_index=index,
                content=chunk.content,
                tokens=chunk.tokens,
                heading=chunk.heading,
                kind=chunk.kind,
                page=chunk.page,
                created_at=now,
            )
            for index, chunk in enumerate(chunks)
        ]
        try:
            # 切片写入与状态推进在同一事务内提交：失败则整体回滚，绝不留下
            # “切片已写但状态仍为 parsing”的半完成状态。
            async with self.files.database.transaction():
                await self.index.replace_within(file_id, records)
                await self.files.set_status_within(file_id, "recorded")
        except Exception as exc:
            logger.warning("index_persist_failed file_id=%s type=%s", file_id, type(exc).__name__)
            await self._fail(file_id, "切片保存失败。")

    async def _fail(self, file_id: str, message: str) -> None:
        """尽力标记失败；标记本身失败只记录，文件留给启动对账回收。"""
        try:
            await self.files.set_status(file_id, "failed", message)
        except Exception as exc:
            logger.warning(
                "index_fail_status_failed file_id=%s type=%s", file_id, type(exc).__name__
            )

    async def wait(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def close(self) -> None:
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    def _finished(self, kb_id: str, task: asyncio.Task) -> None:
        if self.tasks.get(kb_id) is task:
            self.tasks.pop(kb_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "index_batch_failed kb_id=%s type=%s", kb_id, type(task.exception()).__name__
            )
