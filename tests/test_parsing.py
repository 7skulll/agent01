"""解析精细化与结构切片的专项测试（不依赖网络与外部模型）。"""

import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from support import settings

from app.main import create_app
from app.rag.chunking import (
    _greedy_groups,
    _group_tokens,
    _overlap_prefix,
    _rebalance_tail,
    chunk_blocks,
    count_tokens,
    split_text,
)
from app.rag.parsers import (
    Block,
    ParseOptions,
    _looks_like_toc,
    _pdf_heading,
    _strip_running_lines,
    parse,
)
from app.rag.storage import MAX_FILE_BYTES

UPLOAD_URL = "/api/v1/knowledge-bases/default/files"
INDEX_URL = "/api/v1/knowledge-bases/default/index"


@pytest.fixture
def client():
    with TestClient(create_app(settings())) as result:
        yield result


def upload(client, name, body):
    return client.post(
        UPLOAD_URL,
        files={"file": (name, body)},
        headers={"Idempotency-Key": uuid4().hex},
    )


def wait_index(client):
    client.portal.call(client.app.state.index_service.wait)


def chunk_columns(file_id):
    with sqlite3.connect("data/chat.sqlite3") as connection:
        return connection.execute(
            "SELECT chunk_index, content, heading, kind, heading_level, source_order "
            "FROM chunks WHERE file_id=? ORDER BY chunk_index",
            (file_id,),
        ).fetchall()


def kinds(blocks):
    result: dict[str, list[Block]] = {}
    for block in blocks:
        result.setdefault(block.kind, []).append(block)
    return result


# --------------------------------------------------------------------------- 限额


def test_upload_limit_is_50_mib():
    assert MAX_FILE_BYTES == 50 * 1024 * 1024


# --------------------------------------------------------------------------- markdown


def test_markdown_structures_are_preserved():
    body = (
        "# 手册\n\n"
        "引言段落。\n\n"
        "## 安装\n\n"
        "步骤说明。\n\n"
        "- 第一步\n- 第二步\n\n"
        "| 名字 | 部门 |\n| --- | --- |\n| 张三 | 研发 |\n\n"
        "```python\nprint(1)\n```\n"
    ).encode()
    blocks = parse(".md", body)
    grouped = kinds(blocks)

    assert "code" in grouped and "print(1)" in grouped["code"][0].text
    assert "list" in grouped
    assert grouped["list"][0].text.split("\n") == ["- 第一步", "- 第二步"]

    table = grouped["table"][0]
    assert table.text == "名字: 张三; 部门: 研发"

    # 标题不单独成块，只作为 heading 路径；层级被记录。
    install = [b for b in blocks if "步骤说明。" in b.text][0]
    assert install.heading == "手册 > 安装"
    assert install.heading_level == 2
    assert all(b.kind != "title" for b in blocks)

    # order 按文档顺序连续编号。
    assert [b.order for b in blocks] == list(range(len(blocks)))


def test_markdown_without_structures_still_works():
    blocks = parse(".md", "# 第一章\n\n内容A。".encode())
    assert len(blocks) == 1
    assert blocks[0].heading == "第一章"
    assert blocks[0].heading_level == 1
    assert blocks[0].kind == "text"


# --------------------------------------------------------------------------- docx / xlsx


def test_docx_lists_are_grouped():
    from docx import Document

    document = Document()
    document.add_heading("员工手册", level=1)
    document.add_paragraph("总则说明。")
    document.add_paragraph("第一条", style="List Bullet")
    document.add_paragraph("第二条", style="List Bullet")
    buffer = BytesIO()
    document.save(buffer)

    blocks = parse(".docx", buffer.getvalue())
    lists = [b for b in blocks if b.kind == "list"]
    assert len(lists) == 1
    assert lists[0].text.split("\n") == ["第一条", "第二条"]
    assert lists[0].heading == "员工手册"


@pytest.mark.parametrize("value", ["100", "相同内容"])
def test_docx_independent_cells_keep_equal_values(value):
    from docx import Document

    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "收入"
    table.cell(0, 1).text = "支出"
    table.cell(1, 0).text = value
    table.cell(1, 1).text = value
    buffer = BytesIO()
    document.save(buffer)

    blocks = parse(".docx", buffer.getvalue())
    assert blocks[0].text == f"收入: {value}; 支出: {value}"


def test_docx_horizontal_merge_is_not_repeated():
    from docx import Document

    document = Document()
    table = document.add_table(rows=2, cols=3)
    for index, title in enumerate(["说明", "占位", "数量"]):
        table.cell(0, index).text = title
    table.cell(1, 0).merge(table.cell(1, 1)).text = "合并内容"
    table.cell(1, 2).text = "100"
    buffer = BytesIO()
    document.save(buffer)

    blocks = parse(".docx", buffer.getvalue())
    assert blocks[0].text == "说明: 合并内容; 数量: 100"


