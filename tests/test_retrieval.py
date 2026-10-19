"""检索过滤、并行降级、多查询、重排、来源和追问改写的行为测试。"""

import asyncio

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import ScriptModel, rag_answer, settings, test_database, wait_run
from test_rag_pipeline import BASE, Embeddings, index, rag_run, upload

from app.main import create_app
from app.rag.embedding import EmbeddingError
from app.rag.retrieval import RetrievalService, Source


def search(client, **kwargs):
    return client.post(BASE + "/search", json={"query": "年假", **kwargs})


def test_keyword_is_independent_of_embedding_service(monkeypatch):
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)

        async def failed_query(text):
            raise EmbeddingError("服务不可用", retryable=True)

        monkeypatch.setattr(model, "embed_query", failed_query)
        response = search(client)
        assert response.status_code == 200
        result = response.json()
        assert result["sources"][0]["channels"] == ["keyword"]
        assert result["diagnostics"]["degraded"] == ["vector_failed"]
        assert search(client, mode="vector").status_code == 502
        assert search(client, query="不匹配的航天问题").status_code == 503
        assert search(client, mode="keyword").status_code == 200


def test_keyword_mode_works_after_restart_without_embedding_configuration():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
    with TestClient(create_app(settings())) as client:
        assert search(client, mode="keyword").json()["sources"]


def test_lexical_filter_is_applied_before_limit():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        stale = upload(client, "年假")
        current = upload(client, "员工年假为十天。")
        index(client)
        with test_database() as db:
            db.execute("UPDATE files SET index_status='failed' WHERE file_id=%s", (stale,))
        result = search(client, mode="keyword").json()
        assert result["sources"][0]["file_id"] == current
        assert result["diagnostics"]["keyword_count"] == 1


def test_file_scope_is_shared_by_both_recall_paths():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        first = upload(client, "员工年假为十天。")
        upload(client, "员工年假审批人是主管。")
        index(client)
        for mode in ["hybrid", "vector", "keyword"]:
            result = search(client, mode=mode, file_ids=[first]).json()
            assert {source["file_id"] for source in result["sources"]} == {first}
            assert search(client, mode=mode, file_ids=[]).json()["sources"] == []


def test_recalls_run_in_parallel(monkeypatch):
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)
        ready = client.portal.call(asyncio.Event)
        service = client.app.state.retrieval_service
        original_keywords = service._keywords
        original_query = model.embed_query

        async def gated_query(text):
            await asyncio.wait_for(ready.wait(), 1)
            return await original_query(text)

        async def observed_keywords(*args):
            ready.set()
            return await original_keywords(*args)

        monkeypatch.setattr(model, "embed_query", gated_query)
        monkeypatch.setattr(service, "_keywords", observed_keywords)
        result = search(client).json()
        assert result["sources"][0]["channels"] == ["vector", "keyword"]
        assert result["diagnostics"]["degraded"] == []


def test_keyword_failure_can_degrade_to_vector(monkeypatch):
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)

        async def failed_keywords(*args):
            raise psycopg.OperationalError("injected")

        monkeypatch.setattr(client.app.state.retrieval_service, "_keywords", failed_keywords)
        result = search(client).json()
        assert result["sources"][0]["channels"] == ["vector"]
        assert result["diagnostics"]["degraded"] == ["keyword_failed"]


def test_duplicate_queries_do_not_boost_rrf_score():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        single = search(client).json()
        duplicate = search(client, queries=["年假", " 年假 "]).json()
        assert duplicate["queries"] == ["年假"]
        assert single["sources"][0]["score"] == duplicate["sources"][0]["score"]


def test_expanded_queries_preserve_original_and_recall_other_terms():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client, "员工年假为十天。")
        upload(client, "报销需要发票。")
        index(client)
        result = search(client, queries=["报销"]).json()
        assert result["queries"] == ["年假", "报销"]
        assert len(result["sources"]) == 2
        assert all(source["matched_queries"] for source in result["sources"])


class Reranker:
    def __init__(self, failure=False):
        self.documents = []
        self.failure = failure

    async def rerank(self, query, documents):
        self.documents = documents
        if self.failure:
            raise RuntimeError("temporary")
        return [2.0 if "报销" in text else -1.0 for text in documents]


def test_optional_reranker_receives_natural_text_and_changes_ranking():
    reranker = Reranker()
    with TestClient(
        create_app(
            settings(retrieval_rerank_weight=1), embedding_model=Embeddings(), reranker=reranker
        )
    ) as client:
        upload(client, "年假为十天。")
        expense = upload(client, "报销需要发票。")
        index(client)
        result = search(client, queries=["报销"], top_k=1).json()
        assert result["sources"][0]["file_id"] == expense
        assert result["sources"][0]["rerank_score"] == 2
        assert result["diagnostics"]["rerank_applied"]
        assert any("报销需要发票。" in text for text in reranker.documents)


def test_rerank_failure_retains_recall_results():
    with TestClient(
        create_app(settings(), embedding_model=Embeddings(), reranker=Reranker(True))
    ) as client:
        upload(client)
        index(client)
        result = search(client).json()
        assert result["sources"] and not result["diagnostics"]["rerank_applied"]
        assert "rerank_failed" in result["diagnostics"]["degraded"]


