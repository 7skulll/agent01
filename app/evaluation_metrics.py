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
    }


def summarize(results, k):
    # Failed labelled cases count as zero; stopped/unattempted cases are excluded.
    labelled = [r for r in results if r["case"]["expected"] == "answerable"]
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
    negatives = [r for r in completed if r["case"]["expected"] == "unanswerable" and r.get("run")]
    positives = [r for r in completed if r["case"]["expected"] == "answerable" and r.get("run")]
    return {
        "k": k,
        "metrics": metrics,
        "metric_queries": len(eligible),
        "labelled_failures": sum(r["status"] == "failed" for r in eligible),
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
        "reviewed": sum(
            r.get("review", {}).get("verdict", "pending") != "pending" for r in results
        ),
    }
