"""Explicit live smoke test: one request through FastAPI to the configured cloud model."""

import json

from fastapi.testclient import TestClient

from app.main import create_app


def main():
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/chat", json={"message": "你好，请只回复：连接成功"})
    print(
        json.dumps(
            {"http_status": response.status_code, "result": response.json()}, ensure_ascii=False
        )
    )
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