def test_duplicate_content_keeps_alternative_sources():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client, name="手册A.txt")
        upload(client, name="手册B.txt")
        index(client)
        result = search(client).json()
        assert len(result["sources"]) == 1
        assert len(result["sources"][0]["alternatives"]) == 1


def test_neighbors_include_same_section_metadata_for_context_assembly():
    with TestClient(
        create_app(
            settings(chunk_size_tokens=64, chunk_overlap_tokens=8, retrieval_neighbor_chunks=1),
            embedding_model=Embeddings(),
        )
    ) as client:
        upload(client, "\n\n".join(f"年假制度第{i}条：申请需要主管审批。" for i in range(30)))
        index(client)
        result = search(client, top_k=1).json()
        source = Source(**result["sources"][0])
        assert source.supporting_chunks
        assert all(
            neighbor["file_id"] == source.file_id
            and neighbor["heading"] == source.heading
            and neighbor["page"] == source.page
            and neighbor["kind"] == "text"
            and abs(neighbor["chunk_index"] - source.chunk_index) == 1
            for neighbor in source.supporting_chunks
        )


def test_followup_is_rewritten_and_query_plan_is_checkpointed():
    chat = ScriptModel(
        outputs=[
            rag_answer(content="十天。"),
            AIMessage(content='["年假需要谁审批？"]'),
            RuntimeError("temporary"),
            rag_answer(content="主管审批。"),
        ]
    )
    embeddings = Embeddings()
    with TestClient(
        create_app(settings(rag_query_rewrite=True), chat_model=chat, embedding_model=embeddings)
    ) as client:
        upload(client, "年假为十天，申请需要主管审批。")
        index(client)
        cid, first = rag_run(client)
        assert wait_run(client, first)["status"] == "completed"
        response = client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={
                "message": "那需要谁审批？",
                "idempotency_key": "followup",
                "knowledge_base_id": "default",
            },
        )
        rid = response.json()["run_id"]
        assert wait_run(client, rid)["status"] == "failed"
        assert "年假需要谁审批？" in embeddings.queries
        calls = len(embeddings.queries)
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 202
        result = wait_run(client, rid)
        assert result["status"] == "completed"
        assert len(embeddings.queries) == calls
        assert len(chat.seen) == 4
        assert "query_plan" in client.get(f"/api/v1/runs/{rid}/events").text


def test_invalid_query_plan_falls_back_without_losing_original_question():
    chat = ScriptModel(
        outputs=[
            rag_answer(content="十天。"),
            AIMessage(content="invalid JSON"),
            rag_answer(content="十天。"),
        ]
    )
    with TestClient(
        create_app(settings(rag_query_rewrite=True), chat_model=chat, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        cid, first = rag_run(client)
        assert wait_run(client, first)["status"] == "completed"
        response = client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={"message": "年假", "idempotency_key": "next", "knowledge_base_id": "default"},
        )
        rid = response.json()["run_id"]
        assert wait_run(client, rid)["status"] == "completed"
        assert '"query_degraded": true' in client.get(f"/api/v1/runs/{rid}/events").text


@pytest.mark.parametrize("behavior", ["timeout", "nan", "wrong_count"])
def test_invalid_or_slow_reranker_degrades(behavior):
    class BadReranker:
        async def rerank(self, query, documents):
            if behavior == "timeout":
                await asyncio.sleep(1)
            if behavior == "nan":
                return [float("nan")] * len(documents)
            return []

    with TestClient(
        create_app(
            settings(retrieval_rerank_timeout_seconds=0.01),
            embedding_model=Embeddings(),
            reranker=BadReranker(),
        )
    ) as client:
        upload(client)
        index(client)
        result = search(client).json()
        assert result["sources"] and result["diagnostics"]["degraded"] == ["rerank_failed"]


def test_empty_scope_and_no_matches_do_not_trigger_reranking():
    reranker = Reranker()
    with TestClient(
        create_app(settings(), embedding_model=Embeddings(), reranker=reranker)
    ) as client:
        upload(client)
        index(client)
        assert search(client, file_ids=[]).json()["sources"] == []
        assert reranker.documents == []


def test_document_diversity_limits_one_file_without_discarding_other_files():
    with TestClient(
        create_app(
            settings(chunk_size_tokens=64, chunk_overlap_tokens=8, retrieval_max_per_file=1),
            embedding_model=Embeddings(),
        )
    ) as client:
        first = upload(client, "\n\n".join(f"年假第{i}条申请规则：必须审批。" for i in range(20)))
        second = upload(client, "员工年假为十天。")
        index(client)
        sources = search(client, top_k=20).json()["sources"]
        assert len(sources) == 2 and {source["file_id"] for source in sources} == {first, second}


def test_deduplication_preserves_code_indentation():
    def source(index, content):
        return Source(
            citation_id=index,
            chunk_id=str(index),
            file_id="f",
            name="code.md",
            content=content,
            heading=None,
            page=None,
            kind="code",
            score=1,
        )

    sources = RetrievalService._dedupe(
        [source(1, "if ready:\n    run()"), source(2, "if ready:\n        run()")]
    )
    assert len(sources) == 2
    assert all(not source.alternatives for source in sources)
