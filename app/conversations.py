import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite

from app.schemas import Conversation


class ConversationNotFoundError(RuntimeError):
    pass


class ConversationConflictError(RuntimeError):
    pass


class ConversationRepository:
    """Session metadata; messages and tool states live in LangGraph checkpoints."""

    def __init__(self, connection: aiosqlite.Connection):
        self.connection = connection
        self.lock = asyncio.Lock()

    async def setup(self):
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA busy_timeout = 5000")
        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            "thread_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ('ready', 'running', 'failed')))"
        )
        await self.connection.execute(
            "UPDATE conversations SET status='failed' WHERE status='running'"
        )
        await self.connection.commit()

    async def create(self) -> Conversation:
        now = datetime.now(timezone.utc).isoformat()
        item = Conversation(thread_id=uuid4(), created_at=now, updated_at=now, status="ready")
        async with self.lock:
            await self.connection.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?)",
                (str(item.thread_id), now, now, "ready"),
            )
            await self.connection.commit()
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

    async def claim(self, thread_id: str):
        async with self.lock:
            cursor = await self.connection.execute(
                "UPDATE conversations SET status='running' WHERE thread_id=? AND status='ready'",
                (thread_id,),
            )
            await self.connection.commit()
            changed = cursor.rowcount
            await cursor.close()
        if not changed:
            item = await self.get(thread_id)
            if item.status == "failed":
                raise ConversationConflictError("上次调用未完成，请新建会话；失败历史仍可查看。")
            raise ConversationConflictError("该会话正在处理请求，请等待完成。")

    async def finish(self, thread_id: str, status: str):
        async with self.lock:
            await self.connection.execute(
                "UPDATE conversations SET status=?, updated_at=? WHERE thread_id=?",
                (status, datetime.now(timezone.utc).isoformat(), thread_id),
            )
            await self.connection.commit()
