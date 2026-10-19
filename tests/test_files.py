import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from support import settings, test_database

from app.main import create_app
from app.rag.file_api import LimitedMultipartParser
from app.rag.storage import MAX_FILE_BYTES, LocalStorage

URL = "/api/v1/knowledge-bases/default/files"


@pytest.fixture
def client():
    with TestClient(create_app(settings())) as result:
        yield result


def upload(client, name="制度.txt", body=b"hello", key="key"):
    return client.post(URL, files={"file": (name, body)}, headers={"Idempotency-Key": key})


def rows():
    with test_database() as connection:
        return connection.execute("SELECT file_id, name, index_status FROM files").fetchall()


def objects():
    return [p for p in Path("data/files").rglob("*") if p.is_file()]


@pytest.mark.parametrize("extension", ["PDF", "doc", "DOCX", "md", "txt", "xls", "XLSX"])
def test_supported_types_and_hash(client, extension):
    response = upload(client, f"测试.{extension}")
    assert response.status_code == 201
    record = response.json()
    assert record["extension"] == f".{extension.lower()}"
    assert record["content_hash"] == hashlib.sha256(b"hello").hexdigest()
    assert record["index_status"] == "not_indexed"
    assert record["storage_backend"] == "local"
    assert record["updated_at"] == record["created_at"]
    assert Path("data/files", record["storage_path"]).read_bytes() == b"hello"


@pytest.mark.parametrize(
    "name,body,status",
    [
        ("a.txt", b"", 422),
        ("a.exe", b"x", 415),
        ("a.pdf.exe", b"x", 415),
        ("无扩展名", b"x", 415),
        ("a%00.txt", b"x", 422),
        ("中" * 84 + ".txt", b"x", 422),
        ("a.txt", b"x" * (MAX_FILE_BYTES + 1), 413),
    ],
)
def test_rejected_uploads_leave_no_data(client, name, body, status):
    assert upload(client, name, body).status_code == status
    assert rows() == []
    assert objects() == []


def test_exact_size_limit_and_utf8_name_limit(client):
    response = upload(client, "中" * 83 + "ab.txt", b"x" * MAX_FILE_BYTES)
    assert response.status_code == 201
    assert response.json()["size"] == MAX_FILE_BYTES
    assert len(response.json()["name"].encode()) == 255


@pytest.mark.parametrize(
    "name,expected",
    [
        ("../制度.txt", "制度.txt"),
        ("..\\制度.txt", "制度.txt"),
        ("%E5%88%B6%E5%BA%A6.txt", "制度.txt"),
        ("%2e%2e%2f制度.txt", "制度.txt"),
        (
            "腾讯游戏MOBA品类：战略目标-项目组合-项目集-项目矩阵.pdf",
            "腾讯游戏MOBA品类：战略目标-项目组合-项目集-项目矩阵.pdf",
        ),
        (
            "多部门职能组织下AI嵌入农业项目:迭代开发方案.docx",
            "多部门职能组织下AI嵌入农业项目:迭代开发方案.docx",
        ),
        ("【方案】需求？预算100%！[最终版]👩‍💻.pdf", "【方案】需求？预算100%！[最终版]👩‍💻.pdf"),
        ("Q&A+预算=汇总,2026;修订.xlsx", "Q&A+预算=汇总,2026;修订.xlsx"),
    ],
)
def test_basename_and_chinese_decoding(client, name, expected):
    result = upload(client, name)
    assert result.status_code == 201
    assert result.json()["name"] == expected
    assert objects()[0].parent == Path("data/files/default")
    assert objects()[0].name == result.json()["file_id"] + result.json()["extension"]


def test_same_name_is_allowed_and_idempotency_is_content_based(client):
    first = upload(client).json()
    assert upload(client).json() == first
    assert upload(client, body=b"other").status_code == 409  # Same size, different content.
    assert upload(client, name="别名.txt").status_code == 409
    second = upload(client, key="second").json()
    assert second["file_id"] != first["file_id"]
    assert len(rows()) == len(objects()) == 2


def test_concurrent_idempotent_uploads(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(client), range(2)))
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["file_id"] == responses[1].json()["file_id"]
    assert len(rows()) == len(objects()) == 1


