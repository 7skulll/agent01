import math

import pytest
from pydantic import ValidationError

from app.evaluation_metrics import retrieval_metrics, summarize
from app.evaluation_models import EvalCase, EvalRequest


def test_ir_metrics_use_unique_files_and_fixed_k_denominator():
    sources = [{"file_id": key} for key in ["wrong", "a", "a", "b"]]
    scores = retrieval_metrics(sources, ["a", "b", "missing"], 5)
    assert scores["precision"] == 2 / 5
    assert scores["recall"] == 2 / 3
    assert scores["hit"] == 1
    assert scores["mrr"] == 1 / 2
    expected = (1 / math.log2(3) + 1 / math.log2(4)) / (1 + 1 / math.log2(3) + 1 / math.log2(4))
    assert scores["ndcg"] == pytest.approx(expected)
    assert scores["returned_files"] == 3
    assert retrieval_metrics([], ["a"], 5)["precision"] == 0
    assert retrieval_metrics(sources, [], 5) is None


def test_macro_scores_include_failures_and_exclude_cancelled_unlabelled_and_negatives():
    rows = []
    for status, expected, files in [
        ("completed", "answerable", ["a"]),
        ("failed", "answerable", ["a"]),
        ("cancelled", "answerable", ["a"]),
        ("completed", "unanswerable", []),
        ("completed", "unlabelled", []),
    ]:
        rows.append(
            {
                "case": {"expected": expected, "relevant_file_ids": files},
                "status": status,
                "metrics": retrieval_metrics([{"file_id": "a"}], files, 1)
                if status == "completed"
                else None,
            }
        )
    result = summarize(rows, 1)
    assert result["metrics"]["recall"] == 0.5
    assert result["metric_queries"] == 2
    assert result["labelled_failures"] == 1
    assert result["cancelled"] == 1
    assert summarize([], 5)["metrics"]["precision"] is None


def test_labels_do_not_treat_empty_gold_as_perfect_refusal():
    for expected, ids in [("answerable", []), ("unlabelled", ["a"]), ("unanswerable", ["a"])]:
        with pytest.raises(ValidationError):
            EvalCase(query="question", expected=expected, relevant_file_ids=ids)
    assert EvalCase(
        query="question", expected="answerable", relevant_file_ids=["a", "a"]
    ).relevant_file_ids == ["a"]
    with pytest.raises(ValidationError):
        EvalRequest(
            idempotency_key="9c6556aa-a839-46c6-83bd-4f0649902895",
            knowledge_base_id="default",
            cases=[{"query": "x"}] * 21,
        )
