import pytest
from pydantic import ValidationError
from test_evaluation_v2 import experiment

from app.evaluation_comparison import compare
from app.evaluation_models import CaseSetRequest
from app.evaluation_quality import markdown_report, quality_summary
from app.evaluation_usage import actual_tokens
from app.evaluations import EvaluationService
from app.schemas import EvaluationOptions


def test_quality_scores_never_make_pending_or_na_a_pass_and_reuse_has_no_new_usage():
    def result(review, **kwargs):
        return {"status": "completed", "run": {"model_calls": 1}, "review": review, **kwargs}

    rows = [
        result(
            {"verdict": "pass", "fact_coverage": "full", "citation_support": "full"},
            token_usage=[{"actual": {"input_tokens": 10, "output_tokens": 2}}],
        ),
        result({"verdict": "pending", "fact_coverage": "pending", "citation_support": "na"}),
        result(
            {"verdict": "fail", "fact_coverage": "none", "citation_support": "none"},
            reused_from="original",
        ),
    ]
    quality = quality_summary(rows)
    assert quality["pass_rate"] == 0.5
    assert quality["dimensions"]["citation_support"]["judged"] == 2
    assert quality["dimensions"]["citation_support"]["na"] == 1
    assert quality["usage"] == {
        "known_tokens": 12,
        "known_queries": 1,
        "unknown_queries": 1,
        "new_model_calls": 2,
    }
    assert actual_tokens({"run": {"model_calls": 0}}) == 0
    assert actual_tokens({"token_usage": [{"actual": {"input_tokens": 10}}]}) is None


def test_answer_quality_metrics_are_strict_and_do_not_mutate_reviews():
    from copy import deepcopy

    reviews = [
        {
            "verdict": "pass",
            "unsupported_claims": "no",
            "answer_relevancy": "relevant",
            "fact_coverage": "full",
        },
        {
            "verdict": "partial",
            "unsupported_claims": "no",
            "answer_relevancy": "relevant",
            "fact_coverage": "partial",
        },
        {
            "verdict": "fail",
            "unsupported_claims": "yes",
            "answer_relevancy": "irrelevant",
            "fact_coverage": "none",
        },
        {
            "verdict": "pending",
            "unsupported_claims": "pending",
            "answer_relevancy": "pending",
            "fact_coverage": "pending",
        },
        {
            "verdict": "pending",
            "unsupported_claims": "na",
            "answer_relevancy": "partial",
            "fact_coverage": "na",
        },
    ]
    rows = [
        {"status": "completed", "run": {"model_calls": 1}, "review": review}
        for review in reviews
    ]
    before = deepcopy(rows)
    quality = quality_summary(rows)

    # pending and na are excluded from every strict denominator, never treated as pass.
    assert quality["metrics"]["faithfulness"]["denominator"] == 3
    assert quality["metrics"]["faithfulness"]["numerator"] == 2
    assert quality["metrics"]["faithfulness"]["pending"] == 1
    assert quality["metrics"]["faithfulness"]["na"] == 1
    assert quality["metrics"]["faithfulness"]["value"] == pytest.approx(2 / 3)

    assert quality["metrics"]["correctness"]["denominator"] == 3
    assert quality["metrics"]["correctness"]["numerator"] == 1
    assert quality["metrics"]["correctness"]["value"] == pytest.approx(1 / 3)
    assert quality["metrics"]["correctness"]["acceptable"] == 2
    assert quality["metrics"]["correctness"]["acceptable_rate"] == pytest.approx(2 / 3)

    assert quality["metrics"]["completeness"]["denominator"] == 3
    assert quality["metrics"]["completeness"]["numerator"] == 1
    assert quality["metrics"]["completeness"]["value"] == pytest.approx(1 / 3)

    assert quality["metrics"]["answer_relevancy"]["denominator"] == 4
    assert quality["metrics"]["answer_relevancy"]["numerator"] == 2
    assert quality["metrics"]["answer_relevancy"]["value"] == pytest.approx(0.5)

    # Reading and deriving metrics must not rewrite the raw human judgments.
    assert rows == before
    assert rows[0]["review"] is reviews[0]


