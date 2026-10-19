import asyncio
import io
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile
from support import ScriptModel, rag_answer, settings, wait_run
from test_knowledge_bases import client as client
from test_knowledge_bases import create, index, upload
from test_rag_pipeline import Embeddings

from app.main import create_app
from app.schemas import RunRequest

BASE = "/api/v1/knowledge-bases"


def rows(client, statement, params=()):
    async def query():
        db = client.app.state.runtime.repository.database
        async with db.transaction():
            cursor = await db.connection.execute(statement, params)
            return await cursor.fetchall() if cursor.description else []

    return client.portal.call(query)


def settle_worker(client, run_id):
    async def wait():
        task = client.app.state.run_service.tasks.get(run_id)
        if task:
            await task

    client.portal.call(wait)


def ask(client, cid, key="one", **scope):
    return client.post(
        f"/api/v1/conversations/{cid}/runs",
        json={
            "message": "年假多少天？",
            "idempotency_key": key,
            "knowledge_base_id": "default",
            "retrieval_mode": "keyword",
            **scope,
        },
    )


def test_download_and_file_delete_cascade_preserve_history(client):
    record = upload(client, "default")
    index(client, "default")
    cid = client.post("/api/v1/conversations").json()["thread_id"]
    run_id = ask(client, cid).json()["run_id"]
    answer = wait_run(client, run_id, ("completed",))
    settle_worker(client, run_id)
    path = f"{BASE}/default/files/{record['file_id']}"
    download = client.get(path + "/download")
    assert download.status_code == 200
    assert download.content == "员工年假十天。".encode()
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    other = create(client, "其他库")
    assert client.get(f"{BASE}/{other}/files/{record['file_id']}/download").status_code == 404
    assert client.delete(f"{BASE}/{other}/files/{record['file_id']}").status_code == 404
    assert client.delete(path).json()["cleanup_pending"] == 0
    assert client.get(path + "/download").status_code == 404
    assert client.delete(path).status_code == 404
    assert not client.app.state.knowledge_base_service.storage._path(
        record["storage_path"]
    ).exists()
    for table in ["files", "chunks", "chunk_vectors", "lexical_indexes", "chunk_terms"]:
        assert rows(client, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0
    assert client.get(f"/api/v1/runs/{run_id}").json()["sources"] == answer["sources"]
    assert (
        client.get(f"/api/v1/conversations/{cid}/turns").json()["items"][0]["sources"]
        == answer["sources"]
    )


def test_file_cleanup_failure_retries_on_restart(monkeypatch):
    config = settings()
    with TestClient(create_app(config)) as client:
        record = upload(client, "default")
        storage = client.app.state.knowledge_base_service.storage
        original = storage.delete

        async def broken(key):
            raise OSError("unavailable")

        monkeypatch.setattr(storage, "delete", broken)
        response = client.delete(f"{BASE}/default/files/{record['file_id']}")
        assert response.status_code == 200
        assert response.json()["cleanup_pending"] == 1
        assert rows(client, "SELECT COUNT(*) AS n FROM file_cleanup_queue")[0]["n"] == 1
        assert storage._path(record["storage_path"]).exists()
        monkeypatch.setattr(storage, "delete", original)
    with TestClient(create_app(config)) as restarted:
        assert not storage._path(record["storage_path"]).exists()
        assert rows(restarted, "SELECT COUNT(*) AS n FROM file_cleanup_queue")[0]["n"] == 0


@pytest.mark.parametrize("status", ["queued", "running", "paused", "recovering"])
def test_file_and_conversation_delete_reject_unfinished_run(client, status):
    record = upload(client, "default")
    cid = client.post("/api/v1/conversations").json()["thread_id"]
    body = RunRequest(message="年假", idempotency_key="busy", knowledge_base_id="default")
    row, _ = client.portal.call(
        client.app.state.run_service.repository.create, cid, body, str(uuid4())
    )
    rows(client, "UPDATE runs SET status=%s WHERE run_id=%s", (status, row["run_id"]))
    assert client.delete(f"{BASE}/default/files/{record['file_id']}").status_code == 409
    assert client.delete(f"/api/v1/conversations/{cid}").status_code == 409
    assert client.get(f"{BASE}/default/files/{record['file_id']}/download").status_code == 200


def test_file_delete_rejects_index_in_progress(client):
    record = upload(client, "default")

    async def verify():
        service = client.app.state.index_service
        task = asyncio.create_task(asyncio.Event().wait())
        service.tasks["default"] = task
        try:
            with pytest.raises(Exception) as failure:
                await client.app.state.knowledge_base_service.delete_file(
                    "default", record["file_id"]
                )
            assert failure.value.code == "knowledge_base_busy"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            service.tasks.pop("default")

    client.portal.call(verify)


def test_gc_only_removes_old_managed_unreferenced_objects(client, tmp_path):
    record = upload(client, "default")
    storage = client.app.state.knowledge_base_service.storage
    referenced = storage._path(record["storage_path"])
    os.utime(referenced, (1, 1))
    old = storage._path(f"default/{uuid4()}.txt")
    part = storage._path(f"default/.{uuid4()}.txt.{uuid4().hex}.part")
    recent = storage._path(f"default/{uuid4()}.txt")
    unknown = storage._path("default/manual-notes.txt")
    for path in [old, part, recent, unknown]:
        path.write_bytes(b"keep or remove")
    for path in [old, part, unknown]:
        os.utime(path, (1, 1))
    external = tmp_path / "external.txt"
    external.write_text("outside")
    link = storage._path(f"default/{uuid4()}.txt")
    link.symlink_to(external)
    result = client.post("/api/v1/maintenance/files/gc")
    assert result.status_code == 200
    assert result.json() == {"queued": 2, "cleanup_pending": 0, "min_age_seconds": 86400}
    assert not old.exists() and not part.exists()
    assert all(p.exists() for p in [referenced, recent, unknown, external, link])
    assert client.post("/api/v1/maintenance/files/gc").json()["queued"] == 0


def test_gc_does_not_race_uncommitted_upload(client, monkeypatch):
    async def verify():
        service = client.app.state.file_service
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.storage.put

        async def delayed(key, chunks):
            result = await original(key, chunks)
            os.utime(service.storage._path(key), (1, 1))
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(service.storage, "put", delayed)
        uploading = asyncio.create_task(
            service.upload(
                "default", UploadFile(io.BytesIO(b"upload"), filename="upload.txt"), "race"
            )
        )
        await entered.wait()
        collecting = asyncio.create_task(client.app.state.knowledge_base_service.collect_orphans())
        await asyncio.sleep(0.02)
        assert not collecting.done()
        release.set()
        record, result = await asyncio.gather(uploading, collecting)
        assert result.queued == 0
        assert await service.storage.exists(record.storage_path)

    client.portal.call(verify)


def test_conversation_rename_delete_and_checkpoint_cleanup(client):
    cid = client.post("/api/v1/conversations").json()["thread_id"]
    result = client.patch(f"/api/v1/conversations/{cid}", json={"title": "  年假咨询  "})
    assert result.status_code == 200
    assert result.json()["title"] == "年假咨询"
    assert client.get("/api/v1/conversations").json()[0]["title"] == "年假咨询"
    for title in ["", " ", "长" * 101, "坏\x00名称"]:
        assert (
            client.patch(f"/api/v1/conversations/{cid}", json={"title": title}).status_code == 422
        )
    assert (
        client.patch(f"/api/v1/conversations/{uuid4()}", json={"title": "missing"}).status_code
        == 404
    )
    response = client.post(
        f"/api/v1/conversations/{cid}/runs", json={"message": "hi", "idempotency_key": "chat"}
    )
    run_id = response.json()["run_id"]
    wait_run(client, run_id)
    settle_worker(client, run_id)
    assert (
        rows(client, "SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id=%s", (run_id,))[0]["n"]
        > 0
    )
    result = client.delete(f"/api/v1/conversations/{cid}")
    assert result.status_code == 200 and result.json()["cleanup_pending"] == 0
    for path in [
        f"/api/v1/conversations/{cid}",
        f"/api/v1/conversations/{cid}/turns",
        f"/api/v1/runs/{run_id}",
    ]:
        assert client.get(path).status_code == 404
    assert (
        rows(client, "SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id=%s", (run_id,))[0]["n"]
        == 0
    )
    assert (
        rows(client, "SELECT COUNT(*) AS n FROM run_events WHERE run_id=%s", (run_id,))[0]["n"] == 0
    )
    assert client.delete(f"/api/v1/conversations/{cid}").status_code == 404


def test_checkpoint_cleanup_failure_survives_restart(monkeypatch):
    config = settings()
    with TestClient(
        create_app(config, chat_model=ScriptModel(outputs=[rag_answer("hello")]))
    ) as client:
        cid = client.post("/api/v1/conversations").json()["thread_id"]
        client.patch(f"/api/v1/conversations/{cid}", json={"title": "持久会话"})
        run_id = client.post(
            f"/api/v1/conversations/{cid}/runs", json={"message": "hello", "idempotency_key": "x"}
        ).json()["run_id"]
        wait_run(client, run_id)
        settle_worker(client, run_id)

        async def broken(_):
            raise OSError("checkpoint unavailable")

        monkeypatch.setattr(client.app.state.runtime.checkpointer, "adelete_thread", broken)
        assert client.delete(f"/api/v1/conversations/{cid}").json()["cleanup_pending"] == 1
        assert rows(client, "SELECT COUNT(*) AS n FROM checkpoint_cleanup_queue")[0]["n"] == 1
    with TestClient(create_app(config)) as restarted:
        assert rows(restarted, "SELECT COUNT(*) AS n FROM checkpoint_cleanup_queue")[0]["n"] == 0
        assert rows(restarted, "SELECT COUNT(*) AS n FROM checkpoints")[0]["n"] == 0
        assert restarted.get("/api/v1/conversations").json() == []


def test_question_file_scope_is_durable_and_idempotent(client):
    first = upload(client, "default", "first")
    second = upload(client, "default", "second")
    index(client, "default")
    cid = client.post("/api/v1/conversations").json()["thread_id"]
    response = ask(client, cid, file_ids=[second["file_id"], second["file_id"]])
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    result = wait_run(client, run_id, ("completed",))
    settle_worker(client, run_id)
    assert result["file_ids"] == [second["file_id"]]
    assert {s["file_id"] for s in result["sources"]} == {second["file_id"]}
    # Same request with deduplicated scope attaches to original run; changed scope conflicts.
    assert ask(client, cid, file_ids=[second["file_id"]]).json()["run_id"] == run_id
    assert ask(client, cid, file_ids=[first["file_id"]]).status_code == 409
    assert ask(client, cid).status_code == 409
    history = client.get(f"/api/v1/conversations/{cid}/turns").json()["items"][0]
    assert history["file_ids"] == [second["file_id"]]

    async def checkpoint():
        return await client.app.state.run_service.get_graph().aget_state(
            {"configurable": {"thread_id": run_id}}
        )

    assert client.portal.call(checkpoint).values["file_ids"] == [second["file_id"]]
    empty = ask(client, cid, "empty", file_ids=[])
    empty_run = wait_run(client, empty.json()["run_id"], ("completed",))
    assert empty_run["answer_reason"] == "no_retrieval_hits"
    assert empty_run["model_calls"] == 0 and empty_run["sources"] == []
    other = create(client, "别的库")
    foreign = upload(client, other)
    for ids in [[foreign["file_id"]], ["missing"], [""], ["x"] * 101]:
        assert ask(client, cid, "bad", file_ids=ids).status_code == 422
    assert (
        client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={
                "message": "hello",
                "idempotency_key": "plain",
                "file_ids": [],
            },
        ).status_code
        == 422
    )


