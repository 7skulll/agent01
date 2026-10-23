import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from openai import APITimeoutError, LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import Field
from support import ScriptModel, events, rag_answer, settings, wait_run
from test_rag_pipeline import Embeddings, index, rag_run, upload

from app.main import create_app


def seed(client):
    upload(client)
    index(client)


@pytest.mark.parametrize("repair", [False, True])
def test_sdk_output_truncation_is_not_internal_error_or_retried(repair):
    completion = ChatCompletion(
        id="truncated", created=0, model="test", object="chat.completion", choices=[]
    )
    error = LengthFinishReasonError(completion=completion)
    outputs = ([AIMessage(content="private_bad_json")] if repair else []) + [error]
    model = ScriptModel(outputs=outputs)
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        seed(client)
        _, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "failed"
        assert result["error"]["code"] == "model_response_truncated"
        assert result["error"]["http_status"] == 502 and not result["error"]["retryable"]
        assert result["model_calls"] == 1 + int(repair)
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 409
        stream = client.get(f"/api/v1/runs/{rid}/events").text
        assert "private_bad_json" not in stream
        assert '"finish_reason":"length"' in stream or '"finish_reason": "length"' in stream


def refusal_message(reason):
    return AIMessage(
        content=json.dumps({"status": "insufficient_evidence", "segments": [], "reason": reason})
    )


def test_structured_ids_are_authoritative_and_numeric_body_is_preserved():
    text = "年份[2026]、array[3]和普通编号[1]都是正文。"
    model = ScriptModel(outputs=[rag_answer(text, [2, 1, 2])])
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        upload(client)
        upload(client, "年假申请需要主管批准。", "审批.txt")
        index(client)
        cid, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "completed", result
        assert result["answer"] == text + "[2][1]"
        assert result["answer_status"] == "answered"
        assert result["answer_protocol"] == "rag-answer-v2"
        assert result["answer_segments"] == [{"text": text, "source_ids": [2, 1]}]
        assert len(result["sources"]) == 2
        assert result["model_calls"] == 1 and result["answer_repair_calls"] == 0
        frames = events(client.get(f"/api/v1/runs/{rid}/events"))
        assert not any(name == "text_delta" for _, name, _ in frames)
        done = next(data for _, name, data in frames if name == "done")
        turns = client.get(f"/api/v1/conversations/{cid}/turns").json()["items"][0]
        for field in ["answer", "answer_status", "answer_protocol", "answer_segments", "sources"]:
            assert done[field] == result[field] == turns[field]


@pytest.mark.parametrize("reason", ["missing_fact", "conflicting_sources"])
def test_insufficient_with_retrieval_is_completed_and_persists_after_restart(reason):
    config = settings()
    model = ScriptModel(outputs=[refusal_message(reason)])
    with TestClient(create_app(config, chat_model=model, embedding_model=Embeddings())) as client:
        seed(client)
        cid, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "completed", result
        assert result["answer_status"] == "insufficient_evidence"
        assert result["answer_reason"] == reason
        assert result["sources"] == result["answer_segments"] == []
        assert result["model_calls"] == 1
        assert (
            client.get(f"/api/v1/conversations/{cid}").json()["messages"][-1]["content"]
            == result["answer"]
        )
    with TestClient(create_app(config, chat_model=model)) as client:
        persisted = client.get(f"/api/v1/runs/{rid}").json()
        assert persisted["answer_status"] == result["answer_status"]
        assert persisted["answer_reason"] == reason
        assert persisted["answer"] == result["answer"]


