import asyncio
from uuid import uuid4

import pytest
from test_evaluations import BASE, finish, run_body
from test_evaluations import client as client
from test_knowledge_bases import create as create_kb
from test_knowledge_bases import index, upload

from app.errors import ChatError


def test_experiment_options_apply_to_real_run_without_mutating_workspace(client):
    file = upload(client, "default")
    index(client, "default")
    before = client.app.state.runtime.settings.rag_context_tokens
    body = run_body(file["file_id"], kind="rag", options={"rerank": "off", "context_tokens": 512})
    report = finish(client, client.post(BASE, json=body))
    result = report["results"][0]
    assert result["status"] == "completed", result
    assert result["run"]["evaluation_options"]["context_tokens"] == 512
    assert result["assembly"]["evidence_tokens"] <= 512
    assert result["diagnostics"]["rerank_skipped"] == "evaluation_disabled"
    assert client.app.state.runtime.settings.rag_context_tokens == before
    assert report["snapshot"]["experiment_options"]["context_tokens"] == 512
    assert client.post(BASE, json={**body, "options": {"rerank": "inherit"}}).status_code == 409
    assert (
        client.post(
            BASE, json=run_body(file["file_id"], options={"context_tokens": 512})
        ).status_code
        == 422
    )
    assert (
        client.post(BASE, json=run_body(file["file_id"], options={"rerank": "on"})).status_code
        == 422
    )


def test_pagination_archive_restore_and_delete_clean_only_owned_evaluation_conversations(client):
    file = upload(client, "default")
    index(client, "default")
    report = finish(client, client.post(BASE, json=run_body(file["file_id"], kind="rag")))
    eid = report["evaluation_id"]
    conversation_id = report["results"][0]["run"]["conversation_id"]
    assert report["owned_conversation_ids"] == [conversation_id]
    assert client.get(BASE + "/history?knowledge_base_id=default&limit=1").json()["total"] == 1
    assert client.get(BASE + "/history?knowledge_base_id=default&offset=1").json()["items"] == []
    assert client.patch(BASE + "/" + eid + "/archive", json={"archived": True}).status_code == 200
    assert client.get(BASE + "/history?knowledge_base_id=default").json()["total"] == 0
    assert (
        client.get(BASE + "/history?knowledge_base_id=default&archived=true").json()["total"] == 1
    )
    assert client.patch(BASE + "/" + eid + "/archive", json={"archived": False}).status_code == 200
    assert client.delete(BASE + "/" + eid).status_code == 200
    assert client.get(BASE + "/" + eid).status_code == 404
    assert client.get("/api/v1/conversations/" + conversation_id).status_code == 404


