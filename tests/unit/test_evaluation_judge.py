import asyncio
from types import SimpleNamespace

from app.evaluation_quality import generation_summary
from app.evaluations import EvaluationService


def _result(query, *, reference="参考答案"):
    return {
        "status": "completed",
        "case": {"query": query, "reference_answer": reference, "expected": "answerable"},
        "run": {"answer": "回答"},
        "context_sources": [{"content": "资料"}],
    }


def test_judge_batch_failure_falls_back_to_individual_scores():
    class Judge:
        available = True
        metadata = {"ragas_version": "test", "worker": "fake"}

        def __init__(self):
            self.sizes = []

        async def score(self, records):
            self.sizes.append(len(records))
            if len(records) > 1:
                raise TimeoutError("batch timeout")
            return [
                {
                    "faithfulness": 1.0,
                    "answer_relevancy": 0.8,
                    "answer_correctness": 0.7,
                    "completeness": 1.0,
                }
            ]

    judge = Judge()
    app = SimpleNamespace(state=SimpleNamespace(evaluation_judge=judge))
    service = EvaluationService(app, None)
    saved = []

    async def save_results(_, changed):
        saved.extend((index, row["judge_status"]) for index, row in changed)

    service.save_results = save_results
    results = [_result("一"), _result("二")]
    asyncio.run(service.judge_results("evaluation", results))
    assert judge.sizes == [2, 1, 1]
    assert saved == [(0, "running"), (1, "running"), (0, "done"), (1, "done")]
    assert [r["judge"]["faithfulness"] for r in results] == [1.0, 1.0]
    assert all(r["judge_status"] == "done" for r in results)
    assert generation_summary(results)["judged_queries"] == 2


def test_judge_reports_missing_scores_without_inventing_zero():
    class Judge:
        available = True
        metadata = {"ragas_version": "test"}

        async def score(self, records):
            return [{"faithfulness": None, "answer_relevancy": None} for _ in records]

    app = SimpleNamespace(state=SimpleNamespace(evaluation_judge=Judge()))
    service = EvaluationService(app, None)

    async def save_results(_, __):
        pass

    service.save_results = save_results
    results = [_result("一")]
    asyncio.run(service.judge_results("evaluation", results))
    assert "judge" not in results[0]
    assert results[0]["judge_error"] == "RAGAS 未返回有效分数。"
    assert generation_summary(results)["errors"] == 1


def test_judge_without_reference_expects_only_supported_metrics():
    class Judge:
        available = True
        metadata = {"ragas_version": "test"}

        async def score(self, records):
            return [{"faithfulness": 1.0, "answer_relevancy": 0.5}]

    app = SimpleNamespace(state=SimpleNamespace(evaluation_judge=Judge()))
    service = EvaluationService(app, None)

    async def save_results(_, __):
        pass

    service.save_results = save_results
    results = [_result("一", reference="")]
    asyncio.run(service.judge_results("evaluation", results))
    assert results[0]["judge"]["answer_correctness"] is None
    assert "judge_error" not in results[0]


def test_judge_excludes_refusals_and_marks_missing_context():
    class Judge:
        available = True
        metadata = {}

        async def score(self, _):
            raise AssertionError("No answerable case has scoreable context")

    app = SimpleNamespace(state=SimpleNamespace(evaluation_judge=Judge()))
    service = EvaluationService(app, None)
    saved = []

    async def save_results(_, changed):
        saved.extend(changed)

    service.save_results = save_results
    refusal = _result("拒答题")
    refusal["case"]["expected"] = "unanswerable"
    missing = _result("有答案题")
    missing["context_sources"] = []
    results = [refusal, missing]
    asyncio.run(service.judge_results("evaluation", results))
    assert "judge" not in refusal and "judge_error" not in refusal
    assert missing["judge_error"] == "没有可用的回答上下文，RAGAS 无法评分。"
    assert [index for index, _ in saved] == [1]
