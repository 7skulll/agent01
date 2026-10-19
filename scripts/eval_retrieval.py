"""固定合成集上的 全文 / 向量 / FastGPT 分级 RRF 对比，使用真实 Embedding。

只上传版本控制中的合成资料，使用临时数据库。查询向量预计算后为三种模式复用，
本地搜索延迟与远端向量化延迟分开报告，不把缓存后的搜索时间当作端到端延迟。
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app
from app.rag.embedding import build_embedding_model
from scripts._database import temporary_database

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * p
    lower = int(pos)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (pos - lower), 2)


def metrics(rows):
    labelled = [row for row in rows if row["relevant"]]
    result = {"queries": len(labelled)}
    for k in [1, 3, 5]:
        result[f"recall@{k}"] = round(
            statistics.mean(
                len(set(row["ranked_ids"][:k]) & set(row["relevant"])) / len(set(row["relevant"]))
                for row in labelled
            ),
            4,
        )
    result["mrr@10"] = round(
        statistics.mean(
            next(
                (
                    1 / rank
                    for rank, doc in enumerate(row["ranked_ids"][:10], 1)
                    if doc in row["relevant"]
                ),
                0,
            )
            for row in labelled
        ),
        4,
    )
    result["local_search_p50_ms"] = percentile([r["local_search_ms"] for r in rows], 0.5)
    result["local_search_p95_ms"] = percentile([r["local_search_ms"] for r in rows], 0.95)
    result["no_answer_queries_returning_candidates"] = sum(
        bool(row["ranked_ids"]) for row in rows if not row["relevant"]
    )
    return result


class CachedQueries:
    """仅用于评估公平比较，生产链路不增加查询缓存。"""

    def __init__(self, model):
        self.model = model
        self.cache = {}

    @property
    def model_name(self):
        return self.model.model_name

    @property
    def dim(self):
        return self.model.dim

    async def embed_documents(self, texts):
        return await self.model.embed_documents(texts)

    async def embed_query(self, text):
        return self.cache[text]


async def measure_modes(client, base, dataset, file_ids, min_score):
    runs = {}
    for mode in ["keyword", "vector", "hybrid"]:
        rows = []
        for item in dataset["queries"]:
            start = time.perf_counter()
            response = await client.post(
                base + "/search",
                json={
                    "query": item["query"],
                    "mode": mode,
                    "top_k": 10,
                    "min_score": min_score,
                },
            )
            response.raise_for_status()
            result = response.json()
            if result["diagnostics"]["degraded"]:
                raise SystemExit("评估过程中召回发生降级，不输出混合质量结论。")
            rows.append(
                {
                    **item,
                    "ranked_ids": [file_ids[s["file_id"]] for s in result["sources"]],
                    "scores": [s["score"] for s in result["sources"]],
                    "local_search_ms": round((time.perf_counter() - start) * 1000, 2),
                }
            )
        runs[mode] = {"metrics": metrics(rows), "queries": rows}
        print(mode, runs[mode]["metrics"], flush=True)
    return runs


async def evaluate(args):
    dataset = json.loads(args.dataset.read_text())
    settings = Settings()
    model = build_embedding_model(settings)
    if model is None:
        raise SystemExit("Embedding 配置不完整，未执行真实评估。")
    cached = CachedQueries(model)
    embedding_times = []
    semaphore = asyncio.Semaphore(2)

    async def embed(query):
        async with semaphore:
            start = time.perf_counter()
            cached.cache[query] = await model.embed_query(query)
            embedding_times.append((time.perf_counter() - start) * 1000)

    # 先用测试集首条问题验证服务，失败即停止，避免每个文件都重复网络重试。
    queries = list(dict.fromkeys(item["query"] for item in dataset["queries"]))
    if not queries:
        raise SystemExit("评估集没有查询，未执行 Embedding。")
    await embed(queries[0])
    async with temporary_database(settings) as database_url:
        with tempfile.TemporaryDirectory(prefix="rag-baseline-") as directory:
            root = Path(directory)
            config = settings.model_copy(
                update={
                    "database_url": database_url,
                    "file_storage_path": root / "files",
                    "rag_query_rewrite": False,
                    "rag_query_expand": False,
                    "rerank_enabled": False,
                    "retrieval_neighbor_chunks": 0,
                    "retrieval_max_per_file": 20,
                    "retrieval_dense_weight": 0.5,
                }
            )
            app = create_app(config, embedding_model=cached)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://eval"
                ) as client:
                    base = "/api/v1/knowledge-bases/default"
                    file_ids = {}
                    for doc in dataset["documents"]:
                        response = await client.post(
                            base + "/files",
                            files={
                                "file": (
                                    f"{doc['id']}.md",
                                    f"# {doc['title']}\n\n{doc['text']}".encode(),
                                )
                            },
                            headers={"Idempotency-Key": doc["id"]},
                        )
                        response.raise_for_status()
                        file_ids[response.json()["file_id"]] = doc["id"]
                    (await client.post(base + "/index")).raise_for_status()
                    await app.state.index_service.wait()
                    files = (await client.get(base + "/files")).json()["items"]
                    if len(files) != len(dataset["documents"]) or any(
                        item["index_status"] != "indexed" for item in files
                    ):
                        raise SystemExit("评估索引失败；请检查 Embedding 配置、鉴权或网络。")
                    print(f"Indexed {len(files)} synthetic documents.", flush=True)
                    # 创建协程前去重，避免并发请求在缓存写入前重复调用远端。
                    await asyncio.gather(*(embed(query) for query in queries[1:]))
                    print(f"Embedded {len(cached.cache)} unique queries.", flush=True)
                    experiments = None
                    if getattr(args, "bm25_comparison", False):
                        from scripts.bm25_experiment import compare_bm25

                        experiments = await compare_bm25(
                            app, client, base, dataset, file_ids, args.min_score
                        )
                        selected_variant = "fastgpt"
                        runs = experiments[selected_variant]["runs"]
                    else:
                        runs = await measure_modes(client, base, dataset, file_ids, args.min_score)
                    report = {
                        "dataset_description": dataset["description"],
                        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                        "document_count": len(dataset["documents"]),
                        "model": model.model_name,
                        "dimension": model.dim,
                        "parameters": {
                            "fulltext_analyzer": "jieba-rs-hmm",
                            "fusion": "channel_rrf_then_weighted_rrf",
                            "dense_candidates": {"hybrid": 80, "vector": 100},
                            "keyword_candidates": {"hybrid": 60, "keyword": 100},
                            "similarity_filter": "vector_only",
                            "dense_weight": 0.5,
                            "rrf_k": 60,
                            "min_score": args.min_score,
                            "top_k": 10,
                            "rewrite": False,
                            "expand": False,
                            "rerank": False,
                            "neighbors": False,
                        },
                        "embedding_p50_ms": percentile(embedding_times, 0.5),
                        "embedding_p95_ms": percentile(embedding_times, 0.95),
                        "notes": [
                            "仅人工合成小样本，不代表实际知识库质量。",
                            "预计算相同查询向量用于公平比较；local_search 延迟不含远端调用。",
                            "无答案问题只观察是否返回候选，不计入 Recall/MRR，不验证回答或拒答。",
                        ],
                        "runs": runs,
                        "bm25_comparison": experiments,
                        "selected_variant": selected_variant if experiments else None,
                    }
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                    print("Report:", args.output, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/retrieval_baseline.json")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/results/fastgpt-retrieval.json"
    )
    parser.add_argument("--min-score", type=float, default=0.2)
    parser.add_argument(
        "--bm25-comparison",
        action="store_true",
        help="使用相同向量比较历史双字词项与当前 FastGPT 分词（共用当前召回流程）",
    )
    args = parser.parse_args()
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