def test_scoped_run_resume_after_restart_keeps_file_filter():
    from app.errors import ChatError

    config = settings()
    with TestClient(
        create_app(
            config,
            chat_model=ScriptModel(outputs=[rag_answer("十天")]),
            embedding_model=Embeddings(),
        )
    ) as client:
        first = upload(client, "default", "first")
        second = upload(client, "default", "second")
        index(client, "default")
        cid = client.post("/api/v1/conversations").json()["thread_id"]
        client.patch(f"/api/v1/conversations/{cid}", json={"title": "恢复范围"})

        async def unavailable(*args, **kwargs):
            raise ChatError("retrieval_unavailable", "临时不可用", 503, True)

        client.app.state.retrieval_service.search = unavailable
        response = ask(client, cid, file_ids=[second["file_id"]])
        run_id = response.json()["run_id"]
        assert wait_run(client, run_id, ("failed",))["error"]["retryable"]
        settle_worker(client, run_id)
    with TestClient(
        create_app(
            config,
            chat_model=ScriptModel(outputs=[rag_answer("十天")]),
            embedding_model=Embeddings(),
        )
    ) as restarted:
        assert restarted.get("/api/v1/conversations").json()[0]["title"] == "恢复范围"
        assert restarted.post(f"/api/v1/runs/{run_id}/resume").status_code == 202
        result = wait_run(restarted, run_id, ("completed",))
        assert result["file_ids"] == [second["file_id"]]
        assert {s["file_id"] for s in result["sources"]} == {second["file_id"]}
        assert first["file_id"] not in {s["file_id"] for s in result["sources"]}


