"""Shared test doubles and helpers for the /runs based test suite."""

import asyncio
import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from app.config import Settings


def rag_answer(content, source_ids=None, **kwargs):
    """显式构造新版 RAG 模型返回值，不替生产代码兼容旧的纯文本输出。"""
    return AIMessage(
        content=json.dumps(
            {
                "status": "answered",
                "segments": [
                    {"text": content, "source_ids": [1] if source_ids is None else source_ids}
                ],
                "reason": None,
            },
            ensure_ascii=False,
        ),
        **kwargs,
    )


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


class StreamingModel(BaseChatModel):
    responses: list = Field(default_factory=lambda: [["你", "好"]])
    calls: int = 0
    gate: object = None
    first_token_sent: bool = False

    @property
    def _llm_type(self):
        return "streaming-test-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="非流式"))])

    async def _astream(self, messages, **kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, AIMessage):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=response.content,
                    tool_call_chunks=[
                        {
                            "name": c["name"],
                            "args": json.dumps(c["args"]),
                            "id": c["id"],
                            "index": i,
                        }
                        for i, c in enumerate(response.tool_calls)
                    ],
                )
            )
            return
        for i, value in enumerate(response):
            if i and self.gate is not None:
                await self.gate.wait()
            if isinstance(value, Exception):
                raise value
            yield ChatGenerationChunk(
                message=value
                if isinstance(value, AIMessageChunk)
                else AIMessageChunk(content=value)
            )
            self.first_token_sent = True


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def tool_call():
    return AIMessage(
        content="",
        tool_calls=[{"name": "multiply_numbers", "args": {"a": 12, "b": 7}, "id": "multiply-1"}],
    )


def create_run(client, message="你好", conversation_id=None, key="first"):
    conversation_id = conversation_id or client.post("/api/v1/conversations").json()["thread_id"]
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"message": message, "idempotency_key": key},
    )
    assert response.status_code == 202, response.text
    return conversation_id, response.json()["run_id"]


def wait_run(client, run_id, statuses=("completed", "failed", "paused")):
    async def wait():
        for _ in range(500):
            row = await client.app.state.run_service.repository.get(run_id)
            if row["status"] in statuses:
                return client.app.state.run_service.repository.response(row).model_dump(mode="json")
            await asyncio.sleep(0.01)
        raise AssertionError(f"task did not reach {statuses}: {row}")

    return client.portal.call(wait)


def wait_for_token(client, model):
    async def wait():
        for _ in range(500):
            if model.first_token_sent:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("model did not emit first token")

    client.portal.call(wait)


def events(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.startswith("id: "):
            continue
        event_id, event, data = frame.split("\n", 2)
        result.append((event_id[4:], event[7:], json.loads(data[6:])))
    return result


def test_database():
    """连接当前测试隔离 schema，绝不访问应用默认数据库。"""
    import psycopg

    return psycopg.connect(settings().database_url.get_secret_value())


test_database.__test__ = False
