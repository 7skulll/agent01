import asyncio
import json
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from openai import APIConnectionError
from support import (
    ScriptModel,
    StreamingModel,
    create_run,
    events,
    settings,
    tool_call,
    wait_for_token,
    wait_run,
)

from app.main import create_app


def test_run_tool_loop_publishes_history_and_followup_works():
    model = ScriptModel(outputs=[tool_call(), AIMessage(content="84"), AIMessage(content="记得")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client, "12乘7")
        result = wait_run(client, run)
        assert result["status"] == "completed"
        assert result["answer"] == "84" and result["tools_used"] == ["multiply_numbers"]
        assert (result["model_calls"], result["tool_calls"]) == (2, 1)
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        assert len(history["messages"]) == 4
        assert history["conversation"]["active_run_id"] is None
        streamed = events(client.get(f"/api/v1/runs/{run}/events"))
        assert streamed[-1][1] == "done"
        assert any(event == "state_update" for _, event, _ in streamed)
        assert all(data["run_id"] == run for _, _, data in streamed)
        assert [data["event_seq"] for _, _, data in streamed] == list(range(1, len(streamed) + 1))
        _, followup = create_run(client, "追问", conversation_id=conversation, key="followup")
        assert wait_run(client, followup)["status"] == "completed"
        assert model.seen[-1][-2].content == "84"


def test_idempotency_concurrent_submission_and_session_isolation():
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        repeat = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "你好", "idempotency_key": "first"},
        )
        assert repeat.status_code == 202 and repeat.json()["run_id"] == run
        conflict = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "其他", "idempotency_key": "first"},
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["detail"]["code"] == "idempotency_conflict"
        )
        assert (
            client.post(
                f"/api/v1/conversations/{conversation}/runs",
                json={"message": "其他", "idempotency_key": "second"},
            ).status_code
            == 409
        )
        busy = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "旧接口", "idempotency_key": "busy"},
        )
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "conversation_busy"
        other_conversation, other_run = create_run(client, key="first")
        assert other_conversation != conversation and other_run != run
        client.portal.call(model.gate.set)
        assert wait_run(client, run)["status"] == "completed"
        assert wait_run(client, other_run)["status"] == "completed"
        repeat = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "你好", "idempotency_key": "first"},
        )
        assert repeat.json()["run_id"] == run and repeat.json()["status"] == "completed"


def test_pause_is_at_node_boundary_and_resume_keeps_budget_and_history():
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        pause = client.post(f"/api/v1/runs/{run}/pause")
        assert pause.status_code == 200 and pause.json()["pause_requested"]
        assert pause.json()["status"] == "running"
        client.portal.call(model.gate.set)
        paused = wait_run(client, run, ("paused",))
        assert paused["model_calls"] == 1
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        busy = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "并发", "idempotency_key": "busy"},
        )
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "conversation_busy"
        stream = events(client.get(f"/api/v1/runs/{run}/events"))
        assert stream[-1][1] == "paused"
        resumed = client.post(f"/api/v1/runs/{run}/resume")
        assert resumed.status_code == 202, resumed.text
        completed = wait_run(client, run, ("completed",))
        assert completed["model_calls"] == 1 and model.calls == 1
        assert completed["elapsed_seconds"] >= paused["elapsed_seconds"]
        assert len(client.get(f"/api/v1/conversations/{conversation}").json()["messages"]) == 2
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409


