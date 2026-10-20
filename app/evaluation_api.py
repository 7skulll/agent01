"""HTTP routes; workers and persistence live in EvaluationService."""

import json
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.errors import ChatError
from app.evaluation_comparison import compare
from app.evaluation_models import ArchiveRequest, CaseSetRequest, EvalRequest, RetryRequest, Review
from app.evaluation_quality import markdown_report
from app.knowledge_bases import require_knowledge_base

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])


@router.post("", status_code=202)
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
    }


@router.post("/case-sets", status_code=201)
async def save_case_set(body: CaseSetRequest, request: Request):
    return await request.app.state.evaluations.save_case_set(body)


@router.get("/case-sets")
async def list_case_sets(request: Request, knowledge_base_id: str):
    service = request.app.state.evaluations
    async with service.database.read():
        cur = await service.database.connection.execute(
            "SELECT case_set_id,name,version,tags,created_at,fingerprint,"
            "split,review_status,frozen "
            "FROM evaluation_case_sets "
            "WHERE kb_id=%s ORDER BY created_at DESC LIMIT 100",
            (knowledge_base_id,),
        )
        return await cur.fetchall()


@router.get("/case-sets/{case_set_id}")
async def get_case_set(case_set_id: UUID, request: Request):
    return await request.app.state.evaluations.case_set(str(case_set_id))


@router.get("/corpus")
async def evaluation_corpus(request: Request, knowledge_base_id: str):
    service = request.app.state.evaluations
    await require_knowledge_base(service.database, knowledge_base_id)
    async with service.database.read():
        cur = await service.database.connection.execute(
            "SELECT file_id,name,content_hash FROM files WHERE kb_id=%s ORDER BY file_id",
            (knowledge_base_id,),
        )
        return await cur.fetchall()


@router.get("/compare")
async def compare_evaluations(request: Request, left: UUID, right: UUID):
    service = request.app.state.evaluations
    return compare(await service.row(str(left)), await service.row(str(right)))


@router.get("/{evaluation_id}")
async def get_evaluation(
    evaluation_id: UUID, request: Request, tag: str | None = Query(default=None, max_length=80)
):
    return await request.app.state.evaluations.row(str(evaluation_id), tag)


@router.post("/{evaluation_id}/stop")
async def stop_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.stop(str(evaluation_id))


@router.patch("/{evaluation_id}/reviews/{index}")
async def review_evaluation(evaluation_id: UUID, index: int, body: Review, request: Request):
    service = request.app.state.evaluations
    async with service.lock:
        row = await service.stored(str(evaluation_id))
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


@router.get("/{evaluation_id}/retry-plan")
async def retry_plan(evaluation_id: UUID, request: Request):
    service = request.app.state.evaluations
    return service.retry_plan(await service.row(str(evaluation_id)))


@router.post("/{evaluation_id}/retry", status_code=202)
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


@router.patch("/{evaluation_id}/archive")
async def archive_evaluation(evaluation_id: UUID, body: ArchiveRequest, request: Request):
    return await request.app.state.evaluations.manage(str(evaluation_id), archived=body.archived)


@router.delete("/{evaluation_id}")
async def delete_evaluation(evaluation_id: UUID, request: Request):
    return await request.app.state.evaluations.manage(str(evaluation_id), delete=True)


@router.get("/{evaluation_id}/report")
async def evaluation_report(
    evaluation_id: UUID, request: Request, tag: str | None = Query(default=None, max_length=80)
):
    row = await request.app.state.evaluations.row(str(evaluation_id), tag)
    return {"markdown": markdown_report(row)}
