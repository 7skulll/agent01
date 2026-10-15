"""检索审查发现的问题：空范围、候选保留、批次对应关系和切片重叠。"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from support import settings
from test_embedding import FakeEmbeddings, model
from test_rag_pipeline import Embeddings

from app.main import create_app
from app.rag.chunking import chunk_blocks
from app.rag.embedding import EmbeddingError
from app.rag.parsers import Block
from app.rag.retrieval import Source
from app.tokenization import TokenCounter


@pytest.mark.parametrize("mode", ["vector", "hybrid", "keyword"])
def test_empty_file_scope_never_calls_embedding_or_reports_outage(mode, monkeypatch):
    embeddings = Embeddings()
    calls = []

    async def unavailable(text):
        calls.append(text)
        raise EmbeddingError("unavailable", retryable=True)

    monkeypatch.setattr(embeddings, "embed_query", unavailable)
    with TestClient(create_app(settings(), embedding_model=embeddings)) as client:
        result = client.post(
            "/api/v1/knowledge-bases/default/search",
            json={"query": "年假", "mode": mode, "file_ids": []},
        )
        assert result.status_code == 200
        assert result.json()["sources"] == []
        assert result.json()["diagnostics"]["degraded"] == []
        assert calls == []


def test_rerank_keeps_tail_to_fill_results_after_document_diversity():
    class Reranker:
        async def rerank(self, query, documents):
            return list(range(len(documents)))

    candidates = [
        Source(
            citation_id=0,
            chunk_id=f"chunk-{i}",
            file_id="dominant" if i < 40 else f"file-{i}",
            name="manual.txt",
            content=f"年假政策 {i}",
            heading=None,
            page=None,
            kind="text",
            score=1,
        )
        for i in range(45)
    ]
    with TestClient(create_app(settings(), reranker=Reranker())) as client:
        service = client.app.state.retrieval_service
        diagnostics = {"degraded": [], "stage_ms": {}}
        ranked = client.portal.call(service._rerank, "年假", candidates, diagnostics)
    assert len(ranked) == 45
    assert [source.chunk_id for source in ranked[40:]] == [f"chunk-{i}" for i in range(40, 45)]
    # 前 40 条来自同一文件，尾部应仍可补充另外 5 个文件。
    assert len({source.file_id for source in ranked}) == 6
    assert all(source.rerank_score is None for source in ranked[40:])


def test_embedding_batch_count_mismatch_cannot_cancel_out_in_later_batch():
    class Misaligned(FakeEmbeddings):
        async def aembed_documents(self, texts):
            self.calls.append(list(texts))
            count = 1 if len(self.calls) == 1 else 3
            return [[1.0, 2.0, 3.0, 4.0] for _ in range(count)]

    client = Misaligned()
    with pytest.raises(EmbeddingError, match="数量"):
        asyncio.run(model(client, batch_size=2).embed_documents(["a", "b", "c", "d"]))
    assert len(client.calls) == 1


@pytest.mark.parametrize("vector", [[0.0, 0.0], [1e308, 1e308, 1e308, 1e308]])
def test_invalid_vector_norm_is_rejected_even_when_normalization_is_disabled(vector):
    with pytest.raises(EmbeddingError):
        asyncio.run(model(FakeEmbeddings(vectors=[vector]), normalize=False).embed_documents(["a"]))


def test_configured_overlap_above_quarter_chunk_is_preserved():
    counter = TokenCounter("heuristic")
    chunks = chunk_blocks(
        [Block(text="甲" * 64 + "乙" * 64 + "丙" * 64)], size=64, overlap=32, counter=counter
    )
    assert chunks[0].content == "甲" * 32
    assert chunks[1].content == "甲" * 64
    assert all(counter.count(chunk.content) <= 64 for chunk in chunks)


@pytest.mark.parametrize(
    "rewrite,expand,output",
    [
        (True, False, '["年假审批人是谁？", "带薪年假审批流程"]'),
        (False, True, '["模型擅自改写的第一条", "带薪年假审批流程"]'),
    ],
)
def test_query_plan_cannot_override_disabled_rewrite_or_expansion(rewrite, expand, output):
    from langchain_core.messages import AIMessage, HumanMessage
    from support import ScriptModel

    from app.agent.runtime import AgentRuntime
    from app.rag.query_planning import plan_queries

    class Repository:
        async def reserve_call(self, *args):
            return 1

    runtime = AgentRuntime(
        settings(rag_query_rewrite=rewrite, rag_query_expand=expand),
        None,
        None,
        ScriptModel(outputs=[AIMessage(content=output)]),
    )
    state = {
        "run_id": "test",
        "messages": [
            HumanMessage(content="年假几天？"),
            AIMessage(content="十天"),
            HumanMessage(content="那谁批准？"),
        ],
    }
    plan = asyncio.run(plan_queries(runtime, Repository(), state, {}))
    assert plan["search_queries"] == ["那谁批准？"]
    assert plan["rerank_query"] == "那谁批准？"
    assert plan["query_degraded"]


def test_expansion_without_rewrite_keeps_original_rerank_query_and_excludes_history():
    import json

    from langchain_core.messages import AIMessage, HumanMessage
    from support import ScriptModel

    from app.agent.runtime import AgentRuntime
    from app.rag.query_planning import plan_queries

    class Repository:
        async def reserve_call(self, *args):
            return 1

    chat = ScriptModel(outputs=[AIMessage(content='["年假审批", "带薪假期审批人"]')])
    runtime = AgentRuntime(
        settings(rag_query_rewrite=False, rag_query_expand=True), None, None, chat
    )
    state = {
        "run_id": "test",
        "messages": [
            HumanMessage(content="不相关历史"),
            AIMessage(content="历史回答"),
            HumanMessage(content="年假审批"),
        ],
    }
    plan = asyncio.run(plan_queries(runtime, Repository(), state, {}))
    assert plan["search_queries"] == ["年假审批", "带薪假期审批人"]
    assert plan["rerank_query"] == "年假审批" and not plan["query_degraded"]
    assert json.loads(chat.seen[0][-1].text)["history"] == []
