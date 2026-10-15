"""FastGPT 原生分词快照、分级融合、模式预算及阈值边界。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support import settings
from test_rag_pipeline import BASE, Embeddings, index, upload

from app.main import create_app
from app.rag.retrieval import SearchRequest, ranked_concat, recall_limits
from app.rag.terms import document_terms

CASES = json.loads((Path(__file__).parent / "fixtures/fastgpt_terms.json").read_text())["fixtures"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["text"][:30])
def test_terms_match_fastgpt_native_node_reference(case):
    assert document_terms(case["text"]) == case["terms"]


def test_rrf_ignores_empty_and_zero_weight_lists_and_passes_single_list_through():
    items = [("a", 0.9), ("b", 0.3)]
    assert ranked_concat([(0.5, items), (0.5, [])]) is items
    assert ranked_concat([(0, [("c", 1)]), (1, items)]) is items
    assert ranked_concat([(1, []), (0, items)]) == []


def test_query_rrf_is_completed_per_channel_before_weighted_channel_rrf():
    def row(key):
        return {
            "chunk_id": key,
            "file_id": key,
            "name": key,
            "content": key,
            "heading": None,
            "page": None,
            "kind": "text",
            "chunk_index": 0,
        }

    with TestClient(create_app(settings())) as client:
        service = client.app.state.retrieval_service
        body = SearchRequest(query="one", queries=["two"])
        result = service._fuse(
            [
                ("vector", "one", [("a", 0.9), ("b", 0.8)]),
                ("vector", "two", [("b", 0.95), ("c", 0.7)]),
                ("keyword", "one", [("c", 30), ("a", 20)]),
                ("keyword", "two", [("c", 40), ("b", 10)]),
            ],
            {key: row(key) for key in "abc"},
            body,
            ["one", "two"],
        )
    assert [s.chunk_id for s in result] == ["b", "c", "a"]
    assert result[0].score == pytest.approx(0.5 / 61 + 0.5 / 63)
    assert result[2].score == pytest.approx(1 / 62)
    assert result[0].vector_score == 0.95
    assert result[0].keyword_score == 10
    assert result[0].matched_queries == ["one", "two"]


@pytest.mark.parametrize(
    "mode,limits", [("vector", (100, 0)), ("keyword", (0, 100)), ("hybrid", (80, 60))]
)
def test_mode_limits_match_fastgpt(mode, limits):
    assert recall_limits(mode) == limits


def test_hybrid_does_not_prefilter_dense_hits_by_similarity():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        file_id = upload(client, "员工年假为十天。")
        index(client)
        payload = {"query": "飞行速度", "min_score": 0.99}
        vector = client.post(BASE + "/search", json={**payload, "mode": "vector"}).json()
        hybrid = client.post(BASE + "/search", json={**payload, "mode": "hybrid"}).json()
        assert vector["sources"] == []
        assert vector["diagnostics"]["using_similarity_filter"] is True
        assert hybrid["sources"][0]["file_id"] == file_id
        assert hybrid["sources"][0]["vector_score"] == 0
        assert hybrid["diagnostics"]["using_similarity_filter"] is False


@pytest.mark.parametrize("weight,disabled", [(0, "_dense"), (1, "_keywords")])
def test_zero_weight_disables_the_entire_channel(weight, disabled, monkeypatch):
    with TestClient(
        create_app(settings(retrieval_dense_weight=weight), embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)

        async def unexpected(*args):
            raise AssertionError("disabled channel must not run")

        monkeypatch.setattr(client.app.state.retrieval_service, disabled, unexpected)
        response = client.post(BASE + "/search", json={"query": "年假"})
        assert response.status_code == 200
        assert response.json()["sources"]
        assert response.json()["diagnostics"]["degraded"] == []


def test_query_syntax_is_escaped_and_stopword_only_query_returns_empty():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        for query in ['" OR * (NEAR) :', "the and 请问 如何"]:
            result = client.post(BASE + "/search", json={"query": query, "mode": "keyword"})
            assert result.status_code == 200
            assert result.json()["sources"] == []
