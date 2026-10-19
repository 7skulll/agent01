"""用两条合成资料验证真实 Rerank 与融合；不读写知识库，不输出正文或凭据。"""

import argparse
import asyncio

from app.config import Settings
from app.rag.reranking import configured_reranker
from app.rag.retrieval import RetrievalService, Source


async def main(enable=False, window_test=False):
    settings = Settings()
    if enable:
        settings = settings.model_copy(update={"rerank_enabled": True})
    if not settings.rerank_enabled:
        raise SystemExit("Rerank 未启用；可用 --enable 仅为本次冒烟临时启用。")
    # 冒烟单独使用纯重排顺序，避免融合权重掩盖模型输出；不修改 .env。
    settings = settings.model_copy(update={"retrieval_rerank_weight": 1.0})
    if window_test:
        settings = settings.model_copy(
            update={
                "rerank_max_input_tokens": 128,
                "rerank_window_overlap_tokens": 16,
                "rerank_batch_size": 4,
            }
        )
    candidates = [
        Source(
            citation_id=0,
            chunk_id=str(i),
            file_id=str(i),
            name="冒烟测试.txt",
            content=text,
            heading=None,
            page=None,
            kind="text",
            score=1 / (i + 1),
        )
        for i, text in enumerate(["番茄炒蛋需要番茄和鸡蛋。", "公司员工每年享有十天带薪年假。"])
    ]
    if window_test:
        candidates[1].content = "本资料记录一般办公通知。" * 30 + candidates[1].content
    async with configured_reranker(settings) as reranker:
        service = RetrievalService(None, None, settings, reranker)
        diagnostics = {"degraded": [], "stage_ms": {}, "rerank_applied": False}
        result = await service._rerank("公司员工每年有多少天带薪年假？", candidates, diagnostics)
        if not diagnostics["rerank_applied"]:
            raise SystemExit("Rerank 调用失败：" + diagnostics.get("rerank_error", "unknown"))
        if result[0].chunk_id != "1" or not result[0].rerank_score > result[1].rerank_score:
            raise SystemExit("Rerank 返回有效分数，但未将相关合成资料排到前面。")
        print(
            f"真实 Rerank{'分窗/批次聚合' if window_test else ''} → 响应索引映射 → 排序：通过；"
            f"候选数=2，耗时={diagnostics['stage_ms']['rerank']}ms。"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="本次验证临时启用，不修改配置")
    parser.add_argument(
        "--window-test", action="store_true", help="缩小预算验证长文尾部命中与批次聚合"
    )
    args = parser.parse_args()
    asyncio.run(main(args.enable, args.window_test))
