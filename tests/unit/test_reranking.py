"""真实 HTTP 协议、重试、生命周期和搜索降级的离线回归。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.rag.reranking import RerankError, SiliconFlowReranker, configured_reranker
from app.rag.retrieval import RetrievalService, SearchRequest, Source


def response(scores=(0.2, 0.9)):
    return httpx.Response(
        200,
        json={
            "results": [
                {"index": i, "relevance_score": score}
                for i, score in reversed(list(enumerate(scores)))
            ]
        },
    )


async def request(handler, *, timeout=1, documents=None):
    async with httpx.AsyncClient(
        base_url="https://example.test/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        return await SiliconFlowReranker(client, "test-model", timeout).rerank(
            "年假有多少天？", documents if documents is not None else ["报销要发票。", "年假十天。"]
        )


def test_request_returns_scores_in_original_order():
    def handler(req):
        assert str(req.url) == "https://example.test/v1/rerank"
        assert json.loads(req.content) == {
            "model": "test-model",
            "query": "年假有多少天？",
            "documents": ["报销要发票。", "年假十天。"],
            "top_n": 2,
            "return_documents": False,
        }
        return response()

    assert asyncio.run(request(handler)) == [0.2, 0.9]


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "relevance_score": 0.5}] * 2,
        [{"index": 0, "relevance_score": 0.5}, {"index": 2, "relevance_score": 0.5}],
        [{"index": -1, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.5}],
        [{"index": True, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": "0.5"}, {"index": 1, "relevance_score": 0.5}],
        [{"index": 0, "relevance_score": True}, {"index": 1, "relevance_score": 0.5}],
        [{"index": 0}, {"index": 1, "relevance_score": 0.5}],
        None,
        {},
        [None, None],
    ],
)
def test_invalid_response_is_not_retried(results):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"results": results})

    with pytest.raises(RerankError, match="invalid_response"):
        asyncio.run(request(handler))
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_scores_are_rejected(value):
    body = (
        '{"results":[{"index":0,"relevance_score":'
        + value
        + ('},{"index":1,"relevance_score":0.5}]}')
    )
    with pytest.raises(RerankError, match="invalid_response"):
        asyncio.run(request(lambda _: httpx.Response(200, text=body)))


def test_invalid_json_does_not_expose_body():
    with pytest.raises(RerankError) as exc:
        asyncio.run(request(lambda _: httpx.Response(200, text="secret-body")))
    assert str(exc.value) == "invalid_response"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 302])
def test_permanent_errors_do_not_retry_or_expose_response(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, text="secret-token-and-body")

    with pytest.raises(RerankError, match=f"^http_{status}$"):
        asyncio.run(request(handler))
    assert len(calls) == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_retries_once(status):
    calls = []

    def handler(req):
        calls.append(req)
        return (
            httpx.Response(status, headers={"Retry-After": "0"}) if len(calls) == 1 else response()
        )

    assert asyncio.run(request(handler)) == [0.2, 0.9]
    assert len(calls) == 2


def test_transient_error_exhaustion():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, headers={"Retry-After": "0"})

    with pytest.raises(RerankError, match="http_503"):
        asyncio.run(request(handler))
    assert len(calls) == 2


def test_transport_failure_retries_once_without_secret(caplog):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ConnectError("secret-token", request=req)
        return response()

    assert asyncio.run(request(handler)) == [0.2, 0.9]
    assert len(calls) == 2
    assert "secret-token" not in caplog.text


def test_retry_after_is_inside_total_timeout():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "60"})

    with pytest.raises(RerankError, match="timeout"):
        asyncio.run(request(handler, timeout=0.02))
    assert len(calls) == 1


def test_timeout_and_cancellation():
    async def slow(req):
        await asyncio.sleep(10)
        return response()

    with pytest.raises(RerankError, match="timeout"):
        asyncio.run(request(slow, timeout=0.02))

    async def cancel(req):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(request(cancel))


def test_empty_candidates_do_not_call_service():
    def unexpected(req):
        pytest.fail("empty candidates must not call service")

    assert asyncio.run(request(unexpected, documents=[])) == []


def test_configured_client_headers_url_and_cleanup(monkeypatch):
    clients = []
    original = httpx.AsyncClient

    def factory(**kwargs):
        def handler(req):
            assert str(req.url) == "https://example.test/v1/rerank"
            assert req.headers["Authorization"] == "Bearer test-key"
            return response()

        client = original(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def run():
        config = Settings(
            _env_file=None,
            rerank_enabled=True,
            rerank_api_key="test-key",
            rerank_base_url="https://example.test/v1",
        )
        with pytest.raises(RuntimeError, match="startup failure"):
            async with configured_reranker(config) as reranker:
                assert await reranker.rerank("年假", ["报销", "十天"]) == [0.2, 0.9]
                assert not clients[0].is_closed
                raise RuntimeError("startup failure")
        assert clients[0].is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"rerank_api_key": SecretStr("")},
        {"rerank_model": " "},
        {"rerank_base_url": "ftp://example.test"},
        {"rerank_base_url": "https://user:secret@example.test/v1"},
        {"rerank_base_url": "https://example.test/v1?key=secret"},
        {"rerank_base_url": "https://example.test/v1#fragment"},
    ],
)
def test_incomplete_or_unsafe_config_fails_without_exposing_values(changes):
    config = Settings(_env_file=None, rerank_enabled=True, rerank_api_key="test-key")
    config = config.model_copy(update=changes)

    async def run():
        async with configured_reranker(config):
            pytest.fail("invalid configuration must fail")

    with pytest.raises(ValueError) as exc:
        asyncio.run(run())
    assert "secret" not in str(exc.value)


def test_disabled_and_injected_reranker_do_not_create_client(monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("should not construct owned client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected)

    async def run():
        async with configured_reranker(Settings(_env_file=None)) as instance:
            assert instance is None
        injected = object()
        async with configured_reranker(
            Settings(_env_file=None, rerank_enabled=True), injected
        ) as instance:
            assert instance is injected

    asyncio.run(run())


def test_application_startup_failure_closes_owned_rerank_client(monkeypatch):
    import app.main as main_module

    clients = []
    original = httpx.AsyncClient

    def factory(**kwargs):
        client = original(**kwargs, transport=httpx.MockTransport(lambda _: response()))
        clients.append(client)
        return client

    class UnavailableDatabase:
        def __init__(self, settings):
            self.pool = self.open_pool()

        @asynccontextmanager
        async def open_pool(self):
            yield

        async def setup(self):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(main_module, "Database", UnavailableDatabase)

    async def run():
        config = Settings(_env_file=None, rerank_enabled=True, rerank_api_key="test-key")
        app = main_module.create_app(config)
        with pytest.raises(RuntimeError, match="database unavailable"):
            async with app.router.lifespan_context(app):
                pytest.fail("database startup should fail")
        assert len(clients) == 1 and clients[0].is_closed

    asyncio.run(run())


def test_dotenv_rerank_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "RAG_RERANK_ENABLED=true\nRAG_RERANK_PROVIDER=siliconflow\n"
        "RAG_RERANK_MODEL=test-model\nRAG_RERANK_API_KEY=test-key\n"
        "RAG_RERANK_BASE_URL=https://example.test/v1\n"
    )
    settings = Settings(_env_file=env)
    assert settings.rerank_enabled
    assert settings.rerank_model == "test-model"
    assert settings.rerank_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(settings)


def make_source(i):
    return Source(
        citation_id=0,
        chunk_id=str(i),
        file_id=str(i),
        name="制度.txt",
        content=f"制度{i}",
        heading="制度",
        page=1,
        kind="text",
        score=1 / (i + 1),
    )


def test_http_rerank_flows_into_search_and_keeps_vector_threshold(monkeypatch):
    async def known_knowledge_base(*args):
        return {"kb_id": "default"}

    monkeypatch.setattr("app.rag.retrieval.require_knowledge_base", known_knowledge_base)

    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/v1/", transport=httpx.MockTransport(lambda _: response())
        ) as client:
            service = RetrievalService(
                SimpleNamespace(database=None),
                SimpleNamespace(version="test"),
                Settings(_env_file=None, retrieval_rerank_weight=1),
                SiliconFlowReranker(client, "test-model", 1),
            )

            async def dense(*args):
                rows = {
                    str(i): {**make_source(i).model_dump(), "vector_score": score}
                    for i, score in enumerate([0.9, 0.1])
                }
                return [[("0", 0.9), ("1", 0.1)]], rows, 0

            service._dense = dense
            result = await service.search(
                "default", SearchRequest(query="年假", mode="vector", min_score=0.2)
            )
            assert [s.chunk_id for s in result.sources] == ["0"]
            assert result.sources[0].rerank_score == 0.2
            assert result.diagnostics["rerank_applied"]
            assert result.diagnostics["rerank_model"] == "test-model"
            assert result.diagnostics["using_similarity_filter"]

    asyncio.run(run())


def test_service_failure_retains_original_candidates_and_reports_safe_reason():
    async def run():
        async with httpx.AsyncClient(
            base_url="https://example.test/v1/",
            transport=httpx.MockTransport(lambda _: httpx.Response(401, text="secret")),
        ) as client:
            service = RetrievalService(
                None, None, Settings(_env_file=None), SiliconFlowReranker(client, "test-model", 1)
            )
            candidates = [make_source(1), make_source(2)]
            diagnostics = {"degraded": [], "stage_ms": {}, "rerank_applied": False}
            assert await service._rerank("年假", candidates, diagnostics) == candidates
            assert diagnostics["rerank_error"] == "http_401"
            assert not diagnostics["rerank_applied"]
            assert all(item.rerank_score is None for item in candidates)
            assert diagnostics["degraded"] == ["rerank_failed"]

    asyncio.run(run())
