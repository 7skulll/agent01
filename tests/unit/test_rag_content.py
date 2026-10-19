"""表格保真与检索去重回归，不调用数据库或外部模型。"""

from io import BytesIO

import pytest

from app.rag.chunking import chunk_blocks
from app.rag.parsers import _table_text_from_cells, parse
from app.rag.retrieval import RetrievalService, Source


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ([["项目", ""], ["预算", "5000元"]], "项目: 预算; 第2列: 5000元"),
        ([["项目", " \t"], ["预算", "5000元"]], "项目: 预算; 第2列: 5000元"),
        ([["项目"], ["预算", "5000元", "已批准"]], "项目: 预算; 第2列: 5000元; 第3列: 已批准"),
        ([["", ""], ["预算", "5000元"]], "第1列: 预算; 第2列: 5000元"),
        ([["项目", "金额"], ["预算", ""]], "项目: 预算"),
        ([["项目", "金额"], ["预算"]], "项目: 预算"),
        ([["项目", "金额"]], "项目 | 金额"),
        ([], ""),
    ],
)
def test_table_keeps_values_without_headers(cells, expected):
    assert _table_text_from_cells(cells) == expected


@pytest.mark.parametrize("extension", [".md", ".docx", ".xlsx"])
def test_blank_header_values_survive_parsing_and_chunking(extension):
    if extension == ".md":
        body = "|项目||\n|---|---|\n|预算|5000元|".encode()
    elif extension == ".docx":
        from docx import Document

        document = Document()
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(1, 0).text = "预算"
        table.cell(1, 1).text = "5000元"
        buffer = BytesIO()
        document.save(buffer)
        body = buffer.getvalue()
    else:
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active.append(["项目", None])
        workbook.active.append(["预算", "5000元"])
        buffer = BytesIO()
        workbook.save(buffer)
        workbook.close()
        body = buffer.getvalue()

    blocks = parse(extension, body)
    assert len(blocks) == 1
    assert blocks[0].text == "项目: 预算; 第2列: 5000元"
    chunks = chunk_blocks(blocks, size=64, overlap=8)
    assert [chunk.content for chunk in chunks] == [blocks[0].text]
    assert chunks[0].kind == "table"


def source(index, content, *, heading=None, kind="text"):
    return Source(
        citation_id=0,
        chunk_id=str(index),
        file_id=f"file-{index}",
        name=f"file-{index}.md",
        content=content,
        heading=heading,
        page=index,
        kind=kind,
        score=1 / index,
    )


@pytest.mark.parametrize(
    ("first", "second", "kind"),
    [
        ("价格为1.5万元", "价格为15万元", "text"),
        ("型号A-B", "型号AB", "text"),
        ("x >= 10", "x > 10", "code"),
        ("if ready:\n    run()", "if ready:\n        run()", "code"),
        ("    run()", "run()", "code"),
        ("a b", "ab", "text"),
        ("甲\n乙", "甲乙", "text"),
        ("字段: A-B", "字段: AB", "table"),
        ("- 第一条", "第一条", "list"),
    ],
)
def test_dedupe_preserves_meaningful_characters(first, second, kind):
    sources = [source(1, first, kind=kind), source(2, second, kind=kind)]
    assert RetrievalService._dedupe(sources) == sources
    assert all(not item.alternatives for item in sources)


def test_dedupe_keeps_heading_and_content_boundaries_and_kind():
    sources = [
        source(1, "BC", heading="A"),
        source(2, "C", heading="AB"),
        source(3, "BC", heading="A", kind="code"),
    ]
    assert RetrievalService._dedupe(sources) == sources


@pytest.mark.parametrize("kind", ["text", "table", "list", "code"])
def test_dedupe_normalizes_newlines_and_preserves_alternative_sources(kind):
    first = source(1, "甲\n乙", heading="制度", kind=kind)
    second = source(2, "甲\r\n乙", heading="制度", kind=kind)
    third = source(3, "甲\r乙", heading="制度", kind=kind)
    result = RetrievalService._dedupe([first, second, third])
    assert result == [first]
    assert first.content == "甲\n乙"
    assert first.score == 1
    assert first.alternatives == [
        {
            "chunk_id": item.chunk_id,
            "file_id": item.file_id,
            "name": item.name,
            "heading": item.heading,
            "page": item.page,
        }
        for item in (second, third)
    ]


def test_dedupe_ignores_only_outer_whitespace_in_non_code_content():
    first = source(1, "年假需要审批。")
    second = source(2, " \n年假需要审批。\t")
    assert RetrievalService._dedupe([first, second]) == [first]
    assert first.alternatives[0]["chunk_id"] == "2"
