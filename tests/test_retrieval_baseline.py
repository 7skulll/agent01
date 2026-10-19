"""基础版：真实 PostgreSQL BM25、分词和查询/通道分级 RRF；高级能力须显式开启。"""

import pytest
from fastapi.testclient import TestClient
from support import ScriptModel, rag_answer, settings, test_database, wait_run
from test_rag_pipeline import BASE, Embeddings, index, rag_run, upload

from app.main import create_app
from app.rag.terms import document_terms, search_terms
from scripts.eval_retrieval import metrics


def test_document_analyzer_preserves_frequency_and_order():
    assert document_terms("年假 年假。alpha alpha") == ["年", "假", "年", "假", "alpha", "alpha"]
    assert search_terms("年假 年假。alpha alpha") == ["年", "假", "alpha"]


def test_bm25_uses_term_frequency_in_ranking():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        first = upload(client, "zeta zeta zeta alpha", name="a.txt")
        second = upload(client, "zeta alpha alpha alpha", name="b.txt")
        index(client)
        with test_database() as db:
            terms = db.execute(
                "SELECT terms FROM chunk_terms WHERE file_id=%s", (first,)
            ).fetchone()[0]
        assert terms.split().count("zeta") == 3
        result = client.post(BASE + "/search", json={"query": "zeta", "mode": "keyword"})
        assert result.status_code == 200
        sources = result.json()["sources"]
        assert [source["file_id"] for source in sources] == [first, second]
        assert sources[0]["keyword_score"] > sources[1]["keyword_score"]


def test_single_query_fuses_channels_without_score_normalization(monkeypatch):
    def row(key):
        return {
            "chunk_id": key,
            "file_id": key,
            "name": f"{key}.txt",
            "content": key,
            "heading": None,
            "page": None,
            "kind": "text",
            "chunk_index": 0,
        }

    with TestClient(create_app(settings())) as client:
        service = client.app.state.retrieval_service
        calls = []
        original = service._fuse

        def observe(*args):
            calls.append(1)
            return original(*args)

        async def dense(kb, body, queries):
            assert queries == ["test"]
            return [[("a", 0.9), ("b", 0.8)]], {key: row(key) for key in ["a", "b"]}, 0

        async def keyword(kb, body, queries):
            assert queries == ["test"]
            # 与余弦完全不同的数值范围，仅名次参与融合。
            return [[("b", 200), ("c", 100)]], {key: row(key) for key in ["b", "c"]}, 0

        monkeypatch.setattr(service, "_dense", dense)
        monkeypatch.setattr(service, "_keywords", keyword)
        monkeypatch.setattr(service, "_fuse", observe)
        result = client.post(BASE + "/search", json={"query": "test", "top_k": 3}).json()
        assert [s["chunk_id"] for s in result["sources"]] == ["b", "a", "c"]
        assert result["sources"][0]["score"] == pytest.approx(0.5 / 62 + 0.5 / 61)
        assert calls == [1]
        assert not result["diagnostics"]["rerank_applied"]
        assert "neighbors" not in result["diagnostics"]["stage_ms"]


def test_default_followup_uses_original_question_without_extra_model_call():
    embeddings = Embeddings()
    chat = ScriptModel(outputs=[rag_answer(content="十天。"), rag_answer(content="主管。")])
    with TestClient(create_app(settings(), chat_model=chat, embedding_model=embeddings)) as client:
        upload(client, "年假为十天，申请需要主管审批。")
        index(client)
        cid, first = rag_run(client)
        assert wait_run(client, first)["status"] == "completed"
        response = client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={
                "message": "那谁审批？",
                "idempotency_key": "followup",
                "knowledge_base_id": "default",
            },
        )
        assert wait_run(client, response.json()["run_id"])["status"] == "completed"
        assert len(chat.seen) == 2
        assert embeddings.queries == ["年假有多少天？", "那谁审批？"]
        assert not settings().rag_query_rewrite and not settings().rag_query_expand
        assert settings().retrieval_neighbor_chunks == 0


def test_baseline_metrics_count_misses_and_exclude_unanswerable_from_recall():
    rows = [
        {"relevant": ["a"], "ranked_ids": ["b", "a"], "local_search_ms": 2},
        {"relevant": ["c"], "ranked_ids": [], "local_search_ms": 4},
        {"relevant": [], "ranked_ids": ["d"], "local_search_ms": 3},
    ]
    result = metrics(rows)
    assert result["queries"] == 2
    assert result["recall@1"] == 0 and result["recall@3"] == 0.5
    assert result["mrr@10"] == 0.25
    assert result["no_answer_queries_returning_candidates"] == 1


def test_evaluation_runner_reuses_query_vectors_and_isolates_database(tmp_path, monkeypatch):
    import argparse
    import asyncio
    import hashlib
    import json
    from pathlib import Path

    import scripts.eval_retrieval as evaluation

    fixture = Path(__file__).resolve().parents[1] / "evals/retrieval_baseline.json"
    embeddings = Embeddings()
    monkeypatch.setattr(evaluation, "Settings", settings)
    monkeypatch.setattr(evaluation, "build_embedding_model", lambda config: embeddings)
    output = tmp_path / "result.json"
    asyncio.run(
        evaluation.evaluate(argparse.Namespace(dataset=fixture, output=output, min_score=0.2))
    )
    report = json.loads(output.read_text())
    dataset = json.loads(fixture.read_text())
    assert len(embeddings.queries) == len({q["query"] for q in dataset["queries"]})
    assert len([text for batch in embeddings.calls for text in batch]) == len(dataset["documents"])
    assert set(report["runs"]) == {"keyword", "vector", "hybrid"}
    assert report["dataset_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert not report["parameters"]["rewrite"] and not report["parameters"]["rerank"]
    assert not (tmp_path / "data").exists()
