import json
from datetime import datetime, timezone
from uuid import uuid4

from app.conversations import ConversationRepository
from app.errors import ChatError
from app.schemas import RunRequest, RunResponse

GRAPH_VERSION = "chat-v1"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# Roll elapsed time forward from the execution start (including downtime) and
# clear it, so restarts cannot reset the accumulated run budget.
_ELAPSED_ROLLOVER = (
    "elapsed_seconds=elapsed_seconds+CASE WHEN execution_started_at IS NOT NULL "
    "THEN MAX(0,(julianday('now')-julianday(execution_started_at))*86400) ELSE 0 END, "
    "execution_started_at=NULL"
)


def now():
    return datetime.now(timezone.utc).isoformat()


class RunRepository:
    """Share the conversation connection and lock for atomic ownership/publication."""

    def __init__(self, conversations: ConversationRepository):
        self.connection = conversations.connection
        self.lock = conversations.lock
        self.transaction = conversations.transaction

    async def setup(self):
        async with self.transaction():
            schema = (
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, "
                "idempotency_key TEXT NOT NULL, message TEXT NOT NULL, base_run_id TEXT, "
                "graph_version TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "pause_requested INTEGER NOT NULL DEFAULT 0, "
                "model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0, "
                "elapsed_seconds REAL NOT NULL DEFAULT 0, event_seq INTEGER NOT NULL DEFAULT 0, "
                "execution_started_at TEXT, "
                "answer TEXT, tools_used TEXT NOT NULL DEFAULT '[]', error TEXT, "
                "UNIQUE(conversation_id, idempotency_key));",
                "CREATE TABLE IF NOT EXISTS run_events ("
                "run_id TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, "
                "data TEXT NOT NULL, PRIMARY KEY(run_id, seq));",
                "CREATE INDEX IF NOT EXISTS runs_conversation "
                "ON runs(conversation_id, created_at);",
            )
            for statement in schema:
                await self.connection.execute(statement)
            async with self.connection.execute("PRAGMA table_info(runs)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            if "execution_started_at" not in columns:
                await self.connection.execute(
                    "ALTER TABLE runs ADD COLUMN execution_started_at TEXT"
                )
            # Step 5 will schedule automatic recovery. For now expose resumable
            # tasks and keep their checkpoints and conversation ownership intact.
            await self.connection.execute(
                f"UPDATE runs SET status='recovering', {_ELAPSED_ROLLOVER} "
                "WHERE status IN ('queued','running')"
            )

    async def _row(self, run_id):
        async with self.connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ChatError("run_not_found", "任务不存在。", 404)
        return dict(row)

    @staticmethod
    def response(row):
        return RunResponse(
            **{
                **row,
                "tools_used": json.loads(row["tools_used"]),
                "error": json.loads(row["error"]) if row["error"] else None,
            }
        )

    async def get(self, run_id):
        async with self.lock:
            return await self._row(run_id)

    async def recovering(self):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM runs WHERE status='recovering'"
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def _event(self, row, event, data):
        seq = row["event_seq"] + 1
        payload = {
            **data,
            "run_id": row["run_id"],
            "conversation_id": row["conversation_id"],
            "thread_id": row["conversation_id"],
            "request_id": row["request_id"],
            "event_seq": seq,
        }
        await self.connection.execute(
            "INSERT INTO run_events VALUES (?, ?, ?, ?)",
            (row["run_id"], seq, event, json.dumps(payload, ensure_ascii=False)),
        )
        timestamp = now()
        await self.connection.execute(
            "UPDATE runs SET event_seq=?, updated_at=? WHERE run_id=?",
            (seq, timestamp, row["run_id"]),
        )
        row.update(event_seq=seq, updated_at=timestamp)

    async def event(self, run_id, event, data):
        async with self.transaction():
            row = await self._row(run_id)
            await self._event(row, event, data)

    async def events(self, run_id, after, limit=100):
        async with self.lock:
            async with self.connection.execute(
                "SELECT seq, event, data FROM run_events WHERE run_id=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            ) as cursor:
                return [
                    (r["seq"], r["event"], json.loads(r["data"])) for r in await cursor.fetchall()
                ]

    async def last_message_id(self, run_id):
        async with self.lock:
            async with self.connection.execute(
                "SELECT data FROM run_events WHERE run_id=? AND event='message_start' "
                "ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return json.loads(row["data"])["message_id"] if row else None

    async def create(self, conversation_id, body: RunRequest, request_id):
        async with self.transaction():
            async with self.connection.execute(
                "SELECT * FROM runs WHERE conversation_id=? AND idempotency_key=?",
                (conversation_id, body.idempotency_key),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                if existing["message"] != body.message:
                    raise ChatError("idempotency_conflict", "幂等键已用于其他消息。", 409)
                return dict(existing), False
            async with self.connection.execute(
                "SELECT * FROM conversations WHERE thread_id=?", (conversation_id,)
            ) as cursor:
                conversation = await cursor.fetchone()
            if conversation is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            run_id = str(uuid4())
            cursor = await self.connection.execute(
                "UPDATE conversations SET status='running', active_run_id=?, request_id=? "
                "WHERE thread_id=? AND active_run_id IS NULL AND status IN ('ready','failed')",
                (run_id, request_id, conversation_id),
            )
            if not cursor.rowcount:
                raise ChatError("conversation_busy", "会话已有未结束的任务。", 409, True)
            timestamp = now()
            await self.connection.execute(
                "INSERT INTO runs (run_id, conversation_id, request_id, idempotency_key, "
                "message, base_run_id, graph_version, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    run_id,
                    conversation_id,
                    request_id,
                    body.idempotency_key,
                    body.message,
                    conversation["checkpoint_run_id"],
                    GRAPH_VERSION,
                    timestamp,
                    timestamp,
                ),
            )
            row = await self._row(run_id)
            await self._event(row, "start", {"status": "queued"})
            return row, True

    async def set_running(self, run_id):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE runs SET status='running', updated_at=?, execution_started_at=? "
                "WHERE run_id=? AND status='queued'",
                (now(), now(), run_id),
            )

    async def mark_recovering(self, run_id):
        async with self.transaction():
            row = await self._row(run_id)
            if row["status"] in ("queued", "running"):
                await self.connection.execute(
                    f"UPDATE runs SET status='recovering', updated_at=?, {_ELAPSED_ROLLOVER} "
                    "WHERE run_id=?",
                    (now(), run_id),
                )
                await self._event(row, "recovery_required", {"status": "recovering"})

    async def pause(self, run_id):
        async with self.transaction():
            row = await self._row(run_id)
            if row["status"] == "paused":
                return row
            if row["status"] not in ("queued", "running"):
                raise ChatError("run_not_running", "只有执行中的任务可以请求暂停。", 409)
            timestamp = now()
            await self.connection.execute(
                "UPDATE runs SET pause_requested=1, updated_at=? WHERE run_id=?",
                (timestamp, run_id),
            )
            row.update(pause_requested=1, updated_at=timestamp)
            return row

    async def paused(self, run_id):
        async with self.transaction():
            row = await self._row(run_id)
            await self.connection.execute(
                "UPDATE runs SET status='paused', updated_at=? WHERE run_id=?", (now(), run_id)
            )
            await self._event(row, "paused", {"status": "paused"})

    async def reserve_call(self, run_id, kind, maximum):
        column = {"model": "model_calls", "tool": "tool_calls"}[kind]
        async with self.transaction():
            row = await self._row(run_id)
            if row[column] >= maximum:
                raise ChatError("agent_limit_exceeded", "任务达到调用上限。", 429)
            value = row[column] + 1
            await self.connection.execute(
                f"UPDATE runs SET {column}=? WHERE run_id=?", (value, run_id)
            )
            return value

    async def add_elapsed(self, run_id, seconds):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE runs SET elapsed_seconds=elapsed_seconds+?, execution_started_at=NULL "
                "WHERE run_id=?",
                (seconds, run_id),
            )

    async def resume(self, run_id, reset_message=None):
        async with self.transaction():
            row = await self._row(run_id)
            if row["graph_version"] != GRAPH_VERSION:
                raise ChatError("graph_version_mismatch", "任务图版本不兼容。", 409)
            if row["status"] not in ("paused", "recovering", "failed"):
                raise ChatError("run_not_resumable", "当前任务状态不能恢复。", 409)
            if row["status"] == "failed" and not json.loads(row["error"] or "{}").get("retryable"):
                raise ChatError("run_not_resumable", "该错误不能直接恢复，请创建新任务。", 409)
            cursor = await self.connection.execute(
                "UPDATE conversations SET active_run_id=?, status='running', request_id=? "
                "WHERE thread_id=? AND checkpoint_run_id IS ? "
                "AND ((active_run_id=? AND status='running') OR "
                "(active_run_id IS NULL AND status IN ('ready','failed')))",
                (run_id, row["request_id"], row["conversation_id"], row["base_run_id"], run_id),
            )
            if not cursor.rowcount:
                raise ChatError("resume_conflict", "会话正在执行其他任务或历史已发生变化。", 409)
            await self.connection.execute(
                "UPDATE runs SET status='queued', pause_requested=0, "
                "error=NULL, updated_at=? WHERE run_id=?",
                (now(), run_id),
            )
            row.update(status="queued", pause_requested=0, error=None)
            await self._event(row, "resumed", {"status": "queued"})
            if reset_message:
                await self._event(
                    row,
                    "message_reset",
                    {
                        "message_id": reset_message,
                        "reason": "node_retry",
                    },
                )
            return row

    async def finish(
        self,
        run_id,
        status,
        *,
        checkpoint_id=None,
        answer=None,
        tools_used=None,
        error=None,
        elapsed=0,
    ):
        async with self.transaction():
            row = await self._row(run_id)
            if row["status"] in TERMINAL_STATUSES:
                return
            timestamp = now()
            if status == "completed":
                cursor = await self.connection.execute(
                    "UPDATE conversations SET status='ready', active_run_id=NULL, "
                    "checkpoint_run_id=?, checkpoint_id=?, last_error_code=NULL, updated_at=? "
                    "WHERE thread_id=? AND active_run_id=? AND checkpoint_run_id IS ?",
                    (
                        run_id,
                        checkpoint_id,
                        timestamp,
                        row["conversation_id"],
                        run_id,
                        row["base_run_id"],
                    ),
                )
                if not cursor.rowcount:
                    raise ChatError("publication_conflict", "会话历史发布发生冲突。", 409)
                event, data = (
                    "done",
                    {"answer": answer, "tools_used": tools_used or [], "status": status},
                )
            else:
                await self.connection.execute(
                    "UPDATE conversations SET status='failed', active_run_id=NULL, "
                    "last_error_code=?, updated_at=? WHERE thread_id=? AND active_run_id=?",
                    (
                        (error or {}).get("code", "request_cancelled"),
                        timestamp,
                        row["conversation_id"],
                        run_id,
                    ),
                )
                event = "cancelled" if status == "cancelled" else "error"
                data = {**(error or {}), "status": status}
            await self.connection.execute(
                "UPDATE runs SET status=?, answer=?, tools_used=?, error=?, updated_at=?, "
                "elapsed_seconds=elapsed_seconds+?, execution_started_at=NULL WHERE run_id=?",
                (
                    status,
                    answer,
                    json.dumps(tools_used or []),
                    json.dumps(error) if error else None,
                    timestamp,
                    elapsed,
                    run_id,
                ),
            )
            await self._event(row, event, data)
