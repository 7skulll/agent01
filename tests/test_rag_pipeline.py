"""向量入库、故障恢复、索引版本、混合检索和 RAG 任务的端到端测试。"""

import asyncio
import hashlib
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from psycopg.rows import dict_row
from support import ScriptModel, events, rag_answer, settings, test_database, wait_run

from app.main import create_app
from app.rag.embedding import EmbeddingError

BASE = "/api/v1/knowledge-bases/default"


class Embeddings:
    model_name = "test-embedding"
    dim = 3

    def __init__(self):
        self.calls = []
        self.queries = []
        self.failure = False
        self.gate = None

    def vector(self, text):
        if "年假" in text:
            return [1.0, 0.0, 0.0]
        if "报销" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def embed_documents(self, texts):
        self.calls.append(texts)
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise EmbeddingError("限流测试", retryable=True)
        return [self.vector(text) for text in texts]

    async def embed_query(self, text):
        self.queries.append(text)
        return self.vector(text)


def upload(client, text="员工年假为十天。", name="手册.txt"):
    response = client.post(
        BASE + "/files",
        files={"file": (name, text.encode())},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 201
    return response.json()["file_id"]


def index(client):
    response = client.post(BASE + "/index")
    assert response.status_code == 202
    client.portal.call(client.app.state.index_service.wait)
    return response.json()


def db_rows(table):
    with test_database() as db:
        db.row_factory = dict_row
        return db.execute(f"SELECT * FROM {table}").fetchall()


def rag_run(client, query="年假有多少天？", key=None):
    cid = client.post("/api/v1/conversations").json()["thread_id"]
    response = client.post(
        f"/api/v1/conversations/{cid}/runs",
        json={
            "message": query,
            "idempotency_key": key or uuid4().hex,
            "knowledge_base_id": "default",
        },
    )
    assert response.status_code == 202, response.text
    return cid, response.json()["run_id"]


def test_upload_to_indexed_and_search_and_idempotent_index():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        file_id = upload(client)
        index(client)
        assert db_rows("files")[0]["index_status"] == "indexed"
        vector = db_rows("chunk_vectors")[0]
        assert vector["dimension"] == 3 and vector["model"] == model.model_name
        assert len(vector["input_hash"]) == 64
        assert model.calls[0] == ["手册.txt\n员工年假为十天。"]
        assert index(client)["count"] == 0
        assert len(model.calls) == len(db_rows("chunk_vectors")) == 1
        response = client.post(BASE + "/search", json={"query": "年假", "mode": "vector"})
        assert response.status_code == 200
        source = response.json()["sources"][0]
        assert source["file_id"] == file_id and source["content"] == "员工年假为十天。"
        assert source["vector_score"] == pytest.approx(1)
        assert client.post(BASE + "/search", json={"query": "   "}).status_code == 422


def test_embedding_failure_retries_without_reparsing(monkeypatch):
    model = Embeddings()
    model.failure = True
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)
        original_ids = [row["chunk_id"] for row in db_rows("chunks")]
        assert db_rows("files")[0]["index_status"] == "recorded"
        assert db_rows("files")[0]["index_error"] == "限流测试"
        assert not db_rows("chunk_vectors")
        model.failure = False

        async def unexpected_parse(file_id):
            raise AssertionError("must reuse chunks")

        monkeypatch.setattr(client.app.state.index_service, "parse_file", unexpected_parse)
        index(client)
        assert db_rows("files")[0]["index_status"] == "indexed"
        assert [row["chunk_id"] for row in db_rows("chunks")] == original_ids


def test_vector_publication_rollback_and_retry(monkeypatch):
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        repo = client.app.state.file_service.repository
        original = repo.set_status_within

        async def fail_publication(file_id, status, error=None):
            if status == "indexed":
                raise psycopg.OperationalError("injected")
            await original(file_id, status, error)

        monkeypatch.setattr(repo, "set_status_within", fail_publication)
        index(client)
        assert db_rows("files")[0]["index_status"] == "recorded"
        assert not db_rows("chunk_vectors")
        assert db_rows("chunk_terms")
        assert client.post(BASE + "/search", json={"query": "年假", "mode": "keyword"}).json()[
            "sources"
        ]
        monkeypatch.setattr(repo, "set_status_within", original)
        index(client)
        assert len(db_rows("chunk_vectors")) == len(db_rows("chunk_terms")) == 1


