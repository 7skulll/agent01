from fastapi.testclient import TestClient
from support import settings
from test_rag_pipeline import BASE, index, upload

from app.main import create_app


def test_batch_bm25_matches_independent_queries_and_file_scope():
    with TestClient(create_app(settings())) as client:
        a = upload(client, "alpha alpha beta", name="a.txt")
        b = upload(client, "beta beta gamma", name="b.txt")
        index(client)
        lexical = client.app.state.retrieval_service.lexical
        queries = [["alpha"], ["beta"], ["doesnotexist"]]
        batch = client.portal.call(lexical.search_many, "default", None, queries, 1)
        separate = [client.portal.call(lexical.search, "default", None, q, 1) for q in queries]
        assert batch == separate
        assert batch[0][0]["file_id"] == a
        assert batch[1][0]["file_id"] == b
        assert batch[2] == []
        scoped = client.portal.call(lexical.search_many, "default", [a], queries, 1)
        assert all(row["file_id"] == a for hits in scoped for row in hits)
        assert client.portal.call(lexical.search_many, "default", [], queries, 1) == [[], [], []]


def test_character_fallback_uses_indexed_original_text_and_respects_scope():
    with TestClient(create_app(settings())) as client:
        a = upload(client, "负责人员为张小明，设备型号为ZX-314。", name="manual.txt")
        b = upload(client, "另一个文件。", name="other.txt")
        index(client)
        lexical = client.app.state.retrieval_service.lexical
        result = client.portal.call(lexical.character_fallback, "default", [a], ["张小明"], 5)
        assert result[0][0]["file_id"] == a
        assert client.portal.call(
            lexical.character_fallback,
            "default",
            [b],
            ["张小明"],
            5,
        ) == [[]]
        assert client.portal.call(
            lexical.character_fallback,
            "default",
            [],
            ["张小明"],
            5,
        ) == [[]]

        # 删除 BM25 词项但保留有效版本，模拟分析器边界不匹配；纯 keyword 仍可回退。
        async def erase_terms():
            database = lexical.database
            async with database.transaction():
                await database.connection.execute("DELETE FROM lexical_postings")

        client.portal.call(erase_terms)
        response = client.post(
            BASE + "/search",
            json={
                "query": "张小明",
                "mode": "keyword",
                "file_ids": [a],
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["sources"][0]["file_id"] == a
        assert result["sources"][0]["character_fallback"]
        assert result["sources"][0]["rank"] == 1
        assert "min_score" in result["diagnostics"]["unapplied_parameters"]


def test_upgrade_backfills_statistics_and_character_index_without_reembedding():
    from pathlib import Path

    from support import test_database

    with TestClient(create_app(settings())) as client:
        file_id = upload(client, "负责人员为张小明。\n设备型号ZX-314。")
        index(client)
    with test_database() as db:
        expected = db.execute(
            "SELECT count(*),sum(token_count) FROM chunk_terms WHERE file_id=%s",
            (file_id,),
        ).fetchone()
        chunks = db.execute("SELECT chunk_id,content FROM chunks ORDER BY chunk_id").fetchall()
        # 模拟升级前的已有索引；本测试的 schema 完全隔离。
        db.execute("DROP INDEX terms_kb")
        db.execute("ALTER TABLE chunks DROP COLUMN character_terms")
        db.execute("DROP FUNCTION rag_character_terms(text)")
        db.execute("ALTER TABLE lexical_indexes DROP COLUMN chunk_count,DROP COLUMN token_count")
        migration = Path(__file__).resolve().parents[1] / "app/migrations/012_retrieval_search.sql"
        db.execute(migration.read_text(), prepare=False)
        assert (
            db.execute(
                "SELECT chunk_count,token_count FROM lexical_indexes WHERE file_id=%s",
                (file_id,),
            ).fetchone()
            == expected
        )
        assert (
            db.execute("SELECT chunk_id,content FROM chunks ORDER BY chunk_id").fetchall() == chunks
        )
        assert db.execute(
            "SELECT character_terms @> ARRAY['张小','小明','zx-314'] FROM chunks LIMIT 1"
        ).fetchone()[0]
        db.execute("SET LOCAL enable_seqscan=off")
        plan = db.execute(
            "EXPLAIN SELECT chunk_id FROM chunks WHERE character_terms && ARRAY['张小']"
        ).fetchall()
        assert "chunks_character_terms" in str(plan)
