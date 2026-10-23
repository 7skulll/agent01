"""Paired descriptive comparisons; no significance claims for small samples."""

from app.errors import ChatError
from app.evaluation_quality import ANSWER_METRIC_SPECS, review_metric_score
from app.evaluation_usage import actual_tokens

KEYS = ("precision", "recall", "hit", "mrr", "ndcg")


def case_identity(case):
    case = {"judgment_scope": "full", **case}
    return {key: value for key, value in case.items() if value not in (None, [], "")}


def compare(left, right):
    def corpus(row):
        scope = row["request"].get("file_ids")
        return sorted(
            (f["file_id"], f["content_hash"], f.get("chunk_version"), f.get("index_status"))
            for f in row["snapshot"]["files"]
            if scope is None or f["file_id"] in scope
        )

    problems = []
    for field in ("knowledge_base_id", "kind", "k", "case_set_id"):
        if left["request"].get(field) != right["request"].get(field):
            problems.append(field)
    if corpus(left) != corpus(right):
        problems.append("corpus")
    if [case_identity(c) for c in left["request"]["cases"]] != [
        case_identity(c) for c in right["request"]["cases"]
    ]:
        problems.append("cases_or_labels")
    if left["status"] != "completed" or right["status"] != "completed":
        problems.append("incomplete_experiment")
    if any(len(row["results"]) != len(row["request"]["cases"]) for row in (left, right)):
        problems.append("incomplete_experiment")
    if problems:
        labels = {
            "knowledge_base_id": "知识库不同",
            "kind": "运行方式不同",
            "k": "指标 K 不同",
            "case_set_id": "案例集版本不同",
            "corpus": "语料或索引版本不同",
            "cases_or_labels": "案例或标注不同",
            "incomplete_experiment": "实验尚未完整结束",
        }
        raise ChatError(
            "incompatible_evaluations",
            "无法直接比较：" + "、".join(labels[p] for p in problems),
            409,
        )

    rows = []
    for a, b in zip(left["results"], right["results"], strict=True):
        complete = all(r["status"] == "completed" and not r.get("reused_from") for r in (a, b))

        def score(r, key):
            if (
                r["case"].get("judgment_scope", "full") != "full"
                or r["case"]["expected"] != "answerable"
            ):
                return None
            return 0 if r["status"] == "failed" else (r.get("metrics") or {}).get(key)

        a_scores = {key: score(a, key) for key in KEYS}
        b_scores = {key: score(b, key) for key in KEYS}
        delta = {
            key: b_scores[key] - a_scores[key]
            if a_scores[key] is not None and b_scores[key] is not None
            else None
            for key in KEYS
        }
        primary = delta["ndcg"]
        answer = {}
        for spec in ANSWER_METRIC_SPECS:
            a_value = review_metric_score(a, spec)
            b_value = review_metric_score(b, spec)
            answer[spec["key"]] = {
                "left": a_value,
                "right": b_value,
                "delta": b_value - a_value
                if a_value is not None and b_value is not None
                else None,
            }
        ta, tb = actual_tokens(a), actual_tokens(b)
        rows.append(
            {
                "query": a["case"]["query"],
                "delta": delta,
                "answer": answer,
                "outcome": "unscored"
                if primary is None
                else "win"
                if primary > 1e-9
                else "loss"
                if primary < -1e-9
                else "tie",
                "left_status": a["status"],
                "right_status": b["status"],
                "elapsed_ms_delta": b["elapsed_ms"] - a["elapsed_ms"] if complete else None,
                "tokens_delta": tb - ta if complete and ta is not None and tb is not None else None,
            }
        )
    answer_metrics = {}
    for spec in ANSWER_METRIC_SPECS:
        key = spec["key"]
        paired = [row["answer"][key] for row in rows if row["answer"][key]["delta"] is not None]
        answer_metrics[key] = {
            "label": spec["label"],
            "left": sum(item["left"] for item in paired) / len(paired) if paired else None,
            "right": sum(item["right"] for item in paired) / len(paired) if paired else None,
            "delta": sum(item["delta"] for item in paired) / len(paired) if paired else None,
            "paired": len(paired),
            "unpaired": len(rows) - len(paired),
            "definition": spec["definition"],
        }
    config_changes = {
        key: {
            "left": left["snapshot"]["config"].get(key),
            "right": right["snapshot"]["config"].get(key),
        }
        for key in left["snapshot"]["config"].keys() | right["snapshot"]["config"].keys()
        if left["snapshot"]["config"].get(key) != right["snapshot"]["config"].get(key)
    }
    return {
        "left_id": left["evaluation_id"],
        "right_id": right["evaluation_id"],
        "rows": rows,
        "counts": {
            value: sum(row["outcome"] == value for row in rows)
            for value in ("win", "tie", "loss", "unscored")
        },
        "metric_delta": {
            key: sum(r["delta"][key] for r in rows if r["delta"][key] is not None)
            / sum(r["delta"][key] is not None for r in rows)
            if any(r["delta"][key] is not None for r in rows)
            else None
            for key in KEYS
        },
        "answer_metrics": answer_metrics,
        "config_changes": config_changes,
        "experiment_changes": {
            "mode": {"left": left["request"]["mode"], "right": right["request"]["mode"]},
            "options": {
                "left": left["request"].get("options", {}),
                "right": right["request"].get("options", {}),
            },
        },
        "code_changed": left["snapshot"].get("code_sha256") != right["snapshot"].get("code_sha256"),
        "modes": [left["request"]["mode"], right["request"]["mode"]],
    }
