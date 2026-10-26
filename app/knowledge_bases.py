"""知识库元数据与删除；物理文件清理队列随数据库事务持久化。"""

import asyncio
import logging
import unicodedata
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import utc_now
from app.errors import ChatError
from app.users import require_staff

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


class FileDeleteResult(BaseModel):
    file_id: str
    deleted: bool = True
    cleanup_pending: int = 0


class GarbageCollectionResult(BaseModel):
    queued: int
    cleanup_pending: int
    min_age_seconds: int = 86400


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("知识库名称不能包含控制字符。")
        return value


class KnowledgeBase(BaseModel):
    kb_id: str
    name: str
    created_at: str
    file_count: int = 0
    archived: bool = False


class KnowledgeBaseArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


class DeleteResult(BaseModel):
    kb_id: str
    deleted: bool = True
    cleanup_pending: int = 0


async def require_knowledge_base(database, kb_id, *, lock=False):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT * FROM knowledge_bases WHERE kb_id=%s" + (" FOR KEY SHARE" if lock else ""),
            (kb_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ChatError("knowledge_base_not_found", "知识库不存在或已被删除，请重新选择。", 404)
        if row["archived"]:
            raise ChatError(
                "knowledge_base_archived", "知识库已归档，仅保留历史记录，不能再写入或运行。", 409
            )
        return row


class KnowledgeBaseService:
    def __init__(self, database, storage, index_service, run_service, file_service):
        self.database = database
        self.storage = storage
        self.index_service = index_service
        self.run_service = run_service
        self.file_service = file_service
        self.cleanup_lock = asyncio.Lock()

    async def list(self, include_archived=False):
        condition = "" if include_archived else "WHERE k.archived=FALSE"
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT k.*, COUNT(f.file_id) AS file_count FROM knowledge_bases k "
                "LEFT JOIN files f ON f.kb_id=k.kb_id "
                + condition
                + " GROUP BY k.kb_id ORDER BY k.created_at,k.kb_id"
            )
            return [KnowledgeBase(**row) for row in await cursor.fetchall()]

    async def create(self, body):
        kb_id, timestamp = str(uuid4()), utc_now()
        try:
            async with self.database.transaction():
                await self.database.connection.execute(
                    "INSERT INTO knowledge_bases (kb_id,name,created_at) VALUES (%s,%s,%s)",
                    (kb_id, body.name, timestamp),
                )
        except psycopg.errors.UniqueViolation:
            raise ChatError(
                "knowledge_base_name_exists", "已有同名知识库，请使用其他名称。", 409
            ) from None
        return KnowledgeBase(kb_id=kb_id, name=body.name, created_at=timestamp)

    async def archive(self, kb_id, archived):
        async with self.database.transaction():
            cursor = await self.database.connection.execute(
                "SELECT * FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (kb_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ChatError(
                    "knowledge_base_not_found", "知识库不存在或已被删除，请重新选择。", 404
                )
            if archived:
                await self._ensure_no_running_evaluations(kb_id)
                cursor = await self.database.connection.execute(
                    "SELECT 1 FROM runs WHERE knowledge_base_id=%s "
                    "AND status IN ('queued','running','paused','recovering') LIMIT 1",
                    (kb_id,),
                )
                if await cursor.fetchone():
                    raise ChatError(
                        "knowledge_base_busy",
                        "知识库有未结束的问答任务，请先完成或取消。",
                        409,
                        True,
                    )
            cursor = await self.database.connection.execute(
                "UPDATE knowledge_bases SET archived=%s WHERE kb_id=%s RETURNING *",
                (archived, kb_id),
            )
            row = await cursor.fetchone()
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT COUNT(*) AS n FROM files WHERE kb_id=%s", (kb_id,)
            )
            count = (await cursor.fetchone())["n"]
        return KnowledgeBase(**row, file_count=count)

    async def cleanup(self):
        async with self.cleanup_lock:
            return await self._cleanup()

    async def _cleanup(self):
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT storage_path FROM file_cleanup_queue"
            )
            paths = [row["storage_path"] for row in await cursor.fetchall()]
        for path in paths:
            try:
                await self.storage.delete(path)
            except OSError:
                logger.warning("knowledge_base_file_cleanup_failed")
                continue
            async with self.database.transaction():
                await self.database.connection.execute(
                    "DELETE FROM file_cleanup_queue WHERE storage_path=%s", (path,)
                )
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT COUNT(*) AS n FROM file_cleanup_queue"
            )
            return (await cursor.fetchone())["n"]

    async def file(self, kb_id, file_id):
        await require_knowledge_base(self.database, kb_id)
        row = await self.file_service.repository.get(file_id)
        if row is None or row["kb_id"] != kb_id:
            raise ChatError("file_not_found", "文件不存在或已被删除。", 404)
        return row

    async def download(self, kb_id, file_id):
        row = await self.file(kb_id, file_id)
        try:
            content = await self.storage.get(row["storage_path"])
        except FileNotFoundError:
            raise ChatError("file_content_not_found", "原始文件不存在。", 404) from None
        except OSError:
            raise ChatError(
                "file_storage_unavailable", "原始文件读取失败，请重试。", 503, True
            ) from None
        return row["name"], content

    async def _ensure_no_running_evaluations(self, kb_id):
        cursor = await self.database.connection.execute(
            "SELECT 1 FROM evaluations WHERE status='running' "
            "AND request->>'knowledge_base_id'=%s LIMIT 1",
            (kb_id,),
        )
        if await cursor.fetchone():
            raise ChatError(
                "knowledge_base_busy",
                "知识库有未结束的评测任务，请先停止评测。",
                409,
                True,
            )

    async def delete_file(self, kb_id, file_id):
        async with self.index_service.schedule_lock, self.run_service.control_lock:
            task = self.index_service.tasks.get(kb_id)
            if task is not None and not task.done():
                raise ChatError(
                    "knowledge_base_busy", "知识库正在建立索引，请完成后再删除。", 409, True
                )
            async with self.database.transaction():
                await require_knowledge_base(self.database, kb_id, lock=True)
                await self._ensure_no_running_evaluations(kb_id)
                cursor = await self.database.connection.execute(
                    "SELECT * FROM files WHERE kb_id=%s AND file_id=%s FOR UPDATE", (kb_id, file_id)
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ChatError("file_not_found", "文件不存在或已被删除。", 404)
                cursor = await self.database.connection.execute(
                    "SELECT 1 FROM runs WHERE knowledge_base_id=%s "
                    "AND status IN ('queued','running','paused','recovering') LIMIT 1",
                    (kb_id,),
                )
                if await cursor.fetchone():
                    raise ChatError(
                        "knowledge_base_busy",
                        "知识库有未结束的问答任务，请先完成或取消。",
                        409,
                        True,
                    )
                await self.database.connection.execute(
                    "INSERT INTO file_cleanup_queue (storage_path,created_at) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (row["storage_path"], utc_now()),
                )
                await self.database.connection.execute(
                    "DELETE FROM files WHERE file_id=%s", (file_id,)
                )
        return FileDeleteResult(file_id=file_id, cleanup_pending=await self.cleanup())

    async def collect_orphans(self):
        # 发布锁覆盖对象写入到记录提交，扫描不会把尚未提交的上传误认作孤儿。
        async with self.file_service.publication_lock:
            candidates = await asyncio.to_thread(self.storage.orphan_candidates)
            queued = 0
            async with self.database.transaction():
                for path in candidates:
                    cursor = await self.database.connection.execute(
                        "INSERT INTO file_cleanup_queue (storage_path,created_at) "
                        "SELECT %s,%s WHERE NOT EXISTS "
                        "(SELECT 1 FROM files WHERE storage_path=%s) ON CONFLICT DO NOTHING",
                        (path, utc_now(), path),
                    )
                    queued += cursor.rowcount
            pending = await self.cleanup()
        return GarbageCollectionResult(queued=queued, cleanup_pending=pending)

    async def delete(self, kb_id):
        # 与索引调度、任务创建/恢复串行，禁止删除仍在被后台任务使用的知识库。
        async with self.index_service.schedule_lock, self.run_service.control_lock:
            task = self.index_service.tasks.get(kb_id)
            if task is not None and not task.done():
                raise ChatError(
                    "knowledge_base_busy", "知识库正在建立索引，请完成后再删除。", 409, True
                )
            async with self.database.transaction():
                cursor = await self.database.connection.execute(
                    "SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (kb_id,)
                )
                if await cursor.fetchone() is None:
                    raise ChatError("knowledge_base_not_found", "知识库不存在或已被删除。", 404)
                await self._ensure_no_running_evaluations(kb_id)
                cursor = await self.database.connection.execute(
                    "SELECT 1 FROM runs WHERE knowledge_base_id=%s "
                    "AND status IN ('queued','running','paused','recovering') LIMIT 1",
                    (kb_id,),
                )
                if await cursor.fetchone():
                    raise ChatError(
                        "knowledge_base_busy",
                        "知识库有未结束的问答任务，请先完成或取消任务。",
                        409,
                        True,
                    )
                await self.database.connection.execute(
                    "INSERT INTO file_cleanup_queue (storage_path,created_at) "
                    "SELECT storage_path,%s FROM files WHERE kb_id=%s ON CONFLICT DO NOTHING",
                    (utc_now(), kb_id),
                )
                await self.database.connection.execute(
                    "DELETE FROM knowledge_bases WHERE kb_id=%s", (kb_id,)
                )
        # 数据库先原子删除，原始文件失败则留队列，重启或下一次删除时重试。
        pending = await self.cleanup()
        return DeleteResult(kb_id=kb_id, cleanup_pending=pending)


@router.get("", response_model=list[KnowledgeBase])
async def list_knowledge_bases(request: Request, include_archived: bool = False):
    return await request.app.state.knowledge_base_service.list(include_archived)


@router.post(
    "", response_model=KnowledgeBase, status_code=201, dependencies=[Depends(require_staff)]
)
async def create_knowledge_base(body: KnowledgeBaseCreate, request: Request):
    return await request.app.state.knowledge_base_service.create(body)


@router.patch(
    "/{kb_id}/archive", response_model=KnowledgeBase, dependencies=[Depends(require_staff)]
)
async def archive_knowledge_base(kb_id: str, body: KnowledgeBaseArchive, request: Request):
    return await request.app.state.knowledge_base_service.archive(kb_id, body.archived)


@router.delete("/{kb_id}", response_model=DeleteResult, dependencies=[Depends(require_staff)])
async def delete_knowledge_base(kb_id: str, request: Request):
    return await request.app.state.knowledge_base_service.delete(kb_id)