@pytest.mark.parametrize(
    "bad",
    [
        "{private_raw_broken_json",
        json.dumps(
            {
                "status": "answered",
                "segments": [{"text": "private_raw_illegal_id", "source_ids": [99]}],
                "reason": None,
            }
        ),
    ],
)
def test_invalid_response_is_repaired_once_without_leaking_json(bad):
    usage = {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}
    model = ScriptModel(
        outputs=[
            AIMessage(content=bad, usage_metadata=usage),
            rag_answer("十天。", usage_metadata=usage),
        ]
    )
    embedding = Embeddings()
    with TestClient(create_app(settings(), chat_model=model, embedding_model=embedding)) as client:
        seed(client)
        cid, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "completed", result
        assert result["answer"] == "十天。[1]"
        assert result["model_calls"] == 2 and result["answer_repair_calls"] == 1
        assert len(embedding.queries) == 1
        assert model.seen[0][-1] == model.seen[1][-1]
        assert "错误码" in model.seen[1][0].text
        stream = client.get(f"/api/v1/runs/{rid}/events")
        assert "private_raw" not in stream.text
        frames = events(stream)
        assert not any(name == "text_delta" for _, name, _ in frames)
        usage_frames = [data for _, name, data in frames if name == "token_usage"]
        assert [data["stage"] for data in usage_frames] == ["answer", "answer_repair"]
        assert all(data["actual"]["input_tokens"] == 50 for data in usage_frames)
        assert "private_raw" not in client.get(f"/api/v1/conversations/{cid}").text
        metrics = client.get("/api/v1/metrics").json()
        assert any(stage["stage"] == "citation_repair" for stage in metrics["timings"])


def test_second_invalid_response_is_failed_not_replaced_with_insufficient():
    model = ScriptModel(
        outputs=[AIMessage(content="private_bad_json"), rag_answer("private_bad_reference", [99])]
    )
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        seed(client)
        cid, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "failed" and result["error"]["code"] == "invalid_citations"
        assert result["model_calls"] == 2 and result["answer_repair_calls"] == 1
        assert result["answer_status"] is None and result["answer"] is None
        assert result["answer_segments"] == result["sources"] == []
        assert client.get(f"/api/v1/conversations/{cid}").json()["messages"] == []
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 409
        assert "private_bad" not in client.get(f"/api/v1/runs/{rid}/events").text


def test_repair_cannot_bypass_model_call_budget():
    model = ScriptModel(outputs=[AIMessage(content="not json"), rag_answer("不能调用")])
    with TestClient(
        create_app(
            settings(agent_max_model_calls=1), chat_model=model, embedding_model=Embeddings()
        )
    ) as client:
        seed(client)
        _, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "failed" and result["error"]["code"] == "agent_limit_exceeded"
        assert result["model_calls"] == 1 and len(model.seen) == 1


def test_repair_network_failure_preserves_error_and_cannot_call_again():
    error = APITimeoutError(request=httpx.Request("POST", "https://example.test"))
    model = ScriptModel(outputs=[AIMessage(content="not json"), error, rag_answer("不能调用")])
    config = settings()
    with TestClient(create_app(config, chat_model=model, embedding_model=Embeddings())) as client:
        seed(client)
        _, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "failed" and result["error"]["code"] == "model_timeout"
        assert not result["error"]["retryable"]
        assert result["answer_status"] is None
        assert result["answer_repair_calls"] == 1
    with TestClient(create_app(config, chat_model=model)) as client:
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 409
        assert len(model.seen) == 2


