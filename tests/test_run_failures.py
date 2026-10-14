import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import (
    ScriptModel,
    StreamingModel,
    create_run,
    events,
    settings,
    wait_for_token,
    wait_run,
)

from app.main import create_app
from app.schemas import RunRequest


def test_create_returns_from_transaction_and_retries_attach_queued_worker(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        repository = client.app.state.run_service.repository
        original = repository._row

        async def transactional_row(run_id):
            # No fallible re-read is allowed after creation has committed.
            assert repository.connection.in_transaction
            return await original(run_id)

        monkeypatch.setattr(repository, "_row", transactional_row)
        body = RunRequest(message="你好", idempotency_key="queued")
        row, created = client.portal.call(repository.create, conversation, body, str(uuid4()))
        assert created and not repository.connection.in_transaction
        assert row["event_seq"] == 1
        monkeypatch.setattr(repository, "_row", original)
        # Simulate a persisted creation whose original caller never registered a worker.
        response = client.post(f"/api/v1/conversations/{conversation}/runs", json=body.model_dump())
        assert response.status_code == 202 and response.json()["run_id"] == row["run_id"]
        assert wait_run(client, row["run_id"])["status"] == "completed"
        assert model.calls == 1


def test_idempotent_retry_after_commit_error_starts_exactly_one_worker(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        connection = client.app.state.run_service.repository.connection
        original = connection.commit

        async def successful_commit_reported_as_error():
            await original()
            raise sqlite3.OperationalError("private-commit-detail")

        monkeypatch.setattr(connection, "commit", successful_commit_reported_as_error)
        body = {"message": "你好", "idempotency_key": "same"}
        response = client.post(f"/api/v1/conversations/{conversation}/runs", json=body)
        assert response.status_code == 503
        assert client.app.state.run_service.tasks == {}
        assert not connection.in_transaction
        monkeypatch.setattr(connection, "commit", original)
        retry = client.post(f"/api/v1/conversations/{conversation}/runs", json=body)
        assert retry.status_code == 202
        run = retry.json()["run_id"]
        assert wait_run(client, run)["status"] == "completed" and model.calls == 1
        assert (
            client.post(f"/api/v1/conversations/{conversation}/runs", json=body).json()["run_id"]
            == run
        )


def test_retry_queued_resumption_uses_checkpoint_without_adding_user_message():
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        client.portal.call(lambda: setattr(model, "gate", asyncio.Event()))
        conversation, run = create_run(client)
        wait_for_token(client, model)
        client.post(f"/api/v1/runs/{run}/pause")
        client.portal.call(model.gate.set)
        wait_run(client, run, ("paused",))
        service = client.app.state.run_service

        async def persist_resume_only():
            task = service.tasks.get(run)
            if task:
                await task
            return await service.repository.resume(run)

        assert client.portal.call(persist_resume_only)["status"] == "queued"
        response = client.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"message": "你好", "idempotency_key": "first"},
        )
        assert response.status_code == 202
        assert wait_run(client, run)["status"] == "completed" and model.calls == 1
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        assert len(history["messages"]) == 2


@pytest.mark.parametrize(
    "operation",
    ["set_running", "pause", "reserve_call", "add_elapsed", "event", "resume", "finish"],
)
def test_write_failure_rolls_back_and_does_not_poison_other_conversations(monkeypatch, operation):
    with TestClient(create_app(settings(), chat_model=StreamingModel())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        repository = client.app.state.run_service.repository
        row, _ = client.portal.call(
            repository.create,
            conversation,
            RunRequest(message="你好", idempotency_key="a"),
            str(uuid4()),
        )
        run = row["run_id"]
        if operation == "resume":
            client.portal.call(repository.paused, run)
        before = client.portal.call(repository.get, run)
        original = repository.connection.commit

        async def fail_commit():
            raise sqlite3.OperationalError("private-database-detail")

        async def mutate():
            if operation == "reserve_call":
                await repository.reserve_call(run, "model", 6)
            elif operation == "add_elapsed":
                await repository.add_elapsed(run, 1)
            elif operation == "event":
                await repository.event(run, "test", {})
            elif operation == "finish":
                await repository.finish(run, "completed", answer="回答", checkpoint_id="pending")
            else:
                await getattr(repository, operation)(run)

        monkeypatch.setattr(repository.connection, "commit", fail_commit)
        with pytest.raises(sqlite3.OperationalError):
            client.portal.call(mutate)
        assert not repository.connection.in_transaction
        monkeypatch.setattr(repository.connection, "commit", original)
        assert client.portal.call(repository.get, run) == before
        assert client.post("/api/v1/conversations").status_code == 201


def test_preparation_is_cancelled_at_total_deadline_without_calling_model(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(agent_timeout_seconds=0.08), chat_model=model)) as client:
        stopped = []

        async def blocked_history(_):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        monkeypatch.setattr(client.app.state.runtime, "committed_messages", blocked_history)
        _, run = create_run(client)
        result = wait_run(client, run)
        assert result["status"] == "failed" and result["error"]["code"] == "agent_timeout"
        assert result["model_calls"] == 0 and model.calls == 0 and stopped == [True]
        assert result["elapsed_seconds"] >= 0.08


