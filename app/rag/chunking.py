"""切片层：先按结构（标题/表格/列表/代码）分组，再按 token 递归切分并加重叠。

使用显式传入的离线编码器；结构装饰、重叠和最终 Embedding 输入均复检。

借鉴 FastGPT / RAGFlow 的三个关键改进：
- 预留重叠预算：分组时按 ``size - overlap`` 封装，并在递归切分阶段使用相同主体预算
  （RAGFlow ``threshold = token_size * (100 - overlap%) / 100`` 的同一思路）。
- 消除末尾碎片：尾块过小时优先并入上一块，或从上一块借若干分段过来
  （FastGPT ``minChunkLen`` / 尾块回并）。
- 重叠对齐语义边界：重叠前缀尽量从整句/整段开头截取，而不是从词中间开始。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from app.rag.parsers import Block
from app.tokenization import DEFAULT_COUNTER, TokenCounter, embedding_text
from app.tokenization import count_tokens as count_tokens

# 递归切分优先级：段落 -> 换行 -> 中文句末 -> 英文句末 -> 分号 -> 逗号 -> 空格 -> 兜底
_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " ", "")

# 重叠前缀优先在这个集合里对齐到边界，避免从词或句子中间开始。
_OVERLAP_BOUNDARIES = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")

# 尾块小于目标的该比例时，尝试回并或借用相邻分段，避免产生低信息量碎片。
_MIN_CHUNK_RATIO = 0.25


@dataclass(frozen=True)
class Chunk:
    content: str
    tokens: int
    heading: str | None
    kind: str
    page: int | None
    heading_level: int | None = None
    order: int = 0


# 表格/列表/代码按行原子切分，不加字符重叠，避免把整行拆开或开头带半行。
_STRUCTURED_KINDS = frozenset({"table", "list", "code"})


def chunk_blocks(
    blocks: list[Block],
    size: int,
    overlap: int,
    max_chunks: int | None = None,
    counter: TokenCounter = DEFAULT_COUNTER,
    name: str = "",
    embedding_limit: int = 0,
) -> list[Chunk]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    overlap = max(0, min(overlap, size - 1))
    chunks: list[Chunk] = []
    for group in _group(blocks):
        remaining = None if max_chunks is None else max_chunks - len(chunks)
        cap = size
        if embedding_limit:
            prefix = embedding_text({"name": name, "heading": group.heading, "content": ""})
            cap = min(cap, embedding_limit - counter.count(prefix) - 1)
            if cap <= 0:
                raise ValueError("文件名或标题超过 Embedding 输入预算，请缩短标题或调整上限。")
        while True:
            if group.kind in _STRUCTURED_KINDS:
                structured = _split_structured(group.text, cap, group.kind, counter)
                pieces = list(
                    structured if remaining is None else islice(structured, remaining + 1)
                )
                if remaining is not None and len(pieces) > remaining:
                    raise ValueError("切片数量超过上限，请拆分文件或增大切块大小。")
            else:
                # Embedding 预算缩小切片时，按用户指定比例缩小 overlap；
                # 没有预算收缩时保留原始配置，不额外限制为 25%。
                effective_overlap = overlap * cap // size
                pieces = split_text(group.text, cap, effective_overlap, remaining, counter)
            overflow = (
                max(
                    (
                        counter.count(
                            embedding_text(
                                {"name": name, "heading": group.heading, "content": piece}
                            )
                        )
                        - embedding_limit
                        for piece in pieces
                    ),
                    default=0,
                )
                if embedding_limit
                else 0
            )
            if overflow <= 0:
                break
            cap -= max(1, overflow)
            if cap <= 0:
                raise ValueError("切片无法容纳于 Embedding 输入预算。")
        for piece in pieces:
            if max_chunks is not None and len(chunks) >= max_chunks:
                raise ValueError(f"切片数量超过上限 {max_chunks}，请拆分文件或增大切块大小。")
            chunks.append(
                Chunk(
                    piece,
                    counter.count(piece),
                    group.heading,
                    group.kind,
                    group.page,
                    group.heading_level,
                    group.order,
                )
            )
    if not chunks:
        raise ValueError("no chunkable content")
    return chunks


@dataclass
class _BlockGroup:
    text: str
    heading: str | None
    heading_level: int | None
    kind: str
    page: int | None
    order: int


def _group(blocks: list[Block]) -> Iterator[_BlockGroup]:
    """只合并相邻的同标题、同页正文；独立表格和代码块保留原有边界。"""
    current: list[Block] = []

    def build() -> _BlockGroup:
        first = current[0]
        return _BlockGroup(
            "\n\n".join(block.text for block in current),
            first.heading,
            first.heading_level,
            first.kind,
            first.page,
            first.order,
        )

    for block in blocks:
        if not block.text.strip():
            continue
        if current:
            first = current[0]
            same = block.kind == first.kind == "text" and (
                block.heading,
                block.heading_level,
                block.page,
            ) == (first.heading, first.heading_level, first.page)
            if not same:
                yield build()
                current = []
        current.append(block)
    if current:
        yield build()


def split_text(
    text: str,
    size: int,
    overlap: int,
    max_chunks: int | None = None,
    counter: TokenCounter = DEFAULT_COUNTER,
) -> list[str]:
    """正文按主体预算切分，保留分隔符，再加来自前一主体的后缀重叠。

    小于完整上限的正文直接保留；数量限额在递归产出分段时检查，避免巨量中间列表。
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not text.strip():
        return []
    if max_chunks is not None and max_chunks < 1:
        raise ValueError("切片数量超过上限，请拆分文件或增大切块大小。")
    if counter.count(text) <= size:
        return [text]
    overlap = max(0, min(overlap, size - 1))
    cap = size - overlap
    # 流式合并递归片段；超出限额时不为整篇文件分配中间切片列表。
    groups = _greedy_groups(
        _split_recursive(text, cap, counter=counter),
        cap,
        counter,
        max_groups=None if max_chunks is None else max_chunks + 1,
    )
    groups = _rebalance_tail(groups, cap, counter)
    result = _apply_overlap([_join(group) for group in groups], size, overlap, counter)
    if max_chunks is not None and len(result) > max_chunks:
        raise ValueError(f"切片数量超过上限 {max_chunks}，请拆分文件或增大切块大小。")
    return result