def test_pause_after_repair_and_restart_validates_saved_response_without_new_call(monkeypatch):
    model = ScriptModel(outputs=[AIMessage(content="not json"), rag_answer("十天。")])
    config = settings()
    embedding = Embeddings()
    with TestClient(create_app(config, chat_model=model, embedding_model=embedding)) as client:
        seed(client)
        service = client.app.state.run_service

        async def gates():
            return asyncio.Event(), asyncio.Event()

        entered, release = client.portal.call(gates)
        original = service.repository.reserve_call

        async def delayed(run_id, kind, maximum):
            attempt = await original(run_id, kind, maximum)
            if kind == "repair":
                entered.set()
                await release.wait()
            return attempt

        monkeypatch.setattr(service.repository, "reserve_call", delayed)
        cid, rid = rag_run(client)
        client.portal.call(entered.wait)
        assert client.post(f"/api/v1/runs/{rid}/pause").status_code == 200
        client.portal.call(release.set)
        result = wait_run(client, rid, ("paused",))
        assert result["answer"] is None and result["answer_repair_calls"] == 1
        assert len(model.seen) == 2
    no_model = ScriptModel(outputs=[AssertionError("must not invoke")])
    with TestClient(create_app(config, chat_model=no_model, embedding_model=embedding)) as client:
        assert client.post(f"/api/v1/runs/{rid}/resume").status_code == 202
        result = wait_run(client, rid)
        assert result["status"] == "completed" and result["answer"] == "十天。[1]"
        assert result["answer_status"] == "answered" and result["answer_repair_calls"] == 1
        assert not no_model.seen and len(embedding.queries) == 1
        assert (
            client.get(f"/api/v1/conversations/{cid}/turns").json()["items"][0]["answer_protocol"]
            == "rag-answer-v2"
        )


def test_startup_reconciliation_publishes_already_validated_metadata(monkeypatch):
    config = settings()
    model = ScriptModel(outputs=[refusal_message("missing_fact")])
    with TestClient(create_app(config, chat_model=model, embedding_model=Embeddings())) as client:
        seed(client)
        service = client.app.state.run_service

        async def gate():
            return asyncio.Event()

        completed = client.portal.call(gate)
        original = service.repository.finish

        async def interrupted(run_id, status, **kwargs):
            if status == "completed":
                completed.set()
                raise asyncio.CancelledError
            return await original(run_id, status, **kwargs)

        monkeypatch.setattr(service.repository, "finish", interrupted)
        cid, rid = rag_run(client)
        client.portal.call(completed.wait)
    second = ScriptModel(outputs=[AssertionError("no generation")])
    with TestClient(create_app(config, chat_model=second)) as client:
        result = client.get(f"/api/v1/runs/{rid}").json()
        assert result["status"] == "completed", result
        assert result["answer_status"] == "insufficient_evidence"
        assert result["answer_reason"] == "missing_fact"
        assert result["answer_protocol"] == "rag-answer-v2" and not second.seen


def test_empty_retrieval_sets_persistent_business_status_without_model():
    model = ScriptModel(outputs=[AssertionError("no generation")])
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        _, rid = rag_run(client)
        result = wait_run(client, rid)
        assert result["status"] == "completed"
        assert result["answer_status"] == "insufficient_evidence"
        assert result["answer_reason"] == "no_retrieval_hits" and not model.seen


@pytest.mark.parametrize("json_mode", [True, False])
def test_json_mode_is_explicit_and_prompt_only_mode_still_validates(json_mode):
    class OptionsModel(ScriptModel):
        options: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.options.append(kwargs.get("response_format"))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = OptionsModel(outputs=[rag_answer("十天。")])
    with TestClient(
        create_app(
            settings(rag_answer_json_mode=json_mode), chat_model=model, embedding_model=Embeddings()
        )
    ) as client:
        seed(client)
        _, rid = rag_run(client)
        assert wait_run(client, rid)["status"] == "completed"
        assert model.options == [{"type": "json_object"} if json_mode else None]


def test_truncated_model_response_is_not_repaired_or_misclassified_as_insufficient():
    model = ScriptModel(
        outputs=[
            rag_answer("十天。", response_metadata={"finish_reason": "length"}),
            rag_answer("不能调用"),
        ]
    )
    with TestClient(
        create_app(settings(), chat_model=model, embedding_model=Embeddings())
    ) as client:
        seed(client)
        _, rid = rag_run(client)
        result = wait_run(client, rid)
        assert (
            result["status"] == "failed" and result["error"]["code"] == "model_response_truncated"
        )
        assert result["answer_status"] is None and result["model_calls"] == 1
        assert result["answer_repair_calls"] == 0 and len(model.seen) == 1
