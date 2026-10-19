"""关键词发布、失败恢复和 BM25 统计边界，与向量阶段互不阻塞。"""

import asyncio

import psycopg
import pytest
from fastapi.testclient import TestClient
from support import settings, test_database
from test_bm25 import keyword
from test_rag_pipeline import BASE, Embeddings, db_rows, index, upload

from app.main import create_app


def summary(client):
    return client.get(BASE + "/files").json()["items"][0]


def test_fresh_file_without_embedding_is_keyword_ready_and_idempotent():
    with TestClient(create_app(settings())) as client:
        file_id = upload(client)
        index(client)
        assert not db_rows("chunk_vectors")
        assert summary(client)["index_status"] == "recorded"
        assert summary(client)["keyword_ready"] is True
        assert summary(client)["keyword_error"] is None
        response = keyword(client, "年假").json()
        assert response["sources"][0]["file_id"] == file_id
        assert response["lexical_version"] == db_rows("lexical_indexes")[0]["version"]
        assert index(client)["count"] == 0


def test_embedding_failure_and_retry_do_not_rebuild_keyword_index():
    model = Embeddings()
    model.failure = True
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client)
        index(client)
        metadata = db_rows("lexical_indexes")
        chunks = db_rows("chunks")
        assert summary(client)["keyword_ready"] is True
        assert keyword(client, "年假").json()["sources"]
        model.failure = False
        index(client)
        assert db_rows("lexical_indexes") == metadata
        assert db_rows("chunks") == chunks
        assert summary(client)["index_status"] == "indexed"


def test_keyword_available_during_embedding_and_after_cancellation():
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:

        async def block_embedding():
            model.gate = asyncio.Event()

        client.portal.call(block_embedding)
        upload(client)
        assert client.post(BASE + "/index").status_code == 202

        async def wait_embedding():
            async with asyncio.timeout(5):
                while not model.calls:
                    await asyncio.sleep(0.001)

        client.portal.call(wait_embedding)
        assert summary(client)["index_status"] == "indexing"
        assert keyword(client, "年假").json()["sources"]
        client.portal.call(client.app.state.index_service.close)
        assert keyword(client, "年假").json()["sources"]
        assert not db_rows("chunk_vectors")


def test_lexical_failure_does_not_block_vectors_and_retry_is_local(monkeypatch):
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        lexical = client.app.state.lexical_index
        original = lexical.replace_within

        async def fail(*args):
            await original(*args)
            raise psycopg.OperationalError("injected")

        monkeypatch.setattr(lexical, "replace_within", fail)
        upload(client)
        index(client)
        assert summary(client)["index_status"] == "indexed"
        assert summary(client)["keyword_ready"] is False
        assert summary(client)["keyword_error"]
        assert not db_rows("chunk_terms") and not db_rows("lexical_indexes")
        assert keyword(client, "年假").status_code == 503
        response = client.post(BASE + "/search", json={"query": "年假"}).json()
        assert response["sources"] and response["diagnostics"]["degraded"] == ["keyword_failed"]
        calls = len(model.calls)
        monkeypatch.setattr(lexical, "replace_within", original)
        index(client)
        assert summary(client)["keyword_ready"] is True
        assert summary(client)["keyword_error"] is None
        assert len(model.calls) == calls


def test_bm25_statistics_ignore_foreign_kb_and_stale_versions():
    with TestClient(create_app(settings())) as client:
        target = upload(client, "年假审批。")
        upload(client, "设备报销。", name="设备.txt")
        upload(client, "门禁维修。", name="门禁.txt")
        index(client)
        baseline = keyword(client, "年假").json()["sources"][0]["keyword_score"]
        other = upload(client, "年假审批。" * 50, name="外部.txt")
        index(client)
        with test_database() as db:
            db.execute(
                "INSERT INTO knowledge_bases (kb_id,name,created_at) "
                "VALUES ('other','其他库','now')"
            )
            for table in ("files", "chunks", "chunk_terms"):
                db.execute(f"UPDATE {table} SET kb_id='other' WHERE file_id=%s", (other,))
        assert keyword(client, "年假").json()["sources"][0]["keyword_score"] == pytest.approx(
            baseline
        )
        assert keyword(client, "年假", file_ids=[target]).json()["sources"][0][
            "keyword_score"
        ] == pytest.approx(baseline)
        with test_database() as db:
            for table in ("files", "chunks", "chunk_terms"):
                db.execute(f"UPDATE {table} SET kb_id='default' WHERE file_id=%s", (other,))
            db.execute("UPDATE lexical_indexes SET version='stale' WHERE file_id=%s", (other,))
        # 指定有效文件仍可检索，过期索引既不贡献文档数，也不贡献 DF / avgdl。
        assert keyword(client, "年假", file_ids=[target]).json()["sources"][0][
            "keyword_score"
        ] == pytest.approx(baseline)
        assert keyword(client, "年假").status_code == 503


def test_keyword_neighbors_do_not_require_vectors():
    config = settings(chunk_size_tokens=64, chunk_overlap_tokens=0, retrieval_neighbor_chunks=1)
    with TestClient(create_app(config)) as client:
        upload(client, "年假审批。" + "申请材料需要经理签字。" * 40)
        index(client)
        assert len(db_rows("chunks")) > 1
        source = keyword(client, "年假", top_k=1).json()["sources"][0]
        assert source["supporting_chunks"]
        assert not db_rows("chunk_vectors")
