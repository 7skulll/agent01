"""Embedding 客户端测试：批处理、归一化、维度校验、重试与装配。"""

import asyncio
import math

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIStatusError
from support import settings

from app.main import create_app
from app.rag.embedding import (
    EmbeddingError,
    OpenAIEmbeddingModel,
    _classify,
    build_embedding_model,
)


def run(coro):
    return asyncio.run(coro)


class FakeEmbeddings:
    def __init__(self, dim=4, failures=None, vectors=None):
        self.dim = dim
        self.calls: list[list[str]] = []
        self.failures = list(failures or [])
        self.vectors = vectors

    async def aembed_documents(self, texts):
        self.calls.append(list(texts))
        if self.failures:
            error = self.failures.pop(0)
            if error is not None:
                raise error
        if self.vectors is not None:
            return self.vectors
        return [[float(index + 1) for index in range(self.dim)] for _ in texts]

    async def aembed_query(self, text):
        return (await self.aembed_documents([text]))[0]


def model(client, **kwargs):
    kwargs.setdefault("model_name", "fake")
    kwargs.setdefault("backoff_seconds", 0)
    return OpenAIEmbeddingModel(client, **kwargs)


# --------------------------------------------------------------------------- 批处理 / 归一化


def test_documents_are_sent_in_batches():
    client = FakeEmbeddings(dim=4)
    instance = model(client, batch_size=2, normalize=False)
    vectors = run(instance.embed_documents(["a", "b", "c", "d", "e"]))
    assert [len(call) for call in client.calls] == [2, 2, 1]
    assert len(vectors) == 5 and all(len(vector) == 4 for vector in vectors)


def test_vectors_are_l2_normalized():
    instance = model(FakeEmbeddings(dim=4), normalize=True)
    vectors = run(instance.embed_documents(["a"]))
    assert math.isclose(math.sqrt(sum(value * value for value in vectors[0])), 1.0)


def test_normalization_can_be_disabled():
    instance = model(FakeEmbeddings(dim=4), normalize=False)
    vectors = run(instance.embed_documents(["a"]))
    assert vectors[0] == [1.0, 2.0, 3.0, 4.0]


def test_embed_query_returns_single_vector():
    instance = model(FakeEmbeddings(dim=3), normalize=False)
    assert run(instance.embed_query("hello")) == [1.0, 2.0, 3.0]


# --------------------------------------------------------------------------- 校验


def test_empty_input_is_rejected():
    with pytest.raises(EmbeddingError, match="不能为空"):
        run(model(FakeEmbeddings()).embed_documents(["a", " "]))
    with pytest.raises(EmbeddingError, match="不能为空"):
        run(model(FakeEmbeddings()).embed_query("   "))


def test_dimension_mismatch_is_rejected():
    instance = model(FakeEmbeddings(dim=4), dim=3)
    with pytest.raises(EmbeddingError, match="维度不符"):
        run(instance.embed_documents(["a"]))


def test_dimension_is_inferred_from_first_call():
    instance = model(FakeEmbeddings(dim=6))
    assert instance.dim is None
    run(instance.embed_documents(["a"]))
    assert instance.dim == 6


def test_empty_returned_vector_is_rejected():
    client = FakeEmbeddings(vectors=[[]])
    with pytest.raises(EmbeddingError, match="空向量"):
        run(model(client, normalize=False).embed_documents(["a"]))


def test_inconsistent_returned_count_is_rejected():
    client = FakeEmbeddings(vectors=[[1.0, 2.0]])
    with pytest.raises(EmbeddingError, match="数量"):
        run(model(client, normalize=False).embed_documents(["a", "b"]))


# --------------------------------------------------------------------------- 重试


def test_retryable_error_is_retried_then_succeeds():
    client = FakeEmbeddings(
        dim=2,
        failures=[EmbeddingError("boom", retryable=True), EmbeddingError("boom", retryable=True)],
    )
    instance = model(client, max_retries=3, normalize=False)
    assert run(instance.embed_documents(["a"])) == [[1.0, 2.0]]
    assert len(client.calls) == 3


def test_retry_exhaustion_raises():
    client = FakeEmbeddings(dim=2, failures=[EmbeddingError("boom", retryable=True)] * 5)
    instance = model(client, max_retries=2, normalize=False)
    with pytest.raises(EmbeddingError):
        run(instance.embed_documents(["a"]))
    assert len(client.calls) == 3  # 初次 + 2 次重试


def test_non_retryable_error_is_not_retried():
    client = FakeEmbeddings(dim=2, failures=[EmbeddingError("bad request")])
    instance = model(client, max_retries=3, normalize=False)
    with pytest.raises(EmbeddingError, match="bad request"):
        run(instance.embed_documents(["a"]))
    assert len(client.calls) == 1


def test_generic_errors_are_treated_as_retryable():
    client = FakeEmbeddings(dim=2, failures=[RuntimeError("network")])
    instance = model(client, max_retries=1, normalize=False)
    assert run(instance.embed_documents(["a"])) == [[1.0, 2.0]]
    assert len(client.calls) == 2


def test_classify_status_errors():
    request = httpx.Request("POST", "http://example.test/v1/embeddings")
    retryable = APIStatusError("busy", response=httpx.Response(503, request=request), body=None)
    fatal = APIStatusError("bad", response=httpx.Response(400, request=request), body=None)
    assert _classify(retryable).retryable is True
    assert _classify(fatal).retryable is False


# --------------------------------------------------------------------------- 装配


def test_build_embedding_model_requires_full_config():
    assert build_embedding_model(settings()) is None
    configured = build_embedding_model(
        settings(
            embedding_model="text-embedding-v4",
            embedding_base_url="https://example.test/compatible-mode/v1",
            embedding_api_key="secret",
            embedding_dim=1024,
            embedding_max_input_tokens=2048,
        )
    )
    assert configured is not None
    assert configured.model_name == "text-embedding-v4"
    assert configured.dim == 1024


class ScriptEmbeddingModel:
    model_name = "script"
    dim = 4

    async def embed_documents(self, texts):
        return [[0.0, 0.0, 0.0, 1.0] for _ in texts]

    async def embed_query(self, text):
        return [0.0, 0.0, 0.0, 1.0]


def test_health_reports_embedding_readiness():
    with TestClient(create_app(settings())) as client:
        assert client.get("/api/v1/health").json()["embedding_ready"] is False

    with TestClient(create_app(settings(), embedding_model=ScriptEmbeddingModel())) as client:
        assert client.get("/api/v1/health").json()["embedding_ready"] is True
        assert client.app.state.embedding_model is not None
