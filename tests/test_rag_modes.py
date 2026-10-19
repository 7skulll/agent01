"""知识库问答的检索模式、依赖边界、幂等及持久化。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import ScriptModel, events, rag_answer, settings, wait_run
from test_rag_pipeline import Embeddings, db_rows, index, upload

from app.main import create_app


def request_run(client, mode="keyword", *, conversation=None, key="mode-test"):
    cid = conversation or client.post("/api/v1/conversations").json()["thread_id"]
    body = {
        "message": "年假有多少天？",
        "idempotency_key": key,
        "knowledge_base_id": "default",
        "retrieval_mode": mode,
    }
    return cid, body, client.post(f"/api/v1/conversations/{cid}/runs", json=body)


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_knowledge_answer_without_embedding(mode):
    model = ScriptModel(outputs=[rag_answer(content="年假为十天。")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        upload(client)
        index(client)
        cid, body, response = request_run(client, mode)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        result = wait_run(client, run_id)
        assert result["status"] == "completed", result
        assert result["retrieval_mode"] == mode
        assert result["sources"] and not db_rows("chunk_vectors")
        frames = events(client.get(f"/api/v1/runs/{run_id}/events"))
        end = next(data for _, event, data in frames if event == "retrieval_end")
        assert end["diagnostics"]["keyword_count"] > 0
        assert end["diagnostics"]["degraded"] == (["vector_failed"] if mode == "hybrid" else [])
        repeat = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
        assert repeat.status_code == 202 and repeat.json()["run_id"] == run_id
        body["retrieval_mode"] = "keyword" if mode == "hybrid" else "hybrid"
        conflict = client.post(f"/api/v1/conversations/{cid}/runs", json=body)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    with TestClient(create_app(settings(), chat_model=model)) as client:
        persisted = client.get(f"/api/v1/runs/{run_id}").json()
        assert persisted["retrieval_mode"] == mode and persisted["answer"] == result["answer"]


def test_keyword_mode_never_calls_configured_embedding():
    embedding = Embeddings()
    with TestClient(
        create_app(
            settings(),
            embedding_model=embedding,
            chat_model=ScriptModel(outputs=[rag_answer(content="十天。")]),
        )
    ) as client:
        upload(client)
        index(client)
        _, _, response = request_run(client)
        assert wait_run(client, response.json()["run_id"])["status"] == "completed"
        assert embedding.queries == []


@pytest.mark.parametrize("mode,weight", [("vector", 0.5), ("hybrid", 1)])
def test_vector_only_paths_fail_before_creating_task_without_embedding(mode, weight):
    with TestClient(
        create_app(
            settings(retrieval_dense_weight=weight),
            chat_model=ScriptModel(outputs=[AIMessage(content="不得调用")]),
        )
    ) as client:
        _, _, response = request_run(client, mode)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "embedding_not_configured"
        assert db_rows("runs") == []


def test_keyword_empty_scope_returns_insufficient_without_answer_model():
    model = ScriptModel(outputs=[AIMessage(content="不得调用")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        _, _, response = request_run(client)
        assert response.status_code == 202
        result = wait_run(client, response.json()["run_id"])
        assert result["status"] == "completed" and not result["sources"]
        assert "资料" in result["answer"] and not model.seen


def test_retrieval_mode_validation_and_chat_dependency():
    with TestClient(create_app(settings())) as client:
        cid = client.post("/api/v1/conversations").json()["thread_id"]
        path = f"/api/v1/conversations/{cid}/runs"
        assert (
            client.post(
                path,
                json={
                    "message": "你好",
                    "idempotency_key": uuid4().hex,
                    "knowledge_base_id": "default",
                    "retrieval_mode": "unknown",
                },
            ).status_code
            == 422
        )
        _, _, response = request_run(client, conversation=cid)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "model_not_configured"


def test_keyword_mode_checkpoint_resumes_without_embedding_after_restart(monkeypatch):
    with TestClient(
        create_app(
            settings(),
            chat_model=ScriptModel(outputs=[RuntimeError("injected_model_failure")]),
        )
    ) as client:
        upload(client)
        index(client)
        _, _, response = request_run(client)
        run_id = response.json()["run_id"]
        failed = wait_run(client, run_id)
        assert failed["status"] == "failed" and failed["error"]["retryable"]
        service = client.app.state.run_service

        async def state():
            return (await service.get_graph().aget_state(service.config(run_id))).values

        checkpoint = client.portal.call(state)
        assert checkpoint["retrieval_done"] and checkpoint["retrieval_mode"] == "keyword"
    with TestClient(
        create_app(
            settings(),
            chat_model=ScriptModel(outputs=[rag_answer(content="年假为十天。")]),
        )
    ) as client:

        async def unexpected_search(*args):
            raise AssertionError("successful retrieval must be reused")

        monkeypatch.setattr(client.app.state.retrieval_service, "search", unexpected_search)
        assert client.post(f"/api/v1/runs/{run_id}/resume").status_code == 202
        result = wait_run(client, run_id)
        assert result["status"] == "completed" and result["retrieval_mode"] == "keyword"
        assert not db_rows("chunk_vectors")
