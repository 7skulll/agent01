"""Embedding 客户端：把文本批量转成向量，供后续向量化流程使用。

设计要点（借鉴 FastGPT 的 embedding 入口与 RAGFlow 的批处理/归一化）：
- 统一入口 ``embed_documents`` / ``embed_query``，内部按 ``batch_size`` 分批顺序调用。
- 失败按可重试/不可重试分类，指数退避重试；超时、连接错误、429、5xx 视为可重试。
- 校验返回数量与维度一致，可对齐配置维度；首次调用后推断并缓存实际维度。
- 可选 L2 归一化（归一化后余弦相似度退化为点积）。
- 通过注入底层 client 支持测试，不依赖网络。
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.tokenization import DEFAULT_COUNTER, TokenCounter, embedding_limit

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """可预期的 Embedding 失败；``retryable`` 表示是否值得重试。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class EmbeddingModel(Protocol):
    model_name: str
    dim: int | None

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class OpenAIEmbeddingModel:
    """OpenAI 兼容接口的 Embedding 客户端（DashScope 兼容模式等同样适用）。"""

    def __init__(
        self,
        client,
        *,
        model_name: str,
        dim: int | None = None,
        batch_size: int = 10,
        max_retries: int = 3,
        normalize: bool = True,
        backoff_seconds: float = 0.5,
        counter: TokenCounter = DEFAULT_COUNTER,
        max_input_tokens: int = 0,
        max_batch_tokens: int = 0,
    ):
        self._client = client
        self.counter = counter
        self.max_input_tokens = max_input_tokens
        self.max_batch_tokens = max_batch_tokens
        self.model_name = model_name
        self.dim = dim
        self.batch_size = max(1, batch_size)
        self.max_retries = max(0, max_retries)
        self.normalize = normalize
        self.backoff_seconds = max(0.0, backoff_seconds)

    @classmethod
    def from_settings(cls, settings) -> OpenAIEmbeddingModel:
        from langchain_openai import OpenAIEmbeddings

        if settings.embedding_max_input_tokens <= 0:
            raise ValueError("请按 Embedding 服务限制配置 RAG_EMBEDDING_MAX_INPUT_TOKENS。")
        client = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url or None,
            timeout=settings.embedding_timeout_seconds,
            max_retries=0,  # 重试由本类统一处理，便于分类与退避
            check_embedding_ctx_length=False,  # 本类统一校验，禁止底层静默截断
        )
        return cls(
            client,
            model_name=settings.embedding_model,
            dim=settings.embedding_dim or None,
            batch_size=settings.embedding_batch_size,
            max_retries=settings.embedding_max_retries,
            normalize=settings.embedding_normalize,
            counter=TokenCounter(settings.embedding_tokenizer),
            max_input_tokens=embedding_limit(settings),
            max_batch_tokens=(
                max(1, int(settings.embedding_max_batch_tokens * settings.token_safety_ratio))
                if settings.embedding_max_batch_tokens
                else 0
            ),
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # 保留实际输入原文，确保向量 input_hash 与远端输入一致。
        cleaned = list(texts)
        if not cleaned or any(not text or not text.strip() for text in cleaned):
            raise EmbeddingError("Embedding 输入不能为空。")
        counts = [self.counter.count(text) for text in cleaned]
        # 整批预检，任何单条不合法都不先发送前面的条目。
        for count in counts:
            if (self.max_input_tokens and count > self.max_input_tokens) or (
                self.max_batch_tokens and count > self.max_batch_tokens
            ):
                raise EmbeddingError("Embedding 输入超过 token 预算，请缩短问题或重新切片。")
        vectors: list[list[float]] = []
        batch: list[str] = []
        total = 0
        for text, count in zip(cleaned, counts):
            if batch and (
                len(batch) >= self.batch_size
                or self.max_batch_tokens
                and total + count > self.max_batch_tokens
            ):
                vectors.extend(await self._embed_batch(batch))
                batch, total = [], 0
            batch.append(text)
            total += count
        if batch:
            vectors.extend(await self._embed_batch(batch))
        self._validate(vectors, len(cleaned))
        if not self.normalize:
            return vectors
        return [self._normalize(vector) for vector in vectors]

    async def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise EmbeddingError("Embedding 输入不能为空。")
        return (await self.embed_documents([text]))[0]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last: EmbeddingError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await self._client.aembed_documents(batch)
                try:
                    vectors = [[float(value) for value in vector] for vector in result]
                except (ValueError, TypeError, OverflowError):
                    raise EmbeddingError("Embedding 返回了无效向量。") from None
                # 每个实际请求独立校验，防止批次之间的数量偏差抵消后错配 chunk。
                self._validate(vectors, len(batch))
                return vectors
            except EmbeddingError as exc:
                last = exc
            except Exception as exc:  # noqa: BLE001 - 统一分类后再决定是否重试
                last = _classify(exc)
            if not last.retryable or attempt >= self.max_retries:
                raise last
            delay = self.backoff_seconds * (2**attempt)
            logger.warning(
                "embedding_retry attempt=%s delay=%.2f error=%s",
                attempt + 1,
                delay,
                last,
            )
            if delay > 0:
                await asyncio.sleep(delay)
        assert last is not None
        raise last  # pragma: no cover - 循环必然返回或抛出

    def _validate(self, vectors: list[list[float]], expected: int) -> None:
        if len(vectors) != expected:
            raise EmbeddingError("Embedding 返回向量数量与输入不一致。")
        dims = {len(vector) for vector in vectors}
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise EmbeddingError("Embedding 返回了非有限值。")
        if not dims or 0 in dims:
            raise EmbeddingError("Embedding 返回了空向量。")
        if len(dims) > 1:
            raise EmbeddingError("Embedding 返回向量维度不一致。")
        for vector in vectors:
            norm = math.hypot(*vector)
            if not norm or not math.isfinite(norm):
                raise EmbeddingError("Embedding 返回了零向量或超出有限范围。")
        actual = dims.pop()
        if self.dim is not None and actual != self.dim:
            raise EmbeddingError(f"Embedding 维度不符：期望 {self.dim}，实际 {actual}。")
        if self.dim is None:
            self.dim = actual

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.hypot(*vector)
        if not math.isfinite(norm):
            raise EmbeddingError("Embedding 向量超出有限范围。")
        if norm == 0:
            return vector
        return [value / norm for value in vector]


def _classify(error: Exception) -> EmbeddingError:
    if isinstance(error, APITimeoutError):
        return EmbeddingError("Embedding 请求超时。", retryable=True)
    if isinstance(error, APIConnectionError):
        return EmbeddingError("无法连接 Embedding 服务。", retryable=True)
    if isinstance(error, APIStatusError):
        if error.status_code == 429 or error.status_code >= 500:
            return EmbeddingError("Embedding 服务限流或暂时不可用。", retryable=True)
        return EmbeddingError("Embedding 服务拒绝请求，请检查模型与参数。")
    return EmbeddingError("Embedding 调用失败。", retryable=True)


def build_embedding_model(settings) -> OpenAIEmbeddingModel | None:
    """按配置构建 Embedding 客户端；必填项缺失时返回 None（表示未启用）。"""
    if not (
        settings.embedding_model.strip()
        and settings.embedding_base_url.strip()
        and settings.embedding_api_key.get_secret_value().strip()
    ):
        return None
    return OpenAIEmbeddingModel.from_settings(settings)
