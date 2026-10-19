import asyncio
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from support import settings, test_database

from app.errors import ChatError
from app.main import create_app
from app.rag.chunking import chunk_blocks, count_tokens, split_text
from app.rag.indexing import IndexRepository, IndexService
from app.rag.parsers import Block, ParseError, parse

UPLOAD_URL = "/api/v1/knowledge-bases/default/files"
LIST_URL = "/api/v1/knowledge-bases/default/files"
INDEX_URL = "/api/v1/knowledge-bases/default/index"


@pytest.fixture
def client():
    with TestClient(create_app(settings())) as result:
        yield result


def upload(client, name="制度.txt", body=b"hello", key=None):
    key = key or uuid4().hex
    return client.post(UPLOAD_URL, files={"file": (name, body)}, headers={"Idempotency-Key": key})


def wait_index(client):
    client.portal.call(client.app.state.index_service.wait)


def rows(where=""):
    with test_database() as connection:
        return connection.execute(
            f"SELECT file_id, index_status, index_error FROM files {where}"
        ).fetchall()


def objects():
    return [path for path in Path("data/files").rglob("*") if path.is_file()]


def chunk_rows(file_id):
    with test_database() as connection:
        return connection.execute(
            "SELECT chunk_id, chunk_index, content, heading, kind FROM chunks "
            "WHERE file_id=%s ORDER BY chunk_index",
            (file_id,),
        ).fetchall()


