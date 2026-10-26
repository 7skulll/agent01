import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import ScriptModel, events, rag_answer, settings
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
    assert report["snapshot"]["config"]["llm_max_tokens"] > 0
    assert report["snapshot"]["config"]["rag_context_tokens"] > 0
    assert "api_token" not in report["snapshot"]["config"]


def test_retrieval_and_rag_use_same_chunk_budget(client, monkeypatch):
    file = upload(client, "default")
    index(client, "default")
    original = client.app.state.retrieval_service.search
    limits = []

    async def capture(kb_id, body, **kwargs):
        limits.append(body.top_k)
        return await original(kb_id, body, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", capture)
    for kind in ("retrieval", "rag"):
        report = finish(client, client.post(BASE, json=run_body(file["file_id"], kind=kind)))
        assert report["results"][0]["status"] == "completed", report
    expected = client.app.state.runtime.settings.retrieval_answer_candidates
    assert limits == [expected, expected]


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


def test_evaluation_retrieval_event_omits_bodies_and_trace(client):
    file = upload(client, "default")
    index(client, "default")
    report = finish(client, client.post(BASE, json=run_body(file["file_id"], kind="rag")))
    result = report["results"][0]
    # 评测结果只保留证据流转需要的候选定位与排名。
    trace = result["diagnostics"]["trace"]
    assert trace["before_rerank"]
    assert all("content" not in source for source in trace["before_rerank"])
    assert all(
        {"chunk_id", "file_id", "name", "rank"} <= set(source)
        for source in trace["before_rerank"]
    )
    assert len(trace["before_rerank"]) <= 40
    assert result["context_sources"][0]["content"]

    stream = events(client.get(f"/api/v1/runs/{result['run_id']}/events"))
    end = next(data for _, name, data in stream if name == "retrieval_end")
    assert "trace" not in end["diagnostics"]
    assert end["sources"]
    assert all("content" not in source for source in end["sources"])
    assert all("evidence_chunks" not in source for source in end["sources"])


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


def test_archived_case_set_is_hidden_by_default_but_still_readable(client):
    file = upload(client, "default")
    index(client, "default")
    response = client.post(
        BASE + "/case-sets",
        json={
            "knowledge_base_id": "default",
            "name": "归档测试集",
            "tags": ["archive-test"],
            "cases": [
                {
                    "query": "年假多少天",
                    "expected": "answerable",
                    "relevant_file_ids": [file["file_id"]],
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    saved = response.json()

    archived = client.patch(
        BASE + f"/case-sets/{saved['case_set_id']}/archive", json={"archived": True}
    )
    assert archived.status_code == 200 and archived.json()["archived"] is True
    visible = client.get(BASE + "/case-sets").json()
    assert all(item["case_set_id"] != saved["case_set_id"] for item in visible)
    included = client.get(BASE + "/case-sets?include_archived=true").json()
    assert any(item["case_set_id"] == saved["case_set_id"] for item in included)
    assert client.get(BASE + f"/case-sets/{saved['case_set_id']}").status_code == 200

    restored = client.patch(
        BASE + f"/case-sets/{saved['case_set_id']}/archive", json={"archived": False}
    )
    assert restored.status_code == 200 and restored.json()["archived"] is False


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


def test_shutdown_marks_running_evaluation_interrupted():
    config = settings()

    async def slow(*args, **kwargs):
        await asyncio.Event().wait()

    with TestClient(
        create_app(config, chat_model=ScriptModel(outputs=[rag_answer("十天")]))
    ) as client:
        client.app.state.retrieval_service.search = slow
        response = client.post(BASE, json=run_body())
        assert response.status_code == 202
        evaluation_id = response.json()["evaluation_id"]

    with TestClient(
        create_app(config, chat_model=ScriptModel(outputs=[rag_answer("十天")]))
    ) as restarted:
        result = restarted.get(BASE + "/" + evaluation_id).json()
        assert result["status"] == "interrupted"
        assert all(row["status"] != "running" for row in result["results"])


def test_restart_marks_unfinished_evaluations_interrupted_without_model_calls():
    config = settings()
    with TestClient(create_app(config)) as client:
        report = finish(client, client.post(BASE, json=run_body(file_ids=[])))
        evaluation_id = report["evaluation_id"]

        async def simulate_crash():
            service = client.app.state.evaluations
            results = report["results"]
            results[0]["status"] = "running"
            async with service.database.transaction():
                await service.database.connection.execute(
                    "UPDATE evaluations SET status='running' WHERE evaluation_id=%s",
                    (evaluation_id,),
                )
            # 结果本身逐题落在子表里，模拟崩溃只需把该题写回 running。
            await service.save_result(evaluation_id, 0, results[0])

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


def test_case_set_versions_bind_labels_and_corpus_and_remain_immutable(client):
    file = upload(client, "default")
    index(client, "default")
    body = run_body(file["file_id"])
    draft = {
        "knowledge_base_id": "default",
        "name": "人事问答",
        "tags": ["年假"],
        "cases": body["cases"],
    }
    first = client.post(BASE + "/case-sets", json=draft)
    assert first.status_code == 201, first.text
    saved = first.json()
    assert saved["version"] == 1
    assert (
        client.post(BASE + "/case-sets", json=draft).json()["case_set_id"] == saved["case_set_id"]
    )
    assert len(client.get(BASE + "/case-sets?knowledge_base_id=default").json()) == 1
    listing = client.get(BASE + "/case-sets?knowledge_base_id=default").json()[0]
    assert listing["case_count"] == 1
    assert listing["corpus_count"] == 1
    assert listing["kb_id"] == "default"
    assert listing["kb_name"]
    assert client.get(BASE + "/case-sets").json()[0]["case_set_id"] == saved["case_set_id"]
    body.update(case_set_id=saved["case_set_id"], cases=saved["cases"])
    report = finish(client, client.post(BASE, json=body))
    assert report["snapshot"]["case_set"]["version"] == 1
    draft["cases"][0]["required_facts"] = "十天年假"
    second = client.post(BASE + "/case-sets", json=draft).json()
    assert second["version"] == 2
    assert (
        client.get(BASE + "/case-sets/" + saved["case_set_id"]).json()["cases"][0]["required_facts"]
        == ""
    )
    body.update(idempotency_key=str(uuid4()), cases=second["cases"])
    assert client.post(BASE, json=body).status_code == 409  # edited cases, old version
    body.update(cases=saved["cases"])
    upload(client, "default", "new-document")
    response = client.post(BASE, json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "case_set_stale"


def test_partial_labels_and_evidence_coverage_are_saved_and_exported(client):
    file = upload(client, "default")
    index(client, "default")
    corpus = client.get(BASE + "/corpus?knowledge_base_id=default").json()
    assert corpus[0]["content_hash"] == file["content_hash"]
    body = run_body(file["file_id"], kind="rag")
    body["cases"][0].update(
        judgment_scope="partial",
        evidence_groups=[
            {
                "fact": "年假天数",
                "alternatives": [
                    {
                        "file_id": file["file_id"],
                        "content_hash": file["content_hash"],
                        "quote": "员工年假十天",
                    }
                ],
            }
        ],
    )
    report = finish(client, client.post(BASE, json=body))
    result = report["results"][0]
    assert result["status"] == "completed", result
    assert report["summary"]["metric_queries"] == 0
    assert result["metrics"] is None
    assert result["judgments"]["coverage"] == 1
    assert result["evidence"]["context_complete"] is True
    assert report["summary"]["context_evidence_complete"] == 1
    review = {
        "verdict": "partial",
        "fact_coverage": "full",
        "unsupported_claims": "no",
        "conditions": "fail",
        "answer_relevancy": "partial",
        "citation_support": "full",
        "refusal": "na",
        "causes": ["generation"],
        "notes": "条件遗漏",
    }
    response = client.patch(BASE + "/" + report["evaluation_id"] + "/reviews/0", json=review)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["review"] == review
    export = client.get(BASE + "/" + report["evaluation_id"] + "/export").json()
    assert export["results"][0]["review"]["conditions"] == "fail"
    assert export["results"][0]["evidence"]["coverage"]["context"] == 1


def test_stale_evidence_is_reported_as_annotation_gap(client):
    file = upload(client, "default")
    index(client, "default")
    body = run_body(file["file_id"])
    body["cases"][0]["evidence_groups"] = [
        {
            "fact": "天数",
            "alternatives": [
                {"file_id": file["file_id"], "content_hash": "a" * 64, "quote": "员工年假十天"}
            ],
        }
    ]
    report = finish(client, client.post(BASE, json=body))
    evidence = report["results"][0]["evidence"]
    assert evidence["valid"] is False
    assert evidence["coverage"]["selected"] is None
    assert evidence["causes"] == ["annotation_gap"]


def test_paired_comparison_on_real_retrieval_and_incompatible_labels(client):
    file = upload(client, "default")
    index(client, "default")
    a = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    b = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    url = BASE + "/compare?left=" + a["evaluation_id"] + "&right=" + b["evaluation_id"]
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["counts"] == {"win": 0, "tie": 1, "loss": 0, "unscored": 0}
    assert response.json()["rows"][0]["tokens_delta"] is None
    gate = client.get(
        BASE
        + "/gate?baseline="
        + a["evaluation_id"]
        + "&candidate="
        + b["evaluation_id"]
    )
    assert gate.status_code == 200, gate.text
    # Retrieval checks pass; answer checks are unscored until human/RAGAS review.
    assert gate.json()["status"] == "unscored"
    assert {check["status"] for check in gate.json()["checks"]} >= {"pass", "unscored"}
    changed = run_body(file["file_id"])
    changed["cases"][0]["required_facts"] = "新审核要求"
    c = finish(client, client.post(BASE, json=changed))
    response = client.get(url.replace(b["evaluation_id"], c["evaluation_id"]))
    assert response.status_code == 409
    assert "案例或标注不同" in response.text


def test_history_filters_before_limit_and_retrieval_has_no_refusal_denominator(client):
    report = finish(
        client,
        client.post(BASE, json=run_body(cases=[{"query": "未知", "expected": "unanswerable"}])),
    )
    assert report["summary"]["refusal_attempted"] == 0

    async def seed():
        from psycopg.types.json import Jsonb

        service = client.app.state.evaluations
        async with service.database.transaction():
            for _ in range(31):
                await service.database.connection.execute(
                    "INSERT INTO evaluations(evaluation_id,idempotency_key,created_at,updated_at,"
                    "status,request,snapshot) "
                    "VALUES(%s,%s,'2099','2099','completed',%s,'{}')",
                    (
                        str(uuid4()),
                        str(uuid4()),
                        Jsonb({"knowledge_base_id": "other", "kind": "retrieval"}),
                    ),
                )

    client.portal.call(seed)
    assert len(client.get(BASE).json()) == 30
    own = client.get(BASE + "?knowledge_base_id=default").json()
    assert len(own) == 1 and own[0]["evaluation_id"] == report["evaluation_id"]


def test_worker_control_polling_never_rebuilds_metric_curves(client, monkeypatch):
    original = client.app.state.evaluations.row
    calls = []

    async def count(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.evaluations, "row", count)
    finish(client, client.post(BASE, json=run_body()))
    assert len(calls) == 2  # POST response and final GET only; worker polls the small control row.