def test_startup_keeps_failed_checkpoint_cleanup_queued(monkeypatch):
    config = settings()
    with TestClient(
        create_app(config, chat_model=ScriptModel(outputs=[rag_answer("ok")]))
    ) as client:
        cid = client.post("/api/v1/conversations").json()["thread_id"]
        run_id = client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={
                "message": "hello",
                "idempotency_key": "x",
            },
        ).json()["run_id"]
        wait_run(client, run_id)
        settle_worker(client, run_id)
        checkpointer_type = type(client.app.state.runtime.checkpointer)

        async def broken(*args):
            raise OSError("unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(checkpointer_type, "adelete_thread", broken)
            assert client.delete(f"/api/v1/conversations/{cid}").json()["cleanup_pending"] == 1
    with monkeypatch.context() as patch:
        patch.setattr(checkpointer_type, "adelete_thread", broken)
        with TestClient(create_app(config)) as restarted:
            assert restarted.get("/api/v1/health").status_code == 200
            assert (
                rows(restarted, "SELECT COUNT(*) AS n FROM checkpoint_cleanup_queue")[0]["n"] == 1
            )
    with TestClient(create_app(config)) as recovered:
        assert rows(recovered, "SELECT COUNT(*) AS n FROM checkpoints")[0]["n"] == 0
        assert rows(recovered, "SELECT COUNT(*) AS n FROM checkpoint_cleanup_queue")[0]["n"] == 0
