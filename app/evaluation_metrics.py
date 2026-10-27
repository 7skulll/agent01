"""Binary file-level IR metrics. Missing ranks count as non-relevant; no labels -> null."""

import math


def file_ranking(sources):
    return list(dict.fromkeys(source["file_id"] for source in sources))


def retrieval_metrics(sources, relevant, k):
    if not relevant:
        return None
    gold = set(relevant)
    ranked = file_ranking(sources)[:k]
    hits = [i for i, key in enumerate(ranked, 1) if key in gold]
    found = len(hits)
    dcg = sum(1 / math.log2(i + 1) for i in hits)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(gold)) + 1))
    return {
        "precision": found / k,
        "recall": found / len(gold),
        "hit": int(bool(hits)),
        "mrr": 1 / hits[0] if hits else 0.0,
        "ndcg": dcg / ideal,
        "relevant_found": found,
        "relevant_total": len(gold),
        "returned_files": len(ranked),
        "k": k,
        "ranking_unit": "unique_file_within_returned_chunks",
        "returned_chunks": len(sources),
    }


def case_metrics(sources, case, k):
    if case.get("judgment_scope", "full") != "full":
        return None
    return retrieval_metrics(sources, case["relevant_file_ids"], k)


def judgment_coverage(sources, case, k):
    ranked = file_ranking(sources)[:k]
    positives = set(case["relevant_file_ids"])
    full = case.get("judgment_scope", "full") == "full" and case["expected"] in (
        "answerable",
        "unanswerable",
    )
    judged = (
        set(ranked) if full else set(ranked) & (positives | set(case.get("negative_file_ids", [])))
    )
    return {
        "judged": len(judged),
        "returned": len(ranked),
        "coverage": len(judged) / len(ranked) if ranked else None,
        "judged_precision": len(positives & judged) / len(judged) if judged else None,
    }


def summarize(results, k, *, kind=None):
    # Use the observed retrieval score even if a later stage failed.
    # Failures without a usable retrieval result count as zero.
    labelled = [
        r
        for r in results
        if r["case"]["expected"] == "answerable"
        and r["case"].get("judgment_scope", "full") == "full"
    ]
    eligible = [r for r in labelled if r["status"] in ("completed", "failed")]
    values = [
        r.get("metrics") or dict.fromkeys(("precision", "recall", "hit", "mrr", "ndcg"), 0)
        for r in eligible
    ]
    metrics = {
        key: sum(v[key] for v in values) / len(values) if values else None
        for key in ("precision", "recall", "hit", "mrr", "ndcg")
    }
    completed = [r for r in results if r["status"] == "completed"]
    negatives = [
        r
        for r in completed
        if r["case"]["expected"] == "unanswerable"
        and r["case"].get("judgment_scope", "full") == "full"
        and r.get("run")
    ]
    positives = [r for r in completed if r["case"]["expected"] == "answerable" and r.get("run")]
    return {
        "k": k,
        "metrics": metrics,
        "metric_queries": len(eligible),
        "labelled_failures": sum(r["status"] == "failed" for r in eligible),
        "retrieval_scored_failures": sum(
            r["status"] == "failed" and r.get("metrics") is not None for r in eligible
        ),
        "completed": len(completed),
        "failed": sum(r["status"] == "failed" for r in results),
        "cancelled": sum(r["status"] == "cancelled" for r in results),
        "refusal_correct": sum(
            r["run"]["answer_status"] == "insufficient_evidence" for r in negatives
        ),
        "refusal_total": len(negatives),
        "false_refusals": sum(
            r["run"]["answer_status"] == "insufficient_evidence" for r in positives
        ),
        "answerable_total": len(positives),
        "partial_label_queries": sum(r["case"].get("judgment_scope") == "partial" for r in results),
        "refusal_attempted": sum(
            r["case"]["expected"] == "unanswerable"
            and r["case"].get("judgment_scope", "full") == "full"
            and kind != "retrieval"
            and r["status"] in ("completed", "failed")
            for r in results
        ),
        "answerable_attempted": sum(
            r["case"]["expected"] == "answerable"
            and kind != "retrieval"
            and r["status"] in ("completed", "failed")
            for r in results
        ),
        "context_evidence_queries": sum(
            r.get("evidence", {}).get("context_complete") is not None for r in completed
        ),
        "context_evidence_complete": sum(
            r.get("evidence", {}).get("context_complete") is True for r in completed
        ),
        "reviewed": sum(
            r.get("review", {}).get("verdict", "pending") != "pending" for r in results
        ),
    }