def test_failed_model_node_resumes_without_repeating_successful_tool():
    error = APIConnectionError(
        message="private-upstream-detail", request=httpx.Request("POST", "https://example.test")
    )
    model = StreamingModel(responses=[tool_call(), ["部分", error], ["84"]])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client, "12乘7")
        failed = wait_run(client, run, ("failed",))
        assert failed["error"]["code"] == "upstream_connection_error"
        assert (failed["model_calls"], failed["tool_calls"]) == (2, 1)
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        result = wait_run(client, run, ("completed",))
        assert (result["model_calls"], result["tool_calls"]) == (3, 1)
        stream = events(client.get(f"/api/v1/runs/{run}/events"))
        assert sum(name == "tool_start" for _, name, _ in stream) == 1
        assert sum(name == "done" for _, name, _ in stream) == 1
        reset = [data for _, name, data in stream if name == "message_reset"]
        assert reset[0]["message_id"] == f"{run}:model:2"
        assert "private-upstream-detail" not in json.dumps(stream)
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        assert len(history["messages"]) == 4
        assert sum(m["role"] == "human" for m in history["messages"]) == 1


def test_resume_does_not_reset_call_budget_or_rewind_newer_history():
    error = APIConnectionError(request=httpx.Request("POST", "https://example.test"))
    with TestClient(
        create_app(
            settings(agent_max_model_calls=1),
            chat_model=ScriptModel(outputs=[error, AIMessage(content="恢复")]),
        )
    ) as client:
        conversation, run = create_run(client)
        assert wait_run(client, run)["error"]["retryable"]
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        exhausted = wait_run(client, run, ("failed",))
        assert exhausted["error"]["code"] == "agent_limit_exceeded"
        assert exhausted["model_calls"] == 1
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409
        # A failed run may only resume against the successful history it started with.
    with TestClient(
        create_app(
            settings(), chat_model=ScriptModel(outputs=[error, AIMessage(content="新轮成功")])
        )
    ) as client:
        conversation, old = create_run(client)
        wait_run(client, old)
        _, new = create_run(client, conversation_id=conversation, key="new")
        assert wait_run(client, new)["status"] == "completed"
        conflict = client.post(f"/api/v1/runs/{old}/resume")
        assert (
            conflict.status_code == 409 and conflict.json()["detail"]["code"] == "resume_conflict"
        )


@pytest.mark.parametrize("invalid", ["length", "invalid_tool", "empty"])
def test_invalid_output_is_not_published_or_executed(invalid):
    if invalid == "length":
        output = AIMessage(content="不完整", response_metadata={"finish_reason": "length"})
        code = "model_response_truncated"
    elif invalid == "empty":
        output = AIMessage(content="")
        code = "invalid_model_response"
    else:
        output = AIMessage(
            content="84",
            invalid_tool_calls=[
                {"name": "multiply_numbers", "args": "not-json", "id": "bad", "error": "secret"}
            ],
            tool_calls=tool_call().tool_calls,
        )
        code = "tool_error"
    with TestClient(create_app(settings(), chat_model=ScriptModel(outputs=[output]))) as client:
        conversation, run = create_run(client)
        result = wait_run(client, run)
        assert result["status"] == "failed" and result["error"]["code"] == code
        assert result["tool_calls"] == 0
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409


