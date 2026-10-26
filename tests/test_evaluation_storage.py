"""逐题结果存储：一题一行、只写变化的题、删除级联。"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import ScriptModel, rag_answer, settings
from test_knowledge_bases import index, upload

from app.main import create_app

BASE = "/api/v1/evaluations"


@pytest.fixture
def client():
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[rag_answer("十天")]))
    ) as client:
        yield client


def run_body(file_id, **kwargs):
    return {
        "idempotency_key": str(uuid4()),
        "knowledge_base_id": "default",
        "mode": "keyword",
        "k": 3,
        "cases": [
            {"query": "年假多少天", "expected": "answerable", "relevant_file_ids": [file_id]},
            {"query": "年假多少天", "expected": "answerable", "relevant_file_ids": [file_id]},
        ],
        **kwargs,
    }


def finish(client, response):
    assert response.status_code == 202, response.text
    evaluation_id = response.json()["evaluation_id"]

    async def wait():
        task = client.app.state.evaluations.tasks.get(evaluation_id)
        if task:
            await task

    client.portal.call(wait)
    return client.get(BASE + "/" + evaluation_id).json()


def test_results_are_read_from_the_child_table(client):
    file = upload(client, "default")
    index(client, "default")
    report = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    evaluation_id = report["evaluation_id"]
    service = client.app.state.evaluations

    async def stored_rows():
        async with service.database.read():
            cursor = await service.database.connection.execute(
                "SELECT case_index,payload FROM evaluation_results "
                "WHERE evaluation_id=%s ORDER BY case_index",
                (evaluation_id,),
            )
            return await cursor.fetchall()

    rows = client.portal.call(stored_rows)
    assert [row["case_index"] for row in rows] == [0, 1]
    assert all(row["payload"]["status"] == "completed" for row in rows)

    async def patch_second():
        await service.patch_result(evaluation_id, 1, {"storage_probe": "child-only"})

    client.portal.call(patch_second)
    updated = client.get(BASE + "/" + evaluation_id).json()
    assert updated["results"][1]["storage_probe"] == "child-only"
    assert "storage_probe" not in updated["results"][0]


def test_review_updates_only_one_result_row(client):
    file = upload(client, "default")
    index(client, "default")
    report = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    evaluation_id = report["evaluation_id"]
    service = client.app.state.evaluations

    async def row_versions():
        async with service.database.read():
            cursor = await service.database.connection.execute(
                "SELECT case_index,xmin::text AS version FROM evaluation_results "
                "WHERE evaluation_id=%s ORDER BY case_index",
                (evaluation_id,),
            )
            return {row["case_index"]: row["version"] for row in await cursor.fetchall()}

    before = client.portal.call(row_versions)
    response = client.patch(
        BASE + "/" + evaluation_id + "/reviews/0",
        json={"verdict": "pass", "notes": "只改这一题"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][1].get("review", {}).get("notes", "") == ""
    after = client.portal.call(row_versions)
    assert after[0] != before[0]
    assert after[1] == before[1]


def test_deleting_evaluation_cascades_result_rows(client):
    file = upload(client, "default")
    index(client, "default")
    report = finish(client, client.post(BASE, json=run_body(file["file_id"])))
    evaluation_id = report["evaluation_id"]
    service = client.app.state.evaluations

    assert client.delete(BASE + "/" + evaluation_id).status_code == 200

    async def child_count():
        async with service.database.read():
            cursor = await service.database.connection.execute(
                "SELECT COUNT(*) AS n FROM evaluation_results WHERE evaluation_id=%s",
                (evaluation_id,),
            )
            return (await cursor.fetchone())["n"]

    assert client.portal.call(child_count) == 0
