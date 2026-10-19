"""Live test: cloud tool calling, then a follow-up after reopening the PostgreSQL database."""

import argparse
import json
import time

from fastapi.testclient import TestClient

from app.main import create_app

TERMINAL = {"completed", "failed", "cancelled", "paused"}


def create_run(client, conversation_id, message, key):
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"message": message, "idempotency_key": key},
    )
    response.raise_for_status()
    return response.json()["run_id"]


def wait_run(client, run_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        row = response.json()
        if row["status"] in TERMINAL:
            return row
        time.sleep(0.1)
    raise SystemExit("run did not reach a terminal state in time")


def parse_events(text):
    events = []
    for frame in text.split("\n\n"):
        if not frame.startswith("id: "):
            continue
        event_id, event, data = frame.split("\n", 2)
        events.append((event_id[4:], event[7:], json.loads(data[6:])))
    return events


def ask(client, conversation_id, message, key, stream):
    run_id = create_run(client, conversation_id, message, key)
    if not stream:
        return wait_run(client, run_id), None
    response = client.get(f"/api/v1/runs/{run_id}/events")
    response.raise_for_status()
    return wait_run(client, run_id), [name for _, name, _ in parse_events(response.text)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true", help="Subscribe to the SSE event stream")
    stream = parser.parse_args().stream

    with TestClient(create_app()) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        result, events = ask(
            client,
            conversation,
            "请记住我的测试代号是蓝鲸42。请调用 multiply_numbers 计算12乘7，告诉我结果。",
            "smoke-1",
            stream,
        )
        print(
            json.dumps(
                {"step": "tool_call", "result": result, "events": events}, ensure_ascii=False
            )
        )
        if (
            result["status"] != "completed"
            or "multiply_numbers" not in result["tools_used"]
            or "84" not in (result["answer"] or "")
        ):
            return 1

    with TestClient(create_app()) as client:
        result, events = ask(
            client, conversation, "我刚才告诉你的测试代号是什么？", "smoke-2", stream
        )
        print(
            json.dumps(
                {"step": "persistent_memory", "result": result, "events": events},
                ensure_ascii=False,
            )
        )
        if result["status"] != "completed" or "蓝鲸42" not in (result["answer"] or ""):
            return 1
        history = client.get(f"/api/v1/conversations/{conversation}").json()
        print(json.dumps({"history_messages": len(history["messages"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
