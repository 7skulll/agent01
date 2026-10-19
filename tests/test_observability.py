"""实际 HTTP / 上传 / 检索 / RAG 路径的指标与隐私边界。"""

from uuid import UUID

from fastapi.testclient import TestClient
from support import ScriptModel, rag_answer, settings, wait_run
from test_rag_pipeline import BASE, Embeddings, index, rag_run, upload

from app.main import create_app


class FailingReranker:
    async def rerank(self, query, documents):
        raise RuntimeError("private_provider_response")


def count(snapshot, name, **labels):
    return sum(
        row["value"]
        for row in snapshot["counters"]
        if row["name"] == name and all(row["labels"].get(k) == v for k, v in labels.items())
    )


def test_pipeline_metrics_failure_degradation_usage_and_http_privacy():
    model = ScriptModel(
        outputs=[
            rag_answer(
                content="年假为十天。",
                usage_metadata={
                    "input_tokens": 40,
                    "output_tokens": 9,
                    "total_tokens": 49,
                },
            )
        ]
    )
    with TestClient(
        create_app(
            settings(), chat_model=model, embedding_model=Embeddings(), reranker=FailingReranker()
        )
    ) as client:
        upload(client, name="private_filename.txt")
        index(client)
        response = client.post(
            BASE + "/search?private_query=secret",
            json={
                "query": "年假 private_question",
                "mode": "keyword",
            },
            headers={"Authorization": "Bearer private_key", "X-Request-ID": "private_client_id"},
        )
        assert response.status_code == 200 and UUID(response.headers["x-request-id"])
        assert response.json()["diagnostics"]["degraded"] == ["rerank_failed"]
        _, run_id = rag_run(client)
        assert wait_run(client, run_id)["status"] == "completed"
        for path in ("/not-found/private_1", "/not-found/private_2"):
            assert client.get(path).status_code == 404
        snapshot = client.get("/api/v1/metrics").json()
        assert client.get("/api/v1/metrics").json()["counters"] == snapshot["counters"]
        text = str(snapshot)
        for private in ("private_", run_id, "secret", "Bearer"):
            assert private not in text
        stages = {row["stage"] for row in snapshot["timings"]}
        assert {
            "http",
            "parse",
            "chunk",
            "lexical_index",
            "vector_index",
            "embedding_documents",
            "retrieval",
            "keyword_recall",
            "vector_recall",
            "fusion",
            "rerank",
            "answer_model",
        } <= stages
        assert any(t["stage"] == "rerank" and t["outcome"] == "error" for t in snapshot["timings"])
        assert count(snapshot, "retrieval_degradations", reason="rerank_failed") == 2
        assert count(snapshot, "http_requests", route="unmatched", status="404") == 2
        assert count(snapshot, "tokens_actual_input", stage="answer") == 40
        assert count(snapshot, "tokens_actual_output", stage="answer") == 9
        assert count(snapshot, "tokens_estimated_input", stage="embedding_documents") > 0
        assert all(value == 0 for stage, value in snapshot["active"].items() if stage != "http")


def test_retrieval_errors_and_independent_registry_lifecycle():
    with TestClient(create_app(settings())) as client:
        response = client.post(BASE + "/search", json={"query": "年假", "mode": "vector"})
        assert response.status_code == 503
        snapshot = client.get("/api/v1/metrics").json()
        assert count(snapshot, "retrieval_results", outcome="error") == 1
        assert any(
            t["stage"] == "retrieval" and t["outcome"] == "error" for t in snapshot["timings"]
        )
    with TestClient(create_app(settings())) as client:
        assert client.get("/api/v1/metrics").json()["counters"] == []
