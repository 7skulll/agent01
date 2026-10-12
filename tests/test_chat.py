import asyncio
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import APIConnectionError, APITimeoutError
from pydantic import Field

from app.config import Settings
from app.main import create_app


class ScriptModel(BaseChatModel):
    outputs: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        value = self.outputs[min(len(self.seen) - 1, len(self.outputs) - 1)]
        if isinstance(value, Exception):
            raise value
        return ChatResult(generations=[ChatGeneration(message=value.model_copy(deep=True))])


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def tool_call():
    return AIMessage(
        content="",
        tool_calls=[{"name": "multiply_numbers", "args": {"a": 12, "b": 7}, "id": "multiply-1"}],
    )


def test_history_is_loaded_from_database_and_sessions_are_isolated():
    model = ScriptModel(
        outputs=[
            AIMessage(content="你好，小王"),
            AIMessage(content="你是小王"),
            AIMessage(content="新会话"),
        ]
    )
    with TestClient(create_app(settings(), chat_model=model)) as client:
        first = client.post("/api/v1/chat", json={"message": "我是小王"}).json()
        thread_id = first["thread_id"]
        second = client.post("/api/v1/chat", json={"message": "我是谁？", "thread_id": thread_id})
        assert second.json()["answer"] == "你是小王"
        assert [m.type for m in model.seen[1]] == ["system", "human", "ai", "human"]
        assert model.seen[1][1].content == "我是小王"
        other = client.post("/api/v1/chat", json={"message": "新会话"}).json()
        assert other["thread_id"] != thread_id
        assert len(model.seen[2]) == 2
        history = client.get(f"/api/v1/conversations/{thread_id}").json()
        assert len(history["messages"]) == 4
        assert history["conversation"]["status"] == "ready"
        assert len(client.get("/api/v1/conversations").json()) == 2


def test_tool_loop_and_tool_messages_are_persisted():
    model = ScriptModel(outputs=[tool_call(), AIMessage(content="12×7=84")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        response = client.post("/api/v1/chat", json={"message": "12乘7？"})
        assert response.status_code == 200
        result = response.json()
        assert result["tools_used"] == ["multiply_numbers"]
        assert len(model.seen) == 2
        assert model.seen[1][-1].type == "tool"
        assert model.seen[1][-1].content == "84.0"
        history = client.get(f"/api/v1/conversations/{result['thread_id']}").json()
        assert [m["role"] for m in history["messages"]] == ["human", "ai", "tool", "ai"]


def test_history_survives_application_restart():
    config = settings()
    with TestClient(
        create_app(config, chat_model=ScriptModel(outputs=[AIMessage(content="记住了")]))
    ) as client:
        thread_id = client.post("/api/v1/chat", json={"message": "我的名字是小李"}).json()[
            "thread_id"
        ]
    model = ScriptModel(outputs=[AIMessage(content="你叫小李")])
    with TestClient(create_app(config, chat_model=model)) as client:
        assert len(client.get(f"/api/v1/conversations/{thread_id}").json()["messages"]) == 2
        assert (
            client.post(
                "/api/v1/chat", json={"message": "我叫什么？", "thread_id": thread_id}
            ).status_code
            == 200
        )
        assert model.seen[0][1].content == "我的名字是小李"


def test_unconfigured_invalid_and_unknown_session():
    with TestClient(create_app(settings())) as client:
        assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 503
        assert client.post("/api/v1/chat", json={"message": "  "}).status_code == 422
        assert (
            client.post("/api/v1/chat", json={"message": "x", "thread_id": "bad"}).status_code
            == 422
        )
        assert client.post("/api/v1/chat", json={"message": "x", "history": []}).status_code == 422
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[AIMessage(content="x")]))
    ) as client:
        assert (
            client.post(
                "/api/v1/chat", json={"message": "x", "thread_id": str(uuid4())}
            ).status_code
            == 404
        )
        assert client.get(f"/api/v1/conversations/{uuid4()}").status_code == 404


@pytest.mark.parametrize(
    "error,status",
    [
        (APITimeoutError(request=httpx.Request("POST", "https://example.test")), 504),
        (
            APIConnectionError(
                message="sensitive-information",
                request=httpx.Request("POST", "https://example.test"),
            ),
            502,
        ),
    ],
)
def test_failure_is_not_retried_and_failed_session_is_blocked(error, status):
    model = ScriptModel(outputs=[error])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        thread_id = client.post("/api/v1/conversations").json()["thread_id"]
        result = client.post("/api/v1/chat", json={"message": "你好", "thread_id": thread_id})
        assert result.status_code == status
        assert "sensitive-information" not in result.text
        assert (
            client.get(f"/api/v1/conversations/{thread_id}").json()["conversation"]["status"]
            == "failed"
        )
        assert (
            client.post(
                "/api/v1/chat", json={"message": "重试", "thread_id": thread_id}
            ).status_code
            == 409
        )
    assert len(model.seen) == 1


def test_model_call_limit_stops_repeated_tool_loop():
    model = ScriptModel(outputs=[tool_call()])
    with TestClient(create_app(settings(agent_max_model_calls=2), chat_model=model)) as client:
        result = client.post("/api/v1/chat", json={"message": "一直计算"})
        assert result.status_code == 429
    assert len(model.seen) == 2


def test_concurrent_request_for_running_session_is_rejected():
    model = ScriptModel(outputs=[AIMessage(content="x")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        thread_id = client.post("/api/v1/conversations").json()["thread_id"]
        client.portal.call(client.app.state.chat_service.repository.claim, thread_id)
        assert (
            client.post("/api/v1/chat", json={"message": "x", "thread_id": thread_id}).status_code
            == 409
        )
        assert not model.seen


def test_provider_disables_retries():
    config = settings(
        llm_model="model", llm_base_url="https://example.test/v1", llm_api_key="test-key"
    )
    with patch("app.chat.ChatDeepSeek") as factory:
        factory.return_value = ScriptModel(outputs=[AIMessage(content="OK")])
        with TestClient(create_app(config)) as client:
            assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 200
    assert factory.call_args.kwargs["max_retries"] == 0
    assert factory.call_args.kwargs["streaming"] is False


def test_tool_execution_limit():
    model = ScriptModel(outputs=[tool_call()])
    with TestClient(create_app(settings(agent_max_tool_calls=1), chat_model=model)) as client:
        result = client.post("/api/v1/chat", json={"message": "继续乘法"})
        assert result.status_code == 429
        thread_id = client.get("/api/v1/conversations").json()[0]["thread_id"]
        history = client.get(f"/api/v1/conversations/{thread_id}").json()
        assert len([m for m in history["messages"] if m["role"] == "tool"]) == 1


def test_running_session_is_marked_failed_after_restart():
    config = settings()
    with TestClient(create_app(config)) as client:
        thread_id = client.post("/api/v1/conversations").json()["thread_id"]
        client.portal.call(client.app.state.chat_service.repository.claim, thread_id)
    with TestClient(create_app(config)) as client:
        item = client.get(f"/api/v1/conversations/{thread_id}").json()
        assert item["conversation"]["status"] == "failed"


class SlowModel(ScriptModel):
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(1)
        return self._generate(messages)


def test_overall_timeout_marks_session_failed():
    model = SlowModel(outputs=[AIMessage(content="迟到的回答")])
    with TestClient(create_app(settings(agent_timeout_seconds=0.05), chat_model=model)) as client:
        assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 504
        assert client.get("/api/v1/conversations").json()[0]["status"] == "failed"
