"""切片层：先按结构（标题/表格）分组，再按 token 递归切分并加重叠。

不依赖外部 tokenizer，用确定性估算避免离线/网络差异；误差对切片边界不敏感。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.rag.parsers import Block

# 递归切分优先级：段落 -> 换行 -> 中文句末 -> 英文句末 -> 分号 -> 逗号 -> 空格 -> 兜底
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " ", "")

# 估算 token 数的 CJK 区段（含中日韩文字、假名、谚文、全角与 CJK 标点）。
_CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


@dataclass(frozen=True)
class Chunk:
    content: str
    tokens: int
    heading: str | None
    kind: str
    page: int | None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    return cjk + math.ceil((len(text) - cjk) / 4)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def chunk_blocks(blocks: list[Block], size: int, overlap: int) -> list[Chunk]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    overlap = max(0, min(overlap, size - 1))
    chunks: list[Chunk] = []
    for heading, kind, page, text in _group(blocks):
        for piece in split_text(text, size, overlap):
            chunks.append(Chunk(piece, count_tokens(piece), heading, kind, page))
    if not chunks:
        raise ValueError("no chunkable content")
    return chunks


def _group(blocks: list[Block]) -> list[tuple[str | None, str, int | None, str]]:
    """把相邻且结构一致的块并成一段，供后续整体切分。"""
    groups: list[tuple[str | None, str, int | None, list[str]]] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        key = (block.heading, block.kind, block.page)
        if groups and (groups[-1][0], groups[-1][1], groups[-1][2]) == key:
            groups[-1][3].append(text)
        else:
            groups.append((block.heading, block.kind, block.page, [text]))
    return [(h, k, p, "\n\n".join(parts)) for h, k, p, parts in groups]


def split_text(text: str, size: int, overlap: int) -> list[str]:
    pieces = _split_recursive(text, size)
    return _merge(pieces, size, overlap)


def _split_recursive(text: str, size: int) -> list[str]:
    if count_tokens(text) <= size:
        return [text]
    for index, separator in enumerate(_SEPARATORS):
        if separator == "":
            break
        parts = _split_keep_separator(text, separator)
        if len(parts) == 1:
            continue
        result: list[str] = []
        for part in parts:
            if count_tokens(part) <= size:
                result.append(part)
            else:
                result.extend(_split_recursive(part, size))
        return result
    return _hard_split(text, size)


def _split_keep_separator(text: str, separator: str) -> list[str]:
    parts = text.split(separator)
    result = [part + separator for part in parts[:-1]]
    if parts[-1]:
        result.append(parts[-1])
    return [part for part in result if part]


def _hard_split(text: str, size: int) -> list[str]:
    """最后手段：按字符硬切，保证每片不超过 size 个估算 token。

    单遍扫描并增量维护 token 计数（O(n)），避免对每个字符重算整段 token 导致 O(n^2)。
    """
    result: list[str] = []
    start = 0
    cjk = other = 0
    for index, char in enumerate(text):
        char_is_cjk = _is_cjk(char)
        next_cjk = cjk + (1 if char_is_cjk else 0)
        next_other = other + (0 if char_is_cjk else 1)
        if start < index and next_cjk + math.ceil(next_other / 4) > size:
            result.append(text[start:index])
            start = index
            cjk = 1 if char_is_cjk else 0
            other = 0 if char_is_cjk else 1
        else:
            cjk, other = next_cjk, next_other
    if start < len(text):
        result.append(text[start:])
    return result


def _merge(pieces: list[str], size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    current_tokens = 0
    for piece in pieces:
        piece_tokens = count_tokens(piece)
        if current and current_tokens + piece_tokens > size:
            chunks.append(current)
            current = _tail(current, overlap) + piece
            current_tokens = count_tokens(current)
            if current_tokens > size:
                # 本片自身已接近上限，重叠会突破 size；放弃重叠并只保留本片。
                current, current_tokens = piece, piece_tokens
        else:
            current += piece
            current_tokens += piece_tokens
    if current.strip():
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _tail(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    total = 0
    for index in range(len(text) - 1, -1, -1):
        total += count_tokens(text[index])
        if total > max_tokens:
            return text[index + 1 :]
    return text