def test_retry_reuses_completed_cases_and_is_idempotent(client, monkeypatch):
    calls = []
    original = client.app.state.retrieval_service.search

    async def flaky(*args, **kwargs):
        calls.append(args[1].query)
        if args[1].query == "失败题":
            raise ChatError("temporary", "暂不可用", 503, True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", flaky)
    parent = finish(
        client, client.post(BASE, json=run_body(cases=[{"query": "成功题"}, {"query": "失败题"}]))
    )
    eid = parent["evaluation_id"]
    assert [r["status"] for r in parent["results"]] == ["completed", "failed"]
    monkeypatch.setattr(client.app.state.retrieval_service, "search", original)
    payload = {"idempotency_key": str(uuid4())}
    child = finish(client, client.post(BASE + "/" + eid + "/retry", json=payload))
    assert [r["status"] for r in child["results"]] == ["completed", "completed"]
    assert child["results"][0]["reused_from"] == eid
    assert child["results"][0]["elapsed_ms"] == parent["results"][0]["elapsed_ms"]
    assert child["quality"]["reused"] == 1
    assert (
        client.post(BASE + "/" + eid + "/retry", json=payload).json()["evaluation_id"]
        == child["evaluation_id"]
    )
    assert (
        client.post(
            BASE + "/" + child["evaluation_id"] + "/retry", json={"idempotency_key": str(uuid4())}
        ).status_code
        == 409
    )


def test_unknown_rag_calls_need_explicit_confirmation_and_running_tasks_cannot_be_deleted(client):
    parent = finish(client, client.post(BASE, json=run_body(kind="rag")))
    eid = parent["evaluation_id"]

    async def make_unknown():
        results = parent["results"]
        results[0].update(status="failed", run={"model_calls": 1})
        await client.app.state.evaluations.update(eid, results=results)

    client.portal.call(make_unknown)
    plan = client.get(BASE + "/" + eid + "/retry-plan").json()
    assert plan["unknown_indices"] == [0]
    response = client.post(BASE + "/" + eid + "/retry", json={"idempotency_key": str(uuid4())})
    assert response.status_code == 409 and "unknown_calls_require_confirmation" in response.text
    child = finish(
        client,
        client.post(
            BASE + "/" + eid + "/retry",
            json={"idempotency_key": str(uuid4()), "confirm_unknown_calls": True},
        ),
    )
    assert child["status"] == "completed"


def test_quality_tag_filters_and_report_are_source_backed(client):
    file = upload(client, "default")
    index(client, "default")
    cases = [
        dict(
            query="年假多少天",
            expected="answerable",
            relevant_file_ids=[file["file_id"]],
            tags=[tag],
        )
        for tag in ("人事", "审批")
    ]
    report = finish(client, client.post(BASE, json=run_body(kind="rag", cases=cases)))
    eid = report["evaluation_id"]
    assert (
        client.patch(
            BASE + "/" + eid + "/reviews/0",
            json={"verdict": "pass", "fact_coverage": "full", "citation_support": "full"},
        ).status_code
        == 200
    )
    filtered = client.get(BASE + "/" + eid + "?tag=人事").json()
    assert filtered["quality"]["answer_queries"] == 1
    assert filtered["quality"]["pass_rate"] == 1
    assert filtered["summary"]["metric_queries"] == 1
    assert len(filtered["results"]) == 2  # Stable original indexes for reviews.
    text = client.get(BASE + "/" + eid + "/report?tag=人事").json()["markdown"]
    assert "100.0%" in text and "人工回答质量" in text
    assert "业务标签筛选：人事" in text


def test_freeze_requires_evidence_and_case_set_metadata_is_in_snapshot(client):
    body = {
        "knowledge_base_id": "default",
        "name": "拒答集合",
        "cases": [{"query": "未知", "expected": "unanswerable"}],
        "split": "acceptance",
        "review_status": "draft",
        "frozen": True,
    }
    assert client.post(BASE + "/case-sets", json=body).status_code == 422
    body["review_status"] = "verified"
    saved = client.post(BASE + "/case-sets", json=body).json()
    report = finish(
        client,
        client.post(BASE, json=run_body(cases=saved["cases"], case_set_id=saved["case_set_id"])),
    )
    assert report["snapshot"]["case_set"]["frozen"] is True
    assert report["snapshot"]["case_set"]["split"] == "acceptance"


def test_interrupted_retry_keeps_later_completed_seed_results(client, monkeypatch):
    original = client.app.state.retrieval_service.search

    async def first_fails(*args, **kwargs):
        if args[1].query == "first":
            raise ChatError("temporary", "暂不可用", 503)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", first_fails)
    parent = finish(
        client, client.post(BASE, json=run_body(cases=[{"query": "first"}, {"query": "second"}]))
    )
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", slow)
    child = client.post(
        BASE + "/" + parent["evaluation_id"] + "/retry", json={"idempotency_key": str(uuid4())}
    ).json()
    client.portal.call(started.wait)
    assert client.delete(BASE + "/" + child["evaluation_id"]).status_code == 409
    assert (
        client.patch(
            BASE + "/" + child["evaluation_id"] + "/archive", json={"archived": True}
        ).status_code
        == 409
    )
    stopped = client.post(BASE + "/" + child["evaluation_id"] + "/stop").json()
    assert stopped["results"][0]["status"] == "cancelled"
    assert stopped["results"][1]["status"] == "completed"
    assert stopped["results"][1]["reused_from"] == parent["evaluation_id"]
    monkeypatch.setattr(client.app.state.retrieval_service, "search", original)
    again = finish(
        client,
        client.post(
            BASE + "/" + child["evaluation_id"] + "/retry", json={"idempotency_key": str(uuid4())}
        ),
    )
    assert again["quality"]["reused"] == 1
    assert [r["status"] for r in again["results"]] == ["completed", "completed"]


def test_normal_chat_cannot_override_experiment_parameters(client):
    conversation = client.post("/api/v1/conversations").json()
    response = client.post(
        f"/api/v1/conversations/{conversation['thread_id']}/runs",
        json={
            "message": "hello",
            "idempotency_key": str(uuid4()),
            "evaluation_options": {"context_tokens": 512},
        },
    )
    assert response.status_code == 422


def test_project_draft_has_verified_provenance_and_scene_separation():
    from scripts.prepare_evaluation_v3 import build_manifest

    manifest = build_manifest()
    assert len(manifest["cases"]) == 30
    assert manifest["review_status"] == "draft" and manifest["frozen"] is False
    assert sum(c["split"] == "development" for c in manifest["cases"]) == 18
    scenes = {
        split: {c["tags"][-1] for c in manifest["cases"] if c["split"] == split}
        for split in ("development", "acceptance")
    }
    assert not scenes["development"] & scenes["acceptance"]
    assert all(c["judgment_scope"] == "partial" for c in manifest["cases"])


def test_retry_seeds_are_durable_even_when_worker_never_starts(client, monkeypatch):
    original = client.app.state.retrieval_service.search

    async def fail_first(*args, **kwargs):
        if args[1].query == "first":
            raise ChatError("temporary", "暂不可用", 503)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", fail_first)
    parent = finish(
        client, client.post(BASE, json=run_body(cases=[{"query": "first"}, {"query": "second"}]))
    )

    async def never_started(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(client.app.state.evaluations, "execute", never_started)
    child = client.post(
        BASE + "/" + parent["evaluation_id"] + "/retry", json={"idempotency_key": str(uuid4())}
    ).json()
    assert child["results"][1]["status"] == "completed"
    stopped = client.post(BASE + "/" + child["evaluation_id"] + "/stop").json()
    assert stopped["results"][1]["reused_from"] == parent["evaluation_id"]
    assert stopped["results"][0]["status"] == "pending"


@pytest.mark.parametrize("status", ["paused", "recovering"])
def test_stalled_run_status_fails_case_instead_of_blocking_evaluation(client, monkeypatch, status):
    file = upload(client, "default")
    index(client, "default")

    async def stalled(*args, **kwargs):
        return {"status": status}

    monkeypatch.setattr(client.app.state.run_service, "inspect", stalled)
    report = finish(client, client.post(BASE, json=run_body(file["file_id"], kind="rag")))
    assert report["status"] == "completed"
    result = report["results"][0]
    assert result["status"] == "failed"
    assert result["error"]["code"] == "evaluation_run_stalled"
    assert client.app.state.evaluations.tasks == {}


def test_create_rechecks_knowledge_base_after_snapshot(client, monkeypatch):
    kb_id = create_kb(client, "竞态库")
    service = client.app.state.evaluations
    original = service.snapshot
    deleted = []

    async def snapshot_then_delete(body):
        result = await original(body)
        if not deleted:
            deleted.append(True)
            await client.app.state.knowledge_base_service.delete(body.knowledge_base_id)
        return result

    monkeypatch.setattr(service, "snapshot", snapshot_then_delete)
    response = client.post(
        BASE,
        json=run_body(None, knowledge_base_id=kb_id, cases=[{"query": "任意问题"}]),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "knowledge_base_not_found"
    assert client.portal.call(service.history, kb_id, False, 20, 0)["total"] == 0
