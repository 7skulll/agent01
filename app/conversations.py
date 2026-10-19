import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.errors import ChatError
from app.schemas import Conversation

logger = logging.getLogger(__name__)


class ConversationNotFoundError(RuntimeError):
    pass


class ConversationRepository:
    """Session metadata; messages and tool states live in LangGraph checkpoints."""

    def __init__(self, database):
        self.database = database
        self.connection = database.connection
        self.transaction = database.transaction

    async def setup(self):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE conversations SET status='failed', last_error_code='process_interrupted' "
                "WHERE status='running' AND active_run_id IS NULL"
            )

    async def create(self, *, purpose="chat") -> Conversation:
        now = datetime.now(timezone.utc).isoformat()
        item = Conversation(thread_id=uuid4(), created_at=now, updated_at=now, status="ready")
        async with self.transaction():
            await self.connection.execute(
                "INSERT INTO conversations (thread_id, created_at, updated_at, status, purpose) "
                "VALUES (%s, %s, %s, %s, %s)",
                (str(item.thread_id), now, now, "ready", purpose),
            )
        return item

    async def get(self, thread_id: str) -> Conversation:
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT * FROM conversations WHERE thread_id=%s", (thread_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            raise ConversationNotFoundError("会话不存在。")
        return Conversation(**row)

    async def list(self, limit: int, offset: int) -> list[Conversation]:
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT * FROM conversations WHERE purpose='chat' "
                "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return [Conversation(**row) for row in await cursor.fetchall()]

    async def rename(self, thread_id, title):
        async with self.transaction():
            cursor = await self.connection.execute(
                "UPDATE conversations SET title=%s,updated_at=%s WHERE thread_id=%s RETURNING *",
                (title, datetime.now(timezone.utc).isoformat(), thread_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
        return Conversation(**row)

    async def delete(self, thread_id):
        async with self.transaction():
            cursor = await self.connection.execute(
                "SELECT * FROM conversations WHERE thread_id=%s FOR UPDATE", (thread_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            cursor = await self.connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id=%s "
                "AND status IN ('queued','running','paused','recovering') LIMIT 1",
                (thread_id,),
            )
            if row["active_run_id"] or await cursor.fetchone():
                raise ChatError(
                    "conversation_busy", "会话有未结束的任务，请先完成或取消。", 409, True
                )
            await self.connection.execute(
                "INSERT INTO checkpoint_cleanup_queue (thread_id,created_at) "
                "SELECT run_id,%s FROM runs WHERE conversation_id=%s ON CONFLICT DO NOTHING",
                (datetime.now(timezone.utc).isoformat(), thread_id),
            )
            await self.connection.execute("DELETE FROM runs WHERE conversation_id=%s", (thread_id,))
            await self.connection.execute(
                "DELETE FROM conversations WHERE thread_id=%s", (thread_id,)
            )

    async def cleanup_checkpoints(self, checkpointer):
        async with self.database.read():
            cursor = await self.connection.execute("SELECT thread_id FROM checkpoint_cleanup_queue")
            ids = [row["thread_id"] for row in await cursor.fetchall()]
        for thread_id in ids:
            try:
                await checkpointer.adelete_thread(thread_id)
            except Exception:
                logger.warning("conversation_checkpoint_cleanup_failed")
                continue
            async with self.transaction():
                await self.connection.execute(
                    "DELETE FROM checkpoint_cleanup_queue WHERE thread_id=%s", (thread_id,)
                )
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT COUNT(*) AS n FROM checkpoint_cleanup_queue"
            )
            return (await cursor.fetchone())["n"]

    async def prune_unreferenced_runs(self, checkpointer):
        """查询 PostgreSQL Saver 的 checkpoints 表发现孤儿线程，再通过 Saver API 删除。"""
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id NOT IN "
                "(SELECT checkpoint_run_id FROM conversations WHERE checkpoint_run_id IS NOT NULL "
                "UNION SELECT run_id FROM runs "
                "UNION SELECT thread_id FROM checkpoint_cleanup_queue)"
            )
            ids = [row["thread_id"] for row in await cursor.fetchall()]
        for thread_id in ids:
            await checkpointer.adelete_thread(thread_id)

    async def checkpoint_config(self, thread_id: str):
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT checkpoint_run_id, checkpoint_id FROM conversations WHERE thread_id=%s",
                (thread_id,),
            )
            row = await cursor.fetchone()
        if row and row["checkpoint_run_id"]:
            return {
                "configurable": {
                    "thread_id": row["checkpoint_run_id"],
                    "checkpoint_id": row["checkpoint_id"],
                }
            }
        return None
