"""有界累计指标、取消处理及真实/估算 Token 的口径。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.observability import Metrics


def test_histograms_thread_safety_and_snapshot_copy():
    metrics = Metrics()

    def work(_):
        metrics.increment("requests", route="/runs/{run_id}")
        metrics.observe("retrieval", 0.05)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(work, range(1000)))
    result = metrics.snapshot()
    assert result["counters"][0]["value"] == 1000
    timing = result["timings"][0]
    assert timing["count"] == 1000 and timing["mean_ms"] == 50
    assert [b["count"] for b in timing["buckets"]][:3] == [0, 1000, 1000]
    result["timings"][0]["count"] = -1
    assert metrics.snapshot()["timings"][0]["count"] == 1000


def test_error_and_cancellation_release_active_gauges():
    metrics = Metrics()
    with pytest.raises(ValueError), metrics.span("parse"):
        raise ValueError("private content")
    with pytest.raises(asyncio.CancelledError), metrics.span("rerank"):
        raise asyncio.CancelledError()
    snapshot = metrics.snapshot()
    assert snapshot["active"] == {"parse": 0, "rerank": 0}
    assert {(t["stage"], t["outcome"]) for t in snapshot["timings"]} == {
        ("parse", "error"),
        ("rerank", "cancelled"),
    }
    assert "private content" not in str(snapshot)


def test_actual_tokens_separate_from_estimates_and_invalid_values_ignored():
    metrics = Metrics()
    metrics.tokens("rerank", estimated_input=123)
    metrics.tokens("rerank", usage={"prompt_tokens": 150, "completion_tokens": 0})
    metrics.tokens("rerank", usage={"input_tokens": float("nan"), "output_tokens": -1})
    metrics.increment("invalid", True)
    metrics.increment("invalid", "100")
    rows = {row["name"]: row["value"] for row in metrics.snapshot()["counters"]}
    assert rows == {
        "tokens_actual_input": 150,
        "tokens_actual_output": 0,
        "tokens_estimated_input": 123,
    }


def test_rerank_provider_usage_and_retry_metrics():
    import httpx

    from app.rag.reranking import SiliconFlowReranker

    metrics = Metrics()
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.8}],
                "meta": {"tokens": {"input_tokens": 55, "output_tokens": 0}},
            },
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            adapter = SiliconFlowReranker(client, "test", 1, metrics=metrics)
            assert await adapter.rerank("年假", ["十天年假"]) == [0.8]

    asyncio.run(run())
    counters = {r["name"]: r["value"] for r in metrics.snapshot()["counters"]}
    assert counters["rerank_http_attempts"] == 2
    assert counters["rerank_http_retries"] == 1
    assert counters["tokens_actual_input"] == 55
    assert counters["tokens_estimated_input"] > 0