def docx_bytes():
    from docx import Document

    document = Document()
    document.add_heading("员工手册", level=1)
    document.add_paragraph("年假制度说明。")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "姓名"
    table.rows[0].cells[1].text = "部门"
    table.rows[1].cells[0].text = "张三"
    table.rows[1].cells[1].text = "研发"
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "员工"
    sheet.append(["姓名", "部门"])
    sheet.append(["张三", "研发"])
    sheet.append(["李四", "市场"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_with_text(text="Hello RAG"):
    content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


def blank_pdf():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_text_and_markdown_reach_recorded(client):
    text = upload(client, "制度.txt", "第一段。\n\n第二段。".encode()).json()
    markdown_body = "# 第一章\n\n内容A。".encode()
    markdown = upload(client, "手册.md", markdown_body).json()

    response = client.post(INDEX_URL)
    assert response.status_code == 202
    assert response.json()["count"] == 2
    wait_index(client)

    assert dict((row[0], row[1]) for row in rows()) == {
        text["file_id"]: "recorded",
        markdown["file_id"]: "recorded",
    }
    text_chunks = [chunk[2] for chunk in chunk_rows(text["file_id"])]
    assert len(text_chunks) == 1
    assert "第一段。" in text_chunks[0] and "第二段。" in text_chunks[0]
    markdown_chunks = chunk_rows(markdown["file_id"])
    assert markdown_chunks[0][3] == "第一章"
    assert "内容A。" in markdown_chunks[0][2]


def test_docx_table_and_heading(client):
    record = upload(client, "手册.docx", docx_bytes()).json()
    client.post(INDEX_URL)
    wait_index(client)

    chunks = chunk_rows(record["file_id"])
    assert rows()[0][1] == "recorded"
    assert any(chunk[2] == "姓名: 张三; 部门: 研发" and chunk[4] == "table" for chunk in chunks)
    assert any(chunk[3] == "员工手册" and "年假制度说明。" in chunk[2] for chunk in chunks)


def test_xlsx_table_keeps_header(client):
    record = upload(client, "员工.xlsx", xlsx_bytes()).json()
    client.post(INDEX_URL)
    wait_index(client)

    content = "\n".join(chunk[2] for chunk in chunk_rows(record["file_id"]))
    assert rows()[0][1] == "recorded"
    assert "姓名: 张三; 部门: 研发" in content
    assert "姓名: 李四; 部门: 市场" in content


def test_pdf_with_text_layer(client):
    record = upload(client, "说明.pdf", pdf_with_text()).json()
    client.post(INDEX_URL)
    wait_index(client)

    assert rows()[0][1] == "recorded"
    assert "Hello RAG" in chunk_rows(record["file_id"])[0][2]


@pytest.mark.parametrize(
    "name,body,reason",
    [
        ("扫描件.pdf", blank_pdf(), "文本层"),
        ("旧格式.doc", b"\xd0\xcf\x11\xe0not-a-real-doc", "暂不支持"),
        ("旧格式.xls", b"\xd0\xcf\x11\xe0not-a-real-xls", "暂不支持"),
        ("损坏.docx", b"not a docx", "无法解析"),
    ],
)
def test_unparseable_files_fail_clearly(client, name, body, reason):
    record = upload(client, name, body).json()
    response = client.post(INDEX_URL)
    assert response.status_code == 202
    wait_index(client)

    status, error = rows()[0][1], rows()[0][2]
    assert status == "failed"
    assert reason in error
    assert chunk_rows(record["file_id"]) == []


def test_batch_isolates_failures(client):
    good = upload(client, "制度.txt", b"valid content").json()
    bad = upload(client, "扫描件.pdf", blank_pdf()).json()

    client.post(INDEX_URL)
    wait_index(client)

    statuses = dict((row[0], row[1]) for row in rows())
    assert statuses == {good["file_id"]: "recorded", bad["file_id"]: "failed"}


def test_index_is_idempotent_and_retries_failures(client):
    upload(client, "制度.txt", b"valid content")
    client.post(INDEX_URL)
    wait_index(client)

    again = client.post(INDEX_URL)
    assert again.json()["count"] == 0
    assert len(rows()) == 1

    failed = upload(client, "扫描件.pdf", blank_pdf()).json()
    assert client.post(INDEX_URL).json()["scheduled"] == [failed["file_id"]]
    wait_index(client)
    assert len(chunk_rows(failed["file_id"])) == 0


def test_reparse_replaces_chunks(client):
    record = upload(client, "长文.txt", ("这是一句测试。" * 200).encode()).json()
    client.post(INDEX_URL)
    wait_index(client)
    before = {chunk[0] for chunk in chunk_rows(record["file_id"])}

    async def reparse():
        await client.app.state.index_service.parse_file(record["file_id"])

    client.portal.call(reparse)
    after = {chunk[0] for chunk in chunk_rows(record["file_id"])}
    assert before and before.isdisjoint(after)


def test_index_conflict_while_running(client, monkeypatch):
    upload(client, "制度.txt", b"valid content")
    started = client.portal.call(asyncio.Event)
    release = client.portal.call(asyncio.Event)

    async def slow(self, file_id):
        started.set()
        await release.wait()

    monkeypatch.setattr(IndexService, "parse_file", slow)
    assert client.post(INDEX_URL).status_code == 202
    client.portal.call(started.wait)
    conflict = client.post(INDEX_URL)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "index_in_progress"
    client.portal.call(release.set)
    wait_index(client)


def test_list_files_reports_status_and_chunks(client):
    upload(client, "制度.txt", b"valid content")
    upload(client, "扫描件.pdf", blank_pdf())
    client.post(INDEX_URL)
    wait_index(client)

    response = client.get(LIST_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2 and body["limit"] == 50 and body["offset"] == 0
    by_name = {item["name"]: item for item in body["items"]}
    assert by_name["制度.txt"]["index_status"] == "recorded"
    assert by_name["制度.txt"]["chunk_count"] >= 1
    assert by_name["扫描件.pdf"]["index_status"] == "failed"
    assert by_name["扫描件.pdf"]["index_error"]

    page = client.get(LIST_URL, params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1


def test_unknown_knowledge_base(client):
    assert client.get("/api/v1/knowledge-bases/other/files").status_code == 404
    assert client.post("/api/v1/knowledge-bases/other/index").status_code == 404


def test_restart_resets_stuck_parsing():
    with TestClient(create_app(settings())) as client:
        record = upload(client, "制度.txt", b"valid content").json()
    with test_database() as connection:
        connection.execute(
            "UPDATE files SET index_status='parsing' WHERE file_id=%s", (record["file_id"],)
        )
        connection.commit()

    with TestClient(create_app(settings())) as client:
        items = client.get(LIST_URL).json()["items"]
        assert items[0]["index_status"] == "not_indexed"
        assert items[0]["index_error"] is None


def test_chunking_respects_size_and_overlaps():
    text = "".join(f"第{index}句测试内容。" for index in range(60))
    parts = split_text(text, 24, 8)
    assert len(parts) > 1
    assert all(count_tokens(part) <= 24 for part in parts)
    assert parts[0][-6:] in parts[1]
    assert all(part in text for part in parts)

    chunks = chunk_blocks(parse(".txt", text.encode()), 24, 8)
    assert all(chunk.tokens <= 24 for chunk in chunks)


def test_parser_rejects_unsupported_extension():
    with pytest.raises(ParseError):
        parse(".csv", b"a,b")


def test_upload_compensation_keeps_committed_file(client, monkeypatch):
    connection = client.app.state.file_service.repository.database.connection
    connection_type = type(connection)
    real_commit = connection_type.commit
    state = {"raised": False}

    async def commit_then_raise(self):
        await real_commit(self)
        if not state["raised"]:
            state["raised"] = True
            raise psycopg.OperationalError("injected after commit")

    monkeypatch.setattr(connection_type, "commit", commit_then_raise)
    first = upload(client, key="same")
    assert first.status_code == 201
    record = first.json()
    # 提交已生效时不得删除对象：记录与切片必须一致存在。
    assert dict((row[0], row[1]) for row in rows()) == {record["file_id"]: "not_indexed"}
    assert len(objects()) == 1

    monkeypatch.setattr(connection_type, "commit", real_commit)
    retry = upload(client, key="same")
    assert retry.json()["file_id"] == record["file_id"]
    assert len(objects()) == 1


def test_upload_probe_failure_preserves_committed_file_and_retry(client, monkeypatch):
    service = client.app.state.file_service
    connection = service.repository.database.connection
    real_commit = connection.commit
    real_get = service.repository.get
    state = {"raised": False}

    async def commit_then_raise():
        await real_commit()
        if not state["raised"]:
            state["raised"] = True
            raise psycopg.OperationalError("injected after commit")

    async def failed_probe(file_id):
        raise psycopg.OperationalError("injected probe failure")

    monkeypatch.setattr(connection, "commit", commit_then_raise)
    monkeypatch.setattr(service.repository, "get", failed_probe)
    first = upload(client, key="same")
    assert first.status_code == 503
    persisted = rows()
    assert len(persisted) == 1
    assert len(objects()) == 1
    assert objects()[0].read_bytes() == b"hello"

    monkeypatch.setattr(connection, "commit", real_commit)
    monkeypatch.setattr(service.repository, "get", real_get)
    retry = upload(client, key="same")
    assert retry.status_code == 201
    assert retry.json()["file_id"] == persisted[0][0]
    assert len(rows()) == len(objects()) == 1


def test_schedule_serializes_concurrent_requests(client, monkeypatch):
    upload(client, "a.txt", b"content")
    service = client.app.state.index_service
    entered = client.portal.call(asyncio.Event)
    release = client.portal.call(asyncio.Event)
    calls = {"pending": 0}
    original_pending = service.files.pending

    async def gated_pending(kb_id):
        calls["pending"] += 1
        if calls["pending"] == 1:
            entered.set()
            await release.wait()
        return await original_pending(kb_id)

    async def slow_parse(self, file_id):
        await asyncio.sleep(30)

    monkeypatch.setattr(service.files, "pending", gated_pending)
    monkeypatch.setattr(IndexService, "parse_file", slow_parse)

    async def scenario():
        first = asyncio.create_task(service.schedule("default"))
        await entered.wait()
        second = asyncio.create_task(service.schedule("default"))
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = client.portal.call(scenario)
    errors = [result for result in results if isinstance(result, ChatError)]
    scheduled = [result for result in results if isinstance(result, list)]
    assert len(scheduled) == 1 and len(errors) == 1
    assert errors[0].code == "index_in_progress"
    # 第二个请求在锁内直接看到运行中的任务，绝不会再查一次待处理文件或另起 worker。
    assert calls["pending"] == 1
    assert len(service.tasks) == 1
    client.portal.call(service.close)


def test_chunk_persist_failure_marks_failed_and_continues(client, monkeypatch):
    first = upload(client, "a.txt", b"content a").json()
    second = upload(client, "b.txt", b"content b").json()
    original = IndexRepository.replace_within
    calls = {"n": 0}

    async def flaky(self, file_id, records):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("injected")
        await original(self, file_id, records)

    monkeypatch.setattr(IndexRepository, "replace_within", flaky)
    client.post(INDEX_URL)
    wait_index(client)

    statuses = dict((row[0], row[1]) for row in rows())
    assert statuses == {first["file_id"]: "failed", second["file_id"]: "recorded"}
    assert chunk_rows(first["file_id"]) == []


def test_parse_offloads_from_event_loop(client, monkeypatch):
    upload(client, "a.txt", b"content")
    service = client.app.state.index_service
    file_id = rows()[0][0]
    ticks = {"n": 0}

    def blocking_parse(extension, data, options=None):
        time.sleep(0.3)
        return [Block(text="ok")]

    monkeypatch.setattr("app.rag.indexing.parse", blocking_parse)

    async def scenario():
        async def heartbeat():
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        await service.parse_file(file_id)
        await beat

    client.portal.call(scenario)
    # 同步解析期间事件循环仍持续调度心跳（未移出事件循环时几乎不会跳动）。
    assert ticks["n"] >= 5


def test_hard_split_stays_linear_and_bounded():
    text = "a" * 20000
    started = time.perf_counter()
    chunks = chunk_blocks(parse(".txt", text.encode()), 64, 8)
    assert time.perf_counter() - started < 1.0
    assert len(chunks) > 1
    assert max(chunk.tokens for chunk in chunks) <= 64