def test_docx_vertical_merge_keeps_context_for_each_row():
    from docx import Document

    document = Document()
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "部门"
    table.cell(0, 1).text = "员工"
    table.cell(1, 0).merge(table.cell(2, 0)).text = "研发"
    table.cell(1, 1).text = "张三"
    table.cell(2, 1).text = "李四"
    buffer = BytesIO()
    document.save(buffer)

    blocks = parse(".docx", buffer.getvalue())
    assert blocks[0].text == "部门: 研发; 员工: 张三\n部门: 研发; 员工: 李四"


def test_xlsx_is_table_with_sheet_heading():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "员工"
    sheet.append(["姓名", "部门"])
    sheet.append(["张三", "研发"])
    buffer = BytesIO()
    workbook.save(buffer)

    blocks = parse(".xlsx", buffer.getvalue())
    assert len(blocks) == 1
    assert blocks[0].kind == "table"
    assert blocks[0].heading == "员工"
    assert blocks[0].heading_level == 1
    assert blocks[0].text == "姓名: 张三; 部门: 研发"


# --------------------------------------------------------------------------- pdf heuristics


def test_pdf_heading_detection_is_conservative():
    assert _pdf_heading("第一章 总则")[0] == 1
    assert _pdf_heading("1.2 范围")[0] == 2
    assert _pdf_heading("1.2.3 实现细节")[0] == 3
    assert _pdf_heading("附录A")[0] == 1
    # 单级编号看起来更像列表项，不当作标题。
    assert _pdf_heading("1. 适用范围")[0] == 0
    assert _pdf_heading("这是一段很长的正文句子，结尾带有句号。")[0] == 0


def test_pdf_running_header_footer_and_page_numbers_are_removed():
    pages = [
        ["公司年报", "第一章 概览", "内容一", "第 1 页"],
        ["公司年报", "第二章 详情", "内容二", "第 2 页"],
        ["公司年报", "第三章 附录", "内容三", "第 3 页"],
    ]
    cleaned = _strip_running_lines(pages)
    assert all("公司年报" not in page for page in cleaned)
    assert all(
        not any(line.startswith("第") and line.endswith("页") for line in page) for page in cleaned
    )
    assert [page[0] for page in cleaned] == ["第一章 概览", "第二章 详情", "第三章 附录"]


def test_pdf_toc_page_is_detected():
    assert _looks_like_toc(
        ["目录", "第一章 ......... 1", "第二章 ......... 5", "第三章 ......... 9"]
    )
    assert not _looks_like_toc(["正文第一段。", "正文第二段。"])


def test_pdf_options_can_disable_cleanup():
    # 关闭清理时保留原始行（此处仅验证选项通路存在且可用）。
    assert ParseOptions(remove_header_footer=False).remove_header_footer is False


# --------------------------------------------------------------------------- chunking


def test_structured_chunks_have_no_overlap_and_keep_metadata():
    rows = "\n".join(f"列A: 值{i}; 列B: 数据{i}" for i in range(60))
    blocks = [Block(text=rows, kind="table", heading="表 1", heading_level=2, order=3)]
    chunks = chunk_blocks(blocks, 50, 10)

    assert len(chunks) > 1
    assert all(c.kind == "table" for c in chunks)
    assert all(c.heading == "表 1" and c.heading_level == 2 and c.order == 3 for c in chunks)
    assert all(c.tokens <= 50 for c in chunks)
    # 结构化类型不加字符重叠：第二块第一行不会出现在第一块里。
    assert chunks[1].content.split("\n")[0] not in chunks[0].content


def test_different_kinds_are_never_merged():
    blocks = [
        Block(text="正文段落。", kind="text", order=0),
        Block(text="第一项\n第二项", kind="list", order=1),
    ]
    chunks = chunk_blocks(blocks, 512, 64)
    assert {c.kind for c in chunks} == {"text", "list"}


def test_overlap_is_never_dropped_and_respects_cap():
    text = "".join(f"第{index}句测试内容。" for index in range(40))
    parts = split_text(text, 24, 8)

    assert len(parts) > 1
    assert all(count_tokens(part) <= 24 for part in parts)
    # 每个块都以上一块的某个后缀开头：重叠一定存在，不会被丢弃。
    for previous, current in zip(parts, parts[1:]):
        assert any(
            len(previous) - start >= 3 and current.startswith(previous[start:])
            for start in range(len(previous))
        )


def test_tail_fragment_is_rebalanced():
    # cap=10 时贪心会把 [5,5] 各自成组，最后一个 1 token 成为碎片。
    pieces = ["字" * 5, "字" * 5, "字" * 5, "字" * 5, "字"]
    groups = _greedy_groups(pieces, 10)
    assert _group_tokens(groups[-1]) == 1

    rebalanced = _rebalance_tail(groups, 10)
    assert _group_tokens(rebalanced[-1]) > 1
    assert all(_group_tokens(group) <= 10 for group in rebalanced)


