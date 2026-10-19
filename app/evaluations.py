"""Small, serial evaluations over the real retrieval/run services. No benchmark downloads."""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import ChatError
from app.evaluation_metrics import retrieval_metrics, summarize
from app.knowledge_bases import require_knowledge_base
from app.rag.retrieval import SearchRequest
from app.runs import complete_on_cancel
from app.schemas import RunRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])
FileID = Annotated[str, Field(min_length=1, max_length=128)]


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    expected: Literal["unlabelled", "answerable", "unanswerable"] = "unlabelled"
    relevant_file_ids: list[FileID] = Field(default_factory=list, max_length=100)
    required_facts: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def coherent(self):
        self.relevant_file_ids = sorted(set(self.relevant_file_ids))
        if (self.expected == "answerable") != bool(self.relevant_file_ids):
            raise ValueError("有答案标注必须选择相关文件；未标注和应拒答案例不填写相关文件。")
        return self


class EvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    idempotency_key: UUID
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    kind: Literal["retrieval", "rag"] = "retrieval"
    mode: Literal["keyword", "vector", "hybrid"] = "hybrid"
    k: int = Field(default=5, ge=1, le=20)
    file_ids: list[FileID] | None = Field(default=None, max_length=100)
    cases: list[EvalCase] = Field(min_length=1, max_length=20)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["pending", "pass", "partial", "fail"] = "pending"
    notes: str = Field(default="", max_length=2000)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class EvaluationService:
    def __init__(self, app, database):
        self.app, self.database = app, database
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.closing = False

    async def row(self, evaluation_id):
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=%s", (evaluation_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise ChatError("evaluation_not_found", "评测记录不存在。", 404)
        row["summary"] = summarize(row["results"], row["request"]["k"])
        row["summary"]["curves"] = []
        for k in (1, 3, 5, 10, 20):
            scored = [
                {
                    **result,
                    "metrics": retrieval_metrics(
                        result.get("sources", []), result["case"]["relevant_file_ids"], k
                    )
                    if result["status"] == "completed"
                    else None,
                }
                for result in row["results"]
            ]
            row["summary"]["curves"].append({"k": k, **summarize(scored, k)["metrics"]})
        return row

    async def update(self, evaluation_id, **fields):
        fields["updated_at"] = timestamp()
        values = [
            Jsonb(v) if key in ("results", "error") and v is not None else v
            for key, v in fields.items()
        ]
        async with self.database.transaction():
            await self.database.connection.execute(
                "UPDATE evaluations SET "
                + ",".join(key + "=%s" for key in fields)
                + " WHERE evaluation_id=%s",
                (*values, evaluation_id),
            )

    async def snapshot(self, body):
        await require_knowledge_base(self.database, body.knowledge_base_id)
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT file_id,name,content_hash,chunk_version,index_status "
                "FROM files WHERE kb_id=%s ORDER BY file_id",
                (body.knowledge_base_id,),
            )
            files = await cur.fetchall()
        allowed = {f["file_id"] for f in files}
        if body.file_ids is not None:
            if not set(body.file_ids) <= allowed:
                raise ChatError("invalid_file_scope", "检索范围包含已删除或其他库的文件。", 422)
            allowed &= set(body.file_ids)
        for case in body.cases:
            if not set(case.relevant_file_ids) <= allowed:
                raise ChatError("invalid_qrels", "标注相关文件必须位于本次检索范围内。", 422)
        settings = self.app.state.runtime.settings
        config = {
            key: value
            for key, value in settings.model_dump(mode="json").items()
            if not any(
                word in key for word in ("api_key", "database_url", "storage_path", "base_url")
            )
        }
        for key in ("llm_base_url", "embedding_base_url", "rerank_base_url"):
            config[key + "_sha256"] = digest(getattr(settings, key))
        code = hashlib.sha256()
        for path in sorted(Path(__file__).parent.rglob("*.py")):
            code.update(path.relative_to(Path(__file__).parent).as_posix().encode())
            code.update(path.read_bytes())
        snapshot = {
            "files": files,
            "config": config,
            "vector_version": self.app.state.vector_service.version,
            "lexical_version": self.app.state.lexical_index.version,
            "code_sha256": code.hexdigest(),
        }
        snapshot["fingerprint"] = digest(snapshot)
        return snapshot

    @complete_on_cancel
    async def create(self, body):
        async with self.lock:
            async with self.database.read():
                cur = await self.database.connection.execute(
                    "SELECT evaluation_id,request FROM evaluations WHERE idempotency_key=%s",
                    (str(body.idempotency_key),),
                )
                old = await cur.fetchone()
            request = body.model_dump(mode="json")
            if old:
                if old["request"] != request:
                    raise ChatError("idempotency_conflict", "此幂等键已用于其他评测配置。", 409)
                return await self.row(old["evaluation_id"])
            if self.closing or any(not task.done() for task in self.tasks.values()):
                raise ChatError(
                    "evaluation_busy", "当前有评测执行中，请完成或停止后重试。", 409, True
                )
            if body.kind == "rag" and not self.app.state.runtime.ready:
                raise ChatError("model_not_configured", "请先配置回答模型，或选择仅检索。", 503)
            snapshot = await self.snapshot(body)
            evaluation_id = str(uuid4())
            now = timestamp()
            async with self.database.transaction():
                await self.database.connection.execute(
                    "INSERT INTO evaluations(evaluation_id,idempotency_key,created_at,updated_at,"
                    "status,request,snapshot) "
                    "VALUES(%s,%s,%s,%s,'running',%s,%s)",
                    (
                        evaluation_id,
                        str(body.idempotency_key),
                        now,
                        now,
                        Jsonb(request),
                        Jsonb(snapshot),
                    ),
                )
            task = asyncio.create_task(self.execute(evaluation_id, body, snapshot))
            self.tasks[evaluation_id] = task
            task.add_done_callback(lambda done: self.done(evaluation_id, done))
            return await self.row(evaluation_id)

    def done(self, evaluation_id, task):
        self.tasks.pop(evaluation_id, None)
        if not task.cancelled() and task.exception():
            logger.warning("evaluation_worker_failed type=%s", type(task.exception()).__name__)

    @complete_on_cancel
    async def start_run(self, evaluation_id, body, case):
        conversation = await self.app.state.runtime.repository.create(purpose="evaluation")
        run = await self.app.state.run_service.create(
            str(conversation.thread_id),
            RunRequest(
                message=case.query,
                idempotency_key=str(uuid4()),
                knowledge_base_id=body.knowledge_base_id,
                retrieval_mode=body.mode,
                file_ids=body.file_ids,
                capture_trace=True,
            ),
            str(uuid4()),
        )
        run_id = str(run.run_id)
        try:
            await self.update(evaluation_id, active_run_id=run_id)
        except BaseException:
            await self.app.state.run_service.cancel(run_id)
            raise
        return run_id

    async def cancel_active(self, evaluation_id, active_run=None):
        run_id = active_run or (await self.row(evaluation_id))["active_run_id"]
        if run_id:
            try:
                await self.app.state.run_service.cancel(run_id)
            except ChatError as exc:
                if exc.code != "run_not_found":
                    raise
        await self.update(evaluation_id, active_run_id=None)

    async def execute(self, evaluation_id, body, snapshot):
        results = []
        active_run = None
        try:
            for case in body.cases:
                row = await self.row(evaluation_id)
                if row["stop_requested"]:
                    break
                current = await self.snapshot(body)
                if current["fingerprint"] != snapshot["fingerprint"]:
                    raise ChatError(
                        "evaluation_inputs_changed", "资料或模型配置已变化，请创建新评测。", 409
                    )
                result = {
                    "case": case.model_dump(),
                    "status": "running",
                    "sources": [],
                    "review": Review().model_dump(),
                }
                results.append(result)
                await self.update(evaluation_id, results=results)
                started = time.perf_counter()
                try:
                    if body.kind == "retrieval":
                        found = await self.app.state.retrieval_service.search(
                            body.knowledge_base_id,
                            SearchRequest(
                                query=case.query, mode=body.mode, top_k=20, file_ids=body.file_ids
                            ),
                            capture_trace=True,
                        )
                        result.update(
                            sources=[s.model_dump() for s in found.sources],
                            diagnostics=found.diagnostics,
                            query_plan={"queries": found.queries},
                        )
                    else:
                        active_run = await self.start_run(evaluation_id, body, case)
                        while True:
                            if (await self.row(evaluation_id))["stop_requested"]:
                                await self.app.state.run_service.cancel(active_run)
                            run_row = await self.app.state.run_service.inspect(active_run)
                            if run_row["status"] in ("completed", "failed", "cancelled"):
                                break
                            await asyncio.sleep(0.15)
                        final = self.app.state.run_service.repository.response(run_row).model_dump(
                            mode="json"
                        )
                        state = (
                            await self.app.state.run_service.get_graph().aget_state(
                                self.app.state.run_service.config(active_run)
                            )
                        ).values
                        result.update(
                            run=final,
                            sources=state.get("retrieved_sources", []),
                            context_sources=state.get("rag_sources", []),
                            diagnostics=state.get("retrieval_info", {}).get("diagnostics", {}),
                            assembly=state.get("assembly_diagnostics", {}),
                            query_plan={
                                "queries": state.get("search_queries", [case.query]),
                                "rerank_query": state.get("rerank_query"),
                                "degraded": state.get("query_degraded", False),
                            },
                        )
                        token_events = []
                        cursor = 0
                        while True:
                            events = await self.app.state.run_service.repository.events(
                                active_run, cursor
                            )
                            if not events:
                                break
                            token_events.extend(
                                data for _, event, data in events if event == "token_usage"
                            )
                            cursor = events[-1][0]
                        result["token_usage"] = token_events
                        if final["status"] != "completed":
                            result.update(status=final["status"], error=final.get("error"))
                        active_run = None
                        await self.update(evaluation_id, active_run_id=None)
                    if result["status"] == "running":
                        result["status"] = "completed"
                    # Inference inputs must remain fixed for comparable labels and corpus metadata.
                    if (await self.snapshot(body))["fingerprint"] != snapshot["fingerprint"]:
                        raise ChatError(
                            "evaluation_inputs_changed",
                            "执行期间资料或配置已变化，本题结果不计入成功。",
                            409,
                        )
                    result["metrics"] = (
                        retrieval_metrics(result["sources"], case.relevant_file_ids, body.k)
                        if result["status"] == "completed"
                        else None
                    )
                except ChatError as exc:
                    result.update(
                        status="failed", error={"code": exc.code, "message": str(exc)}, metrics=None
                    )
                except Exception:
                    logger.exception("evaluation_case_failed")
                    result.update(
                        status="failed",
                        error={
                            "code": "evaluation_failed",
                            "message": "执行失败，请检查服务状态。",
                        },
                        metrics=None,
                    )
                if active_run:
                    await self.cancel_active(evaluation_id, active_run)
                    active_run = None
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                await self.update(evaluation_id, results=results)
            stopped = (await self.row(evaluation_id))["stop_requested"]
            await self.update(
                evaluation_id, status="stopped" if stopped else "completed", active_run_id=None
            )
        except asyncio.CancelledError:
            await self.cancel_active(evaluation_id, active_run)
            if results and results[-1]["status"] == "running":
                results[-1]["status"] = "cancelled"
            await self.update(evaluation_id, status="stopped", results=results, active_run_id=None)
        except Exception as exc:
            await self.cancel_active(evaluation_id, active_run)
            error = {
                "code": getattr(exc, "code", "evaluation_failed"),
                "message": str(exc) if isinstance(exc, ChatError) else "评测执行中断。",
            }
            await self.update(
                evaluation_id, status="failed", results=results, active_run_id=None, error=error
            )

    async def stop(self, evaluation_id):
        async with self.lock:
            row = await self.row(evaluation_id)
            if row["status"] == "running":
                await self.update(evaluation_id, stop_requested=True)
                task = self.tasks.get(evaluation_id)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                latest = await self.row(evaluation_id)
                if latest["status"] == "running":
                    await self.cancel_active(evaluation_id)
                    for result in latest["results"]:
                        if result["status"] == "running":
                            result["status"] = "cancelled"
                    await self.update(evaluation_id, status="stopped", results=latest["results"])
            return await self.row(evaluation_id)

    async def setup(self):
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT * FROM evaluations WHERE status='running'"
            )
            interrupted = await cur.fetchall()
        for row in interrupted:
            if row["active_run_id"]:
                try:
                    await self.app.state.run_service.cancel(row["active_run_id"])
                except ChatError:
                    pass
            for result in row["results"]:
                if result["status"] == "running":
                    result["status"] = "cancelled"
            await self.update(
                row["evaluation_id"],
                status="interrupted",
                active_run_id=None,
                results=row["results"],
            )

    async def close(self):
        self.closing = True
        for evaluation_id in list(self.tasks):
            await self.stop(evaluation_id)


