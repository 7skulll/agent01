"""解析层：把原始字节按格式提取成带结构的文本块。

只做文本产出，不做切片、向量化和检索。任何一种格式无法提取有效文本时
必须显式失败（ParseError），不得返回空结果伪装成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO


class ParseError(Exception):
    """可预期的解析失败，例如扫描件无文本层、损坏文件、不支持的格式。"""


@dataclass(frozen=True)
class Block:
    text: str
    heading: str | None = None
    kind: str = "text"  # text | table
    page: int | None = None


_TEXT_DECODINGS = ("utf-8-sig", "gb18030")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def parse(extension: str, data: bytes) -> list[Block]:
    """按扩展名分发解析；返回的 Block 顺序即文档顺序。"""
    if extension == ".txt":
        return _parse_text(data)
    if extension == ".md":
        return _parse_markdown(data)
    if extension == ".pdf":
        return _parse_pdf(data)
    if extension == ".docx":
        return _parse_docx(data)
    if extension == ".xlsx":
        return _parse_xlsx(data)
    if extension in (".doc", ".xls"):
        raise ParseError(f"暂不支持 {extension} 旧格式，请先转换为 .docx 或 .xlsx。")
    raise ParseError(f"不支持的扩展名：{extension}")


def _decode(data: bytes) -> str:
    for encoding in _TEXT_DECODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("无法按 UTF-8 或 GB18030 解码文本文件。")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


class _HeadingPath:
    """维护 Markdown/Word 标题栈，输出形如 '第三章 > 请假制度' 的路径。"""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> str:
        level = max(level, 1)
        self._stack = [item for item in self._stack if item[0] < level]
        self._stack.append((level, title))
        return self.path

    @property
    def path(self) -> str | None:
        return " > ".join(title for _, title in self._stack) or None


def _parse_text(data: bytes) -> list[Block]:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks = [Block(text=p) for p in _paragraphs(text)]
    return _require_blocks(blocks, "文本文件没有可提取内容。")


def _parse_markdown(data: bytes) -> list[Block]:
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    heading = _HeadingPath()
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(Block(text=text, heading=heading.path))
        buffer.clear()

    for line in text.split("\n"):
        match = _MARKDOWN_HEADING.match(line)
        if match:
            flush()
            heading.push(len(match.group(1)), match.group(2))
        elif line.strip():
            buffer.append(line.strip())
        elif buffer:
            buffer.append("")
    flush()
    return _require_blocks(blocks, "Markdown 文件没有可提取内容。")


def _parse_pdf(data: bytes) -> list[Block]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        blocks = [
            Block(text=text.strip(), page=index + 1)
            for index, page in enumerate(reader.pages)
            if (text := page.extract_text() or "").strip()
        ]
    except ParseError:
        raise
    except Exception as exc:  # pypdf 对损坏文件抛多种异常
        raise ParseError("PDF 文件无法解析，可能已损坏。") from exc
    return _require_blocks(blocks, "PDF 没有可提取的文本层，可能是扫描件。")


def _parse_docx(data: bytes) -> list[Block]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(BytesIO(data))
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("Word 文件无法解析，可能已损坏。") from exc

    blocks: list[Block] = []
    heading = _HeadingPath()
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            level = _docx_heading_level(item.style.name if item.style else "")
            if level:
                heading.push(level, text)
                continue
            blocks.append(Block(text=text, heading=heading.path))
        elif isinstance(item, Table):
            blocks.append(Block(text=_table_text(item.rows), heading=heading.path, kind="table"))
    return _require_blocks(blocks, "Word 文件没有可提取内容。")


def _docx_heading_level(style_name: str) -> int:
    match = re.search(r"(\d+)\s*$", style_name)
    if style_name.lower().startswith("heading") and match:
        return int(match.group(1))
    if ("标题" in style_name or "標題" in style_name) and match:
        return int(match.group(1))
    return 0


def _table_text(rows) -> str:
    """把表格转成文本，表头拼进每一行，避免值失去语义。"""
    cells = [[cell.text.strip() for cell in row.cells] for row in rows]
    if not cells:
        return ""
    header = cells[0]
    lines: list[str] = []
    for row in cells[1:]:
        pairs = [
            f"{header[i]}: {value}"
            for i, value in enumerate(row)
            if value and i < len(header) and header[i]
        ]
        if pairs:
            lines.append("; ".join(pairs))
    if not lines and any(header):
        lines = [" | ".join(value for value in header if value)]
    return "\n".join(lines)


def _parse_xlsx(data: bytes) -> list[Block]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("Excel 文件无法解析，可能已损坏。") from exc

    blocks: list[Block] = []
    for sheet in workbook.worksheets:
        rows = [
            ["" if value is None else str(value).strip() for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        header = rows[0]
        lines: list[str] = []
        for row in rows[1:]:
            pairs = [
                f"{header[i]}: {value}"
                for i, value in enumerate(row)
                if value and i < len(header) and header[i]
            ]
            if pairs:
                lines.append("; ".join(pairs))
        if not lines:
            lines = [" | ".join(value for value in header if value)]
        text = "\n".join(line for line in lines if line)
        if text:
            blocks.append(Block(text=text, heading=sheet.title, kind="table"))
    return _require_blocks(blocks, "Excel 文件没有可提取内容。")


def _require_blocks(blocks: list[Block], message: str) -> list[Block]:
    if not any(block.text.strip() for block in blocks):
        raise ParseError(message)
    return blocks