@pytest.mark.parametrize("arguments", [{"a": 12}, {"a": "private-invalid-number", "b": 7}])
def test_invalid_tool_arguments_are_not_resumable(monkeypatch, arguments):
    model = ScriptModel(
        outputs=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "multiply_numbers", "args": arguments, "id": "call"},
                ],
            )
        ]
    )
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client)
        result = wait_run(client, run)
        assert result["error"]["code"] == "tool_error" and not result["error"]["retryable"]
        assert client.post(f"/api/v1/runs/{run}/resume").status_code == 409
        assert client.get(f"/api/v1/runs/{run}").json()["tool_calls"] == 1
        assert len(model.seen) == 1
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []
        assert "private-invalid-number" not in client.get(f"/api/v1/runs/{run}/events").text


@pytest.mark.parametrize("bad_id", ["duplicate", "", None])
def test_tool_call_ids_are_validated_before_execution(bad_id):
    calls = [
        {"name": "multiply_numbers", "args": {"a": 2, "b": 3}, "id": bad_id},
    ]
    if bad_id == "duplicate":
        calls.append({"name": "multiply_numbers", "args": {"a": 4, "b": 5}, "id": bad_id})
    model = ScriptModel(
        outputs=[AIMessage(content="", tool_calls=calls), AIMessage(content="伪造答案")]
    )
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation, run = create_run(client)
        result = wait_run(client, run)
        assert result["status"] == "failed" and result["error"]["code"] == "tool_error"
        assert result["tool_calls"] == 0 and not result["error"]["retryable"]
        assert len(model.seen) == 1
        assert client.get(f"/api/v1/conversations/{conversation}").json()["messages"] == []


@pytest.mark.parametrize("failure", ["events", "inspect"])
def test_subscription_error_is_clean_and_reconnect_preserves_cursor(monkeypatch, failure):
    with TestClient(create_app(settings(), chat_model=StreamingModel())) as client:
        _, run = create_run(client)
        wait_run(client, run)
        service = client.app.state.run_service
        repository = service.repository
        original_events, original_inspect = repository.events, service.inspect
        all_events = events(client.get(f"/api/v1/runs/{run}/events"))
        reads, inspections = 0, 0

        async def one_event_at_a_time(*args, **kwargs):
            nonlocal reads
            reads += 1
            if failure == "events" and reads == 2:
                raise sqlite3.OperationalError("private-read-detail")
            return (await original_events(*args, **kwargs))[:1]

        async def failing_inspect(*args, **kwargs):
            nonlocal inspections
            inspections += 1
            if failure == "inspect" and inspections == 2:
                raise sqlite3.OperationalError("private-inspect-detail")
            return await original_inspect(*args, **kwargs)

        monkeypatch.setattr(repository, "events", one_event_at_a_time)
        monkeypatch.setattr(service, "inspect", failing_inspect)
        response = client.get(f"/api/v1/runs/{run}/events")
        assert response.status_code == 200
        assert events(response) == all_events[:1]
        transport_frame = response.text.strip().split("\n\n")[-1]
        assert transport_frame.startswith("event: stream_error\ndata: ")
        error = json.loads(transport_frame.split("\ndata: ", 1)[1])
        assert error["code"] == "storage_unavailable" and error["retryable"]
        assert error["last_event_id"] == f"{run}:1" and "id: " not in transport_frame
        assert (
            "private-read-detail" not in response.text
            and "private-inspect-detail" not in response.text
        )
        monkeypatch.setattr(repository, "events", original_events)
        monkeypatch.setattr(service, "inspect", original_inspect)
        replay = client.get(
            f"/api/v1/runs/{run}/events", headers={"Last-Event-ID": error["last_event_id"]}
        )
        assert events(replay) == all_events[1:]
        assert client.get(f"/api/v1/runs/{run}").json()["status"] == "completed"


def test_cancelled_creation_still_registers_persisted_task(monkeypatch):
    model = StreamingModel()
    with TestClient(create_app(settings(), chat_model=model)) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        service = client.app.state.run_service
        original = service.repository.create

        async def scenario():
            committed, release = asyncio.Event(), asyncio.Event()
            saved = []

            async def delayed_return(*args, **kwargs):
                result = await original(*args, **kwargs)
                saved.append(result[0]["run_id"])
                committed.set()
                await release.wait()
                return result

            monkeypatch.setattr(service.repository, "create", delayed_return)
            request = asyncio.create_task(
                service.create(
                    conversation,
                    RunRequest(message="你好", idempotency_key="cancel"),
                    str(uuid4()),
                )
            )
            await committed.wait()
            request.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            return saved[0]

        run = client.portal.call(scenario)
        assert wait_run(client, run)["status"] == "completed" and model.calls == 1
