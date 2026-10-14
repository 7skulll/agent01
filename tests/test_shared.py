"""Shared behavior that outlived the removed /chat engine."""

import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from openai import APIStatusError
from support import ScriptModel, create_run, settings, wait_run

from app.main import create_app


def test_conversation_create_list_and_history_not_found():
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[AIMessage(content="x")]))
    ) as client:
        created = client.post("/api/v1/conversations")
        assert created.status_code == 201
        thread_id = created.json()["thread_id"]
        listing = client.get("/api/v1/conversations").json()
        assert [item["thread_id"] for item in listing] == [thread_id]

        detail = client.get(f"/api/v1/conversations/{uuid4()}").json()["detail"]
        assert detail["code"] == "conversation_not_found"
        assert detail["retryable"] is False
        assert detail["thread_id"] and detail["request_id"]


def test_history_exposes_published_messages_only_after_success():
    model = ScriptModel(outputs=[AIMessage(content="你好呀")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client, key="history")
        assert wait_run(client, run)["status"] == "completed"
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        assert [m["role"] for m in history["messages"]] == ["human", "ai"]
        assert history["conversation"]["status"] == "ready"


def test_cors_supports_cross_origin_browser_requests():
    model = ScriptModel(outputs=[AIMessage(content="你好")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        preflight = client.options(
            "/api/v1/conversations",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        response = client.post("/api/v1/conversations", headers={"Origin": "http://localhost:3000"})
        assert response.headers["access-control-allow-origin"] == "*"


def test_unconfigured_model_and_request_validation():
    with TestClient(create_app(settings())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        result = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "你好", "idempotency_key": "k1"},
        )
        assert result.status_code == 503
        assert result.json()["detail"]["code"] == "model_not_configured"

        path = f"/api/v1/conversations/{conversation}/runs"
        assert client.post(path, json={"message": "  ", "idempotency_key": "k"}).status_code == 422
        assert client.post(path, json={"message": "x", "idempotency_key": ""}).status_code == 422
        assert (
            client.post(path, json={"message": "x", "idempotency_key": "k", "extra": 1}).status_code
            == 422
        )

    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[AIMessage(content="x")]))
    ) as client:
        missing = client.post(
            f"/api/v1/conversations/{uuid4()}/runs",
            json={"message": "x", "idempotency_key": "k"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "conversation_not_found"


@pytest.mark.parametrize(
    "upstream_status, expected_status, code, retryable",
    [
        (401, 502, "upstream_auth_error", False),
        (400, 502, "upstream_request_error", False),
        (429, 503, "upstream_rate_limited", True),
        (503, 502, "upstream_unavailable", True),
    ],
)
def test_upstream_status_is_classified_without_leaking_details(
    upstream_status, expected_status, code, retryable
):
    response = httpx.Response(
        upstream_status, request=httpx.Request("POST", "https://example.test")
    )
    error = APIStatusError(
        "private-token-detail", response=response, body={"secret": "private-token-detail"}
    )
    with TestClient(create_app(settings(), chat_model=ScriptModel(outputs=[error]))) as client:
        _, run = create_run(client)
        result = wait_run(client, run)
        assert result["status"] == "failed"
        assert result["error"]["code"] == code
        assert result["error"]["retryable"] is retryable
        assert result["error"]["http_status"] == expected_status
        assert "private-token-detail" not in json.dumps(result)
        detail = client.get(f"/api/v1/runs/{run}").json()
        assert detail["error"]["code"] == code