def test_restart_reuses_vectors_and_model_change_requires_rebuild():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)
        chunk_id = db_rows("chunks")[0]["chunk_id"]
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        assert index(client)["count"] == 0
        assert len(client.post(BASE + "/search", json={"query": "年假"}).json()["sources"]) == 1
    changed = Embeddings()
    changed.model_name = "new-model"
    with TestClient(create_app(settings(), embedding_model=changed)) as client:
        assert (
            client.post(BASE + "/search", json={"query": "年假", "mode": "vector"}).json()[
                "sources"
            ]
            == []
        )
        assert client.post(BASE + "/search", json={"query": "年假", "mode": "keyword"}).json()[
            "sources"
        ]
        assert index(client)["count"] == 1
        assert db_rows("chunk_vectors")[0]["model"] == "new-model"
        assert db_rows("chunks")[0]["chunk_id"] == chunk_id


def test_restart_resets_only_vector_stage():
    with TestClient(create_app(settings())) as client:
        upload(client)
        index(client)
    with test_database() as db:
        db.execute("UPDATE files SET index_status='indexing'")
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        assert db_rows("files")[0]["index_status"] == "recorded"
        index(client)
        assert db_rows("files")[0]["index_status"] == "indexed"


def test_fts_recalls_exact_term_when_dense_score_is_below_threshold():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client, "年假制度编号 POLICY_A42。")
        index(client)
        assert (
            client.post(BASE + "/search", json={"query": "POLICY_A42", "mode": "vector"}).json()[
                "sources"
            ]
            == []
        )
        result = client.post(BASE + "/search", json={"query": "POLICY_A42"}).json()
        assert len(result["sources"]) == 1
        assert result["sources"][0]["vector_score"] == 0
        assert result["sources"][0]["channels"] == ["vector", "keyword"]


