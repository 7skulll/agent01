from uuid import uuid4

import pytest
from test_evaluations import BASE, finish, run_body
from test_evaluations import client as client
from test_knowledge_bases import index, upload

from app.errors import ChatError


def test_freezing_requires_current_mappable_evidence_but_drafts_can_keep_unmapped_labels(client):
    file = upload(client, "default")
    index(client, "default")
    anchor = {"file_id": file["file_id"], "content_hash": "a" * 64, "quote": "员工年假十天"}
    case = {
        "query": "年假几天",
        "expected": "answerable",
        "relevant_file_ids": [file["file_id"]],
        "evidence_groups": [{"fact": "天数", "alternatives": [anchor]}],
    }
    body = {
        "knowledge_base_id": "default",
        "name": "冻结原文校验",
        "cases": [case],
        "frozen": True,
        "review_status": "verified",
    }
    stale = client.post(BASE + "/case-sets", json=body)
    assert stale.status_code == 422 and "invalid_frozen_evidence" in stale.text
    anchor["content_hash"] = file["content_hash"]
    anchor["quote"] = "原文中不存在的证据"
    assert client.post(BASE + "/case-sets", json=body).status_code == 422
    assert (
        client.post(
            BASE + "/case-sets", json={**body, "frozen": False, "review_status": "draft"}
        ).status_code
        == 201
    )
    anchor["quote"] = "员工年假十天"
    frozen = client.post(BASE + "/case-sets", json=body)
    assert frozen.status_code == 201, frozen.text
    assert frozen.json()["frozen"] is True


def test_batch_delete_flushes_checkpoint_queue_once_and_does_not_sum_global_pending(
    client, monkeypatch
):
    report = finish(
        client,
        client.post(
            BASE, json=run_body(kind="rag", cases=[{"query": "无资料一"}, {"query": "无资料二"}])
        ),
    )
    calls = []

    async def pending(checkpointer):
        calls.append(checkpointer)
        return 3

    monkeypatch.setattr(client.app.state.runtime.repository, "cleanup_checkpoints", pending)
    response = client.delete(BASE + "/" + report["evaluation_id"])
    assert response.status_code == 200, response.text
    assert response.json()["cleanup_pending"] == 3
    assert len(calls) == 1


def test_retry_idempotency_survives_parent_deletion_and_rejects_different_confirmation(
    client, monkeypatch
):
    original = client.app.state.retrieval_service.search

    async def failure(*args, **kwargs):
        raise ChatError("temporary", "暂不可用", 503)

    monkeypatch.setattr(client.app.state.retrieval_service, "search", failure)
    parent = finish(client, client.post(BASE, json=run_body()))
    monkeypatch.setattr(client.app.state.retrieval_service, "search", original)
    payload = {"idempotency_key": str(uuid4())}
    url = BASE + "/" + parent["evaluation_id"] + "/retry"
    child = finish(client, client.post(url, json=payload))
    assert client.delete(BASE + "/" + parent["evaluation_id"]).status_code == 200
    repeated = client.post(url, json=payload)
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["evaluation_id"] == child["evaluation_id"]
    assert client.post(url, json={**payload, "confirm_unknown_calls": True}).status_code == 409


def test_failed_ownership_link_rolls_back_hidden_conversation_creation(client):
    completed = finish(client, client.post(BASE, json=run_body()))

    async def check():
        repository = client.app.state.runtime.repository
        async with repository.database.read():
            cur = await repository.connection.execute("SELECT COUNT(*) AS n FROM conversations")
            before = (await cur.fetchone())["n"]
        with pytest.raises(ChatError, match="评测不存在或已结束"):
            await repository.create(
                purpose="evaluation", owner_evaluation_id=completed["evaluation_id"]
            )
        async with repository.database.read():
            cur = await repository.connection.execute("SELECT COUNT(*) AS n FROM conversations")
            assert (await cur.fetchone())["n"] == before

    client.portal.call(check)
