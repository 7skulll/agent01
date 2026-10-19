"""分窗保真、批次原子性、纯重排及阈值补位回归。"""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.rag.reranking import RerankError, SiliconFlowReranker
from app.rag.retrieval import RetrievalService, Source
from app.tokenization import TokenCounter


def candidates(count=40):
    return [
        Source(
            citation_id=0,
            chunk_id=str(i),
            file_id=str(i),
            name="制度.txt",
            content=f"制度{i}",
            heading=None,
            page=None,
            kind="text",
            score=1 / (60 + i),
        )
        for i in range(count)
    ]


class Scores:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    async def rerank(self, query, documents):
        self.calls += 1
        return self.values


def rerank(sources, scores, **settings):
    service = RetrievalService(None, None, Settings(_env_file=None, **settings), scores)
    diagnostics = {"degraded": [], "stage_ms": {}, "rerank_applied": False}
    result = asyncio.run(service._rerank("制度", sources, diagnostics))
    return result, diagnostics


def test_windows_cover_whole_document_and_aggregate_best_score_across_batches():
    counter = TokenCounter()
    long = "First sentence. " * 100 + "TAIL_ANSWER_42"
    sent = []

    def handler(request):
        data = json.loads(request.content)
        assert len(data["documents"]) <= 2
        assert data["top_n"] == len(data["documents"])
        for text in data["documents"]:
            assert counter.count(data["query"] + "\n" + text) + 8 <= 64
        sent.extend(data["documents"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": i, "relevance_score": 0.95 if "TAIL_ANSWER_42" in text else 0.2}
                    for i, text in reversed(list(enumerate(data["documents"])))
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = SiliconFlowReranker(
                client,
                "test",
                2,
                max_input_tokens=64,
                safety_ratio=1,
                overlap_tokens=0,
                batch_size=2,
            )
            assert await adapter.rerank("question", [long, "other document"]) == [0.95, 0.2]

    asyncio.run(run())
    assert "".join(sent[:-1]) == long
    assert sent[-1] == "other document"


@pytest.mark.parametrize(
    "budget,text,max_windows,code",
    [
        (16, "document", 64, "query_too_long"),
        (64, "sentence. " * 100, 1, "window_budget_exceeded"),
        (0, "document", 1, "too_many_windows"),
        (1, "document", 64, "query_too_long"),
    ],
)
def test_invalid_window_budgets_fail_before_any_request(budget, text, max_windows, code):
    def unexpected(request):
        pytest.fail("invalid budget must not send a partial request")

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(unexpected)
        ) as client:
            adapter = SiliconFlowReranker(
                client, "test", 1, max_input_tokens=budget, max_windows=max_windows
            )
            with pytest.raises(RerankError, match=code):
                await adapter.rerank(
                    "question " * 10 if budget == 16 else "question", [text, "other"]
                )

    asyncio.run(run())


def test_later_batch_failure_does_not_publish_partial_scores_or_mutate_recall():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        return httpx.Response(200, json={"results": []})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = SiliconFlowReranker(client, "test", 1, batch_size=1)
            service = RetrievalService(None, None, Settings(_env_file=None), adapter)
            original = candidates(2)
            before = [item.model_dump() for item in original]
            diagnostics = {"degraded": [], "stage_ms": {}, "rerank_applied": False}
            assert await service._rerank("制度", original, diagnostics) == original
            assert [item.model_dump() for item in original] == before
            assert diagnostics["rerank_error"] == "invalid_response"

    asyncio.run(run())
    assert len(calls) == 2


def test_all_batches_share_total_timeout():
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 2:
            await asyncio.sleep(10)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(RerankError, match="timeout"):
                await SiliconFlowReranker(client, "test", 0.05, batch_size=1).rerank(
                    "query", ["first", "second", "third"]
                )

    asyncio.run(run())
    assert len(calls) == 2


def test_pure_rerank_can_recover_last_candidate_without_changing_original_scores():
    sources = candidates()
    before = [source.model_dump() for source in sources]
    model = Scores([0.2 - i / 1000 for i in range(39)] + [0.99])
    pure, diagnostics = rerank(sources, model, retrieval_rerank_weight=1)
    fused, _ = rerank(sources, model, retrieval_rerank_weight=0.75)
    assert pure[0].chunk_id == "39" and pure[0].score == 0.99
    assert fused[0].chunk_id != "39"
    assert diagnostics["rerank_strategy"] == "model"
    assert [source.model_dump() for source in sources] == before


def test_zero_weight_without_threshold_skips_remote_call():
    adapter = Scores([])
    sources = candidates(2)
    result, diagnostics = rerank(sources, adapter, retrieval_rerank_weight=0)
    assert result == sources and adapter.calls == 0
    assert diagnostics["rerank_skipped"] == "zero_weight"


def test_threshold_uses_raw_scores_and_never_backfills_unscored_tail():
    sources = candidates(45)
    adapter = Scores([0.1] * 39 + [0.9])
    result, diagnostics = rerank(
        sources, adapter, retrieval_rerank_weight=0.75, retrieval_rerank_min_score=0.5
    )
    assert [source.chunk_id for source in result] == ["39"]
    assert diagnostics["rerank_filtered_count"] == 39
    assert diagnostics["unreranked_count"] == 5


def test_threshold_can_return_empty_without_turning_it_into_service_failure():
    result, diagnostics = rerank(candidates(45), Scores([0.1] * 40), retrieval_rerank_min_score=0.5)
    assert result == [] and diagnostics["rerank_applied"] and not diagnostics["degraded"]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_threshold_must_be_finite(score):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, retrieval_rerank_min_score=score)
