"""解析切片与可选向量化的批量编排。

解析成功到 recorded；Embedding 已配置时继续向量化到 indexed。
解析失败到 failed，向量化失败保留 recorded 与原有切片。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from pydantic import BaseModel

from app.clock import utc_now
from app.config import Settings
from app.errors import ChatError
from app.knowledge_bases import require_knowledge_base
from app.observability import Metrics
from app.rag.chunking import chunk_blocks, chunk_version
from app.rag.files import FileRepository, FileSummary
from app.rag.parsers import ParseError, ParseOptions, parse
from app.rag.storage import Storage
from app.tokenization import TokenCounter, embedding_limit

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
    heading_level: int | None = None
    order: int = 0
    created_at: str


class IndexRepository:
    """chunks 表；与 files 共用同一连接与事务锁。"""

    def __init__(self, database):
        self.database = database

    async def replace_within(self, file_id: str, records: list[ChunkRecord]) -> None:
        """同一事务内调用：先删旧块再插新块。"""
        await self.database.connection.execute("DELETE FROM chunks WHERE file_id=%s", (file_id,))
        if not records:
            return
        await self.database.connection.executemany(
            "INSERT INTO chunks (chunk_id, kb_id, file_id, chunk_index, content, tokens, "
            "heading, heading_level, kind, page, source_order, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    record.chunk_id,
                    record.kb_id,
                    record.file_id,
                    record.chunk_index,
                    record.content,
                    record.tokens,
                    record.heading,
                    record.heading_level,
                    record.kind,
                    record.page,
                    record.order,
                    record.created_at,
                )
                for record in records
            ],
        )

    async def outdated(self, kb_id, version):
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT file_id FROM files WHERE kb_id=%s AND "
                "index_status IN ('recorded','indexed') AND "
                "(chunk_version IS NULL OR chunk_version!=%s) ORDER BY created_at, id",
                (kb_id, version),
            )
            return [row["file_id"] for row in await cursor.fetchall()]

    async def counts(self, file_ids: list[str]) -> dict[str, int]:
        if not file_ids:
            return {}
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT file_id, COUNT(*) AS count FROM chunks "
                "WHERE file_id=ANY(%s) GROUP BY file_id",
                (file_ids,),
            )
            return {row["file_id"]: row["count"] for row in await cursor.fetchall()}

    async def list_by_file(self, file_id: str) -> list[dict]:
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT * FROM chunks WHERE file_id=%s ORDER BY chunk_index", (file_id,)
            )
            return await cursor.fetchall()


class IndexService:
    """单进程单 worker：批量任务在后台串行处理一个知识库的待解析文件。"""

    def __init__(
        self,
        files: FileRepository,
        index: IndexRepository,
        storage: Storage,
        settings: Settings,
        vector_service=None,
        lexical=None,
        *,
        metrics=None,
    ):
        self.files = files
        self.index = index
        self.storage = storage
        self.settings = settings
        self.vector_service = vector_service
        self.lexical = lexical
        self.metrics = metrics or Metrics()
        self.counter = TokenCounter(settings.embedding_tokenizer)
        self.chunk_version = chunk_version(settings)
        self.tasks: dict[str, asyncio.Task] = {}
        self.schedule_lock = asyncio.Lock()
        self.closing = False

    async def reconcile(self) -> int:
        reset = await self.files.reset_stuck()
        if self.vector_service:
            await self.vector_service.reconcile()
        return reset

    async def list_files(
        self, kb_id: str, limit: int, offset: int
    ) -> tuple[list[FileSummary], int]:
        rows, total = await self.files.list_by_kb(kb_id, limit, offset)
        counts = await self.index.counts([row["file_id"] for row in rows])
        ready = (
            await self.lexical.ready_files([row["file_id"] for row in rows])
            if self.lexical
            else set()
        )
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
                keyword_ready=row["file_id"] in ready,
                keyword_error=row.get("lexical_error"),
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
            await require_knowledge_base(self.files.database, kb_id)
            if self.closing:
                raise ChatError("service_stopping", "服务正在关闭。", 503, True)
            task = self.tasks.get(kb_id)
            if task is not None and not task.done():
                raise ChatError(
                    "index_in_progress", "该知识库正在建立索引，请稍后重试。", 409, True
                )
            pending = await self.files.pending(kb_id)
            file_ids = [row["file_id"] for row in pending]
            file_ids.extend(await self.index.outdated(kb_id, self.chunk_version))
            if self.lexical:
                file_ids.extend(await self.lexical.pending(kb_id))
            if self.vector_service and self.vector_service.model:
                file_ids.extend(await self.vector_service.pending(kb_id))
            file_ids = list(dict.fromkeys(file_ids))
            if not file_ids:
                return []
            batch = asyncio.create_task(self._run(file_ids), name=f"index:{kb_id}")
            self.tasks[kb_id] = batch
            batch.add_done_callback(lambda done: self._finished(kb_id, done))
            return file_ids

    async def _run(self, file_ids: list[str]) -> None:
        for file_id in file_ids:
            try:
                row = await self.files.get(file_id)
                if row and (
                    row["index_status"] in ("not_indexed", "failed")
                    or row.get("chunk_version") != self.chunk_version
                ):
                    await self.parse_file(file_id)
                if self.lexical:
                    try:
                        with self.metrics.span("lexical_index"):
                            await self.lexical.rebuild(file_id, self.files, self.index)
                    except Exception as exc:
                        logger.warning(
                            "lexical_index_failed file_id=%s type=%s", file_id, type(exc).__name__
                        )
                        async with self.files.database.transaction():
                            await self.files.database.connection.execute(
                                "UPDATE files SET lexical_error=%s WHERE file_id=%s",
                                ("全文索引建立失败，请重试。", file_id),
                            )
                if self.vector_service:
                    await self.vector_service.index_file(file_id)
            except Exception as exc:
                # 单个文件的意外错误不应中断整批，也不应让批次任务静默失败。
                logger.warning("index_file_failed file_id=%s type=%s", file_id, type(exc).__name__)

    async def parse_file(self, file_id: str) -> None:
        row = await self.files.get(file_id)
        if row is None:
            return
        await self.files.set_status(file_id, "parsing")
        options = ParseOptions(
            pdf_layout_mode=self.settings.parse_pdf_layout_mode,
            remove_header_footer=self.settings.parse_remove_header_footer,
            remove_toc=self.settings.parse_remove_toc,
            table_context_chars=self.settings.table_context_chars,
        )
        try:
            data = await self.storage.get(row["storage_path"])
            # 解析与切片是 CPU 密集的同步操作，放到线程执行，避免阻塞事件循环上的
            # 聊天流式输出、心跳和取消处理。
            with self.metrics.span("parse"):
                blocks = await asyncio.to_thread(parse, row["extension"], data, options)
            with self.metrics.span("chunk"):
                chunks = await asyncio.to_thread(
                    chunk_blocks,
                    blocks,
                    self.settings.chunk_size_tokens,
                    self.settings.chunk_overlap_tokens,
                    self.settings.chunk_max_per_file,
                    self.counter,
                    row["name"],
                    embedding_limit(self.settings),
                )
        except ParseError as exc:
            await self._fail(file_id, str(exc))
            return
        except ValueError as exc:
            # 切片层可预期的失败（无有效内容、切片数量超限）。
            await self._fail(file_id, str(exc))
            return
        except Exception as exc:
            logger.warning("parse_failed file_id=%s type=%s", file_id, type(exc).__name__)
            await self._fail(file_id, "文件解析失败。")
            return

        timestamp = utc_now()
        records = [
            ChunkRecord(
                chunk_id=str(uuid4()),
                kb_id=row["kb_id"],
                file_id=file_id,
                chunk_index=index,
                content=chunk.content,
                tokens=chunk.tokens,
                heading=chunk.heading,
                heading_level=chunk.heading_level,
                kind=chunk.kind,
                page=chunk.page,
                order=chunk.order,
                created_at=timestamp,
            )
            for index, chunk in enumerate(chunks)
        ]
        try:
            # 切片写入与状态推进在同一事务内提交：失败则整体回滚，绝不留下
            # “切片已写但状态仍为 parsing”的半完成状态。
            async with self.files.database.transaction():
                await self.index.replace_within(file_id, records)
                if self.vector_service:
                    await self.vector_service.invalidate_within(file_id)
                if self.lexical:
                    await self.lexical.invalidate_within(file_id)
                await self.files.database.connection.execute(
                    "UPDATE files SET chunk_version=%s,lexical_error=NULL WHERE file_id=%s",
                    (self.chunk_version, file_id),
                )
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