def test_cancellation_discards_partial_turn_and_is_idempotent():
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        cancelled = client.post(f"/api/v1/runs/{run}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post(f"/api/v1/runs/{run}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        stream = events(client.get(f"/api/v1/runs/{run}/events"))
        assert sum(name == "cancelled" for _, name, _ in stream) == 1
        assert not any(name == "done" for _, name, _ in stream)
        client.portal.call(model.gate.set)
        _, next_run = create_run(client, conversation_id=conversation, key="next")
        assert wait_run(client, next_run)["status"] == "completed"


def test_event_replay_validates_cursor_and_does_not_repeat_events():
    with TestClient(create_app(settings(), chat_model=StreamingModel())) as client:
        _, run = create_run(client)
        wait_run(client, run)
        all_events = events(client.get(f"/api/v1/runs/{run}/events"))
        cursor = all_events[2][0]
        replay = events(client.get(f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": cursor}))
        assert replay == all_events[3:]
        assert events(client.get(f"/api/v1/runs/{run}/events?after=3")) == all_events[3:]
        assert (
            client.get(
                f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": all_events[-1][0]}
            ).text
            == ""
        )
        assert (
            client.get(
                f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": f"{uuid4()}:1"}
            ).status_code
            == 400
        )
        assert client.get(f"/api/v1/runs/{run}/events?after=99999").status_code == 409
        assert [data["text"] for _, name, data in all_events if name == "text_delta"] == [
            "你",
            "好",
        ]


def test_sse_disconnect_leaves_task_running_and_can_reconnect():
    model = StreamingModel()
    app = create_app(settings(), chat_model=model)
    with TestClient(app) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        _, run = create_run(client)
        wait_for_token(client, model)

        async def scenario():
            queue = asyncio.Queue()
            await queue.put({"type": "http.request", "body": b"", "more_body": False})
            frames = []

            async def send(event):
                if event["type"] == "http.response.body":
                    frames.append(event.get("body", b""))
                    if b"event: text_delta" in frames[-1]:
                        await queue.put({"type": "http.disconnect"})

            path = f"/api/v1/runs/{run}/events"
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "client": ("test", 1234),
                "server": ("test", 80),
            }
            await asyncio.wait_for(app(scope, queue.get, send), 1)
            row = await app.state.run_service.repository.get(run)
            assert row["status"] == "running" and run in app.state.run_service.tasks
            model.gate.set()
            return b"".join(frames).decode()

        received = client.portal.call(scenario)
        assert "event: text_delta" in received
        assert wait_run(client, run)["status"] == "completed"
        assert events(client.get(f"/api/v1/runs/{run}/events"))[-1][1] == "done"


def test_paused_checkpoints_survive_restart_and_require_manual_resume():
    config = settings()
    model = StreamingModel()
    with TestClient(create_app(config, chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        client.post(f"/api/v1/runs/{run}/pause")
        client.portal.call(model.gate.set)
        wait_run(client, run, ("paused",))
    restored = StreamingModel()
    with TestClient(create_app(config, chat_model=restored)) as client:
        assert client.get(f"/api/v1/runs/{run}").json()["status"] == "paused"
        assert (
            client.get(f"/api/v1/conversations/{conversation}").json()["conversation"][
                "active_run_id"
            ]
            == run
        )
        assert restored.calls == 0
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        assert wait_run(client, run)["status"] == "completed"
        assert restored.calls == 0  # Resume finalizes the already checkpointed answer.


def test_graph_completed_publication_failure_resumes_without_calling_model(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        repository = client.app.state.run_service.repository
        original = repository.finish

        async def fail_success(run_id, status, **kwargs):
            if status == "completed":
                raise psycopg.OperationalError("private-database-detail")
            return await original(run_id, status, **kwargs)

        monkeypatch.setattr(repository, "finish", fail_success)
        conversation, run = create_run(client)
        failed = wait_run(client, run, ("failed",))
        assert failed["error"]["code"] == "history_publish_failed"
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        monkeypatch.setattr(repository, "finish", original)
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        result = wait_run(client, run, ("completed",))
        assert result["answer"] == "你好" and model.calls == 1
        assert len(client.get(f"/api/v1/conversations/{conversation}").json()["messages"]) == 2
        assert (
            sum(name == "done" for _, name, _ in events(client.get(f"/api/v1/runs/{run}/events")))
            == 1
        )


def test_run_validation_unknown_ids_and_unconfigured_model():
    with TestClient(create_app(settings())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        assert (
            client.post(
                f"/api/v1/conversations/{conversation}/runs",
                json={"message": "你好", "idempotency_key": "a"},
            ).status_code
            == 503
        )
        assert (
            client.post(
                f"/api/v1/conversations/{conversation}/runs", json={"message": "你好"}
            ).status_code
            == 422
        )
    with TestClient(create_app(settings(), chat_model=StreamingModel())) as client:
        assert (
            client.post(
                f"/api/v1/conversations/{uuid4()}/runs",
                json={"message": "你好", "idempotency_key": "a"},
            ).status_code
            == 404
        )
        for suffix in ("", "/events"):
            assert client.get(f"/api/v1/runs/{uuid4()}{suffix}").status_code == 404
        for action in ("pause", "resume", "cancel"):
            assert client.post(f"/api/v1/runs/{uuid4()}/{action}").status_code == 404


def test_tool_limit_and_total_timeout_release_conversation():
    with TestClient(
        create_app(settings(agent_max_tool_calls=1), chat_model=ScriptModel(outputs=[tool_call()]))
    ) as client:
        conversation, run = create_run(client)
        result = wait_run(client, run)
        assert result["status"] == "failed" and result["error"]["code"] == "agent_limit_exceeded"
        assert result["tool_calls"] == 1
        assert (
            client.get(f"/api/v1/conversations/{conversation}").json()["conversation"][
                "active_run_id"
            ]
            is None
        )
    model = StreamingModel()
    with TestClient(create_app(settings(agent_timeout_seconds=0.15), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        timed_out = wait_run(client, run)
        assert timed_out["status"] == "failed" and timed_out["error"]["code"] == "agent_timeout"
        assert timed_out["elapsed_seconds"] >= 0.15
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409


def test_paused_run_does_not_get_a_fresh_time_budget_on_resume():
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        client.post(f"/api/v1/runs/{run}/pause")
        client.portal.call(model.gate.set)
        wait_run(client, run, ("paused",))
        repository = client.app.state.run_service.repository

        async def exhaust_budget():
            async with repository.database.read():
                await repository.connection.execute(
                    "UPDATE runs SET elapsed_seconds=121 WHERE run_id=%s", (run,)
                )
                await repository.connection.commit()

        client.portal.call(exhaust_budget)
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        result = wait_run(client, run, ("failed",))
        assert result["error"]["code"] == "agent_timeout" and model.calls == 1
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []


def test_shutdown_keeps_interrupted_run_for_manual_recovery_without_auto_execution():
    config = settings()
    model = StreamingModel()
    with TestClient(create_app(config, chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)

        async def wait_persisted_token():
            # 模型已 yield 不代表流事件已提交；关闭前确认客户端可重放的部分输出。
            repository = client.app.state.run_service.repository
            async with asyncio.timeout(5):
                while not any(
                    event == "text_delta" for _, event, _ in await repository.events(run, 0)
                ):
                    await asyncio.sleep(0.01)

        client.portal.call(wait_persisted_token)
    restored = StreamingModel()
    with TestClient(create_app(config, chat_model=restored)) as client:
        state = client.get(f"/api/v1/runs/{run}").json()
        assert state["status"] == "recovering" and state["model_calls"] == 1
        assert restored.calls == 0 and client.app.state.run_service.tasks == {}
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        result = wait_run(client, run, ("completed",))
        assert restored.calls == 1 and result["model_calls"] == 2
        assert len(client.get(f"/api/v1/conversations/{conversation}").json()["messages"]) == 2
        assert any(
            name == "message_reset"
            for _, name, _ in events(client.get(f"/api/v1/runs/{run}/events"))
        )


def test_publication_event_and_history_rollback_together(monkeypatch):
    with TestClient(create_app(settings(), chat_model=StreamingModel())) as client:
        repository = client.app.state.run_service.repository
        original = repository._event

        async def fail_done(row, event, data):
            await original(row, event, data)
            if event == "done":
                raise psycopg.OperationalError("private-transaction-detail")

        monkeypatch.setattr(repository, "_event", fail_done)
        conversation, run = create_run(client)
        result = wait_run(client, run, ("failed",))
        assert result["error"]["code"] == "history_publish_failed"
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        assert not any(
            name == "done" for _, name, _ in events(client.get(f"/api/v1/runs/{run}/events"))
        )
        monkeypatch.setattr(repository, "_event", original)
        # Publication alone remains retryable even after the execution budget expires.
        client.portal.call(repository.add_elapsed, run, 121)
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        assert wait_run(client, run, ("completed",))["model_calls"] == 1


def test_two_simultaneous_creations_with_same_key_register_one_worker():
    from app.schemas import RunRequest

    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        service = client.app.state.run_service

        async def submit():
            body = RunRequest(message="你好", idempotency_key="shared")
            first, second = await asyncio.gather(
                service.create(conversation, body, str(uuid4())),
                service.create(conversation, body, str(uuid4())),
            )
            assert first.run_id == second.run_id
            assert len(service.tasks) == 1
            return str(first.run_id)

        run = client.portal.call(submit)
        wait_for_token(client, model)
        client.portal.call(model.gate.set)
        assert wait_run(client, run)["status"] == "completed" and model.calls == 1


def test_storage_recovery_after_worker_settlement_failure_does_not_leave_busy_forever(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        service = client.app.state.run_service
        repository = service.repository
        original = repository.finish

        async def unavailable(*args, **kwargs):
            raise psycopg.OperationalError("database temporarily unavailable")

        monkeypatch.setattr(repository, "finish", unavailable)
        conversation, run = create_run(client)

        async def worker_exits():
            task = service.tasks[run]
            result = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result[0], psycopg.OperationalError)

        client.portal.call(worker_exits)
        monkeypatch.setattr(repository, "finish", original)
        assert client.get(f"/api/v1/runs/{run}").json()["status"] == "recovering"
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        assert wait_run(client, run)["status"] == "completed"
        assert model.calls == 1
        assert (
            client.get(f"/api/v1/conversations/{conversation}").json()["conversation"][
                "active_run_id"
            ]
            is None
        )


def test_multiple_tools_checkpoint_individually_and_only_failed_tool_is_retried(monkeypatch):
    from langchain.tools import tool

    invocations = []

    @tool
    def multiply_numbers(a: float, b: float) -> str:
        """Multiply numbers; fail the second call once for recovery verification."""
        invocations.append(a)
        if a == 3 and invocations.count(3) == 1:
            raise ValueError("private-tool-detail")
        return str(a * b)

    monkeypatch.setattr("app.run_graph.CHAT_TOOLS", [multiply_numbers])
    model = ScriptModel(
        outputs=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "multiply_numbers", "args": {"a": 2, "b": 4}, "id": "tool-a"},
                    {"name": "multiply_numbers", "args": {"a": 3, "b": 5}, "id": "tool-b"},
                ],
            ),
            AIMessage(content="结果为8和15"),
        ]
    )
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client, "计算两组乘法")
        assert wait_run(client, run)["error"]["code"] == "tool_error"
        assert invocations == [2, 3]
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 202
        result = wait_run(client, run, ("completed",))
        assert invocations == [2, 3, 3] and len(model.seen) == 2
        assert result["tool_calls"] == 3
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        assert [m["content"] for m in history["messages"] if m["role"] == "tool"] == ["8.0", "15.0"]
        assert "private-tool-detail" not in client.get(f"/api/v1/runs/{run}/events").text


def test_restart_reconciles_completed_graph_without_executing_model(monkeypatch):
    config = settings()
    with TestClient(create_app(config, chat_model=StreamingModel())) as client:
        service = client.app.state.run_service

        async def stop_publication(*args, **kwargs):
            raise psycopg.OperationalError("simulate stop before publication")

        monkeypatch.setattr(service.repository, "finish", stop_publication)
        conversation, run = create_run(client)

        async def stopped():
            await asyncio.gather(service.tasks[run], return_exceptions=True)

        client.portal.call(stopped)
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
    restored = StreamingModel()
    with TestClient(create_app(config, chat_model=restored)) as client:
        assert restored.calls == 0
        assert client.get(f"/api/v1/runs/{run}").json()["status"] == "completed"
        assert len(client.get(f"/api/v1/conversations/{conversation}").json()["messages"]) == 2
        assert (
            sum(name == "done" for _, name, _ in events(client.get(f"/api/v1/runs/{run}/events")))
            == 1
        )