@router.post("", status_code=202)
async def create_evaluation(body: EvalRequest, request: Request):
    return await request.app.state.evaluations.create(body)


@router.get("")
async def list_evaluations(request: Request):
    service = request.app.state.evaluations
    async with service.database.read():
        cur = await service.database.connection.execute(
            "SELECT evaluation_id,status,created_at,request->>'knowledge_base_id' AS kb_id,"
            "request->>'kind' AS kind FROM evaluations ORDER BY created_at DESC LIMIT 30"
        )
        return await cur.fetchall()


@router.get("/{evaluation_id}")
async def get_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.row(str(evaluation_id))


@router.post("/{evaluation_id}/stop")
async def stop_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.stop(str(evaluation_id))


@router.patch("/{evaluation_id}/reviews/{index}")
async def review_evaluation(evaluation_id: UUID, index: int, body: Review, request: Request):
    service = request.app.state.evaluations
    async with service.lock:
        row = await service.row(str(evaluation_id))
        if row["status"] == "running":
            raise ChatError("evaluation_busy", "请在评测结束后保存人工评价。", 409)
        if not 0 <= index < len(row["results"]):
            raise ChatError("case_not_found", "案例不存在。", 404)
        row["results"][index]["review"] = body.model_dump()
        await service.update(str(evaluation_id), results=row["results"])
    return await service.row(str(evaluation_id))


@router.get("/{evaluation_id}/export")
async def export_evaluation(evaluation_id: UUID, request: Request):
    row = await request.app.state.evaluations.row(str(evaluation_id))
    return Response(
        json.dumps(row, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="evaluation-{evaluation_id}.json"',
            "Cache-Control": "no-store",
        },
    )