def test_database_failure_compensates_and_allows_retry(client, monkeypatch):
    repository = client.app.state.file_service.repository
    original = repository.insert

    async def fail(*args):
        raise psycopg.OperationalError("injected")

    monkeypatch.setattr(repository, "insert", fail)
    assert upload(client).status_code == 503
    assert rows() == objects() == []
    monkeypatch.setattr(repository, "insert", original)
    assert upload(client).status_code == 201


def test_disk_publish_failure_removes_temporary_file(client, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected disk error")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "hardlink_to", fail)
        assert upload(client).status_code == 503
    assert rows() == objects() == []
    assert upload(client).status_code == 201


def test_reboot_preserves_record_and_idempotency():
    with TestClient(create_app(settings())) as client:
        record = upload(client).json()
    with TestClient(create_app(settings())) as client:
        assert upload(client).json() == record
        storage = client.app.state.file_service.storage
        assert client.portal.call(storage.get, record["storage_path"]) == b"hello"
    assert len(rows()) == len(objects()) == 1


def test_contract_validation_and_database_indexes(client):
    assert client.post(URL, files={"file": ("a.txt", b"x")}).status_code == 422
    assert upload(client, key="   ").status_code == 422
    assert client.post(URL, json={}, headers={"Idempotency-Key": "x"}).status_code == 415
    assert (
        client.post(
            URL.replace("default", "unknown"),
            files={"file": ("a.txt", b"x")},
            headers={"Idempotency-Key": "x"},
        ).status_code
        == 404
    )
    with test_database() as connection:
        names = {
            r[0]
            for r in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND "
                "tablename='files'"
            )
        }
    assert "files_kb_created" in names
    assert not {"files_kb_name", "files_content_hash"} & names
    schema = client.get("/openapi.json").json()
    assert (
        "multipart/form-data"
        in schema["paths"][URL.replace("default", "{kb_id}")]["post"]["requestBody"]["content"]
    )


def test_incomplete_multipart_is_not_published(client):
    body = b'--test\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\nhello'
    result = client.post(
        URL,
        content=body,
        headers={"Content-Type": "multipart/form-data; boundary=test", "Idempotency-Key": "x"},
    )
    assert result.status_code == 422
    assert rows() == objects() == []


@pytest.mark.parametrize("interrupt", ["disconnect", "cancel"])
def test_interrupted_receive_closes_spooled_files(client, monkeypatch, interrupt):
    captured = []
    original = LimitedMultipartParser.on_headers_finished

    def capture(parser):
        original(parser)
        captured.extend(parser._files_to_close_on_error)

    monkeypatch.setattr(LimitedMultipartParser, "on_headers_finished", capture)

    async def run():
        received = False
        responses = []

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": (
                        b'--test\r\nContent-Disposition: form-data; name="file"; '
                        b'filename="a.txt"\r\n\r\nhello'
                    ),
                    "more_body": True,
                }
            if interrupt == "cancel":
                raise asyncio.CancelledError
            return {"type": "http.disconnect"}

        async def send(event):
            responses.append(event)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": URL,
            "query_string": b"",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=test"),
                (b"idempotency-key", b"x"),
            ],
        }
        if interrupt == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await client.app(scope, receive, send)
        else:
            await client.app(scope, receive, send)
            assert responses[0]["status"] == 499

    client.portal.call(run)
    assert captured and all(f.closed for f in captured)
    assert rows() == objects() == []


def test_local_storage_cleanup_on_interruption_and_interface(tmp_path):
    async def run():
        storage = LocalStorage(tmp_path / "objects")
        key = "default/id.txt"

        async def interrupted():
            yield b"partial"
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await storage.put(key, interrupted())
        assert not await storage.exists(key)
        assert not list(storage.root.rglob("*.part"))

        async def complete():
            yield b"complete"

        await storage.put(key, complete())
        assert await storage.exists(key)
        assert await storage.get(key) == b"complete"
        await storage.delete(key)
        await storage.delete(key)
        assert not await storage.exists(key)
        with pytest.raises(ValueError):
            await storage.get("../outside.txt")

    asyncio.run(run())
