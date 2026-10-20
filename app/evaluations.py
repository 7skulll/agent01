"""Small, serial evaluations over the real retrieval/run services. No benchmark downloads."""

import asyncio
import hashlib
import json
import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.errors import ChatError
from app.evaluation_evidence import evidence_report
from app.evaluation_metrics import case_metrics, judgment_coverage, summarize
from app.evaluation_models import EvalRequest, Review
from app.evaluation_quality import quality_summary
from app.knowledge_bases import require_knowledge_base
from app.rag.retrieval import SearchRequest
from app.runs import complete_on_cancel
from app.schemas import RunRequest

logger = logging.getLogger(__name__)


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

    async def stored(self, evaluation_id):
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=%s", (evaluation_id,)
            )
            row = await cur.fetchone()
        if row is None:
            raise ChatError("evaluation_not_found", "评测记录不存在。", 404)
        return row

    async def row(self, evaluation_id, tag=None):
        row = await self.stored(evaluation_id)
        selected = [r for r in row["results"] if not tag or tag in r["case"].get("tags", [])]
        row["filter_tag"] = tag
        row["quality"] = quality_summary(selected)
        row["summary"] = summarize(selected, row["request"]["k"], kind=row["request"]["kind"])
        row["summary"]["curves"] = []
        for k in (1, 3, 5, 10, 20):
            scored = [
                {
                    **result,
                    "metrics": case_metrics(result.get("sources", []), result["case"], k)
                    if result["status"] == "completed"
                    else None,
                }
                for result in selected
            ]
            row["summary"]["curves"].append({"k": k, **summarize(scored, k)["metrics"]})
        return row

    async def control(self, evaluation_id):
        """Worker polling needs no candidate snapshots or derived metrics."""
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT status,stop_requested,active_run_id FROM evaluations "
                "WHERE evaluation_id=%s",
                (evaluation_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise ChatError("evaluation_not_found", "评测记录不存在。", 404)
        return row

    async def update(self, evaluation_id, **fields):
        fields["updated_at"] = timestamp()
        values = [
            Jsonb(v)
            if key in ("results", "error", "owned_conversation_ids") and v is not None
            else v
            for key, v in fields.items()
        ]
        async with self.database.transaction():
            await self.database.connection.execute(
                "UPDATE evaluations SET "
                + ",".join(key + "=%s" for key in fields)
                + " WHERE evaluation_id=%s",
                (*values, evaluation_id),
            )

    async def corpus(self, body):
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
            if not set(case.relevant_file_ids + case.negative_file_ids) <= allowed:
                raise ChatError("invalid_qrels", "标注相关文件必须位于本次检索范围内。", 422)
        return files

    async def snapshot(self, body):
        files = await self.corpus(body)
        settings = self.app.state.runtime.settings
        if body.options.context_tokens is not None and body.kind != "rag":
            raise ChatError("invalid_experiment_options", "上下文预算仅用于完整 RAG。", 422)
        if body.options.rerank == "on" and (
            self.app.state.retrieval_service.reranker is None
            or settings.retrieval_rerank_weight == 0
        ):
            raise ChatError(
                "rerank_not_configured", "请先配置权重大于零的重排服务，或选择跟随配置/关闭。", 422
            )
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
            "experiment_options": body.options.model_dump(),
        }
        snapshot["fingerprint"] = digest(snapshot)
        return snapshot

    @staticmethod
    def retry_results(body, seeds):
        return [
            deepcopy(seeds.get(i))
            or {
                "case": case.model_dump(),
                "status": "pending",
                "sources": [],
                "review": Review().model_dump(),
            }
            for i, case in enumerate(body.cases)
        ]

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
                if EvalRequest.model_validate(old["request"]).model_dump(mode="json") != request:
                    raise ChatError("idempotency_conflict", "此幂等键已用于其他评测配置。", 409)
                return await self.row(old["evaluation_id"])
            if self.closing or any(not task.done() for task in self.tasks.values()):
                raise ChatError(
                    "evaluation_busy", "当前有评测执行中，请完成或停止后重试。", 409, True
                )
            if body.kind == "rag" and not self.app.state.runtime.ready:
                raise ChatError("model_not_configured", "请先配置回答模型，或选择仅检索。", 503)
            snapshot = await self.snapshot(body)
            if body.case_set_id:
                saved = await self.case_set(str(body.case_set_id))
                if saved["kb_id"] != body.knowledge_base_id or saved["cases"] != request["cases"]:
                    raise ChatError(
                        "case_set_mismatch", "案例内容与所选版本不一致，请另存版本或重新载入。", 409
                    )
                corpus = [
                    {"file_id": f["file_id"], "content_hash": f["content_hash"]}
                    for f in snapshot["files"]
                ]
                if corpus != saved["corpus"]:
                    raise ChatError(
                        "case_set_stale",
                        "案例集绑定的资料已变化，请重新核对标注并保存新版本。",
                        409,
                    )
                snapshot["case_set"] = {
                    key: saved[key]
                    for key in (
                        "case_set_id",
                        "name",
                        "version",
                        "tags",
                        "fingerprint",
                        "split",
                        "review_status",
                        "frozen",
                    )
                }
            seeds = {}
            if body.retry_of:
                parent = await self.stored(str(body.retry_of))
                if parent["status"] == "running":
                    raise ChatError("evaluation_busy", "请先停止原评测。", 409)
                original = EvalRequest.model_validate(parent["request"])
                compare_fields = (
                    "knowledge_base_id",
                    "kind",
                    "mode",
                    "k",
                    "file_ids",
                    "cases",
                    "case_set_id",
                    "options",
                )
                if any(
                    getattr(original, field) != getattr(body, field) for field in compare_fields
                ):
                    raise ChatError("retry_inputs_changed", "重试必须沿用原案例与配置。", 409)
                if parent["snapshot"]["fingerprint"] != snapshot["fingerprint"]:
                    raise ChatError(
                        "retry_inputs_changed", "资料、配置或代码已变化，请创建独立实验。", 409
                    )
                plan = self.retry_plan(parent)
                if not plan["retry_indices"]:
                    raise ChatError("nothing_to_retry", "没有失败或未完成题。", 409)
                if plan["unknown_indices"] and not body.retry_unknown_confirmed:
                    raise ChatError(
                        "unknown_calls_require_confirmation",
                        "部分调用结果未知，重试可能再次计费，请明确确认。",
                        409,
                    )
                seeds = {
                    i: {**deepcopy(r), "reused_from": parent["evaluation_id"]}
                    for i, r in enumerate(parent["results"])
                    if r["status"] == "completed"
                }
            initial_results = self.retry_results(body, seeds) if body.retry_of else []
            evaluation_id = str(uuid4())
            now = timestamp()
            async with self.database.transaction():
                await self.database.connection.execute(
                    "INSERT INTO evaluations(evaluation_id,idempotency_key,created_at,updated_at,"
                    "status,request,snapshot,results) "
                    "VALUES(%s,%s,%s,%s,'running',%s,%s,%s)",
                    (
                        evaluation_id,
                        str(body.idempotency_key),
                        now,
                        now,
                        Jsonb(request),
                        Jsonb(snapshot),
                        Jsonb(initial_results),
                    ),
                )
            task = asyncio.create_task(self.execute(evaluation_id, body, snapshot, seeds))
            self.tasks[evaluation_id] = task
            task.add_done_callback(lambda done: self.done(evaluation_id, done))
            return await self.row(evaluation_id)

    def done(self, evaluation_id, task):
        self.tasks.pop(evaluation_id, None)
        if not task.cancelled() and task.exception():
            logger.warning("evaluation_worker_failed type=%s", type(task.exception()).__name__)

    @complete_on_cancel
    async def start_run(self, evaluation_id, body, case):
        conversation = await self.app.state.runtime.repository.create(
            purpose="evaluation", owner_evaluation_id=evaluation_id
        )
        run = await self.app.state.run_service.create(
            str(conversation.thread_id),
            RunRequest(
                message=case.query,
                idempotency_key=str(uuid4()),
                knowledge_base_id=body.knowledge_base_id,
                retrieval_mode=body.mode,
                file_ids=body.file_ids,
                capture_trace=True,
                evaluation_options=body.options,
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
        run_id = active_run or (await self.control(evaluation_id))["active_run_id"]
        if run_id:
            try:
                await self.app.state.run_service.cancel(run_id)
            except ChatError as exc:
                if exc.code != "run_not_found":
                    raise
        await self.update(evaluation_id, active_run_id=None)

    async def execute(self, evaluation_id, body, snapshot, seeds=None):
        preallocated = body.retry_of is not None
        results = self.retry_results(body, seeds or {}) if preallocated else []
        active_run = None
        try:
            for index, case in enumerate(body.cases):
                row = await self.control(evaluation_id)
                if row["stop_requested"]:
                    break
                current = await self.snapshot(body)
                if current["fingerprint"] != snapshot["fingerprint"]:
                    raise ChatError(
                        "evaluation_inputs_changed", "资料或模型配置已变化，请创建新评测。", 409
                    )
                if seeds and index in seeds:
                    await self.update(evaluation_id, results=results)
                    continue
                result = {
                    "case": case.model_dump(),
                    "status": "running",
                    "sources": [],
                    "review": Review().model_dump(),
                }
                if preallocated:
                    results[index] = result
                else:
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
                            **(
                                {"rerank": body.options.rerank == "on"}
                                if body.options.rerank != "inherit"
                                else {}
                            ),
                        )
                        result.update(
                            sources=[s.model_dump() for s in found.sources],
                            diagnostics=found.diagnostics,
                            query_plan={"queries": found.queries},
                        )
                    else:
                        active_run = await self.start_run(evaluation_id, body, case)
                        result["run_id"] = active_run
                        await self.update(evaluation_id, results=results)
                        while True:
                            if (await self.control(evaluation_id))["stop_requested"]:
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
                        case_metrics(result["sources"], case.model_dump(), body.k)
                        if result["status"] == "completed"
                        else None
                    )
                    result["judgments"] = judgment_coverage(
                        result["sources"], case.model_dump(), body.k
                    )
                    if case.evidence_groups:
                        async with self.database.read():
                            cur = await self.database.connection.execute(
                                "SELECT file_id,chunk_id,content,page FROM chunks "
                                "WHERE kb_id=%s AND file_id=ANY(%s)",
                                (body.knowledge_base_id, case.relevant_file_ids),
                            )
                            chunks = await cur.fetchall()
                        result["evidence"] = evidence_report(
                            case.model_dump()["evidence_groups"], snapshot["files"], chunks, result
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
            stopped = (await self.control(evaluation_id))["stop_requested"]
            await self.update(
                evaluation_id, status="stopped" if stopped else "completed", active_run_id=None
            )
        except asyncio.CancelledError:
            await self.cancel_active(evaluation_id, active_run)
            for result in results:
                if result["status"] == "running":
                    result["status"] = "cancelled"
            await self.update(evaluation_id, status="stopped", results=results, active_run_id=None)
        except Exception as exc:
            await self.cancel_active(evaluation_id, active_run)
            error = {
                "code": getattr(exc, "code", "evaluation_failed"),
                "message": str(exc) if isinstance(exc, ChatError) else "评测执行中断。",
            }
            for result in results:
                if result["status"] == "running":
                    result.update(status="failed", error=error, metrics=None)
            await self.update(
                evaluation_id, status="failed", results=results, active_run_id=None, error=error
            )

    async def stop(self, evaluation_id):
        async with self.lock:
            row = await self.stored(evaluation_id)
            if row["status"] == "running":
                await self.update(evaluation_id, stop_requested=True)
                task = self.tasks.get(evaluation_id)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                latest = await self.stored(evaluation_id)
                if latest["status"] == "running":
                    await self.cancel_active(evaluation_id)
                    for result in latest["results"]:
                        if result["status"] == "running":
                            result["status"] = "cancelled"
                    await self.update(evaluation_id, status="stopped", results=latest["results"])
            return await self.row(evaluation_id)

    async def case_set(self, case_set_id):
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT * FROM evaluation_case_sets WHERE case_set_id=%s", (case_set_id,)
            )
            saved = await cur.fetchone()
        if not saved:
            raise ChatError("case_set_not_found", "案例集版本不存在。", 404)
        return saved

    async def save_case_set(self, body):
        async with self.lock:
            files = await self.corpus(
                EvalRequest(
                    idempotency_key=uuid4(),
                    knowledge_base_id=body.knowledge_base_id,
                    cases=body.cases,
                )
            )
            cases = [case.model_dump(mode="json") for case in body.cases]
            if body.frozen:
                positive_ids = sorted(
                    {file_id for case in body.cases for file_id in case.relevant_file_ids}
                )
                async with self.database.read():
                    cur = await self.database.connection.execute(
                        "SELECT file_id,content,page FROM chunks "
                        "WHERE kb_id=%s AND file_id=ANY(%s)",
                        (body.knowledge_base_id, positive_ids),
                    )
                    chunks = await cur.fetchall()
                for case in cases:
                    if (
                        case["evidence_groups"]
                        and not evidence_report(case["evidence_groups"], files, chunks, {})["valid"]
                    ):
                        raise ChatError(
                            "invalid_frozen_evidence",
                            "冻结失败：必要原文已失效或尚未定位，请先索引并核对原文、页码和文件版本。",
                            422,
                        )
            # Full judgments are tied to a corpus: additions require annotation review too.
            corpus = [{"file_id": f["file_id"], "content_hash": f["content_hash"]} for f in files]
            fingerprint = digest(
                {
                    "cases": cases,
                    "corpus": corpus,
                    "split": body.split,
                    "review_status": body.review_status,
                    "frozen": body.frozen,
                }
            )
            async with self.database.transaction():
                cur = await self.database.connection.execute(
                    "SELECT * FROM evaluation_case_sets WHERE kb_id=%s AND name=%s "
                    "ORDER BY version DESC LIMIT 1",
                    (body.knowledge_base_id, body.name),
                )
                previous = await cur.fetchone()
                if (
                    previous
                    and previous["fingerprint"] == fingerprint
                    and previous["tags"] == body.tags
                ):
                    return previous
                case_set_id = str(uuid4())
                cur = await self.database.connection.execute(
                    "INSERT INTO evaluation_case_sets(case_set_id,kb_id,name,version,tags,cases,"
                    "corpus,fingerprint,created_at,split,review_status,frozen) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        case_set_id,
                        body.knowledge_base_id,
                        body.name,
                        previous["version"] + 1 if previous else 1,
                        Jsonb(body.tags),
                        Jsonb(cases),
                        Jsonb(corpus),
                        fingerprint,
                        timestamp(),
                        body.split,
                        body.review_status,
                        body.frozen,
                    ),
                )
                return await cur.fetchone()

    @staticmethod
    def retry_plan(row):
        retry, unknown = [], []
        for i in range(len(row["request"]["cases"])):
            result = row["results"][i] if i < len(row["results"]) else None
            if result and result["status"] == "completed":
                continue
            retry.append(i)
            if (
                result
                and result["status"] != "pending"
                and row["request"]["kind"] == "rag"
                and result.get("run", {}).get("model_calls") != 0
            ):
                unknown.append(i)
        return {
            "retry_indices": retry,
            "unknown_indices": unknown,
            "reuse_count": len(row["request"]["cases"]) - len(retry),
        }

    async def history(self, kb_id, archived, limit, offset):
        condition = "(%s::text IS NULL OR request->>'knowledge_base_id'=%s) AND archived=%s"
        args = (kb_id, kb_id, archived)
        async with self.database.read():
            cur = await self.database.connection.execute(
                "SELECT COUNT(*) AS n FROM evaluations WHERE " + condition, args
            )
            total = (await cur.fetchone())["n"]
            cur = await self.database.connection.execute(
                "SELECT evaluation_id,status,created_at,archived,"
                "request->>'knowledge_base_id' AS kb_id,"
                "request->>'kind' AS kind,request->>'mode' AS mode FROM evaluations WHERE "
                + condition
                + " ORDER BY created_at DESC,evaluation_id DESC LIMIT %s OFFSET %s",
                (*args, limit, offset),
            )
            items = await cur.fetchall()
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def manage(self, evaluation_id, archived=None, delete=False):
        async with self.lock:
            row = await self.stored(evaluation_id)
            if row["status"] == "running" or (
                evaluation_id in self.tasks and not self.tasks[evaluation_id].done()
            ):
                raise ChatError("evaluation_busy", "请先停止评测后再管理。", 409)
            if not delete:
                await self.update(evaluation_id, archived=archived)
                return await self.row(evaluation_id)
            for thread_id in row["owned_conversation_ids"]:
                try:
                    await self.app.state.run_service.delete_conversation(thread_id, cleanup=False)
                except ChatError as exc:
                    if exc.code != "conversation_not_found":
                        raise
            pending = await self.app.state.runtime.repository.cleanup_checkpoints(
                self.app.state.runtime.checkpointer
            )
            async with self.database.transaction():
                await self.database.connection.execute(
                    "DELETE FROM evaluations WHERE evaluation_id=%s", (evaluation_id,)
                )
            return {"evaluation_id": evaluation_id, "deleted": True, "cleanup_pending": pending}

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