def test_rag_answer_publishes_sources_to_status_sse_and_history():
    chat = ScriptModel(outputs=[rag_answer(content="年假为十天。")])
    with TestClient(
        create_app(settings(), chat_model=chat, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        cid, run_id = rag_run(client)
        result = wait_run(client, run_id)
        assert result["status"] == "completed", result
        assert result["sources"][0]["content"] == "员工年假为十天。"
        assert "员工年假为十天。" in chat.seen[0][-1].text
        assert '"sources"' in client.get(f"/api/v1/runs/{run_id}/events").text
        assert (
            client.get(f"/api/v1/conversations/{cid}").json()["messages"][-1]["content"]
            == "年假为十天。[1]"
        )


def test_empty_retrieval_refuses_without_calling_chat_model():
    chat = ScriptModel(outputs=[AIMessage(content="should not run")])
    with TestClient(
        create_app(settings(), chat_model=chat, embedding_model=Embeddings())
    ) as client:
        _, run_id = rag_run(client, "飞行速度？")
        result = wait_run(client, run_id)
        assert result["status"] == "completed" and not result["sources"]
        assert "未找到" in result["answer"] and not chat.seen


@pytest.mark.parametrize("source_ids", [[99], []])
def test_invalid_or_missing_citations_are_not_published(source_ids):
    with TestClient(
        create_app(
            settings(),
            chat_model=ScriptModel(outputs=[rag_answer("年假为十天。", source_ids=source_ids)]),
            embedding_model=Embeddings(),
        )
    ) as client:
        upload(client)
        index(client)
        cid, run_id = rag_run(client)
        result = wait_run(client, run_id)
        assert result["status"] == "failed" and result["error"]["code"] == "invalid_citations"
        assert client.get(f"/api/v1/conversations/{cid}").json()["messages"] == []


def test_resume_reuses_checkpointed_retrieval():
    embeddings = Embeddings()
    chat = ScriptModel(outputs=[RuntimeError("temporary"), rag_answer(content="十天。")])
    with TestClient(create_app(settings(), chat_model=chat, embedding_model=embeddings)) as client:
        upload(client)
        index(client)
        _, run_id = rag_run(client)
        assert wait_run(client, run_id)["status"] == "failed"
        assert client.post(f"/api/v1/runs/{run_id}/resume").status_code == 202
        assert wait_run(client, run_id)["status"] == "completed"
        assert len(embeddings.queries) == 1


@pytest.mark.parametrize("bad", [[0.0, 0.0, 0.0], [float("nan"), 1.0, 0.0], [1.0, 0.0]])
def test_invalid_vectors_never_reach_indexed(bad):
    model = Embeddings()
    model.vector = lambda text: bad
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)
        assert db_rows("files")[0]["index_status"] == "recorded"
        assert not db_rows("chunk_vectors")


def test_cancel_index_preserves_chunks_for_retry():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        model.gate = client.portal.call(asyncio.Event)
        upload(client)
        assert client.post(BASE + "/index").status_code == 202

        async def wait_started():
            for _ in range(100):
                if model.calls:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("embedding did not start")

        client.portal.call(wait_started)
        assert client.post(BASE + "/index").status_code == 409
        client.portal.call(client.app.state.index_service.close)
        assert db_rows("files")[0]["index_status"] == "recorded"
        assert db_rows("chunks") and not db_rows("chunk_vectors")


def test_reparse_invalidates_old_vectors_and_fts():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        file_id = upload(client)
        index(client)
        original_id = db_rows("chunks")[0]["chunk_id"]
        client.portal.call(client.app.state.index_service.parse_file, file_id)
        assert db_rows("files")[0]["index_status"] == "recorded"
        assert not db_rows("chunk_vectors") and not db_rows("chunk_terms")
        assert db_rows("chunks")[0]["chunk_id"] != original_id
        index(client)
        assert len(db_rows("chunk_vectors")) == 1


def test_search_errors_and_rag_scope_idempotency():
    with TestClient(create_app(settings())) as client:
        assert client.post(BASE + "/search", json={"query": "年假"}).status_code == 503
        assert (
            client.post("/api/v1/knowledge-bases/other/search", json={"query": "年假"}).status_code
            == 404
        )
    with TestClient(
        create_app(
            settings(),
            chat_model=ScriptModel(outputs=[rag_answer(content="十天。")]),
            embedding_model=Embeddings(),
        )
    ) as client:
        upload(client)
        index(client)
        cid, run_id = rag_run(client, key="same")
        assert wait_run(client, run_id)["status"] == "completed"
        response = client.post(
            f"/api/v1/conversations/{cid}/runs",
            json={"message": "年假有多少天？", "idempotency_key": "same"},
        )
        assert response.status_code == 409


def test_batch_processing_and_file_failure_isolation():
    class SelectiveEmbeddings(Embeddings):
        async def embed_documents(self, texts):
            if any("坏文件" in text for text in texts):
                raise EmbeddingError("文件测试失败")
            return await super().embed_documents(texts)

    embeddings = SelectiveEmbeddings()
    with TestClient(
        create_app(
            settings(chunk_size_tokens=64, chunk_overlap_tokens=8, embedding_batch_size=2),
            embedding_model=embeddings,
        )
    ) as client:
        bad_id = upload(client, "坏文件年假制度", name="坏文件.txt")
        good_id = upload(client, "员工年假为十天。" * 100)
        index(client)
        rows = {row["file_id"]: row for row in db_rows("files")}
        assert rows[bad_id]["index_status"] == "recorded"
        assert rows[good_id]["index_status"] == "indexed"
        assert len(embeddings.calls) > 1 and all(len(call) <= 2 for call in embeddings.calls)
        assert len(db_rows("chunk_vectors")) == len(
            [row for row in db_rows("chunks") if row["file_id"] == good_id]
        )


def test_rag_context_limit_does_not_call_chat_model():
    chat = ScriptModel(outputs=[AIMessage(content="should not run")])
    with TestClient(
        create_app(settings(rag_input_tokens=512), chat_model=chat, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        _, run_id = rag_run(client, "年假" + "字" * 600)
        result = wait_run(client, run_id)
        assert result["status"] == "failed" and result["error"]["code"] == "context_limit_exceeded"
        assert not chat.seen


def test_tokenizer_change_rechunks_and_chat_tokenizer_change_keeps_index():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client, "年假申请流程。" * 100)
        index(client)
        old_ids = {row["chunk_id"] for row in db_rows("chunks")}
    changed = settings(embedding_tokenizer="o200k_base")
    with TestClient(create_app(changed, embedding_model=model)) as client:
        response = client.post(BASE + "/search", json={"query": "年假", "mode": "hybrid"})
        assert response.status_code == 200 and response.json()["sources"]
        assert all(source["keyword_score"] is not None for source in response.json()["sources"])
        assert index(client)["count"] == 1
        new_ids = {row["chunk_id"] for row in db_rows("chunks")}
        assert old_ids.isdisjoint(new_ids)
        assert all(row["chunk_version"] for row in db_rows("files"))
        assert client.post(BASE + "/search", json={"query": "年假"}).json()["sources"]
    with TestClient(
        create_app(
            settings(embedding_tokenizer="o200k_base", chat_tokenizer="o200k_base"),
            embedding_model=model,
        )
    ) as client:
        assert index(client)["count"] == 0
        assert {row["chunk_id"] for row in db_rows("chunks")} == new_ids


def test_legacy_chunk_metadata_rebuilds_only_after_explicit_index():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        old_id = db_rows("chunks")[0]["chunk_id"]
    with test_database() as db:
        db.execute("UPDATE files SET chunk_version=NULL")
        db.execute("UPDATE chunk_vectors SET index_version='legacy-version'")
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        assert db_rows("files")[0]["chunk_version"] is None
        assert db_rows("chunks")[0]["chunk_id"] == old_id
        assert (
            client.post(BASE + "/search", json={"query": "年假", "mode": "keyword"}).status_code
            == 503
        )
        assert index(client)["count"] == 1
        assert db_rows("chunks")[0]["chunk_id"] != old_id


def test_index_limits_formatted_embedding_and_input_hash_matches_sent_text():
    from app.tokenization import TokenCounter

    model = Embeddings()
    config = settings(embedding_max_input_tokens=64, token_safety_ratio=1)
    with TestClient(create_app(config, embedding_model=model)) as client:
        upload(client, "员工每年有十天年假。申请需要审批。\n" * 100, name="员工年假手册.txt")
        index(client)
        assert db_rows("files")[0]["index_status"] == "indexed"
        sent = [text for batch in model.calls for text in batch]
        assert all(TokenCounter().count(text) <= 64 for text in sent)
        assert {row["input_hash"] for row in db_rows("chunk_vectors")} == {
            hashlib.sha256(text.encode()).hexdigest() for text in sent
        }
        chunks = {row["chunk_id"]: row for row in db_rows("chunks")}
        for vector in db_rows("chunk_vectors"):
            chunk = chunks[vector["chunk_id"]]
            text = "\n".join(
                part for part in ("员工年假手册.txt", chunk["heading"], chunk["content"]) if part
            )
            assert TokenCounter().count(text) <= 64
            assert vector["input_hash"] == hashlib.sha256(text.encode()).hexdigest()


def test_real_token_usage_is_saved_in_sse_events():
    chat = ScriptModel(
        outputs=[
            rag_answer(
                content="年假十天。",
                usage_metadata={"input_tokens": 123, "output_tokens": 12, "total_tokens": 135},
            )
        ]
    )
    with TestClient(
        create_app(settings(), chat_model=chat, embedding_model=Embeddings())
    ) as client:
        upload(client)
        index(client)
        _, run_id = rag_run(client)
        assert wait_run(client, run_id)["status"] == "completed"
        response = client.get(f"/api/v1/runs/{run_id}/events")
        usage = [data for _, event, data in events(response) if event == "token_usage"]
        assert usage[0]["actual"]["input_tokens"] == 123
        assert usage[0]["estimated_input_tokens"] > 0
