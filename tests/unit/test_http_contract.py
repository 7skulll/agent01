"""通过真实路由注入故障，不启动数据库，验证统一错误协议。"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from app.config import Settings
from app.errors import ChatError
from app.main import create_app


@pytest.mark.parametrize("failure", [psycopg.OperationalError, PoolTimeout])
@pytest.mark.parametrize(
    "operation",
    [
        "files",
        "index",
        "search",
        "conversations",
        "create_conversation",
        "history",
        "create_run",
        "get_run",
        "events",
        "upload",
    ],
)
def test_database_failures_are_safe_structured_and_traceable(operation, failure):
    app = create_app(Settings(_env_file=None))

    async def fail(*args):
        raise failure("password=private_database_secret sql=private_sql")

    repository = SimpleNamespace(create=fail, list=fail)
    app.state.runtime = SimpleNamespace(repository=repository, history=fail)
    app.state.index_service = SimpleNamespace(list_files=fail, schedule=fail)
    app.state.retrieval_service = SimpleNamespace(search=fail)
    app.state.file_service = SimpleNamespace(upload=fail)
    app.state.run_service = SimpleNamespace(create=fail, inspect=fail, repository=repository)
    client = TestClient(app, raise_server_exceptions=False)
    identifier = str(uuid4())
    cases = {
        "files": ("GET", "/knowledge-bases/default/files", {}),
        "index": ("POST", "/knowledge-bases/default/index", {}),
        "search": ("POST", "/knowledge-bases/default/search", {"json": {"query": "年假"}}),
        "conversations": ("GET", "/conversations", {}),
        "create_conversation": ("POST", "/conversations", {}),
        "history": ("GET", f"/conversations/{identifier}", {}),
        "create_run": (
            "POST",
            f"/conversations/{identifier}/runs",
            {
                "json": {"message": "你好", "idempotency_key": "test"},
            },
        ),
        "get_run": ("GET", f"/runs/{identifier}", {}),
        "events": ("GET", f"/runs/{identifier}/events", {}),
        "upload": (
            "POST",
            "/knowledge-bases/default/files",
            {
                "files": {"file": ("test.txt", b"hello")},
                "headers": {"Idempotency-Key": "test"},
            },
        ),
    }
    method, path, kwargs = cases[operation]
    response = client.request(method, "/api/v1" + path, **kwargs)
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    detail = response.json()["detail"]
    assert detail["code"] == "storage_unavailable" and detail["retryable"] is True
    assert UUID(detail["request_id"])
    assert detail["request_id"] == response.headers["x-request-id"]
    assert "private_" not in response.text


def test_business_validation_and_framework_errors_share_object_envelope():
    app = create_app(Settings(_env_file=None))

    async def fail(*args):
        raise ChatError("index_in_progress", "请稍后重试。", 409, True)

    async def missing(*args):
        raise ChatError("knowledge_base_not_found", "知识库不存在。", 404)

    app.state.index_service = SimpleNamespace(schedule=fail, list_files=missing)
    client = TestClient(app)
    responses = [
        client.post("/api/v1/knowledge-bases/default/index"),
        client.get("/api/v1/knowledge-bases/other/files"),
        client.get("/api/v1/runs/private_invalid_uuid"),
        client.post(
            "/api/v1/knowledge-bases/default/search", json={"query": "", "extra": "private_payload"}
        ),
        client.get("/api/v1/not_found"),
        client.delete("/api/v1/conversations"),
    ]
    assert [r.status_code for r in responses] == [409, 404, 422, 422, 404, 405]
    for response in responses:
        detail = response.json()["detail"]
        assert {"code", "message", "retryable", "request_id"} <= detail.keys()
        assert detail["request_id"] == response.headers["x-request-id"]
        assert "private_" not in response.text
    assert responses[2].json()["detail"]["errors"][0]["loc"] == ["path", "run_id"]
    assert responses[-1].headers["allow"]
    spec = app.openapi()
    assert spec["paths"]["/api/v1/knowledge-bases/{kb_id}/files"]["get"]["responses"]["503"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}


def test_invalid_sse_cursor_is_structured_before_stream_starts():
    app = create_app(Settings(_env_file=None))

    async def inspect(*args):
        return {"event_seq": 0}

    app.state.run_service = SimpleNamespace(inspect=inspect, repository=SimpleNamespace())
    client = TestClient(app)
    response = client.get(f"/api/v1/runs/{uuid4()}/events", headers={"Last-Event-ID": "invalid"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "http_error"
    assert response.json()["detail"]["request_id"] == response.headers["x-request-id"]
