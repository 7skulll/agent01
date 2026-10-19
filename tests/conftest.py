import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    """每个测试独立 PostgreSQL schema；测试 URL 必须明确提供。"""
    monkeypatch.chdir(tmp_path)
    base = os.getenv("RAG_TEST_DATABASE_URL")
    if not base:
        pytest.fail("运行测试必须提供隔离 PostgreSQL 的 RAG_TEST_DATABASE_URL。")
    schema = "test_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(base, options=f"-c search_path={schema},public")
    monkeypatch.setenv("RAG_DATABASE_URL", scoped)
    yield
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
