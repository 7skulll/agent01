"""Live test: cloud tool calling, then a follow-up after reopening the SQLite database."""

import json

from fastapi.testclient import TestClient

from app.main import create_app


def main():
    with TestClient(create_app()) as client:
        first = client.post(
            "/api/v1/chat",
            json={
                "message": (
                    "请记住我的测试代号是蓝鲸42。请调用 multiply_numbers 计算12乘7，告诉我结果。"
                )
            },
        )
        print(
            json.dumps(
                {"step": "tool_call", "status": first.status_code, "result": first.json()},
                ensure_ascii=False,
            )
        )
        if first.status_code != 200:
            return 1
        result = first.json()
        if "multiply_numbers" not in result["tools_used"] or "84" not in result["answer"]:
            return 1
        thread_id = result["thread_id"]
    # New app/checkpointer instance proves that memory is restored from disk.
    with TestClient(create_app()) as client:
        second = client.post(
            "/api/v1/chat",
            json={"thread_id": thread_id, "message": "我刚才告诉你的测试代号是什么？"},
        )
        print(
            json.dumps(
                {
                    "step": "persistent_memory",
                    "status": second.status_code,
                    "result": second.json(),
                },
                ensure_ascii=False,
            )
        )
        history = client.get(f"/api/v1/conversations/{thread_id}")
        if second.status_code != 200 or "蓝鲸42" not in second.json()["answer"]:
            return 1
        print(json.dumps({"history_messages": len(history.json()["messages"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
