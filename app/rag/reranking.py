"""可选重排契约：输入自然文本，输出与文档顺序一致的原始相关性分数。"""

from typing import Protocol


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...
