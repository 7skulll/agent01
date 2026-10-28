"""HTTP routes; workers and persistence live in EvaluationService."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.clock import utc_now
from app.errors import ChatError
from app.evaluation_comparison import compare
from app.evaluation_models import ArchiveRequest, CaseSetRequest, EvalRequest, RetryRequest, Review
from app.evaluation_quality import markdown_report
from app.users import User, require_staff

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])


@router.post("", status_code=202, dependencies=[Depends(require_staff)])
async def create_evaluation(body: EvalRequest, request: Request):
    return await request.app.state.evaluations.create(body)


@router.get("")
async def list_evaluations(request: Request, knowledge_base_id: str | None = None):
    return (await request.app.state.evaluations.history(knowledge_base_id, False, 30, 0))["items"]


@router.get("/history")
async def evaluation_history(
    request: Request,
    knowledge_base_id: str,
    archived: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return await request.app.state.evaluations.history(knowledge_base_id, archived, limit, offset)


@router.get("/capabilities")
async def evaluation_capabilities(request: Request):
    runtime = request.app.state.runtime
    return {
        "max_cases": 20,
        "max_model_calls_per_case": runtime.settings.agent_max_model_calls,
        "default_context_tokens": runtime.settings.rag_context_tokens,
        "rerank_available": request.app.state.retrieval_service.reranker is not None
        and runtime.settings.retrieval_rerank_weight > 0,
        "ragas_available": request.app.state.evaluation_judge.available,
    }


@router.post("/case-sets", status_code=201, dependencies=[Depends(require_staff)])
async def save_case_set(body: CaseSetRequest, request: Request):
    return await request.app.state.evaluations.save_case_set(body)


@router.get("/case-sets")
async def list_case_sets(
    request: Request,
    knowledge_base_id: str | None = None,
    include_archived: bool = False,
):
    return await request.app.state.evaluations.case_sets(
        knowledge_base_id, include_archived=include_archived
    )


@router.patch("/case-sets/{case_set_id}/archive", dependencies=[Depends(require_staff)])
async def archive_case_set(case_set_id: UUID, body: ArchiveRequest, request: Request):
    return await request.app.state.evaluations.archive_case_set(str(case_set_id), body.archived)


@router.get("/case-sets/{case_set_id}")
async def get_case_set(case_set_id: UUID, request: Request):
    return await request.app.state.evaluations.case_set(str(case_set_id))


@router.get("/corpus")
async def evaluation_corpus(request: Request, knowledge_base_id: str):
    return await request.app.state.evaluations.corpus_files(knowledge_base_id)


@router.get("/gate")
async def regression_gate(
    request: Request,
    baseline: UUID,
    candidate: UUID,
    max_recall_drop: float = Query(default=0.02, ge=0, le=1),
    max_mrr_drop: float = Query(default=0.02, ge=0, le=1),
    max_answer_drop: float = Query(default=0.05, ge=0, le=1),
):
    service = request.app.state.evaluations
    comparison = compare(
        await service.row(str(baseline), derived=False),
        await service.row(str(candidate), derived=False),
    )
    checks = []

    def add(name, delta, threshold, source):
        checks.append(
            {
                "name": name,
                "source": source,
                "delta": delta,
                "max_drop": threshold,
                "status": "unscored"
                if delta is None
                else "pass"
                if delta >= -threshold
                else "fail",
            }
        )

    add("召回率", comparison["metric_delta"].get("recall"), max_recall_drop, "retrieval")
    add("MRR", comparison["metric_delta"].get("mrr"), max_mrr_drop, "retrieval")
    for key, label in (
        ("faithfulness", "忠实度"),
        ("answer_relevancy", "答案相关性"),
        ("correctness", "正确性"),
        ("completeness", "完整性"),
    ):
        item = (comparison.get("answer_metrics") or {}).get(key) or {}
        add(label, item.get("delta"), max_answer_drop, "answer")
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        status = "fail"
    elif "unscored" in statuses:
        status = "unscored"
    else:
        status = "pass"
    return {
        "status": status,
        "left_id": comparison["left_id"],
        "right_id": comparison["right_id"],
        "counts": comparison["counts"],
        "checks": checks,
        "thresholds": {
            "max_recall_drop": max_recall_drop,
            "max_mrr_drop": max_mrr_drop,
            "max_answer_drop": max_answer_drop,
        },
        "notice": "门禁基于同一案例与标注版本的配对差值；未评分不等于通过。",
    }


@router.get("/compare")
async def compare_evaluations(request: Request, left: UUID, right: UUID):
    service = request.app.state.evaluations
    return compare(
        await service.row(str(left), derived=False),
        await service.row(str(right), derived=False),
    )


@router.get("/{evaluation_id}")
async def get_evaluation(
    evaluation_id: UUID, request: Request, tag: str | None = Query(default=None, max_length=80)
):
    return await request.app.state.evaluations.row(str(evaluation_id), tag)


@router.post("/{evaluation_id}/stop", dependencies=[Depends(require_staff)])
async def stop_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.stop(str(evaluation_id))


@router.patch(
    "/{evaluation_id}/reviews/{index}",
)
async def review_evaluation(
    evaluation_id: UUID,
    index: int,
    body: Review,
    request: Request,
    user: User = Depends(require_staff),
):
    service = request.app.state.evaluations
    async with service.lock:
        row = await service.stored(str(evaluation_id))
        if row["status"] == "running":
            raise ChatError("evaluation_busy", "请在评测结束后保存人工评价。", 409)
        if not 0 <= index < len(row["results"]):
            raise ChatError("case_not_found", "案例不存在。", 404)
        payload = body.model_dump()
        existing = row["results"][index].get("review") or {}
        if body.review_method is None:
            # Saved from the workbench by a signed-in staff account: who and when are facts,
            # and a human edit supersedes any carried link to an earlier run.
            payload.update(
                review_method="human",
                reviewer=user.display_name or user.username,
                reviewed_at=utc_now(),
                carried_from=None,
            )
        else:
            # Provenance survives an edit that only touches judgments: a carried or proxy
            # review must not silently become a human sign-off because the form omitted it.
            for key in ("reviewer", "reviewed_at", "carried_from"):
                if payload.get(key) is None:
                    payload[key] = existing.get(key)
        await service.patch_result(str(evaluation_id), index, {"review": payload})
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


@router.get("/{evaluation_id}/retry-plan")
async def retry_plan(evaluation_id: UUID, request: Request):
    service = request.app.state.evaluations
    return service.retry_plan(await service.row(str(evaluation_id), derived=False))


@router.post("/{evaluation_id}/retry", status_code=202, dependencies=[Depends(require_staff)])
async def retry_evaluation(evaluation_id: UUID, body: RetryRequest, request: Request):
    service = request.app.state.evaluations
    async with service.database.read():
        cur = await service.database.connection.execute(
            "SELECT evaluation_id,request FROM evaluations WHERE idempotency_key=%s",
            (str(body.idempotency_key),),
        )
        previous = await cur.fetchone()
    if previous:
        if (
            previous["request"].get("retry_of") != str(evaluation_id)
            or previous["request"].get("retry_unknown_confirmed", False)
            != body.confirm_unknown_calls
        ):
            raise ChatError("idempotency_conflict", "此幂等键已用于其他重试。", 409)
        return await service.row(previous["evaluation_id"])
    parent = await service.stored(str(evaluation_id))
    payload = {
        **parent["request"],
        "idempotency_key": str(body.idempotency_key),
        "retry_of": str(evaluation_id),
        "retry_unknown_confirmed": body.confirm_unknown_calls,
    }
    return await service.create(EvalRequest.model_validate(payload))


@router.patch("/{evaluation_id}/archive", dependencies=[Depends(require_staff)])
async def archive_evaluation(evaluation_id: UUID, body: ArchiveRequest, request: Request):
    return await request.app.state.evaluations.manage(str(evaluation_id), archived=body.archived)


@router.delete("/{evaluation_id}", dependencies=[Depends(require_staff)])
async def delete_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.manage(str(evaluation_id), delete=True)


@router.get("/{evaluation_id}/report")
async def evaluation_report(
    evaluation_id: UUID, request: Request, tag: str | None = Query(default=None, max_length=80)
):
    row = await request.app.state.evaluations.row(str(evaluation_id), tag)
    return {"markdown": markdown_report(row)}
