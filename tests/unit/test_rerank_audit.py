"""审计发现的同分排序、重试等待和评估配置回归。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.rag.reranking import RerankError, SiliconFlowReranker
from app.rag.retrieval import RetrievalService, Source


@pytest.mark.parametrize("weight", [0.75, 1.0])
def test_equal_rerank_scores_preserve_recall_order(weight):
    class EqualScores:
        async def rerank(self, query, documents):
            return [0.0] * len(documents)

    candidates = [
        Source(
            citation_id=0,
            chunk_id=key,
            file_id=key,
            name="制度.txt",
            content=key,
            heading=None,
            page=None,
            kind="text",
            score=score,
        )
        for key, score in [("b", 0.9), ("a", 0.8)]
    ]
    service = RetrievalService(
        None, None, Settings(_env_file=None, retrieval_rerank_weight=weight), EqualScores()
    )
    result = asyncio.run(service._rerank("年假", candidates, {"degraded": [], "stage_ms": {}}))
    assert [source.chunk_id for source in result] == ["b", "a"]


def test_retry_after_date_does_not_retry_before_server_deadline():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            retry_date = format_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True
            )
            return httpx.Response(429, headers={"Retry-After": retry_date})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(RerankError, match="timeout"):
                await SiliconFlowReranker(client, "test", 0.4).rerank("问题", ["资料"])
        assert len(calls) == 1

    asyncio.run(run())


def test_retrieval_baseline_explicitly_disables_reranking(monkeypatch, tmp_path):
    import scripts.eval_retrieval as evaluation

    config = Settings(_env_file=None, rerank_enabled=True, rerank_api_key="test-key")

    async def embed_query(query):
        return [1.0, 0.0]

    @asynccontextmanager
    async def database(settings):
        yield SecretStr("postgresql://unused")

    class CapturedConfiguration(Exception):
        pass

    def capture(settings, **kwargs):
        assert settings.rerank_enabled is False
        raise CapturedConfiguration

    monkeypatch.setattr(evaluation, "Settings", lambda: config)
    monkeypatch.setattr(
        evaluation,
        "build_embedding_model",
        lambda _: SimpleNamespace(
            model_name="test",
            dim=2,
            embed_query=embed_query,
        ),
    )
    monkeypatch.setattr(evaluation, "temporary_database", database)
    monkeypatch.setattr(evaluation, "create_app", capture)
    args = SimpleNamespace(
        dataset=evaluation.ROOT / "evals/retrieval_baseline.json",
        output=tmp_path / "report.json",
        min_score=0.2,
    )
    with pytest.raises(CapturedConfiguration):
        asyncio.run(evaluation.evaluate(args))


def test_audit_default_covers_both_complete_datasets_and_scores_once_per_query(monkeypatch):
    import scripts.audit_rerank as audit

    calls = []

    class Scores:
        async def rerank(self, query, documents):
            calls.append((query, list(documents)))
            return [i / len(documents) for i in range(len(documents))]

    @asynccontextmanager
    async def configured(settings):
        yield Scores()

    monkeypatch.setattr(audit, "configured_reranker", configured)
    config = Settings(
        _env_file=None,
        rerank_enabled=True,
        retrieval_rerank_weight=0,
        retrieval_rerank_min_score=0.9,
    )
    report = asyncio.run(audit.audit(audit.parser().parse_args([]), config))
    summary = report["summary"]
    assert summary["query_count"] == 36
    assert summary["labelled_queries"] == 32
    assert summary["no_answer_queries"] == 4
    assert len(calls) == 36
    assert report["threshold"] is None
    assert report["configured_threshold_not_applied"] == 0.9
    assert summary["metrics"]["configured"] == summary["metrics"]["initial"]
    assert report["by_dataset"]["retrieval_baseline.json"]["labelled_queries"] == 20
    assert report["by_dataset"]["bm25_holdout.json"]["labelled_queries"] == 12
    assert sum(group["query_count"] for group in report["by_group"].values()) == 36
    for path in audit.DEFAULT_DATASETS:
        data, digest = audit.load_dataset(path)
        assert {row["id"] for row in report["rows"] if row["dataset"] == path.name} == {
            case["id"] for case in data["queries"]
        }
        assert (
            next(item["sha256"] for item in report["datasets"] if item["name"] == path.name)
            == digest
        )
    for row in report["rows"]:
        assert len(row["scores_by_id"]) == config.retrieval_rerank_candidates
        assert len(row["rankings"]["model"]["ids"]) == config.retrieval_rerank_candidates
        assert row["rankings"]["initial"]["ids"] == row["rankings"]["configured"]["ids"]


def test_audit_initial_order_is_reproducible_and_independent_of_labels():
    from scripts.audit_rerank import make_candidates

    documents = [{"id": str(i), "title": str(i), "text": f"文档 {i}"} for i in range(8)]
    case = {"id": "q1", "relevant": ["0"]}

    def ids(case, seed=12, count=3, order="shuffled"):
        return [s.chunk_id for s in make_candidates(documents, case, count, seed, order)]

    assert ids(case) == ids(case)
    assert ids(case) == ids({**case, "relevant": ["7"]})
    assert ids(case, seed=13) != ids(case)
    assert len(ids(case)) == 3
    assert len(ids(case, count=10)) == len(set(ids(case, count=10))) == 10
    assert ids(case, order="relevant-last")[-1] == "0"


def test_audit_missing_labels_are_zero_and_no_answer_counts_actual_results():
    from scripts.audit_rerank import STRATEGIES, ranking_metrics, summarize

    def row(labels, ids):
        return {
            "relevant": labels,
            "candidate_label_coverage": 0 if labels else None,
            "rankings": {
                name: {"ids": ids, "metrics": ranking_metrics(ids, labels)} for name in STRATEGIES
            },
        }

    summary = summarize([row(["absent"], ["other"]), row([], [])])
    assert summary["labelled_queries"] == 1
    assert summary["no_answer_queries"] == 1
    assert summary["candidate_label_coverage"] == 0
    for name in STRATEGIES:
        assert summary["metrics"][name]["mrr"] == 0
        assert summary["metrics"][name]["recall@5"] == 0
        assert summary["no_answer_queries_returning_candidates"][name] == 0
    assert summarize([row([], ["other"])])["metrics"]["model"]["mrr@10"] is None
    metrics = ranking_metrics(["a", "b", "c"], ["a", "c"])
    assert metrics["recall@1"] == 0.5
    assert metrics["recall@3"] == 1
    assert 0 < metrics["ndcg@5"] < 1
    assert ranking_metrics(["other"] * 10 + ["correct"], ["correct"])["mrr@10"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_document",
        "duplicate_document",
        "duplicate_query",
        "duplicate_label",
        "missing_labels",
    ],
)
def test_audit_rejects_invalid_dataset_labels(tmp_path, mutation):
    import json

    from scripts.audit_rerank import load_dataset

    data = {
        "documents": [{"id": "a", "title": "标题", "text": "正文"}],
        "queries": [{"id": "q", "query": "问题", "relevant": ["a"]}],
    }
    if mutation == "missing_document":
        data["queries"][0]["relevant"] = ["unknown"]
    elif mutation == "duplicate_document":
        data["documents"] *= 2
    elif mutation == "duplicate_query":
        data["queries"] *= 2
    elif mutation == "duplicate_label":
        data["queries"][0]["relevant"] *= 2
    else:
        del data["queries"][0]["relevant"]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_dataset(path)
