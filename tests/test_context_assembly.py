import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import ScriptModel, events, rag_answer, settings, test_database, wait_run
from test_rag_pipeline import Embeddings, index, upload

from app.main import create_app
from app.rag.context_assembly import ContextAssembler


def submit(client, conversation, question="年假几天？", kb_id="default"):
    response = client.post(
        f"/api/v1/conversations/{conversation}/runs",
        json={
            "message": question,
            "idempotency_key": uuid4().hex,
            "knowledge_base_id": kb_id,
            "retrieval_mode": "hybrid",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def conversation(client):
    return client.post("/api/v1/conversations").json()["thread_id"]


def test_switching_kb_resets_answer_and_query_planner_history_but_preserves_ui_history():
    model = ScriptModel(outputs=[rag_answer(content=f"私有答案{i}") for i in range(3)])
    with TestClient(
        create_app(settings(rag_query_rewrite=True), chat_model=model, embedding_model=Embeddings())
    ) as client:
        upload(client, "员工年假为十天，默认库内部资料。")
        second = client.post("/api/v1/knowledge-bases", json={"name": "另一知识库"}).json()["kb_id"]
        response = client.post(
            f"/api/v1/knowledge-bases/{second}/files",
            files={"file": ("制度.txt", "年假为二十天，另一库内部资料。".encode())},
            headers={"Idempotency-Key": "file"},
        )
        assert response.status_code == 201
        index(client)
        assert client.post(f"/api/v1/knowledge-bases/{second}/index").status_code == 202
        client.portal.call(client.app.state.index_service.wait)
        cid = conversation(client)
        for i, kb_id in enumerate(["default", second, "default"]):
            rid = submit(client, cid, kb_id=kb_id)
            result = wait_run(client, rid)
            assert result["status"] == "completed", result
            assert len(model.seen[-1]) == 2
            assert all(f"私有答案{j}" not in str(model.seen[-1]) for j in range(i))
            assert (
                json.loads(model.seen[-1][-1].text)["evidence"][0]["content"]
                == result["sources"][0]["content"]
            )
        assert len(model.seen) == 3  # 切库没有多调用一次问题改写模型。
        assert len(client.get(f"/api/v1/conversations/{cid}").json()["messages"]) == 6


def test_recent_three_turns_only_enter_model_while_full_history_remains():
    model = ScriptModel(outputs=[rag_answer(content="十天。")])
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        cid = conversation(client)
        for i in range(5):
            assert wait_run(client, submit(client, cid, f"年假问题{i}"))["status"] == "completed"
        latest = model.seen[-1]
        assert len(latest) == 8
        assert "年假问题0" not in str(latest)
        assert "年假问题1" in str(latest)
        assert len(client.get(f"/api/v1/conversations/{cid}").json()["messages"]) == 10


def test_failure_after_assembly_reuses_exact_request_across_restart(monkeypatch):
    config = settings()
    model = ScriptModel(outputs=[RuntimeError("temporary")])
    embeddings = Embeddings()
    with TestClient(create_app(config, chat_model=model, embedding_model=embeddings)) as client:
        upload(client)
        index(client)
        cid = conversation(client)
        rid = submit(client, cid)
        assert wait_run(client, rid)["status"] == "failed"
        original_messages = model.seen[-1]
        frames = events(client.get(f"/api/v1/runs/{rid}/events"))
        assembled = [data for _, name, data in frames if name == "context_assembly"]
        assert len(assembled) == 1

    def no_assembly(*args):
        raise AssertionError("already assembled")

    monkeypatch.setattr(ContextAssembler, "assemble", no_assembly)
    second = ScriptModel(outputs=[rag_answer(content="十天。")])
    with TestClient(create_app(config, chat_model=second, embedding_model=embeddings)) as client:

        async def no_search(*args):
            raise AssertionError("already retrieved")

        monkeypatch.setattr(client.app.state.retrieval_service, "search", no_search)
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 202
        result = wait_run(client, rid)
        assert result["status"] == "completed", result
        assert second.seen[-1] == original_messages
        assert len(embeddings.queries) == 1
        assert result["model_calls"] == 2
        frames = events(client.get(f"/api/v1/runs/{rid}/events"))
        assert len([name for _, name, _ in frames if name == "context_assembly"]) == 1
        assert len(client.get(f"/api/v1/conversations/{cid}").json()["messages"]) == 2


def test_failure_between_retrieval_and_assembly_does_not_repeat_embedding(monkeypatch):
    model = ScriptModel(outputs=[rag_answer(content="十天。")])
    embeddings = Embeddings()
    original = ContextAssembler.assemble

    def fail(*args):
        raise RuntimeError("assembly interruption")

    with TestClient(create_app(settings(), chat_model=model, embedding_model=embeddings)) as client:
        upload(client)
        index(client)
        monkeypatch.setattr(ContextAssembler, "assemble", fail)
        rid = submit(client, conversation(client))
        assert wait_run(client, rid)["status"] == "failed"
        assert not model.seen
        monkeypatch.setattr(ContextAssembler, "assemble", original)
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 202
        assert wait_run(client, rid)["status"] == "completed"
        assert len(embeddings.queries) == 1


def test_oversized_structured_evidence_reports_no_context_fit_without_model():
    model = ScriptModel(outputs=[AIMessage(content="不能调用")])
    with TestClient(
        create_app(settings(rag_context_tokens=64), chat_model=model, embedding_model=Embeddings())
    ) as client:
        upload(client, "年假表格 | 金额\n" * 30)
        index(client)
        with test_database() as db:
            db.execute("UPDATE chunks SET kind='table'")
        rid = submit(client, conversation(client))
        result = wait_run(client, rid)
        assert result["status"] == "completed" and not result["sources"]
        assert "预算" in result["answer"] and not model.seen
        frames = events(client.get(f"/api/v1/runs/{rid}/events"))
        diagnostics = next(data for _, name, data in frames if name == "context_assembly")
        assert diagnostics["empty_reason"] == "no_context_fit"


def test_legacy_unfinished_graph_cannot_resume_after_upgrade():
    model = ScriptModel(outputs=[RuntimeError("retry")])
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        rid = submit(client, conversation(client))
        assert wait_run(client, rid)["status"] == "failed"
        with test_database() as db:
            db.execute("UPDATE runs SET graph_version='chat-rag-v4' WHERE run_id=%s", (rid,))
        response = client.post(f"/api/v1/runs/{rid}/resume")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "graph_version_mismatch"


def test_pause_after_retrieval_preserves_candidates_before_assembly(monkeypatch):
    embeddings = Embeddings()
    model = ScriptModel(outputs=[rag_answer(content="十天。")])
    with TestClient(create_app(settings(), chat_model=model, embedding_model=embeddings)) as client:
        upload(client)
        index(client)

        async def gates():
            return asyncio.Event(), asyncio.Event()

        entered, release = client.portal.call(gates)
        service = client.app.state.retrieval_service
        original = service.search

        async def delayed(*args):
            result = await original(*args)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(service, "search", delayed)
        rid = submit(client, conversation(client))
        client.portal.call(entered.wait)
        assert client.post(f"/api/v1/runs/{rid}/pause").status_code == 200
        client.portal.call(release.set)
        assert wait_run(client, rid, ("paused",))["status"] == "paused"

        async def snapshot():
            run_service = client.app.state.run_service
            return await run_service.get_graph().aget_state(run_service.config(rid))

        checkpoint = client.portal.call(snapshot)
        assert checkpoint.values["retrieval_done"]
        assert not checkpoint.values.get("assembly_done")
        assert not model.seen
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 202
        assert wait_run(client, rid)["status"] == "completed"
        assert len(embeddings.queries) == 1
