import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.schemas import Conversation


class ConversationNotFoundError(RuntimeError):
    pass


class ConversationRepository:
    """Session metadata; messages and tool states live in LangGraph checkpoints."""

    def __init__(self, connection: aiosqlite.Connection):
        self.connection = connection
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                yield
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def setup(self):
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        async with self.transaction():
            await self.connection.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "thread_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK (status IN ('ready', 'running', 'failed')))"
            )
            async with self.connection.execute("PRAGMA table_info(conversations)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            for name in (
                "checkpoint_run_id",
                "checkpoint_id",
                "last_error_code",
                "request_id",
                "active_run_id",
            ):
                if name not in columns:
                    await self.connection.execute(
                        f"ALTER TABLE conversations ADD COLUMN {name} TEXT"
                    )
            async with self.connection.execute(
                "SELECT thread_id FROM conversations WHERE checkpoint_run_id IS NULL"
            ) as cursor:
                self.legacy_ids = [row["thread_id"] for row in await cursor.fetchall()]
            await self.connection.execute(
                "UPDATE conversations SET status='failed', last_error_code='process_interrupted' "
                "WHERE status='running' AND active_run_id IS NULL"
            )

    async def create(self) -> Conversation:
        now = datetime.now(timezone.utc).isoformat()
        item = Conversation(thread_id=uuid4(), created_at=now, updated_at=now, status="ready")
        async with self.transaction():
            await self.connection.execute(
                "INSERT INTO conversations (thread_id, created_at, updated_at, status) "
                "VALUES (?, ?, ?, ?)",
                (str(item.thread_id), now, now, "ready"),
            )
        return item

    async def get(self, thread_id: str) -> Conversation:
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM conversations WHERE thread_id=?", (thread_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise ConversationNotFoundError("会话不存在。")
        return Conversation(**dict(row))

    async def list(self, limit: int, offset: int) -> list[Conversation]:
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cursor:
                return [Conversation(**dict(row)) for row in await cursor.fetchall()]

    async def prune_unreferenced_runs(self):
        """Startup only: no live requests exist after legacy checkpoint migration."""
        async with self.transaction():
            for table in ("writes", "checkpoints"):
                await self.connection.execute(
                    f"DELETE FROM {table} WHERE thread_id NOT IN "
                    "(SELECT checkpoint_run_id FROM conversations "
                    "WHERE checkpoint_run_id IS NOT NULL UNION SELECT run_id FROM runs)"
                )

    async def checkpoint_config(self, thread_id: str):
        async with self.lock:
            async with self.connection.execute(
                "SELECT checkpoint_run_id, checkpoint_id FROM conversations WHERE thread_id=?",
                (thread_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row and row["checkpoint_run_id"]:
            return {
                "configurable": {
                    "thread_id": row["checkpoint_run_id"],
                    "checkpoint_id": row["checkpoint_id"],
                }
            }
        return None

    async def migrate_checkpoint(self, thread_id: str, checkpoint_id: str):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE conversations SET checkpoint_run_id=?, checkpoint_id=? WHERE thread_id=?",
                (thread_id, checkpoint_id, thread_id),
            )
