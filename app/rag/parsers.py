"""解析层：把原始字节按格式提取成带结构的文本块。

只做文本产出，不做切片、向量化和检索。任何一种格式无法提取有效文本时
必须显式失败（ParseError），不得返回空结果伪装成功。

结构约定：
- ``kind`` 取值 text / title / table / list / code。标题不单独成块，只写入 ``heading``
  路径与 ``heading_level``，避免产生"只有标题"的空洞切片。
- ``heading`` 为层级路径（如 ``第三章 > 3.1 请假制度``），``heading_level`` 为当前
  最深层级，供切片与后续检索按层级组织上下文。
- ``order`` 为块在文档中的顺序，由 :func:`parse` 统一编号。
- 表格统一渲染为"表头: 值; 表头: 值"的整行文本，使每一行自带表头语义，跨块切开也不会丢列名。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from io import BytesIO

KINDS = frozenset({"text", "title", "table", "list", "code"})


class ParseError(Exception):
    """可预期的解析失败，例如扫描件无文本层、损坏文件、不支持的格式。"""


@dataclass(frozen=True)
class Block:
    text: str
    heading: str | None = None
    kind: str = "text"  # text | title | table | list | code
    page: int | None = None
    heading_level: int | None = None
    order: int = 0


@dataclass(frozen=True)
class ParseOptions:
    """解析开关；默认值偏保守，只删除几乎可以确定是噪声的内容。"""

    pdf_layout_mode: bool = True
    remove_header_footer: bool = True
    remove_toc: bool = True
    table_context_chars: int = 0


_TEXT_DECODINGS = ("utf-8-sig", "gb18030")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_MARKDOWN_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_MARKDOWN_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_MARKDOWN_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MARKDOWN_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")


def parse(extension: str, data: bytes, options: ParseOptions | None = None) -> list[Block]:
    """按扩展名分发解析；返回的 Block 顺序即文档顺序，并统一编号 ``order``。"""
    options = options or ParseOptions()
    blocks = _dispatch(extension, data, options)
    return [replace(block, order=index) for index, block in enumerate(blocks)]


def _dispatch(extension: str, data: bytes, options: ParseOptions) -> list[Block]:
    if extension == ".txt":
        return _parse_text(data)
    if extension == ".md":
        return _parse_markdown(data)
    if extension == ".pdf":
        return _parse_pdf(data, options)
    if extension == ".docx":
        return _parse_docx(data, options)
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


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


class _HeadingPath:
    """维护 Markdown/Word/PDF 标题栈，输出形如 '第三章 > 请假制度' 的路径。"""

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

    @property
    def level(self) -> int | None:
        return self._stack[-1][0] if self._stack else None


def _require_blocks(blocks: list[Block], message: str) -> list[Block]:
    if not any(block.text.strip() for block in blocks):
        raise ParseError(message)
    return blocks


# --------------------------------------------------------------------------- text


def _parse_text(data: bytes) -> list[Block]:
    text = _normalize_newlines(_decode(data))
    blocks = [Block(text=paragraph) for paragraph in _paragraphs(text)]
    return _require_blocks(blocks, "文本文件没有可提取内容。")


# --------------------------------------------------------------------------- markdown


def _parse_markdown(data: bytes) -> list[Block]:
    text = _normalize_newlines(_decode(data))
    lines = text.split("\n")
    blocks: list[Block] = []
    heading = _HeadingPath()
    buffer: list[str] = []
    index = 0

    def flush() -> None:
        content = "\n".join(buffer).strip()
        buffer.clear()
        if content:
            blocks.append(Block(text=content, heading=heading.path, heading_level=heading.level))

    while index < len(lines):
        line = lines[index]
        title = _MARKDOWN_HEADING.match(line)
        if title:
            flush()
            heading.push(len(title.group(1)), title.group(2))
            index += 1
            continue

        fence = _MARKDOWN_FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(1)[0]
            body = [line.strip()]
            index += 1
            while index < len(lines):
                body.append(lines[index].rstrip())
                # 闭合围栏必须和开头同字符且至少同长度，不能由代码里的短围栏关闭。
                closed = bool(
                    re.fullmatch(
                        rf"\s*{re.escape(marker)}{{{len(fence.group(1))},}}\s*",
                        lines[index],
                    )
                )
                index += 1
                if closed:
                    break
            blocks.append(
                Block(
                    text="\n".join(body).strip(),
                    heading=heading.path,
                    kind="code",
                    heading_level=heading.level,
                )
            )
            continue

        if _is_markdown_table_start(lines, index):
            flush()
            table_lines: list[str] = []
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            content = _markdown_table_text(table_lines)
            if content:
                blocks.append(
                    Block(
                        text=content,
                        heading=heading.path,
                        kind="table",
                        heading_level=heading.level,
                    )
                )
            continue

        if _MARKDOWN_LIST.match(line):
            flush()
            items: list[str] = []
            while index < len(lines):
                current = lines[index]
                if _MARKDOWN_LIST.match(current):
                    items.append(current.strip())
                    index += 1
                elif current.strip() and current.startswith(("  ", "\t")):
                    items.append(current.strip())
                    index += 1
                else:
                    break
            blocks.append(
                Block(
                    text="\n".join(items),
                    heading=heading.path,
                    kind="list",
                    heading_level=heading.level,
                )
            )
            continue

        stripped = line.strip()
        if stripped and not _MARKDOWN_RULE.match(stripped):
            if stripped.startswith(">"):
                stripped = stripped[1:].strip()
            if stripped:
                buffer.append(stripped)
            index += 1
            continue

        flush()
        index += 1

    flush()
    return _require_blocks(blocks, "Markdown 文件没有可提取内容。")


def _is_markdown_table_start(lines: list[str], index: int) -> bool:
    if "|" not in lines[index]:
        return False
    return index + 1 < len(lines) and bool(_MARKDOWN_TABLE_SEP.match(lines[index + 1]))


def _markdown_table_text(lines: list[str]) -> str:
    rows: list[list[str]] = []
    for line in lines:
        if _MARKDOWN_TABLE_SEP.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return _table_text_from_cells(rows)


# --------------------------------------------------------------------------- pdf


def _parse_pdf(data: bytes, options: ParseOptions) -> list[Block]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        pages = [_extract_pdf_page(page, options) for page in reader.pages]
    except ParseError:
        raise
    except Exception as exc:  # pypdf 对损坏文件抛多种异常
        raise ParseError("PDF 文件无法解析，可能已损坏。") from exc

    if not any(page.strip() for page in pages):
        raise ParseError("PDF 没有可提取的文本层，可能是扫描件。")

    lines = [[_clean_line(line) for line in page.split("\n")] for page in pages]
    if options.remove_header_footer:
        lines = _strip_running_lines(lines)

    heading = _HeadingPath()
    blocks: list[Block] = []
    for page_number, page_lines in enumerate(lines, start=1):
        if options.remove_toc and _looks_like_toc(page_lines):
            continue
        blocks.extend(_pdf_page_blocks(page_lines, page_number, heading))
    return _require_blocks(blocks, "PDF 没有可提取的文本层，可能是扫描件。")


def _extract_pdf_page(page, options: ParseOptions) -> str:
    if options.pdf_layout_mode:
        try:
            return page.extract_text(extraction_mode="layout") or ""
        except Exception:
            # 空白页等场景 layout 模式会报错（KeyError '/Contents'），回退默认提取；
            # 真正损坏的 PDF 会在下面的默认提取中抛出并被上层统一处理。
            pass
    return page.extract_text() or ""


def _clean_line(line: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", line).strip()


_RUNNING_MAX_EDGE_LINES = 2
_PAGE_NUMBER = re.compile(r"^(?:第\s*\d+\s*页|page\s*\d+(?:\s*/\s*\d+)?|\d{1,4})$", re.IGNORECASE)


def _strip_running_lines(pages: list[list[str]]) -> list[list[str]]:
    """删除重复出现的页眉/页脚与页码，避免进入向量。

    只有当同一行出现在 >=60% 页面（至少 3 页文档）时才判定为页眉页脚；页码行单独识别。
    结果不会把整页删空（删空则保留原样）。
    """
    if len(pages) < 3:
        return [[line for line in page if line] for page in pages]

    top: Counter[str] = Counter()
    bottom: Counter[str] = Counter()
    for page in pages:
        nonempty = [line for line in page if line]
        for line in nonempty[:_RUNNING_MAX_EDGE_LINES]:
            top[line] += 1
        for line in nonempty[-_RUNNING_MAX_EDGE_LINES:]:
            bottom[line] += 1

    threshold = max(2, int(len(pages) * 0.6))
    drop_top = {line for line, count in top.items() if count >= threshold}
    drop_bottom = {line for line, count in bottom.items() if count >= threshold}

    result: list[list[str]] = []
    for page in pages:
        nonempty = [line for line in page if line]
        original = list(nonempty)
        while nonempty and (nonempty[0] in drop_top or _PAGE_NUMBER.match(nonempty[0])):
            nonempty.pop(0)
        while nonempty and (nonempty[-1] in drop_bottom or _PAGE_NUMBER.match(nonempty[-1])):
            nonempty.pop()
        result.append(nonempty or original)
    return result


_TOC_LEADER = re.compile(r"[.·…]{3,}\s*\d+\s*$")
_TOC_HEADING = re.compile(r"^(?:目\s*录|contents|table\s+of\s+contents)\s*$", re.IGNORECASE)


def _looks_like_toc(lines: list[str]) -> bool:
    nonempty = [line for line in lines if line]
    if not nonempty:
        return False
    leaders = sum(1 for line in nonempty if _TOC_LEADER.search(line))
    if _TOC_HEADING.match(nonempty[0]) and leaders >= 2:
        return True
    return leaders >= max(4, len(nonempty) * 0.5)


_PDF_SEP = r"[\s、.．:：]*"
_PDF_NUMBERED = re.compile(rf"^(\d+(?:\.\d+)+)\s*{_PDF_SEP}(.*)$")
_PDF_CHAPTER = re.compile(
    rf"^第\s*([一二三四五六七八九十百千零〇\d]+)\s*([章节篇部]){_PDF_SEP}(.*)$"
)
_PDF_APPENDIX = re.compile(rf"^(附\s*录\s*[A-Za-z\d]*|appendix\s+[A-Za-z\d]+){_PDF_SEP}(.*)$")
_PDF_CAPS = re.compile(r"^[A-Z][A-Z0-9 _\-]{2,60}$")
_PDF_HEADING_MAX_CHARS = 40
_PDF_SENTENCE_END = "。！？!?；;，,、"


def _pdf_heading(line: str) -> tuple[int, str]:
    """保守识别 PDF 标题：长行、句末标点、单级编号都不算，避免误伤正文与列表。"""
    if not line or len(line) > _PDF_HEADING_MAX_CHARS:
        return 0, ""
    if line.endswith(tuple(_PDF_SENTENCE_END)):
        return 0, ""

    chapter = _PDF_CHAPTER.match(line)
    if chapter:
        return (1 if chapter.group(2) in ("章", "篇", "部") else 2), line
    if _PDF_APPENDIX.match(line):
        return 1, line
    numbered = _PDF_NUMBERED.match(line)
    if numbered:
        return numbered.group(1).count(".") + 1, line
    if _PDF_CAPS.match(line):
        return 2, line
    return 0, ""


def _pdf_page_blocks(lines: list[str], page_number: int, heading: _HeadingPath) -> list[Block]:
    blocks: list[Block] = []
    buffer: list[str] = []

    def flush() -> None:
        content = " ".join(buffer).strip()
        buffer.clear()
        if content:
            blocks.append(
                Block(
                    text=content,
                    heading=heading.path,
                    page=page_number,
                    heading_level=heading.level,
                )
            )

    for line in lines:
        if not line:
            flush()
            continue
        level, _title = _pdf_heading(line)
        if level:
            flush()
            heading.push(level, line)
            continue
        buffer.append(line)
    flush()
    return blocks


# --------------------------------------------------------------------------- docx


def _parse_docx(data: bytes, options: ParseOptions) -> list[Block]:
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
    list_buffer: list[str] = []
    last_text = ""

    def flush_list() -> None:
        if list_buffer:
            blocks.append(
                Block(
                    text="\n".join(list_buffer),
                    heading=heading.path,
                    kind="list",
                    heading_level=heading.level,
                )
            )
            list_buffer.clear()

    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            level = _docx_heading_level(item.style.name if item.style else "")
            if level:
                flush_list()
                heading.push(level, text)
                last_text = ""
                continue
            if _docx_is_list(item):
                list_buffer.append(text)
                last_text = ""
                continue
            flush_list()
            blocks.append(Block(text=text, heading=heading.path, heading_level=heading.level))
            last_text = text
        elif isinstance(item, Table):
            flush_list()
            content = _table_text_from_cells(_table_cells(item.rows))
            if not content:
                continue
            if options.table_context_chars > 0 and last_text:
                context = last_text[: options.table_context_chars].strip()
                if context:
                    content = f"{context}\n{content}"
            blocks.append(
                Block(
                    text=content,
                    heading=heading.path,
                    kind="table",
                    heading_level=heading.level,
                )
            )
            last_text = ""

    flush_list()
    return _require_blocks(blocks, "Word 文件没有可提取内容。")


def _docx_heading_level(style_name: str) -> int:
    match = re.search(r"(\d+)\s*$", style_name)
    if style_name.lower().startswith("heading") and match:
        return int(match.group(1))
    if ("标题" in style_name or "標題" in style_name) and match:
        return int(match.group(1))
    return 0


def _docx_is_list(paragraph) -> bool:
    style = paragraph.style.name if paragraph.style else ""
    lowered = style.lower()
    if "list" in lowered or "列表" in style:
        return True
    properties = paragraph._p.pPr
    return properties is not None and properties.numPr is not None


def _table_cells(rows) -> list[list[str]]:
    """按底层 XML 单元格身份消除横向合并占位，保留独立单元格中的相同值。

    每行单独去重，使纵向合并的值仍能为各数据行提供上下文。
    """
    result: list[list[str]] = []
    for row in rows:
        seen = set()
        values: list[str] = []
        for cell in row.cells:
            values.append("" if cell._tc in seen else cell.text.strip())
            seen.add(cell._tc)
        result.append(values)
    return result


def _table_text_from_cells(cells: list[list[str]]) -> str:
    """把表格转成文本，表头拼进每一行，避免值失去语义。"""
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


# --------------------------------------------------------------------------- xlsx


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
        text = _table_text_from_cells(rows)
        if text:
            blocks.append(Block(text=text, heading=sheet.title, kind="table", heading_level=1))
    return _require_blocks(blocks, "Excel 文件没有可提取内容。")
