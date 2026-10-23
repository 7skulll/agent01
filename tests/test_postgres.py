"""PostgreSQL 专项：精确向量、原 BM25 分数、事件并发和单 worker 所有权。"""

import asyncio
import json
import math
import random
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import settings, test_database
from test_rag_pipeline import BASE, Embeddings, index, upload

from app.main import create_app
from app.schemas import RunRequest


def test_vector_rankings_match_reference_with_scope():
    rng = random.Random(20261015)
    vectors = [[rng.uniform(-1, 1) for _ in range(8)] for _ in range(24)]
    queries = [[rng.uniform(-1, 1) for _ in range(8)] for _ in range(3)]

    class Model(Embeddings):
        dim = 8

        async def embed_documents(self, texts):
            return [vectors[int(text.split("document-")[1])] for text in texts]

        async def embed_query(self, text):
            return queries[int(text)]

    with TestClient(
        create_app(settings(embedding_dim=8), embedding_model=Model())
    ) as client:
        files = [upload(client, f"document-{i}", name=f"d{i}.txt") for i in range(len(vectors))]
        index(client)
        allowed = files[::2]
        for qi, query in enumerate(queries):

            def cosine(vector):
                return (
                    sum(a * b for a, b in zip(query, vector))
                    / math.hypot(*query)
                    / math.hypot(*vector)
                )

            expected = sorted(range(0, len(vectors), 2), key=lambda i: -cosine(vectors[i]))[:10]
            response = client.post(
                BASE + "/search",
                json={
                    "query": str(qi),
                    "mode": "vector",
                    "file_ids": allowed,
                    "top_k": 10,
                    "min_score": -1,
                },
            )
            assert response.status_code == 200
            sources = response.json()["sources"]
            assert [s["file_id"] for s in sources] == [files[i] for i in expected]
            assert [s["vector_score"] for s in sources] == pytest.approx(
                [cosine(vectors[i]) for i in expected], abs=1e-6
            )
        with test_database() as db:
            definitions = db.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema()"
            ).fetchall()
            # 仅精确检索，不应创建任何向量近似索引。
            assert not any("USING hnsw" in row[0] for row in definitions)
            assert (
                db.execute("SELECT pg_typeof(vector)::text FROM chunk_vectors LIMIT 1").fetchone()[
                    0
                ]
                == "vector"
            )


def test_postgres_bm25_matches_pre_migration_reference_scores():
    reference = json.loads((Path(__file__).parent / "fixtures/bm25_reference.json").read_text())
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        files = [
            upload(client, text, name=f"d{i}.txt") for i, text in enumerate(reference["documents"])
        ]
        index(client)
        for item in reference["queries"]:
            response = client.post(
                BASE + "/search", json={"query": item["query"], "mode": "keyword", "top_k": 20}
            )
            assert response.status_code == 200
            scores = {
                str(files.index(s["file_id"])): s["keyword_score"]
                for s in response.json()["sources"]
            }
            assert scores == pytest.approx(item["scores"], rel=1e-10, abs=1e-12), item["query"]


def test_parallel_event_writes_allocate_unique_contiguous_sequences():
    with TestClient(create_app(settings())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        repository = client.app.state.run_service.repository
        row, _ = client.portal.call(
            repository.create,
            conversation,
            RunRequest(message="x", idempotency_key="x"),
            str(uuid4()),
        )

        async def publish():
            await asyncio.gather(
                *(repository.event(row["run_id"], "test", {"i": i}) for i in range(25))
            )

        client.portal.call(publish)
        events = client.portal.call(repository.events, row["run_id"], 0)
        assert [event[0] for event in events] == list(range(1, 27))


def test_second_worker_cannot_reset_running_state():
    config = settings()
    with TestClient(create_app(config)) as first:
        conversation = first.post("/api/v1/conversations").json()["thread_id"]
        repository = first.app.state.run_service.repository
        row, _ = first.portal.call(
            repository.create,
            conversation,
            RunRequest(message="x", idempotency_key="x"),
            str(uuid4()),
        )
        first.portal.call(repository.set_running, row["run_id"])
        with pytest.raises(RuntimeError, match="单 worker"):
            with TestClient(create_app(config)):
                pass
        assert first.portal.call(repository.get, row["run_id"])["status"] == "running"


def test_set_running_only_claims_a_queued_run():
    with TestClient(create_app(settings())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        repository = client.app.state.run_service.repository
        row, _ = client.portal.call(
            repository.create,
            conversation,
            RunRequest(message="x", idempotency_key="claim"),
            str(uuid4()),
        )
        assert client.portal.call(repository.set_running, row["run_id"]) is True
        assert client.portal.call(repository.set_running, row["run_id"]) is False
        assert client.portal.call(repository.get, row["run_id"])["status"] == "running"


def test_execute_skips_graph_when_state_claim_fails(monkeypatch):
    with TestClient(create_app(settings())) as client:
        conversation = client.post("/api/v1/conversations").json()["thread_id"]
        service = client.app.state.run_service
        repository = service.repository
        row, _ = client.portal.call(
            repository.create,
            conversation,
            RunRequest(message="x", idempotency_key="skip"),
            str(uuid4()),
        )

        async def no_claim(run_id):
            return False

        called = []

        def unexpected_graph():
            called.append(True)
            raise AssertionError("graph must not run after a failed state claim")

        monkeypatch.setattr(repository, "set_running", no_claim)
        monkeypatch.setattr(service, "get_graph", unexpected_graph)
        client.portal.call(service.execute, row["run_id"], "new")
        assert called == []


def test_child_task_does_not_inherit_parent_transaction_connection():
    with TestClient(create_app(settings())) as client:
        database = client.app.state.vector_service.files.database

        async def inspect():
            async with database.transaction():
                parent = await database.connection.execute("SELECT pg_backend_pid() AS pid")
                parent_pid = (await parent.fetchone())["pid"]

                async def child():
                    async with database.read():
                        cursor = await database.connection.execute("SELECT pg_backend_pid() AS pid")
                        return (await cursor.fetchone())["pid"]

                child_pid = await asyncio.create_task(child())
                assert child_pid != parent_pid

        client.portal.call(inspect)
