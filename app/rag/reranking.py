"""可选重排契约：输入自然文本，输出与文档顺序一致的原始相关性分数。"""

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from app.observability import Metrics
from app.rag.chunking import split_text
from app.tokenization import DEFAULT_COUNTER, TokenCounter

logger = logging.getLogger(__name__)


class RerankError(RuntimeError):
    """仅包含安全错误码，不暴露 HTTP 请求、凭据或服务端响应正文。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Reranker(Protocol):
    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...


class SiliconFlowReranker:
    """将按相关性排序的 HTTP 响应恢复成输入顺序；整次调用共用超时预算。"""

    def __init__(
        self,
        client,
        model_name,
        timeout_seconds,
        *,
        counter=DEFAULT_COUNTER,
        max_input_tokens=0,
        safety_ratio=0.95,
        overlap_tokens=64,
        batch_size=64,
        max_windows=1024,
        metrics=None,
    ):
        self.client = client
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.counter = counter
        self.input_budget = max(1, int(max_input_tokens * safety_ratio)) if max_input_tokens else 0
        self.overlap_tokens = overlap_tokens
        self.batch_size = batch_size
        self.max_windows = max_windows
        self.metrics = metrics or Metrics()

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """请求所有候选的分数；仅瞬态故障重试一次，取消直接传播。"""
        if not query.strip() or any(not text.strip() for text in documents):
            raise RerankError("invalid_input")
        if not documents:
            return []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                windows, owners = await asyncio.to_thread(self._windows, query, documents)
                scores = [-math.inf] * len(documents)
                for start in range(0, len(windows), self.batch_size):
                    batch = windows[start : start + self.batch_size]
                    result = await self._request(query, batch)
                    for owner, score in zip(owners[start : start + len(batch)], result):
                        scores[owner] = max(scores[owner], score)
                return scores
        except TimeoutError:
            raise RerankError("timeout") from None

    def _windows(self, query, documents):
        """保留完整原文并生成有限窗口；复检完整查询/正文预算，不按字符比例猜测。"""
        windows, owners = [], []
        cap = self.input_budget - self.counter.count(query + "\n") - 8
        if self.input_budget and cap <= 0:
            raise RerankError("query_too_long")
        for owner, text in enumerate(documents):
            remaining = self.max_windows - len(windows)
            if remaining <= 0:
                raise RerankError("too_many_windows")
            if not self.input_budget:
                pieces = [text]
            else:
                size = cap
                while True:
                    try:
                        pieces = split_text(
                            text, size, min(self.overlap_tokens, size // 4), remaining, self.counter
                        )
                    except ValueError:
                        raise RerankError("window_budget_exceeded") from None
                    overflow = max(
                        self.counter.count(query + "\n" + piece) + 8 - self.input_budget
                        for piece in pieces
                    )
                    if overflow <= 0:
                        break
                    size -= overflow
                    if size <= 0:
                        raise RerankError("query_too_long")
            windows.extend(pieces)
            owners.extend([owner] * len(pieces))
        return windows, owners

    async def _request(self, query, documents):
        """每批独立校验索引；重试及等待共享 rerank() 的整次超时预算。"""
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "return_documents": False,
        }
        for attempt in range(2):
            delay = 0.25
            self.metrics.tokens(
                "rerank",
                estimated_input=sum(self.counter.count(query + "\n" + text) for text in documents),
            )
            self.metrics.increment("rerank_http_attempts")
            try:
                response = await self.client.post("rerank", json=payload)
            except httpx.RequestError:
                if attempt:
                    raise RerankError("transport_error") from None
                code = "transport_error"
            else:
                status = response.status_code
                trace_id = response.headers.get("x-siliconcloud-trace-id", "")[:128]
                logger.info(
                    "rerank_response model=%s count=%s attempt=%s status=%s trace_id=%r",
                    self.model_name,
                    len(documents),
                    attempt + 1,
                    status,
                    trace_id,
                )
                if response.is_success:
                    scores = self._scores(response, len(documents))
                    data = response.json()
                    meta = data.get("meta")
                    usage = meta.get("tokens") if isinstance(meta, dict) else None
                    self.metrics.tokens("rerank", usage=usage or data.get("usage"))
                    return scores
                code = f"http_{status}"
                if attempt or not (status == 429 or status >= 500):
                    raise RerankError(code)
                delay = self._retry_delay(response.headers.get("retry-after"))
            logger.warning("rerank_retry reason=%s attempt=%s", code, attempt + 1)
            self.metrics.increment("rerank_http_retries", reason=code)
            await asyncio.sleep(delay)

    @staticmethod
    def _retry_delay(value):
        """支持 Retry-After 秒数和 HTTP 日期；不早于服务端指定时间重试。"""
        if value is None:
            return 0.25
        try:
            delay = float(value)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                delay = (deadline - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return 0.25
        return max(0.0, delay) if math.isfinite(delay) else 0.25

    @staticmethod
    def _scores(response, count):
        """索引必须完整且唯一；不把缺失分数填零或误配到其他文档。"""
        try:
            results = response.json()["results"]
            if not isinstance(results, list) or len(results) != count:
                raise ValueError
            scores = [None] * count
            for item in results:
                index, score = item["index"], item["relevance_score"]
                if (
                    type(index) is not int
                    or not 0 <= index < count
                    or scores[index] is not None
                    or type(score) not in (int, float)
                    or not math.isfinite(score)
                ):
                    raise ValueError
                scores[index] = float(score)
            return scores
        except (ValueError, KeyError, TypeError, OverflowError):
            raise RerankError("invalid_response") from None


@asynccontextmanager
async def configured_reranker(settings, injected=None, *, metrics=None):
    """注入实例优先且由调用方持有；自动装配的连接在启动失败和关闭时都释放。"""
    if injected is not None or not settings.rerank_enabled:
        yield injected
        return
    if not settings.rerank_model.strip() or not settings.rerank_api_key.get_secret_value().strip():
        raise ValueError("启用 Rerank 时必须配置 RAG_RERANK_MODEL 和 RAG_RERANK_API_KEY。")
    try:
        url = httpx.URL(settings.rerank_base_url.strip())
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError
    except (httpx.InvalidURL, ValueError):
        raise ValueError(
            "RAG_RERANK_BASE_URL 必须为不含凭据、查询参数和片段的 HTTP(S) 地址。"
        ) from None
    async with httpx.AsyncClient(
        base_url=str(url).rstrip("/") + "/",
        headers={"Authorization": f"Bearer {settings.rerank_api_key.get_secret_value()}"},
        timeout=settings.retrieval_rerank_timeout_seconds,
    ) as client:
        yield SiliconFlowReranker(
            client,
            settings.rerank_model.strip(),
            settings.retrieval_rerank_timeout_seconds,
            counter=TokenCounter(settings.rerank_tokenizer),
            max_input_tokens=settings.rerank_max_input_tokens,
            safety_ratio=settings.token_safety_ratio,
            overlap_tokens=settings.rerank_window_overlap_tokens,
            batch_size=settings.rerank_batch_size,
            max_windows=settings.rerank_max_windows,
            metrics=metrics,
        )
