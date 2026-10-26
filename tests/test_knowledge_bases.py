import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from starlette.datastructures import UploadFile
from support import ScriptModel, rag_answer, settings, test_database, wait_run
from test_rag_pipeline import Embeddings

from app.errors import ChatError
from app.main import create_app

BASE = "/api/v1/knowledge-bases"


@pytest.fixture
def client():
    with TestClient(
        create_app(
            settings(),
            chat_model=ScriptModel(outputs=[rag_answer(content="十天")]),
            embedding_model=Embeddings(),
        )
    ) as result:
        yield result


def create(client, name):
    response = client.post(BASE, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["kb_id"]


def upload(client, kb_id, key="same"):
    response = client.post(
        f"{BASE}/{kb_id}/files",
        files={"file": ("制度.txt", "员工年假十天。".encode())},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def index(client, kb_id):
    assert client.post(f"{BASE}/{kb_id}/index").status_code == 202

    async def wait():
        task = client.app.state.index_service.tasks.get(kb_id)
        if task:
            await task

    client.portal.call(wait)


def test_migration_preserves_default_and_create_validation(client):
    assert client.get(BASE).json()[0]["kb_id"] == "default"
    record = upload(client, "default")
    create(client, " 产品资料 ")
    assert client.get(BASE).json()[0]["file_count"] == 1
    assert client.get(f"{BASE}/default/files").json()["items"][0]["file_id"] == record["file_id"]
    assert client.post(BASE, json={"name": "产品资料"}).status_code == 409
    for name in ["", "   ", "长" * 101, "坏\x00名"]:
        assert client.post(BASE, json={"name": name}).status_code == 422


def test_archived_knowledge_base_is_hidden_and_read_only(client):
    kb_id = create(client, "归档测试库")
    upload(client, kb_id)
    archived = client.patch(f"{BASE}/{kb_id}/archive", json={"archived": True})
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert all(item["kb_id"] != kb_id for item in client.get(BASE).json())
    included = client.get(BASE + "?include_archived=true").json()
    assert any(item["kb_id"] == kb_id and item["archived"] for item in included)

    rejected = client.post(
        f"{BASE}/{kb_id}/files",
        files={"file": ("制度.txt", "员工年假十天。".encode())},
        headers={"Idempotency-Key": "archived-upload"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "knowledge_base_archived"
    search = client.post(
        f"{BASE}/{kb_id}/search", json={"query": "年假", "mode": "keyword"}
    )
    assert search.status_code == 409

    restored = client.patch(f"{BASE}/{kb_id}/archive", json={"archived": False})
    assert restored.status_code == 200 and restored.json()["archived"] is False
    assert any(item["kb_id"] == kb_id for item in client.get(BASE).json())


def test_file_search_and_run_scope_are_isolated(client):
    first, second = create(client, "人事制度"), create(client, "项目资料")
    a, b = upload(client, first), upload(client, second)
    assert a["file_id"] != b["file_id"]
    index(client, first)
    index(client, second)
    for kb_id, own, other in [(first, a, b), (second, b, a)]:
        assert client.get(f"{BASE}/{kb_id}/files").json()["items"][0]["file_id"] == own["file_id"]
        for mode in ["keyword", "vector", "hybrid"]:
            response = client.post(f"{BASE}/{kb_id}/search", json={"query": "年假", "mode": mode})
            assert response.status_code == 200, response.text
            assert {s["file_id"] for s in response.json()["sources"]} == {own["file_id"]}
            assert (
                client.post(
                    f"{BASE}/{kb_id}/search",
                    json={"query": "年假", "mode": mode, "file_ids": [other["file_id"]]},
                ).json()["sources"]
                == []
            )
    conversation = client.post("/api/v1/conversations").json()["thread_id"]
    response = client.post(
        f"/api/v1/conversations/{conversation}/runs",
        json={
            "message": "多少天年假？",
            "idempotency_key": "run",
            "knowledge_base_id": second,
            "retrieval_mode": "keyword",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["knowledge_base_id"] == second
    run = wait_run(client, response.json()["run_id"], ("completed",))
    assert run["sources"][0]["file_id"] == b["file_id"]
    assert client.delete(f"{BASE}/{second}").status_code == 200
    history = client.get(f"/api/v1/conversations/{conversation}/turns").json()["items"]
    assert history[0]["answer"] == run["answer"]
    assert history[0]["sources"] == run["sources"]
    assert client.get(f"{BASE}/{first}/files").json()["total"] == 1


def test_delete_is_blocked_while_an_evaluation_is_running(client):
    kb_id = create(client, "评测中的知识库")
    record = upload(client, kb_id)
    evaluation_id = str(uuid4())
    now = "2026-01-01T00:00:00+00:00"
    with test_database() as db:
        db.execute(
            "INSERT INTO evaluations(evaluation_id,idempotency_key,created_at,updated_at,"
            "status,request,snapshot) VALUES (%s,%s,%s,%s,'running',%s,%s)",
            (
                evaluation_id,
                str(uuid4()),
                now,
                now,
                Jsonb({"knowledge_base_id": kb_id}),
                Jsonb({}),
            ),
        )
    response = client.delete(f"{BASE}/{kb_id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "knowledge_base_busy"
    assert client.delete(f"{BASE}/{kb_id}/files/{record['file_id']}").status_code == 409
    assert any(item["kb_id"] == kb_id for item in client.get(BASE).json())
    with test_database() as db:
        db.execute(
            "UPDATE evaluations SET status='completed' WHERE evaluation_id=%s",
            (evaluation_id,),
        )
    assert client.delete(f"{BASE}/{kb_id}").status_code == 200
    assert not any(item["kb_id"] == kb_id for item in client.get(BASE).json())


def test_delete_cascades_and_can_delete_last_library(client):
    kb_id = create(client, "待删除")
    record = upload(client, kb_id)
    index(client, kb_id)
    path = Path("data/files") / record["storage_path"]
    assert path.exists()
    assert client.delete(f"{BASE}/{kb_id}").json()["cleanup_pending"] == 0
    assert not path.exists()
    with test_database() as db:
        for table in ["files", "chunks", "chunk_vectors", "chunk_terms", "lexical_postings"]:
            assert db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()[0] == 0
    for suffix, method, kwargs in [
        ("/files", "get", {}),
        ("/index", "post", {}),
        ("/search", "post", {"json": {"query": "年假"}}),
    ]:
        assert getattr(client, method)(f"{BASE}/{kb_id}{suffix}", **kwargs).status_code == 404
    assert client.delete(f"{BASE}/default").status_code == 200
    assert client.get(BASE).json() == []
    create(client, "重新开始")


def test_busy_index_prevents_delete(client):
    kb_id = create(client, "工作中")

    async def hold():
        task = asyncio.create_task(asyncio.Event().wait())
        client.app.state.index_service.tasks[kb_id] = task
        return task

    task = client.portal.call(hold)
    assert client.delete(f"{BASE}/{kb_id}").status_code == 409
    client.portal.call(task.cancel)


def test_busy_run_prevents_delete_and_missing_kb_rejected(client):
    kb_id = create(client, "暂停中")
    conversation = client.post("/api/v1/conversations").json()["thread_id"]
    with test_database() as db:
        db.execute(
            "INSERT INTO runs (run_id,conversation_id,request_id,idempotency_key,message,"
            "graph_version,status,created_at,updated_at,knowledge_base_id) "
            "VALUES ('held',%s,'request','key','hi','chat-rag-v4','paused','now','now',%s)",
            (conversation, kb_id),
        )
    assert client.delete(f"{BASE}/{kb_id}").status_code == 409
    response = client.post(
        f"/api/v1/conversations/{conversation}/runs",
        json={
            "message": "hi",
            "idempotency_key": "new",
            "knowledge_base_id": "missing",
            "retrieval_mode": "keyword",
        },
    )
    assert response.status_code == 404


def test_failed_physical_cleanup_is_persisted_and_retried(client, monkeypatch):
    kb_id = create(client, "清理重试")
    record = upload(client, kb_id)
    service = client.app.state.knowledge_base_service
    original = service.storage.delete

    async def fail(path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(service.storage, "delete", fail)
    assert client.delete(f"{BASE}/{kb_id}").json()["cleanup_pending"] == 1
    assert all(k["kb_id"] != kb_id for k in client.get(BASE).json())
    monkeypatch.setattr(service.storage, "delete", original)
    assert client.portal.call(service.cleanup) == 0
    assert not (Path("data/files") / record["storage_path"]).exists()


def test_upload_in_flight_cannot_recreate_deleted_knowledge_base(client, monkeypatch):
    from io import BytesIO

    kb_id = create(client, "上传并发")
    service = client.app.state.file_service
    original = service.storage.put

    async def scenario():
        written, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            result = await original(*args)
            written.set()
            await release.wait()
            return result

        monkeypatch.setattr(service.storage, "put", delayed)
        task = asyncio.create_task(
            service.upload(kb_id, UploadFile(BytesIO(b"content"), filename="a.txt"), "key")
        )
        await asyncio.wait_for(written.wait(), 5)
        await client.app.state.knowledge_base_service.delete(kb_id)
        release.set()
        with pytest.raises(ChatError) as error:
            await task
        assert error.value.code == "knowledge_base_not_found"

    client.portal.call(scenario)
    assert client.get(f"{BASE}/{kb_id}/files").status_code == 404
    assert not list(Path("data/files").rglob("*.txt"))


def test_deletion_and_creation_survive_restart():
    with TestClient(create_app(settings())) as client:
        kb_id = create(client, "保留的库")
        record = upload(client, kb_id)
        assert client.delete(f"{BASE}/default").status_code == 200
    with TestClient(create_app(settings())) as client:
        assert [item["kb_id"] for item in client.get(BASE).json()] == [kb_id]
        assert (
            client.get(f"{BASE}/{kb_id}/files").json()["items"][0]["file_id"] == record["file_id"]
        )
