"""Exact text anchors, not a semantic judge. Never infer evidence from file hits."""

import re


def normalized(text):
    # Preserve word boundaries: "now here" must not match "nowhere".
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", text)


def contains(anchor, source):
    if source.get("file_id") != anchor["file_id"]:
        return False
    # Assembled fragments are authoritative: do not match across artificial joins.
    fragments = source.get("evidence_chunks")
    if fragments is None:
        fragments = [source]
    return any(
        (anchor.get("page") is None or f.get("page") == anchor["page"])
        and normalized(anchor["quote"]) in normalized(f.get("content", ""))
        for f in fragments
    )


def evidence_report(groups, files, chunks, result):
    if not groups:
        return None
    hashes = {f["file_id"]: f["content_hash"] for f in files}
    trace = result.get("diagnostics", {}).get("trace", {})
    stages = {
        "parsed": chunks,
        "retrieved": trace.get("before_rerank", []),
        "reranked": trace.get("after_rerank", []),
        "selected": result.get("sources", []),
    }
    if result.get("run"):
        stages["context"] = result.get("context_sources", [])
    rows = []
    for group in groups:
        alternatives = []
        for anchor in group["alternatives"]:
            valid = hashes.get(anchor["file_id"]) == anchor["content_hash"]
            found = {
                stage: valid and any(contains(anchor, s) for s in sources)
                for stage, sources in stages.items()
            }
            alternatives.append(
                {
                    **anchor,
                    "anchor_status": "stale"
                    if not valid
                    else "matched"
                    if found["parsed"]
                    else "unmapped",
                    "stages": found,
                }
            )
        coverage = {stage: any(a["stages"][stage] for a in alternatives) for stage in stages}
        rows.append({"fact": group["fact"], "alternatives": alternatives, "stages": coverage})
    # Bad anchors are annotation gaps; don't attribute them to parsing/retrieval.
    valid = all(any(a["anchor_status"] == "matched" for a in row["alternatives"]) for row in rows)
    coverage = {
        stage: sum(row["stages"][stage] for row in rows) / len(rows) if valid else None
        for stage in stages
    }
    causes = []
    if not valid:
        causes.append("annotation_gap")
    else:
        for row in rows:
            flags = row["stages"]
            if not flags["retrieved"]:
                causes.append("retrieval_miss")
            elif not flags["reranked"]:
                causes.append("rerank_loss")
            elif not flags["selected"]:
                causes.append("selection_loss")
            elif "context" in flags and not flags["context"]:
                causes.append("assembly_loss")
    return {
        "groups": rows,
        "coverage": coverage,
        "valid": valid,
        "causes": list(dict.fromkeys(causes)),
        "context_complete": coverage.get("context") == 1 if valid and "context" in stages else None,
    }
