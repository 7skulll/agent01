"""全量固定合成候选重排审计；不执行召回、访问数据库或调用聊天模型。"""

import argparse
import asyncio
import hashlib
import json
import math
import random
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.rag.reranking import configured_reranker
from app.rag.retrieval import RetrievalService, Source

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASETS = [ROOT / "evals/retrieval_baseline.json", ROOT / "evals/bm25_holdout.json"]
STRATEGIES = ("initial", "configured", "rrf_075", "model")


def load_dataset(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    documents, queries = data["documents"], data["queries"]
    ids = [doc["id"] for doc in documents]
    query_ids = [case["id"] for case in queries]
    if (
        not documents
        or not queries
        or len(ids) != len(set(ids))
        or len(query_ids) != len(set(query_ids))
    ):
        raise ValueError(f"{path.name}: 语料和问题不能为空，ID 必须唯一。")
    for doc in documents:
        if not all(
            isinstance(doc.get(key), str) and doc[key].strip() for key in ("id", "title", "text")
        ):
            raise ValueError(f"{path.name}: 文档必须包含非空 id/title/text。")
    for case in queries:
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError(f"{path.name}: 问题文本不能为空。")
        labels = case.get("relevant")
        if not isinstance(labels, list) or any(not isinstance(key, str) for key in labels):
            raise ValueError(f"{path.name}: relevant 必须显式为 ID 数组；无答案用 []。")
        if len(labels) != len(set(labels)) or not set(labels) <= set(ids):
            raise ValueError(f"{path.name}: 标签重复或引用不存在的文档。")
    return data, hashlib.sha256(raw).hexdigest()


def make_candidates(documents, case, count, seed, order):
    # 默认初排完全独立于相关标签；截断也不特意将正确文档塞入候选。
    pool = list(documents)
    existing = {doc["id"] for doc in pool}
    index = 0
    while len(pool) < count:
        key = f"audit-distractor-{index}"
        index += 1
        if key in existing:
            continue
        pool.append(
            {
                "id": key,
                "title": f"会议室设备说明{index}",
                "text": f"会议室投影设备编号{index}，离开会议室时请关闭投影和照明。",
            }
        )
        existing.add(key)
    # 种子只依赖语料和问题 ID，不受相关性标签的增删影响。
    corpus_hash = hashlib.sha256(
        json.dumps(documents, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    identity = f"{seed}:{corpus_hash}:{case['id']}"
    random.Random(identity).shuffle(pool)
    if order == "relevant-last":
        relevant = set(case["relevant"])
        pool.sort(key=lambda doc: doc["id"] in relevant)
        # 历史压力场景显式保留相关文档，但不将其冒充标签无关的初排。
        pool = pool[-count:]
    else:
        pool = pool[:count]
    return [
        Source(
            citation_id=0,
            chunk_id=doc["id"],
            file_id=doc["id"],
            name=doc["title"] + ".txt",
            content=doc["text"],
            heading=None,
            page=None,
            kind="text",
            score=1 / (60 + rank),
        )
        for rank, doc in enumerate(pool, 1)
    ]


def ranking_metrics(ranked_ids, relevant):
    relevant = set(relevant)
    rank = next((i for i, key in enumerate(ranked_ids, 1) if key in relevant), None)
    result = {
        "first_relevant_rank": rank,
        "reciprocal_rank": 1 / rank if rank else 0.0,
        "mrr@10": 1 / rank if rank and rank <= 10 else 0.0,
    }
    for k in (1, 3, 5):
        result[f"recall@{k}"] = (
            len(set(ranked_ids[:k]) & relevant) / len(relevant) if relevant else None
        )
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(5, len(relevant)) + 1))
    dcg = sum(1 / math.log2(i + 1) for i, key in enumerate(ranked_ids[:5], 1) if key in relevant)
    result["ndcg@5"] = dcg / ideal if ideal else None
    return result


def summarize(rows):
    labelled = [row for row in rows if row["relevant"]]
    unanswered = [row for row in rows if not row["relevant"]]
    metrics = {}
    for strategy in STRATEGIES:
        metrics[strategy] = {
            ("mrr" if key == "reciprocal_rank" else key): sum(
                row["rankings"][strategy]["metrics"][key] for row in labelled
            )
            / len(labelled)
            if labelled
            else None
            for key in ("reciprocal_rank", "mrr@10", "recall@1", "recall@3", "recall@5", "ndcg@5")
        }
    return {
        "query_count": len(rows),
        "labelled_queries": len(labelled),
        "no_answer_queries": len(unanswered),
        "candidate_label_coverage": sum(row["candidate_label_coverage"] for row in labelled)
        / len(labelled)
        if labelled
        else None,
        "no_answer_queries_returning_candidates": {
            strategy: sum(bool(row["rankings"][strategy]["ids"]) for row in unanswered)
            for strategy in STRATEGIES
        },
        "metrics": metrics,
    }


async def audit(args, settings):
    # 一次真实打分，之后只重放；阈值保持关闭，不用本审计集校准生产参数。
    config = settings.model_copy(
        update={
            "rerank_enabled": settings.rerank_enabled or args.enable,
            "retrieval_rerank_min_score": None,
        }
    )
    if not config.rerank_enabled:
        raise ValueError("请启用 RAG_RERANK_ENABLED，或传入 --enable 仅本次启用。")
    datasets = []
    seen = set()
    names = set()
    for path in args.dataset or DEFAULT_DATASETS:
        path = path.resolve()
        if path in seen:
            raise ValueError("同一数据集不能重复加入审计。")
        if path.name in names:
            raise ValueError("数据集文件名必须唯一，以便分别汇总。")
        seen.add(path)
        names.add(path.name)
        data, digest = load_dataset(path)
        datasets.append((path, data, digest))
    rows = []
    async with configured_reranker(config) as reranker:
        # 真实打分始终启用纯模型排序；生产权重为 0 时仍可做审计。
        scorer = RetrievalService(
            None, None, config.model_copy(update={"retrieval_rerank_weight": 1.0}), reranker
        )
        for path, data, digest in datasets:
            for case in data["queries"]:
                candidates = make_candidates(
                    data["documents"],
                    case,
                    config.retrieval_rerank_candidates,
                    args.seed,
                    args.order,
                )
                initial = [source.chunk_id for source in candidates]
                diagnostics = {"degraded": [], "stage_ms": {}, "rerank_applied": False}
                started = time.perf_counter()
                result = await scorer._rerank(case["query"], candidates, diagnostics)
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                if not diagnostics["rerank_applied"]:
                    raise ValueError(
                        f"审计中止 {path.name}/{case['id']}: "
                        + diagnostics.get("rerank_error", "unknown")
                    )
                scores = {source.chunk_id: source.rerank_score for source in result}

                class ReplayScores:
                    async def rerank(self, query, documents):
                        return [scores[key] for key in initial]

                rankings = {"initial": initial}
                for name, weight in (
                    ("configured", config.retrieval_rerank_weight),
                    ("rrf_075", 0.75),
                    ("model", 1.0),
                ):
                    replay = RetrievalService(
                        None,
                        None,
                        config.model_copy(update={"retrieval_rerank_weight": weight}),
                        ReplayScores(),
                    )
                    items = await replay._rerank(
                        case["query"], candidates, {"degraded": [], "stage_ms": {}}
                    )
                    rankings[name] = [source.chunk_id for source in items]
                labels = set(case["relevant"])
                row = {
                    "dataset": path.name,
                    "id": case["id"],
                    "group": case.get("group", "未分组"),
                    "query": case["query"],
                    "relevant": sorted(labels),
                    "candidate_count": len(candidates),
                    "candidate_label_coverage": len(set(initial) & labels) / len(labels)
                    if labels
                    else None,
                    "scores_by_id": scores,
                    "rankings": {
                        name: {"ids": ids, "metrics": ranking_metrics(ids, labels)}
                        for name, ids in rankings.items()
                    },
                    "elapsed_ms": elapsed,
                    "diagnostics": diagnostics,
                }
                rows.append(row)
                print(
                    json.dumps(
                        {
                            "dataset": path.name,
                            "id": case["id"],
                            "group": row["group"],
                            "model_rank": row["rankings"]["model"]["metrics"][
                                "first_relevant_rank"
                            ],
                            "elapsed_ms": elapsed,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    return {
        "report_version": "rerank-audit-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "synthetic_fixed_candidates_only_not_end_to_end_retrieval",
        "model": config.rerank_model,
        "weight": config.retrieval_rerank_weight,
        "threshold": None,
        "configured_threshold_not_applied": settings.retrieval_rerank_min_score,
        "candidate_order": args.order,
        "candidate_order_version": "corpus-shuffle-v1",
        "seed": args.seed,
        "candidate_limit": config.retrieval_rerank_candidates,
        "batch_size": config.rerank_batch_size,
        "max_input_tokens": config.rerank_max_input_tokens,
        "datasets": [
            {
                "name": path.name,
                "sha256": digest,
                "description": data.get("description"),
                "document_count": len(data["documents"]),
                "query_count": len(data["queries"]),
                "group_counts": dict(
                    Counter(case.get("group", "未分组") for case in data["queries"])
                ),
            }
            for path, data, digest in datasets
        ],
        "summary": summarize(rows),
        "by_dataset": {
            path.name: summarize([row for row in rows if row["dataset"] == path.name])
            for path, _, _ in datasets
        },
        "by_group": {
            group: summarize([row for row in rows if row["group"] == group])
            for group in sorted({row["group"] for row in rows})
        },
        "notes": [
            "使用已有人工合成相关性标签，不代表真实业务或答案事实支持率。",
            "无答案题不进入排序均值；无阈值的非空排序不表示回答正确或应当作答。",
            "默认初排为固定种子的随机顺序，不是检索器输出；候选覆盖率与排序质量分别报告。",
            "relevant-last 是使用标签构造的压力场景，不得作为普通初排效果。",
            "历史验证集在此全量报告后不再是新的未触碰测试集，后续调参需另建业务验证集。",
        ],
        "rows": rows,
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--dataset", type=Path, action="append", help="可重复传入；默认两套已有合成集全量"
    )
    result.add_argument("--output", type=Path, default=ROOT / "evals/results/rerank-audit-v2.json")
    result.add_argument("--seed", type=int, default=20261017)
    result.add_argument("--order", choices=("shuffled", "relevant-last"), default="shuffled")
    result.add_argument("--enable", action="store_true", help="仅为本次审计启用重排")
    return result


async def main(args):
    report = await audit(args, Settings())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("审计结果已保存：" + str(args.output))


if __name__ == "__main__":
    try:
        asyncio.run(main(parser().parse_args()))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
