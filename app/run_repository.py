import json
from datetime import datetime, timezone
from uuid import uuid4

from app.conversations import ConversationRepository
from app.errors import ChatError
from app.knowledge_bases import require_knowledge_base
from app.schemas import ConversationTurn, ConversationTurns, RunRequest, RunResponse

GRAPH_VERSION = "chat-rag-v6"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
# Roll elapsed time forward from the execution start (including downtime) and
# clear it, so restarts cannot reset the accumulated run budget.
_ELAPSED_ROLLOVER = (
    "elapsed_seconds=elapsed_seconds+CASE WHEN execution_started_at IS NOT NULL "
    "THEN GREATEST(0,EXTRACT(EPOCH FROM "
    "(clock_timestamp()-execution_started_at::timestamptz))) ELSE 0 END, "
    "execution_started_at=NULL"
)


def now():
    return datetime.now(timezone.utc).isoformat()


class RunRepository:
    """Use scoped PostgreSQL transactions for atomic ownership and publication."""

    def __init__(self, conversations: ConversationRepository):
        self.connection = conversations.connection
        self.database = conversations.database
        self.transaction = conversations.transaction

    async def setup(self):
        async with self.transaction():
            await self.connection.execute(
                f"UPDATE runs SET status='recovering', {_ELAPSED_ROLLOVER} "
                "WHERE status IN ('queued','running')"
            )

    async def _row(self, run_id):
        cursor = await self.connection.execute(
            "SELECT * FROM runs WHERE run_id=%s"
            + (" FOR UPDATE" if self.connection.in_transaction else ""),
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ChatError("run_not_found", "任务不存在。", 404)
        return row

    @staticmethod
    def response(row):
        return RunResponse(
            **{
                **row,
                "tools_used": json.loads(row["tools_used"]),
                "sources": json.loads(row["sources"]),
                "error": json.loads(row["error"]) if row["error"] else None,
                "answer_segments": json.loads(row.get("answer_segments", "[]")),
                "file_ids": json.loads(row["file_ids"]) if row.get("file_ids") else None,
            }
        )

    async def get(self, run_id):
        async with self.database.read():
            return await self._row(run_id)

    async def same_scope_turns(self, conversation_id, kb_id):
        """从已提交链倒序数连续同库轮次，切库后不捞取更早的同库答案。"""
        async with self.database.read():
            cursor = await self.connection.execute(
                "WITH RECURSIVE chain AS ("
                "SELECT r.run_id,r.base_run_id,r.knowledge_base_id,0 AS depth FROM runs r "
                "JOIN conversations c ON c.checkpoint_run_id=r.run_id WHERE c.thread_id=%s "
                "UNION ALL SELECT r.run_id,r.base_run_id,r.knowledge_base_id,c.depth+1 "
                "FROM runs r JOIN chain c ON r.run_id=c.base_run_id WHERE r.conversation_id=%s) "
                "SELECT knowledge_base_id FROM chain ORDER BY depth",
                (conversation_id, conversation_id),
            )
            count = 0
            for row in await cursor.fetchall():
                if row["knowledge_base_id"] != kb_id:
                    break
                count += 1
            return count

    async def turns(self, conversation_id, limit, offset):
        """Return durable attempts and their membership in the committed checkpoint chain."""
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT COUNT(*) AS total FROM runs WHERE conversation_id=%s", (conversation_id,)
            )
            total = (await cursor.fetchone())["total"]
            cursor = await self.connection.execute(
                "WITH RECURSIVE committed AS ("
                "SELECT run_id, base_run_id FROM runs WHERE conversation_id=%s AND run_id="
                "(SELECT checkpoint_run_id FROM conversations WHERE thread_id=%s) "
                "UNION SELECT r.run_id, r.base_run_id FROM runs r JOIN committed c "
                "ON r.run_id=c.base_run_id WHERE r.conversation_id=%s) "
                "SELECT r.*, r.run_id IN (SELECT run_id FROM committed) AS committed "
                "FROM runs r WHERE conversation_id=%s "
                "ORDER BY created_at DESC, run_id DESC LIMIT %s OFFSET %s",
                (conversation_id, conversation_id, conversation_id, conversation_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return ConversationTurns(
            items=[
                ConversationTurn(
                    **self.response(row).model_dump(),
                    message=row["message"],
                    committed=row["committed"],
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def recovering(self):
        async with self.database.read():
            cursor = await self.connection.execute("SELECT * FROM runs WHERE status='recovering'")
            return await cursor.fetchall()

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
            "INSERT INTO run_events VALUES (%s, %s, %s, %s)",
            (row["run_id"], seq, event, json.dumps(payload, ensure_ascii=False)),
        )
        timestamp = now()
        await self.connection.execute(
            "UPDATE runs SET event_seq=%s, updated_at=%s WHERE run_id=%s",
            (seq, timestamp, row["run_id"]),
        )
        row.update(event_seq=seq, updated_at=timestamp)

    async def event(self, run_id, event, data):
        async with self.transaction():
            row = await self._row(run_id)
            await self._event(row, event, data)

    async def events(self, run_id, after, limit=100):
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT seq, event, data FROM run_events WHERE run_id=%s AND seq>%s "
                "ORDER BY seq LIMIT %s",
                (run_id, after, limit),
            )
            return [(r["seq"], r["event"], json.loads(r["data"])) for r in await cursor.fetchall()]

    async def last_message_id(self, run_id):
        async with self.database.read():
            cursor = await self.connection.execute(
                "SELECT data FROM run_events WHERE run_id=%s AND event='message_start' "
                "ORDER BY seq DESC LIMIT 1",
                (run_id,),
            )
            row = await cursor.fetchone()
        return json.loads(row["data"])["message_id"] if row else None

    async def create(self, conversation_id, body: RunRequest, request_id):
        async with self.transaction():
            await self.connection.execute(
                "SELECT thread_id FROM conversations WHERE thread_id=%s FOR UPDATE",
                (conversation_id,),
            )
            cursor = await self.connection.execute(
                "SELECT * FROM runs WHERE conversation_id=%s AND idempotency_key=%s",
                (conversation_id, body.idempotency_key),
            )
            existing = await cursor.fetchone()
            if existing:
                if (
                    existing["message"],
                    existing["knowledge_base_id"],
                    existing["retrieval_mode"],
                    existing.get("capture_trace", False),
                    json.loads(existing["file_ids"]) if existing.get("file_ids") else None,
                ) != (
                    body.message,
                    body.knowledge_base_id,
                    body.retrieval_mode,
                    body.capture_trace,
                    body.file_ids,
                ):
                    raise ChatError("idempotency_conflict", "幂等键已用于其他消息。", 409)
                return dict(existing), False
            if body.knowledge_base_id:
                await require_knowledge_base(self.database, body.knowledge_base_id, lock=True)
                if body.file_ids:
                    cursor = await self.connection.execute(
                        "SELECT file_id FROM files WHERE kb_id=%s AND file_id=ANY(%s)",
                        (body.knowledge_base_id, body.file_ids),
                    )
                    if {row["file_id"] for row in await cursor.fetchall()} != set(body.file_ids):
                        raise ChatError(
                            "invalid_file_scope",
                            "所选文件不存在或不属于当前知识库，请重新选择。",
                            422,
                        )
            cursor = await self.connection.execute(
                "SELECT * FROM conversations WHERE thread_id=%s", (conversation_id,)
            )
            conversation = await cursor.fetchone()
            if conversation is None:
                raise ChatError("conversation_not_found", "会话不存在。", 404)
            run_id = str(uuid4())
            cursor = await self.connection.execute(
                "UPDATE conversations SET status='running', active_run_id=%s, request_id=%s "
                "WHERE thread_id=%s AND active_run_id IS NULL AND status IN ('ready','failed')",
                (run_id, request_id, conversation_id),
            )
            if not cursor.rowcount:
                raise ChatError("conversation_busy", "会话已有未结束的任务。", 409, True)
            timestamp = now()
            await self.connection.execute(
                "INSERT INTO runs (run_id, conversation_id, request_id, idempotency_key, "
                "message, base_run_id, graph_version, status, created_at, updated_at, "
                "knowledge_base_id, retrieval_mode, file_ids, capture_trace) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s)",
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
                    body.knowledge_base_id,
                    body.retrieval_mode,
                    json.dumps(body.file_ids) if body.file_ids is not None else None,
                    body.capture_trace,
                ),
            )
            row = await self._row(run_id)
            await self._event(row, "start", {"status": "queued"})
            return row, True

    async def set_running(self, run_id):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE runs SET status='running', updated_at=%s, execution_started_at=%s "
                "WHERE run_id=%s AND status='queued'",
                (now(), now(), run_id),
            )

    async def mark_recovering(self, run_id):
        async with self.transaction():
            row = await self._row(run_id)
            if row["status"] in ("queued", "running"):
                await self.connection.execute(
                    f"UPDATE runs SET status='recovering', updated_at=%s, {_ELAPSED_ROLLOVER} "
                    "WHERE run_id=%s",
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
                "UPDATE runs SET pause_requested=1, updated_at=%s WHERE run_id=%s",
                (timestamp, run_id),
            )
            row.update(pause_requested=1, updated_at=timestamp)
            return row

    async def paused(self, run_id):
        async with self.transaction():
            row = await self._row(run_id)
            await self.connection.execute(
                "UPDATE runs SET status='paused', updated_at=%s WHERE run_id=%s", (now(), run_id)
            )
            await self._event(row, "paused", {"status": "paused"})

    async def reserve_call(self, run_id, kind, maximum):
        column = {"model": "model_calls", "repair": "model_calls", "tool": "tool_calls"}[kind]
        async with self.transaction():
            row = await self._row(run_id)
            if kind == "repair" and row["answer_repair_calls"] >= 1:
                raise ChatError(
                    "citation_repair_exhausted", "引用修复次数已用尽，请重新提问。", 409
                )
            if row[column] >= maximum:
                raise ChatError("agent_limit_exceeded", "任务达到调用上限。", 429)
            value = row[column] + 1
            await self.connection.execute(
                f"UPDATE runs SET {column}=%s"
                + (", answer_repair_calls=1" if kind == "repair" else "")
                + " WHERE run_id=%s",
                (value, run_id),
            )
            return value

    async def add_elapsed(self, run_id, seconds):
        async with self.transaction():
            await self.connection.execute(
                "UPDATE runs SET elapsed_seconds=elapsed_seconds+%s, execution_started_at=NULL "
                "WHERE run_id=%s",
                (seconds, run_id),
            )

    async def resume(self, run_id, reset_message=None):
        async with self.transaction():
            row = await self._row(run_id)
            if row["knowledge_base_id"]:
                await require_knowledge_base(self.database, row["knowledge_base_id"], lock=True)
            if row["graph_version"] != GRAPH_VERSION:
                raise ChatError("graph_version_mismatch", "任务图版本不兼容。", 409)
            if row["status"] not in ("paused", "recovering", "failed"):
                raise ChatError("run_not_resumable", "当前任务状态不能恢复。", 409)
            if row["status"] == "failed" and not json.loads(row["error"] or "{}").get("retryable"):
                raise ChatError("run_not_resumable", "该错误不能直接恢复，请创建新任务。", 409)
            cursor = await self.connection.execute(
                "UPDATE conversations SET active_run_id=%s, status='running', request_id=%s "
                "WHERE thread_id=%s AND checkpoint_run_id IS NOT DISTINCT FROM %s "
                "AND ((active_run_id=%s AND status='running') OR "
                "(active_run_id IS NULL AND status IN ('ready','failed')))",
                (run_id, row["request_id"], row["conversation_id"], row["base_run_id"], run_id),
            )
            if not cursor.rowcount:
                raise ChatError("resume_conflict", "会话正在执行其他任务或历史已发生变化。", 409)
            await self.connection.execute(
                "UPDATE runs SET status='queued', pause_requested=0, "
                "error=NULL, updated_at=%s WHERE run_id=%s",
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
        sources=None,
        answer_status=None,
        answer_reason=None,
        answer_segments=None,
        answer_protocol=None,
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
                    "checkpoint_run_id=%s, checkpoint_id=%s, last_error_code=NULL, updated_at=%s "
                    "WHERE thread_id=%s AND active_run_id=%s AND checkpoint_run_id IS NOT "
                    "DISTINCT FROM %s",
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
                    {
                        "answer": answer,
                        "tools_used": tools_used or [],
                        "sources": sources or [],
                        "status": status,
                        "answer_status": answer_status,
                        "answer_reason": answer_reason,
                        "answer_segments": answer_segments or [],
                        "answer_protocol": answer_protocol,
                    },
                )
            else:
                await self.connection.execute(
                    "UPDATE conversations SET status='failed', active_run_id=NULL, "
                    "last_error_code=%s, updated_at=%s WHERE thread_id=%s AND active_run_id=%s",
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
                "UPDATE runs SET status=%s, answer=%s, tools_used=%s, sources=%s, error=%s, "
                "updated_at=%s, answer_status=%s, answer_reason=%s, answer_segments=%s, "
                "answer_protocol=%s, "
                "elapsed_seconds=elapsed_seconds+%s, execution_started_at=NULL WHERE run_id=%s",
                (
                    status,
                    answer,
                    json.dumps(tools_used or []),
                    json.dumps(sources or []),
                    json.dumps(error) if error else None,
                    timestamp,
                    answer_status,
                    answer_reason,
                    json.dumps(answer_segments or [], ensure_ascii=False),
                    answer_protocol,
                    elapsed,
                    run_id,
                ),
            )
            await self._event(row, event, data)
