from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.errors import ChatError
from app.evaluation_comparison import compare
from app.evaluation_evidence import evidence_report
from app.evaluation_metrics import case_metrics, judgment_coverage, summarize
from app.evaluation_models import EvalCase

HASH = "a" * 64
ANCHOR = {"file_id": "a", "content_hash": HASH, "quote": "员工年假十天", "page": None}
SOURCE = {"file_id": "a", "content": "员工年假十天，需提前申请。", "page": None}


def test_partial_judgments_do_not_make_unjudged_files_negative():
    case = {
        "expected": "answerable",
        "relevant_file_ids": ["a"],
        "negative_file_ids": ["b"],
        "judgment_scope": "partial",
    }
    sources = [{"file_id": key} for key in ["a", "a", "b", "c"]]
    assert case_metrics(sources, case, 5) is None
    assert judgment_coverage(sources, case, 5) == {
        "judged": 2,
        "returned": 3,
        "coverage": 2 / 3,
        "judged_precision": 0.5,
    }
    summary = summarize([{"case": case, "status": "completed", "metrics": None}], 5)
    assert summary["metric_queries"] == 0
    assert summary["metrics"]["precision"] is None
    assert summary["partial_label_queries"] == 1
    with pytest.raises(ValidationError):
        EvalCase(
            query="问题", expected="answerable", relevant_file_ids=["a"], negative_file_ids=["a"]
        )


def report(groups=None, **result):
    return evidence_report(
        groups or [{"fact": "年假天数", "alternatives": [ANCHOR]}],
        [{"file_id": "a", "content_hash": HASH}],
        [SOURCE],
        result,
    )


def test_file_hit_does_not_mean_evidence_hit_and_whitespace_is_normalized():
    wrong = {**SOURCE, "content": "员工需提前申请。"}
    result = report(
        sources=[wrong],
        diagnostics={"trace": {"before_rerank": [wrong], "after_rerank": [wrong]}},
        run={"answer": "十天"},
        context_sources=[wrong],
    )
    assert result["valid"]
    assert result["context_complete"] is False
    assert result["coverage"]["selected"] == 0
    assert result["causes"] == ["retrieval_miss"]
    spaced = {**SOURCE, "content": "员工 年假\n十天"}
    assert report(sources=[spaced])["coverage"]["selected"] == 1


def test_anchors_survive_changed_chunk_ids_and_support_alternatives_and_all_facts():
    new = {
        **SOURCE,
        "chunk_id": "new-chunk",
        "evidence_chunks": [{"content": "员工年假十天", "page": None}],
    }
    invalid = {**ANCHOR, "quote": "没有此原文"}
    groups = [
        {"fact": "天数", "alternatives": [invalid, ANCHOR]},
        {"fact": "申请", "alternatives": [{**ANCHOR, "quote": "需提前申请"}]},
    ]
    result = report(groups, sources=[new], run={"answer": "十天"}, context_sources=[new])
    assert result["valid"]
    assert result["coverage"]["context"] == 0.5
    assert result["context_complete"] is False
    # Don't match across synthetic context fragment joins.
    joined = {
        **SOURCE,
        "content": "员工年假十天",
        "evidence_chunks": [
            {"content": "员工年假", "page": None},
            {"content": "十天", "page": None},
        ],
    }
    assert report(run={"answer": "十天"}, context_sources=[joined])["coverage"]["context"] == 0


def test_stale_or_unmapped_anchors_are_annotation_gaps_not_quality_zero():
    groups = [{"fact": "天数", "alternatives": [{**ANCHOR, "content_hash": "b" * 64}]}]
    stale = report(groups, run={"answer": "十天"}, context_sources=[SOURCE])
    assert stale["coverage"]["context"] is None
    assert stale["context_complete"] is None
    assert stale["groups"][0]["alternatives"][0]["anchor_status"] == "stale"
    assert stale["causes"] == ["annotation_gap"]
    page = report([{"fact": "天数", "alternatives": [{**ANCHOR, "page": 2}]}])
    assert page["valid"] is False


def test_stage_attribution_distinguishes_rerank_selection_and_assembly():
    for expected, trace, sources, context in [
        ("rerank_loss", {"before_rerank": [SOURCE], "after_rerank": []}, [], []),
        ("selection_loss", {"before_rerank": [SOURCE], "after_rerank": [SOURCE]}, [], []),
        ("assembly_loss", {"before_rerank": [SOURCE], "after_rerank": [SOURCE]}, [SOURCE], []),
    ]:
        result = report(
            diagnostics={"trace": trace},
            sources=sources,
            context_sources=context,
            run={"answer": "十天"},
        )
        assert result["causes"] == [expected]


