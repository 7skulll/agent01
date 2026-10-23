"""Recompute known-source coverage from saved runs; no network or model calls."""

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path


def coverage(rows, *, chunks, excluded=()):
    eligible = [r for r in rows if not set(r["case"]["tags"]) & set(excluded)]
    scored = []
    for row in eligible:
        if row["status"] not in ("completed", "failed"):
            continue
        gold = set(row["case"]["relevant_file_ids"])
        if not gold:
            continue
        sources = row.get("sources", [])[:chunks] if row["status"] == "completed" else []
        found = {s["file_id"] for s in sources} & gold
        scored.append((len(found) / len(gold), int(found == gold)))
    return {
        "queries": len(scored),
        "chunk_limit": chunks,
        "known_source_coverage": statistics.mean(v[0] for v in scored) if scored else None,
        "all_known_sources_found": sum(v[1] for v in scored),
        "failed": sum(r["status"] == "failed" for r in eligible),
    }


def audit(folder, reviews_path):
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    excluded = {r["case_id"] for r in reviews["reviews"] if r["action"] == "exclude"}
    report = {
        "notice": "已知配套新闻覆盖，不是完整召回率或其下界；排除版本仅为离线重算。",
        "excluded_case_ids": sorted(excluded),
        "configs": {},
        "artifacts": [],
    }
    for mode in ("keyword", "vector", "hybrid", "hybrid-rerank"):
        rows = []
        for batch in range(1, 4):
            path = folder / f"{mode}-development-{batch}.json"
            raw = path.read_bytes()
            run = json.loads(raw)
            if run["status"] != "completed":
                raise ValueError(f"实验未完成：{path.name}")
            rows += run["results"]
            report["artifacts"].append(
                {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}
            )
        case_ids = [
            next(t for t in r["case"]["tags"] if t.startswith("crud-") and t != "crud-rag")
            for r in rows
        ]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("案例批次有重复。")
        triples = [r for r in rows if "questanswer_3docs" in r["case"]["tags"]]
        # Historical report uses ten unique files drawn from twenty returned chunks.
        file_complete = 0
        for row in triples:
            ranked = list(dict.fromkeys(s["file_id"] for s in row["sources"]))[:10]
            file_complete += set(row["case"]["relevant_file_ids"]) <= set(ranked)
        report["configs"][mode] = {
            "original_first_10_chunks": coverage(rows, chunks=10),
            "reviewed_first_10_chunks": coverage(rows, chunks=10, excluded=excluded),
            "historical_three_doc_full": {"found": file_complete, "total": len(triples)},
        }
    smoke = []
    for name in ("rag-smoke.json", "rag-3docs.json", "rag-retry.json"):
        smoke += json.loads((folder / name).read_text())["results"]
    first, retries = smoke[:4], smoke[4:]
    report["rag_execution"] = {
        "first_attempt": dict(Counter(r["status"] for r in first)),
        "separate_retry": dict(Counter(r["status"] for r in retries)),
        "notice": "completed 仅表示执行完成，不表示事实正确或人工通过。",
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument(
        "--reviews", type=Path, default=Path("evals/crud-rag/review-overrides-v1.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.folder, args.reviews)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"configs": report["configs"], "rag_execution": report["rag_execution"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