def test_overlap_prefix_aligns_to_sentence_boundary():
    from app.tokenization import count_tokens

    text = "第一句。第二句。第三句。第四句。"
    # 不裁剪会返回“第三句。第四句。”；对齐边界后只要完整的最后一句。
    assert _overlap_prefix(text, 8) == "第四句。"
    assert text.endswith(_overlap_prefix(text, 4))
    assert count_tokens(_overlap_prefix(text, 4)) <= 4
    assert _overlap_prefix(text, 0) == ""


def test_max_chunks_guard():
    text = "句子。" * 200
    with pytest.raises(ValueError):
        chunk_blocks(parse(".txt", text.encode()), 64, 8, max_chunks=3)


def test_oversized_chunk_count_fails_clearly():
    with TestClient(create_app(settings(chunk_max_per_file=1))) as client:
        record = upload(client, "长文.txt", ("句子。" * 200).encode()).json()
        assert client.post(INDEX_URL).status_code == 202
        wait_index(client)
        status = chunk_columns(record["file_id"])
        assert status == []
    with sqlite3.connect("data/chat.sqlite3") as connection:
        row = connection.execute(
            "SELECT index_status, index_error FROM files WHERE file_id=?",
            (record["file_id"],),
        ).fetchone()
    assert row[0] == "failed"
    assert "上限" in row[1]


# --------------------------------------------------------------------------- 端到端


def test_markdown_structures_reach_chunks(client):
    body = (
        "# 手册\n\n"
        "## 安装\n\n"
        "- 第一步\n- 第二步\n\n"
        "| 名字 | 部门 |\n| --- | --- |\n| 张三 | 研发 |\n\n"
        "```python\nprint(1)\n```\n"
    ).encode()
    record = upload(client, "手册.md", body).json()
    assert client.post(INDEX_URL).status_code == 202
    wait_index(client)

    columns = chunk_columns(record["file_id"])
    by_kind = {row[3] for row in columns}
    assert {"list", "table", "code"} <= by_kind
    assert all(row[4] == 2 for row in columns)  # 都在“## 安装”层级下
    assert all(isinstance(row[5], int) for row in columns)  # source_order 已写入
    table = [row for row in columns if row[3] == "table"][0]
    assert table[1] == "名字: 张三; 部门: 研发"


# --------------------------------------------------------------------------- 迁移


def test_schema_migration_from_old_constraints():
    Path("data").mkdir(exist_ok=True)
    with sqlite3.connect("data/chat.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE files (file_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, "
            "name TEXT NOT NULL, extension TEXT NOT NULL, "
            "size INTEGER NOT NULL CHECK(size > 0 AND size <= 20971520), "
            "content_hash TEXT NOT NULL, storage_path TEXT NOT NULL UNIQUE, "
            "storage_backend TEXT NOT NULL, index_status TEXT NOT NULL CHECK(index_status IN "
            "('not_indexed','recorded','parsing','indexing','indexed','failed')), "
            "index_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL, UNIQUE(kb_id, idempotency_key))"
        )
        connection.execute(
            "CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, "
            "file_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, content TEXT NOT NULL, "
            "tokens INTEGER NOT NULL, heading TEXT, kind TEXT NOT NULL "
            "CHECK(kind IN ('text','table')), page INTEGER, created_at TEXT NOT NULL, "
            "UNIQUE(file_id, chunk_index))"
        )
        connection.execute(
            "INSERT INTO files VALUES "
            "('f','default','a.txt','.txt',10,'hash','default/f.txt','local','recorded',"
            "NULL,'t','t','key')"
        )
        connection.execute(
            "INSERT INTO chunks VALUES ('c','default','f',0,'内容',1,'标题','text',NULL,'t')"
        )
        connection.commit()

    with TestClient(create_app(settings())):
        pass

    with sqlite3.connect("data/chat.sqlite3") as connection:
        files_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='files'"
        ).fetchone()[0]
        chunks_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunks'"
        ).fetchone()[0]
        assert "20971520)" not in files_sql  # 上限已放宽
        assert "source_order" in chunks_sql and "heading_level" in chunks_sql
        # 旧数据保留。
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT chunk_id, content FROM chunks").fetchall() == [
            ("c", "内容")
        ]
        # 新 kind 现在可以写入。
        connection.execute(
            "INSERT INTO chunks (chunk_id, kb_id, file_id, chunk_index, content, tokens, "
            "heading, heading_level, kind, page, source_order, created_at) VALUES "
            "('c2','default','f',1,'x',1,NULL,NULL,'list',NULL,1,'t')"
        )
        connection.commit()
