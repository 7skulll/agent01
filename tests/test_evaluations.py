import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import ScriptModel, rag_answer, settings
from test_knowledge_bases import index, upload

from app.errors import ChatError
from app.main import create_app

BASE = "/api/v1/evaluations"


@pytest.fixture
def client():
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[rag_answer("十天")]))
    ) as client:
        yield client


def run_body(file_id=None, **kwargs):
    return {
        "idempotency_key": str(uuid4()),
        "knowledge_base_id": "default",
        "mode": "keyword",
        "k": 3,
        "cases": [{"query": "年假多少天", "expected": "answerable", "relevant_file_ids": [file_id]}]
        if file_id
        else [{"query": "年假多少天"}],
        **kwargs,
    }


def finish(client, response):
    assert response.status_code == 202, response.text
    evaluation_id = response.json()["evaluation_id"]

    async def wait():
        task = client.app.state.evaluations.tasks.get(evaluation_id)
        if task:
            await task

    client.portal.call(wait)
    return client.get(BASE + "/" + evaluation_id).json()


def test_retrieval_metrics_trace_history_review_and_export(client):
    file = upload(client, "default")
    index(client, "default")
    body = run_body(file["file_id"])
    report = finish(client, client.post(BASE, json=body))
    assert report["status"] == "completed", report
    result = report["results"][0]
    assert result["status"] == "completed", result
    assert result["metrics"]["recall"] == 1
    assert result["metrics"]["precision"] == 1 / 3
    assert result["diagnostics"]["trace"]["before_rerank"][0]["file_id"] == file["file_id"]
    assert result.get("run") is None
    assert report["summary"]["curves"][0]["precision"] == 1
    assert client.get(BASE).json()[0]["evaluation_id"] == report["evaluation_id"]
    assert client.post(BASE, json=body).json()["evaluation_id"] == report["evaluation_id"]
    assert client.post(BASE, json={**body, "k": 5}).status_code == 409
    reviewed = client.patch(
        BASE + "/" + report["evaluation_id"] + "/reviews/0",
        json={"verdict": "pass", "notes": "依据完整"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["summary"]["reviewed"] == 1
    export = client.get(BASE + "/" + report["evaluation_id"] + "/export")
    assert export.json()["results"][0]["review"]["notes"] == "依据完整"
    assert export.headers["cache-control"] == "no-store"
    assert not any(
        "api_key" in key or key == "database_url" for key in report["snapshot"]["config"]
    )


def test_rag_uses_one_actual_retrieval_and_keeps_eval_conversations_out_of_chat(
    client, monkeypatch
):
    file = upload(client, "default")
    index(client, "default")
    original = client.app.state.retrieval_service.search
    calls = []

    async def count(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", count)
    report = finish(client, client.post(BASE, json=run_body(file["file_id"], kind="rag")))
    result = report["results"][0]
    assert result["status"] == "completed", result
    assert len(calls) == 1 and calls[0]["capture_trace"] is True
    assert result["run"]["answer_protocol"] == "rag-answer-v2"
    assert result["run"]["sources"][0]["file_id"] == file["file_id"]
    assert result["context_sources"][0]["file_id"] == file["file_id"]
    assert result["assembly"]["prompt_hash"]
    assert result["token_usage"]
    assert result["metrics"]["recall"] == 1
    assert client.get("/api/v1/conversations").json() == []


def test_empty_scope_and_unanswerable_are_not_precision_queries(client):
    report = finish(
        client,
        client.post(
            BASE,
            json=run_body(
                file_ids=[], cases=[{"query": "未知事实", "expected": "unanswerable"}], kind="rag"
            ),
        ),
    )
    assert report["summary"]["metrics"]["recall"] is None
    assert report["summary"]["refusal_correct"] == report["summary"]["refusal_total"] == 1
    assert report["results"][0]["run"]["model_calls"] == 0


def test_bad_labels_scopes_and_too_many_cases_are_rejected(client):
    file = upload(client, "default")
    for updates in [
        {"cases": [{"query": "x", "expected": "answerable", "relevant_file_ids": ["missing"]}]},
        {"file_ids": []},
        {"file_ids": ["missing"]},
        {"cases": [{"query": "x"}] * 21},
    ]:
        assert client.post(BASE, json=run_body(file["file_id"], **updates)).status_code == 422


def test_failed_labelled_query_counts_zero(client, monkeypatch):
    file = upload(client, "default")

    async def broken(*args, **kwargs):
        raise ChatError("retrieval_unavailable", "暂时不可用", 503, True)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", broken)
    report = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    assert report["results"][0]["status"] == "failed"
    assert report["summary"]["metrics"]["recall"] == 0
    assert report["summary"]["labelled_failures"] == 1


def test_stop_preserves_results_and_enforces_single_batch(client, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(client.app.state.retrieval_service, "search", slow)
    response = client.post(BASE, json=run_body())
    assert response.status_code == 202
    evaluation_id = response.json()["evaluation_id"]
    assert client.post(BASE, json=run_body()).status_code == 409
    stopped = client.post(BASE + "/" + evaluation_id + "/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"
    assert all(row["status"] == "cancelled" for row in stopped.json()["results"])


def test_snapshot_prevents_mixed_corpora_and_resume_does_not_auto_call_models(client, monkeypatch):
    file = upload(client, "default")
    original = client.app.state.evaluations.snapshot
    count = 0

    async def changed(body):
        nonlocal count
        count += 1
        result = await original(body)
        if count > 1:
            result["fingerprint"] = "changed"
        return result

    monkeypatch.setattr(client.app.state.evaluations, "snapshot", changed)
    report = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    assert report["status"] == "failed"
    assert report["error"]["code"] == "evaluation_inputs_changed"


def test_reports_and_manual_reviews_survive_restart():
    config = settings()
    with TestClient(create_app(config)) as client:
        report = finish(client, client.post(BASE, json=run_body(file_ids=[])))
        evaluation_id = report["evaluation_id"]
        assert (
            client.patch(
                BASE + "/" + evaluation_id + "/reviews/0",
                json={"verdict": "partial", "notes": "测试"},
            ).status_code
            == 200
        )
    with TestClient(create_app(config)) as restarted:
        assert (
            restarted.get(BASE + "/" + evaluation_id).json()["results"][0]["review"]["notes"]
            == "测试"
        )


def test_cancel_during_run_creation_does_not_leave_hidden_active_run(client, monkeypatch):
    original = client.app.state.run_service.create
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_create(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(client.app.state.run_service, "create", slow_create)
    response = client.post(BASE, json=run_body(kind="rag", file_ids=[]))
    evaluation_id = response.json()["evaluation_id"]

    async def stop_during_creation():
        await entered.wait()
        task = asyncio.create_task(client.app.state.evaluations.stop(evaluation_id))
        await asyncio.sleep(0.02)
        release.set()
        result = await task
        assert result["status"] == "stopped"
        db = client.app.state.evaluations.database
        async with db.read():
            cur = await db.connection.execute(
                "SELECT COUNT(*) AS n FROM runs "
                "WHERE status IN ('queued','running','recovering','paused')"
            )
            assert (await cur.fetchone())["n"] == 0

    client.portal.call(stop_during_creation)


def test_restart_marks_unfinished_evaluations_interrupted_without_model_calls():
    from psycopg.types.json import Jsonb

    config = settings()
    with TestClient(create_app(config)) as client:
        report = finish(client, client.post(BASE, json=run_body(file_ids=[])))
        evaluation_id = report["evaluation_id"]

        async def simulate_crash():
            db = client.app.state.evaluations.database
            results = report["results"]
            results[0]["status"] = "running"
            async with db.transaction():
                await db.connection.execute(
                    "UPDATE evaluations SET status='running',results=%s WHERE evaluation_id=%s",
                    (Jsonb(results), evaluation_id),
                )

        client.portal.call(simulate_crash)
    model = ScriptModel(outputs=[rag_answer("不会被调用")])
    with TestClient(create_app(config, chat_model=model)) as restarted:
        result = restarted.get(BASE + "/" + evaluation_id).json()
        assert result["status"] == "interrupted"
        assert result["results"][0]["status"] == "cancelled"
        assert model.seen == []


def test_startup_survives_gc_scan_permission_error(monkeypatch):
    from app.rag.storage import LocalStorage

    def denied(*args):
        raise PermissionError("unreadable directory")

    monkeypatch.setattr(LocalStorage, "orphan_candidates", denied)
    with TestClient(create_app(settings())) as app:
        assert app.get("/api/v1/health").status_code == 200


def test_checkpoint_cleanup_releases_global_control_lock(client, monkeypatch):
    first = client.post("/api/v1/conversations").json()["thread_id"]
    second = client.post("/api/v1/conversations").json()["thread_id"]

    async def verify():
        from app.schemas import RunRequest

        service = client.app.state.run_service
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_cleanup(_):
            entered.set()
            await release.wait()
            return 0

        monkeypatch.setattr(service.runtime.repository, "cleanup_checkpoints", slow_cleanup)
        deleting = asyncio.create_task(service.delete_conversation(first))
        try:
            await entered.wait()
            assert not service.control_lock.locked()
            result = await asyncio.wait_for(
                service.create(
                    second, RunRequest(message="hello", idempotency_key="other"), str(uuid4())
                ),
                1,
            )
            assert result.conversation_id
        finally:
            release.set()
            await deleting

    client.portal.call(verify)


def test_sse_reports_deleted_run_without_escaping_generator(client):
    from types import SimpleNamespace
    from uuid import UUID

    from app.run_api import run_events

    cid = client.post("/api/v1/conversations").json()["thread_id"]
    rid = client.post(
        f"/api/v1/conversations/{cid}/runs", json={"message": "hi", "idempotency_key": "events"}
    ).json()["run_id"]

    async def verify():
        service = client.app.state.run_service
        task = service.tasks.get(rid)
        if task:
            await task
        response = await run_events(
            UUID(rid), SimpleNamespace(app=client.app), after=0, last_event_id=None
        )
        stream = response.body_iterator
        assert "event: start" in await anext(stream)
        await service.delete_conversation(cid)
        remaining = [frame async for frame in stream]
        assert "event: stream_error" in remaining[-1]
        assert "run_not_found" in remaining[-1] and '"retryable":false' in remaining[-1].replace(
            " ", ""
        )

    client.portal.call(verify)
