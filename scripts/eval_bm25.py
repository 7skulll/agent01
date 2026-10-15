"""在原基础集与预先固定的独立验证集上进行 FastGPT 分词对照（召回流程保持一致）。"""

import argparse
import asyncio

from scripts.eval_retrieval import ROOT, evaluate


async def main():
    for name, filename in [
        ("baseline", "retrieval_baseline.json"),
        ("holdout", "bm25_holdout.json"),
    ]:
        print("Dataset:", name, flush=True)
        await evaluate(
            argparse.Namespace(
                dataset=ROOT / "evals" / filename,
                output=ROOT / "evals/results" / f"fastgpt-{name}.json",
                min_score=0.2,
                bm25_comparison=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