def _split_recursive(
    text: str, size: int, depth: int = 0, counter=DEFAULT_COUNTER
) -> Iterator[str]:
    if counter.count(text) <= size:
        yield text
        return
    for index in range(depth, len(_SEPARATORS) - 1):
        separator = _SEPARATORS[index]
        position = text.find(separator)
        if position < 0 or position + len(separator) == len(text):
            continue
        for part in _split_keep_separator(text, separator):
            yield from _split_recursive(part, size, index + 1, counter)
        return
    yield from _hard_split(text, size, counter)


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})[^\n]*$")


def _split_structured(text: str, size: int, kind: str, counter=DEFAULT_COUNTER) -> Iterator[str]:
    """按完整行封装结构内容；代码每片补齐围栏，超长原子行明确失败。

    不用普通正文的标点/空格切分，以免丢失表头、列表语义或代码缩进。
    """
    lines = text.splitlines(keepends=True)
    opening = closing = ""
    if kind == "code" and lines:
        fence = _FENCE.match(lines[0].rstrip("\r\n"))
        if fence:
            opening = lines.pop(0).rstrip("\r\n") + "\n"
            closing = "\n" + fence.group(1)
            if lines:
                end = lines[-1].strip()
                marker = fence.group(1)
                if len(end) >= len(marker) and set(end) == {marker[0]}:
                    lines.pop()
    current = ""
    for line in lines:
        candidate = current + line
        if counter.count(opening + candidate + closing) <= size:
            current = candidate
            continue
        if current:
            yield opening + current + closing
            current = ""
        if counter.count(opening + line + closing) > size:
            raise ValueError(f"{kind} 单行内容超过切片预算 {size}，请拆分长行或增大切块大小。")
        current = line
    if current or opening:
        result = opening + current + closing
        if counter.count(result) > size:
            raise ValueError(f"{kind} 围栏超过切片预算 {size}。")
        yield result


def _split_keep_separator(text: str, separator: str) -> Iterator[str]:
    start = 0
    while (position := text.find(separator, start)) >= 0:
        end = position + len(separator)
        yield text[start:end]
        start = end
    if start < len(text):
        yield text[start:]


def _hard_split(text: str, size: int, counter=DEFAULT_COUNTER) -> Iterator[str]:
    """Unicode 字符边界硬切；只对局部候选编码，保证无丢字且指针前进。"""
    start = 0
    while start < len(text):
        # 限制探测文本长度，避免长文每次复制整个剩余后缀。
        window = text[start : start + max(32, size * 32)]
        piece = counter.take(window, size)
        if not piece:
            raise ValueError("单个 Unicode 字符超过切片预算，请增大切片大小。")
        yield piece
        start += len(piece)


# --------------------------------------------------------------------------- 合并


def _group_tokens(pieces: list[str], counter=DEFAULT_COUNTER) -> int:
    return counter.count("".join(pieces))


def _join(pieces: list[str]) -> str:
    return "".join(pieces)