def test_generation_summary_keeps_missing_judge_scores_missing():
    from app.evaluation_quality import generation_summary

    rows = [
        {
            "judge": {
                "faithfulness": 1.0,
                "answer_relevancy": 0.5,
                "answer_correctness": None,
                "completeness": 0.75,
            }
        },
        {"judge": {"faithfulness": 0.0, "answer_relevancy": 0.5}},
        {"judge_error": "failed"},
    ]
    summary = generation_summary(rows)
    assert summary["available"] is True
    assert summary["judged_queries"] == 2
    assert summary["errors"] == 1
    assert summary["attempted_queries"] == 3
    assert summary["metrics"]["faithfulness"]["missing"] == 1
    assert summary["metrics"]["answer_correctness"]["missing"] == 3
    assert summary["metrics"]["completeness"]["missing"] == 2
    assert summary["metrics"]["faithfulness"]["value"] == 0.5
    assert summary["metrics"]["answer_relevancy"]["value"] == 0.5
    assert summary["metrics"]["answer_correctness"]["value"] is None
    assert summary["metrics"]["answer_correctness"]["judged"] == 0
    assert summary["metrics"]["completeness"]["value"] == 0.75


def test_answer_quality_metrics_return_null_for_empty_denominators():
    rows = [
        {
            "status": "completed",
            "run": {"model_calls": 0},
            "review": {
                "verdict": "pending",
                "unsupported_claims": "pending",
                "fact_coverage": "pending",
            },
        }
    ]
    quality = quality_summary(rows)
    for key in ("faithfulness", "correctness", "completeness"):
        assert quality["metrics"][key]["denominator"] == 0
        assert quality["metrics"][key]["value"] is None


def test_all_failed_judge_attempts_remain_missing_not_zero():
    from app.evaluation_quality import generation_summary

    summary = generation_summary([{"judge_error": "failed"}, {"judge_error": "timeout"}])
    assert summary["attempted_queries"] == 2
    assert summary["judged_queries"] == 0
    assert summary["errors"] == 2
    for metric in summary["metrics"].values():
        assert metric["value"] is None
        assert metric["missing"] == 2


def test_frozen_cases_require_full_verified_evidence():
    body = {
        "knowledge_base_id": "default",
        "name": "测试",
        "frozen": True,
        "review_status": "verified",
        "cases": [{"query": "奖金", "expected": "unanswerable", "judgment_scope": "partial"}],
    }
    with pytest.raises(ValidationError):
        CaseSetRequest(**body)
    body["cases"][0]["judgment_scope"] = "full"
    assert CaseSetRequest(**body).frozen
    for options in ({"context_tokens": 32}, {"rerank": "invalid"}):
        with pytest.raises(ValidationError):
            EvaluationOptions(**options)


def test_retry_plan_keeps_completed_and_marks_unknown_rag_attempts():
    row = {
        "request": {"kind": "rag", "cases": [{}, {}, {}, {}]},
        "results": [
            {"status": "completed"},
            {"status": "failed", "run": {"model_calls": 1}},
            {"status": "cancelled", "run": {"model_calls": 0}},
        ],
    }
    assert EvaluationService.retry_plan(row) == {
        "retry_indices": [1, 2, 3],
        "unknown_indices": [1],
        "reuse_count": 1,
    }


def test_reused_cases_keep_ir_delta_but_never_claim_new_runtime_or_token_delta():
    a, b = experiment(), experiment()
    b["results"][0]["reused_from"] = "parent"
    row = compare(a, b)["rows"][0]
    assert row["outcome"] == "tie"
    assert row["elapsed_ms_delta"] is None
    assert row["tokens_delta"] is None


def test_report_escapes_user_markdown_and_explains_denominators():
    row = experiment()
    row["results"][0]["case"]["query"] = "<script>|问题\n下一行"
    row["results"][0]["review"] = {"verdict": "pending", "notes": "待审核"}
    row.update(
        quality=quality_summary(row["results"]),
        summary={"metrics": {"recall": 1}, "metric_queries": 1},
    )
    row["snapshot"]["fingerprint"] = "fixed"
    text = markdown_report(row)
    assert "&lt;script&gt;\\|问题 下一行" in text
    assert "未评分" in text
    assert "无可用检索结果的失败题计零" in text


def test_unattempted_pending_rows_do_not_require_unknown_call_confirmation():
    row = {"request": {"kind": "rag", "cases": [{}]}, "results": [{"status": "pending"}]}
    assert EvaluationService.retry_plan(row)["unknown_indices"] == []


def test_missing_review_dimension_counts_as_pending_not_as_judged():
    # Rows stored before a dimension existed have no key at all; they must show up as
    # unaudited, otherwise the "pending" badge understates the remaining review work.
    legacy = {
        "status": "completed",
        "run": {"answer": "回答"},
        "review": {"verdict": "pass", "fact_coverage": "full"},
    }
    quality = quality_summary([legacy])
    relevancy = quality["metrics"]["answer_relevancy"]
    assert relevancy["denominator"] == 0
    assert relevancy["value"] is None
    assert relevancy["pending"] == 1
    faithfulness = quality["metrics"]["faithfulness"]
    assert faithfulness["pending"] == 1
    assert faithfulness["denominator"] == 0
