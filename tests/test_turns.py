from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import ScriptModel, create_run, settings, wait_run

from app.main import create_app


def test_turns_persist_sources_and_identify_committed_chain():
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[AIMessage(content="答案")]))
    ) as client:
        cid, first = create_run(client, "第一问")
        wait_run(client, first)

        # Sources are durable run data; the history view must read this association,
        # rather than matching identical answer text or relying on browser storage.
        async def attach_source():
            db = client.app.state.run_service.repository.database
            async with db.transaction():
                await db.connection.execute(
                    "UPDATE runs SET sources=%s WHERE run_id=%s",
                    ('[{"citation_id": 1, "name": "制度.txt"}]', first),
                )

        client.portal.call(attach_source)
        _, second = create_run(client, "第二问", conversation_id=cid, key="second")
        wait_run(client, second)
        response = client.get(f"/api/v1/conversations/{cid}/turns?limit=1")
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert response.json()["items"][0]["run_id"] == second
        assert response.json()["items"][0]["committed"] is True
        older = client.get(f"/api/v1/conversations/{cid}/turns?limit=1&offset=1").json()
        assert older["items"][0]["run_id"] == first
        assert older["items"][0]["message"] == "第一问"
        assert older["items"][0]["sources"][0]["name"] == "制度.txt"
        assert older["items"][0]["committed"] is True
        assert client.get(f"/api/v1/conversations/{cid}/turns?offset=20").json()["total"] == 2
        assert client.get(f"/api/v1/conversations/{cid}/turns?limit=101").status_code == 422
        assert client.get(f"/api/v1/conversations/{uuid4()}/turns").status_code == 404
        other = client.post("/api/v1/conversations").json()["thread_id"]
        assert client.get(f"/api/v1/conversations/{other}/turns").json()["items"] == []


def test_failed_attempt_is_not_committed_and_does_not_hide_success():
    model = ScriptModel(outputs=[AIMessage(content="成功"), ValueError("bad")])
    with TestClient(create_app(settings(), chat_model=model)) as client:
        cid, first = create_run(client)
        wait_run(client, first)
        _, failed = create_run(client, "失败问题", conversation_id=cid, key="bad")
        assert wait_run(client, failed)["status"] == "failed"
        items = client.get(f"/api/v1/conversations/{cid}/turns").json()["items"]
        assert [item["run_id"] for item in items] == [failed, first]
        assert [item["committed"] for item in items] == [False, True]
        assert items[0]["error"] and items[0]["answer"] is None