def _merge(pieces: list[str], size: int, overlap: int, counter=DEFAULT_COUNTER) -> list[str]:
    """把递归切出的片段合并成块：分组 -> 消除尾块 -> 加预留重叠前缀。"""
    overlap = max(0, min(overlap, size - 1))
    # 分组时预留重叠预算，保证前缀 + 主体 <= size。
    core_target = max(1, size - overlap)
    groups = _rebalance_tail(_greedy_groups(pieces, core_target, counter), core_target, counter)
    texts = [joined for joined in (_join(group) for group in groups) if joined]
    return _apply_overlap(texts, size, overlap, counter)


def _greedy_groups(pieces, cap: int, counter=DEFAULT_COUNTER, max_groups=None) -> list[list[str]]:
    """直接编码拼接后的正文，不假定 BPE token 数可以逐段相加。"""
    groups, current = [], []
    for piece in pieces:
        if current and counter.count("".join([*current, piece])) > cap:
            groups.append(current)
            if max_groups is not None and len(groups) >= max_groups:
                raise ValueError("切片数量超过上限，请拆分文件或增大切块大小。")
            current = []
        current.append(piece)
    if current:
        groups.append(current)
    return groups


def _rebalance_tail(groups: list[list[str]], cap: int, counter=DEFAULT_COUNTER) -> list[list[str]]:
    """消除末尾碎片：能整体并入上一块就并入，否则从上一块尾部借若干分段。"""
    if len(groups) < 2:
        return groups
    min_size = max(1, int(cap * _MIN_CHUNK_RATIO))
    last = groups[-1]
    if _group_tokens(last, counter) >= min_size:
        return groups

    previous = groups[-2]
    if _group_tokens(previous + last, counter) <= cap:
        groups[-2] = previous + last
        groups.pop()
        return groups

    moved: list[str] = []
    last_tokens = _group_tokens(last, counter)
    while len(previous) > 1 and last_tokens < min_size:
        candidate = previous[-1]
        # 借用后上一块不能变成新的碎片，尾块也不能超过上限。
        if (
            _group_tokens(previous[:-1], counter) < min_size
            or _group_tokens([candidate, *moved, *last], counter) > cap
        ):
            break
        previous.pop()
        moved.insert(0, candidate)
        last_tokens = _group_tokens(moved + last, counter)
    if moved:
        groups[-1] = moved + last
    return groups


def _apply_overlap(texts: list[str], size: int, overlap: int, counter=DEFAULT_COUNTER) -> list[str]:
    """给每块加上一块的尾部作为前缀；预算按剩余空间算，保证不丢重叠也不超上限。"""
    if overlap <= 0 or len(texts) < 2:
        return texts
    result = [texts[0]]
    for text in texts[1:]:
        budget = min(overlap, max(0, size - counter.count(text)))
        prefix = _overlap_prefix(texts[len(result) - 1], budget, counter)
        if prefix and counter.count(prefix + text) > size:
            prefix = counter.take(prefix, size, suffix=True, format_text=lambda p: p + text)
        result.append(prefix + text)
    return result


def _overlap_prefix(text: str, max_tokens: int, counter=DEFAULT_COUNTER) -> str:
    """取 ``text`` 末尾不超过 ``max_tokens`` 的内容作为重叠前缀，尽量对齐到句子/段落边界。"""
    if max_tokens <= 0:
        return ""
    raw = _tail(text, max_tokens, counter)
    if not raw:
        return ""
    boundaries = [(raw.find(separator), len(separator)) for separator in _OVERLAP_BOUNDARIES]
    boundaries = [(index, length) for index, length in boundaries if index != -1]
    if boundaries:
        index, length = min(boundaries)
        candidate = raw[index + length :]
        # 只有裁剪后仍保留足够上下文时才对齐边界，避免把重叠削得过短。
        if candidate.strip() and max(1, max_tokens // 2) <= counter.count(candidate) <= max_tokens:
            return candidate
    # 英文优先从空白边界后开始，保留末尾分隔符，避免形成粘连词。
    start = len(text) - len(raw)
    if start and text[start - 1].isalnum() and raw[0].isascii() and raw[0].isalnum():
        match = re.search(r"\s+", raw)
        if match and raw[match.end() :].strip():
            candidate = raw[match.end() :]
            if counter.count(candidate) <= max_tokens:
                return candidate
    return raw


def _tail(text: str, max_tokens: int, counter=DEFAULT_COUNTER) -> str:
    return counter.take(text, max_tokens, suffix=True)


def chunk_version(settings):
    values = [
        "structure-bpe-v3",  # 表格列保留规则变化，显式 index 时重解析旧文件。
        TokenCounter(settings.embedding_tokenizer).version,
        settings.chunk_size_tokens,
        settings.chunk_overlap_tokens,
        settings.parse_pdf_layout_mode,
        settings.parse_remove_header_footer,
        settings.parse_remove_toc,
        settings.table_context_chars,
        settings.embedding_max_input_tokens,
        settings.embedding_max_batch_tokens,
        settings.token_safety_ratio,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()
