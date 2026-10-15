"""BM25 分词、字段权重和独立重建的回归验证。"""

import sqlite3

from fastapi.testclient import TestClient
from support import settings
from test_rag_pipeline import BASE, Embeddings, db_rows, index, upload

from app.main import create_app
from app.rag.terms import document_terms
from app.rag.vectors import index_version


def keyword(client, query, **kwargs):
    return client.post(BASE + "/search", json={"query": query, "mode": "keyword", **kwargs})


def test_fastgpt_cleanup_preserves_frequency_and_uses_normal_cut():
    assert document_terms("数据库 数据库") == ["数据库", "数据库"]
    assert document_terms("the 数据库 https://example.com/a **测试**") == ["数据库", "测试"]
    assert document_terms("ＰＺ－２０７") == []
    assert document_terms("C++ C# PZ_207") == ["C++", "C", "PZ207"]


def test_fulltext_contains_heading_and_body_without_filename():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        target = upload(client, "# 数据库\n\n连接池配置。", name="unique_filename.md")
        index(client)
        row = db_rows("chunk_terms")[0]
        assert set(row) == {"chunk_id", "file_id", "kb_id", "terms"}
        assert "数据库" in row["terms"] and "配置" in row["terms"]
        assert keyword(client, "数据库").json()["sources"][0]["file_id"] == target
        assert keyword(client, "unique_filename").json()["sources"] == []


def test_version_change_rebuilds_only_fulltext_and_supports_no_embedding_client():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client, "门禁卡遗失后请联系行政。")
        index(client)
        vectors, chunks = db_rows("chunk_vectors"), db_rows("chunks")
    with sqlite3.connect("data/chat.sqlite3") as db:
        db.execute("UPDATE lexical_indexes SET version='obsolete'")
    with TestClient(create_app(settings())) as client:
        assert keyword(client, "门禁卡").status_code == 503
        assert index(client)["count"] == 1
        assert db_rows("chunk_vectors") == vectors
        assert db_rows("chunks") == chunks
        assert keyword(client, "门禁卡").json()["sources"]
        assert index(client)["count"] == 0


def test_old_field_schema_migration_reuses_vectors_and_chunk_ids():
    config = settings()
    with TestClient(create_app(config, embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        chunks = db_rows("chunks")
        vector_data = [(v["vector"], v["input_hash"]) for v in db_rows("chunk_vectors")]
    with sqlite3.connect("data/chat.sqlite3") as db:
        db.execute("DROP TABLE chunk_terms")
        db.execute(
            "CREATE VIRTUAL TABLE chunk_terms USING fts5("
            "chunk_id UNINDEXED,file_id UNINDEXED,kb_id UNINDEXED,terms,filename,title)"
        )
        db.execute(
            "UPDATE chunk_vectors SET index_version=?",
            (index_version(config, Embeddings(), "ordered-bigrams-v2"),),
        )
    model = Embeddings()
    with TestClient(create_app(config, embedding_model=model)) as client:
        assert keyword(client, "年假").status_code == 503
        assert index(client)["count"] == 1
        assert model.calls == []
        assert db_rows("chunks") == chunks
        assert [(v["vector"], v["input_hash"]) for v in db_rows("chunk_vectors")] == vector_data
        assert keyword(client, "年假").json()["sources"]


def test_fulltext_rebuild_failure_rolls_back_and_can_retry_without_embedding(monkeypatch):
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        with sqlite3.connect("data/chat.sqlite3") as db:
            db.execute("UPDATE lexical_indexes SET version='obsolete'")
        old_terms = db_rows("chunk_terms")
        old_metadata = db_rows("lexical_indexes")
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        lexical = client.app.state.vector_service.lexical
        original = lexical.replace_within

        async def fail(*args):
            await original(*args)
            raise sqlite3.OperationalError("injected")

        monkeypatch.setattr(lexical, "replace_within", fail)
        assert index(client)["count"] == 1
        assert db_rows("chunk_terms") == old_terms
        assert db_rows("lexical_indexes") == old_metadata
        assert keyword(client, "年假").status_code == 503
        monkeypatch.setattr(lexical, "replace_within", original)
        assert index(client)["count"] == 1
        assert model.calls == []
        assert keyword(client, "年假").json()["sources"]


def test_missing_fulltext_table_invalidates_metadata_and_rebuilds_without_embedding():
    with TestClient(create_app(settings(), embedding_model=Embeddings())) as client:
        upload(client)
        index(client)
        vectors, chunks = db_rows("chunk_vectors"), db_rows("chunks")
    with sqlite3.connect("data/chat.sqlite3") as db:
        db.execute("DROP TABLE chunk_terms")
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        assert keyword(client, "年假").status_code == 503
        assert index(client)["count"] == 1
        assert keyword(client, "年假").json()["sources"]
        assert model.calls == []
        assert db_rows("chunk_vectors") == vectors
        assert db_rows("chunks") == chunks


def test_comparison_uses_identical_vectors_for_all_bm25_variants(tmp_path, monkeypatch):
    import argparse
    import asyncio
    import json

    import scripts.eval_retrieval as evaluation

    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "description": "test",
                "documents": [
                    {"id": "a", "title": "账号密码", "text": "忘记密码请找 IT。"},
                    {"id": "b", "title": "设备维修", "text": "电脑损坏请报修。"},
                ],
                "queries": [
                    {"id": "q1", "query": "密码", "group": "test", "relevant": ["a"]},
                    {"id": "q2", "query": "不存在", "group": "test", "relevant": []},
                    {"id": "q3", "query": "不存在", "group": "test", "relevant": []},
                ],
            },
            ensure_ascii=False,
        )
    )
    model = Embeddings()
    embed_query = model.embed_query

    async def delayed_query(text):
        await asyncio.sleep(0)
        return await embed_query(text)

    monkeypatch.setattr(model, "embed_query", delayed_query)
    monkeypatch.setattr(evaluation, "Settings", settings)
    monkeypatch.setattr(evaluation, "build_embedding_model", lambda config: model)
    output = tmp_path / "report.json"
    asyncio.run(
        evaluation.evaluate(
            argparse.Namespace(dataset=fixture, output=output, min_score=0.2, bm25_comparison=True)
        )
    )
    report = json.loads(output.read_text())
    experiments = report["bm25_comparison"]
    assert len(experiments) == 2
    assert len({experiment["vector_fingerprint"] for experiment in experiments.values()}) == 1
    assert model.queries == ["密码", "不存在"]
    assert len(model.calls) == 2
    assert report["selected_variant"] == "fastgpt"
    assert report["runs"] == experiments["fastgpt"]["runs"]
    rankings = [
        [row["ranked_ids"] for row in experiment["runs"]["vector"]["queries"]]
        for experiment in experiments.values()
    ]
    assert all(ranking == rankings[0] for ranking in rankings)