def experiment():
    case = {"query": "问题", "expected": "answerable", "relevant_file_ids": ["a"]}
    metrics = dict.fromkeys(("precision", "recall", "hit", "mrr", "ndcg"), 0.5)
    return {
        "evaluation_id": "left",
        "status": "completed",
        "request": {
            "knowledge_base_id": "default",
            "mode": "keyword",
            "kind": "retrieval",
            "k": 5,
            "cases": [case],
        },
        "snapshot": {
            "files": [{"file_id": "a", "content_hash": HASH}],
            "config": {},
            "code_sha256": "code",
        },
        "results": [{"case": case, "status": "completed", "metrics": metrics, "elapsed_ms": 20}],
    }


def test_comparison_reports_real_paired_deltas_and_rejects_incompatible_inputs():
    left = experiment()
    right = deepcopy(left)
    right["results"][0]["metrics"]["ndcg"] = 1
    right["results"][0]["elapsed_ms"] = 30
    right["request"]["mode"] = "hybrid"
    comparison = compare(left, right)
    assert comparison["counts"]["win"] == 1
    assert comparison["metric_delta"]["ndcg"] == 0.5
    assert comparison["rows"][0]["elapsed_ms_delta"] == 10
    assert comparison["rows"][0]["tokens_delta"] is None
    for field in ("kind", "k", "case_set_id"):
        wrong = deepcopy(right)
        wrong["request"][field] = "different"
        with pytest.raises(ChatError, match="无法直接比较"):
            compare(left, wrong)
    right["snapshot"]["files"][0]["content_hash"] = "changed"
    with pytest.raises(ChatError, match="语料"):
        compare(left, right)


def test_comparison_failure_zero_and_partial_unscored_never_fabricates_tokens():
    left = experiment()
    right = deepcopy(left)
    right["results"][0].update(
        status="failed", metrics=None, token_usage=[{"actual": {"input_tokens": 2}}]
    )
    row = compare(left, right)["rows"][0]
    assert row["outcome"] == "loss"
    assert row["elapsed_ms_delta"] is None
    assert row["tokens_delta"] is None
    for item in (left, right):
        item["request"]["cases"][0]["judgment_scope"] = "partial"
    assert compare(left, right)["counts"]["unscored"] == 1


def test_legacy_and_v2_complete_labels_are_comparable_and_partial_usage_is_unknown():
    left = experiment()
    right = deepcopy(left)
    right["request"]["cases"][0]["judgment_scope"] = "full"
    left["results"][0]["token_usage"] = [{"actual": {"input_tokens": 10, "output_tokens": 2}}]
    right["results"][0]["token_usage"] = [
        {"actual": {"input_tokens": 20, "output_tokens": 3}},
        {"stage": "answer_repair"},
    ]
    assert compare(left, right)["rows"][0]["tokens_delta"] is None
    negative = {"expected": "unanswerable", "relevant_file_ids": []}
    assert judgment_coverage([{"file_id": "a"}], negative, 5)["coverage"] == 1


def test_partial_negative_labels_never_publish_refusal_quality():
    def result(scope, status):
        return {
            "case": {"expected": "unanswerable", "judgment_scope": scope},
            "status": status,
            "run": {"answer_status": "insufficient_evidence"},
        }

    results = [
        result("partial", "completed"),
        result("partial", "failed"),
        result("full", "completed"),
        result("full", "failed"),
    ]
    summary = summarize(results, 5, kind="rag")
    assert summary["refusal_correct"] == summary["refusal_total"] == 1
    assert summary["refusal_attempted"] == 2
    assert summarize(results, 5, kind="retrieval")["refusal_attempted"] == 0


def test_english_word_boundaries_are_not_removed_from_anchors():
    groups = [{"fact": "location", "alternatives": [{**ANCHOR, "quote": "now here"}]}]
    result = evidence_report(
        groups,
        [{"file_id": "a", "content_hash": HASH}],
        [{**SOURCE, "content": "now here"}],
        {"sources": [{**SOURCE, "content": "nowhere"}]},
    )
    assert result["valid"] is True
    assert result["coverage"]["selected"] == 0


def test_incomplete_legacy_experiments_return_conflict_instead_of_zip_error():
    left, right = experiment(), experiment()
    right["results"] = []
    with pytest.raises(ChatError, match="尚未完整结束"):
        compare(left, right)
