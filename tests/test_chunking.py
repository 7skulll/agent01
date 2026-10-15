"""切片预算、内容保真与结构边界的回归测试。"""

import random

import pytest

from app.rag.chunking import _tail, chunk_blocks, count_tokens, split_text
from app.rag.parsers import Block, parse
from app.tokenization import DEFAULT_COUNTER


def test_full_size_atoms_keep_reserved_overlap():
    chunks = split_text("甲" * 512 + "乙" * 512, 512, 64)
    assert len(chunks) > 1
    assert all(count_tokens(chunk) <= 512 for chunk in chunks)
    assert chunks[1].startswith(DEFAULT_COUNTER.take(chunks[0], 64, suffix=True))
    assert all(chunk in "甲" * 512 + "乙" * 512 for chunk in chunks)


def test_short_text_does_not_split_just_to_reserve_overlap():
    text = "甲" * 250
    assert count_tokens(text) <= 512
    assert split_text(text, 512, 64) == [text]


def test_ascii_tail_uses_whole_suffix_token_count():
    text = "abcdefghijklmnopqrstuvwxyz"
    assert _tail(text, 4) == text  # 该整串在 cl100k_base 中是一个 token。
    assert count_tokens(_tail(text, 4)) == 1


def test_english_overlap_preserves_word_separators():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa. " * 20
    chunks = split_text(text, 64, 8)
    assert len(chunks) > 1
    assert all(chunk in text for chunk in chunks)
    assert all(count_tokens(chunk) <= 64 for chunk in chunks)
    assert all(not chunk.startswith("gma") for chunk in chunks)
    assert any(chunk.endswith(" ") for chunk in chunks[:-1])


def test_zero_overlap_preserves_mixed_text_and_delimiters():
    rng = random.Random(2026)
    for _ in range(40):
        text = "".join(
            rng.choice(["正文。", "word ", "词，", "\n段落\n", "abc "]) for _ in range(40)
        )
        chunks = split_text(text, 24, 0)
        assert "".join(chunks) == text
        assert all(count_tokens(chunk) <= 24 for chunk in chunks)


def test_zero_overlap_keeps_long_whitespace_between_content():
    text = "begin" + " " * 100 + "end"
    chunks = split_text(text, 12, 0)
    assert "".join(chunks) == text
    assert all(count_tokens(chunk) <= 12 for chunk in chunks)


@pytest.mark.parametrize("kind", ["table", "list"])
def test_structured_chunks_keep_complete_lines(kind):
    text = "\n".join(f"列名: 值{index}; 列二: 数据{index}" for index in range(40))
    chunks = chunk_blocks([Block(text=text, kind=kind)], 50, 10)
    assert "".join(chunk.content for chunk in chunks) == text
    assert all(chunk.tokens <= 50 for chunk in chunks)
    assert all(line in text.splitlines() for chunk in chunks for line in chunk.content.splitlines())


@pytest.mark.parametrize("kind", ["table", "list", "code"])
def test_oversized_structured_line_fails_without_silent_fragmentation(kind):
    with pytest.raises(ValueError, match="单行内容超过切片预算"):
        chunk_blocks([Block(text="字段: " + "值" * 100, kind=kind)], 64, 8)


def test_separate_code_blocks_are_not_merged():
    blocks = [
        Block(text="```python\nprint(1)\n```", kind="code", order=1),
        Block(text="```python\nprint(2)\n```", kind="code", order=2),
    ]
    chunks = chunk_blocks(blocks, 512, 64)
    assert len(chunks) == 2
    assert [chunk.order for chunk in chunks] == [1, 2]


def test_code_chunks_keep_fences_language_and_indentation():
    body = "".join(f"    print({index})\n" for index in range(40))
    chunks = chunk_blocks([Block(text="```python\n" + body + "```", kind="code")], 40, 8)
    recovered = ""
    for chunk in chunks:
        assert chunk.tokens <= 40
        assert chunk.content.startswith("```python\n")
        assert chunk.content.endswith("\n```")
        recovered += chunk.content[len("```python\n") : -len("\n```")]
    assert recovered == body


def test_long_fence_is_not_closed_by_short_fence_in_code():
    text = "````markdown\n```python\nprint(1)\n```\n````"
    blocks = parse(".md", text.encode())
    assert len(blocks) == 1 and blocks[0].kind == "code"
    chunks = chunk_blocks(blocks, 64, 8)
    assert "```python\nprint(1)\n```" in chunks[0].content
    assert chunks[0].content.startswith("````markdown\n")
    assert chunks[0].content.endswith("\n````")


def test_chunk_limit_stops_streaming_split_early(monkeypatch):
    produced = []

    def many_atoms(*args, **kwargs):
        for _ in range(10000):
            produced.append(1)
            yield "甲" * 28

    monkeypatch.setattr("app.rag.chunking._split_recursive", many_atoms)
    with pytest.raises(ValueError, match="上限"):
        split_text("甲" * 10000, 64, 8, max_chunks=2)
    assert len(produced) < 10


def test_grouping_keeps_page_and_heading_level_boundaries():
    blocks = [
        Block(text="正文一", heading="标题", heading_level=1, page=1, order=0),
        Block(text="正文二", heading="标题", heading_level=2, page=1, order=1),
        Block(text="正文三", heading="标题", heading_level=2, page=2, order=2),
    ]
    chunks = chunk_blocks(blocks, 64, 8)
    assert len(chunks) == 3
    assert [(c.heading_level, c.page, c.order) for c in chunks] == [(1, 1, 0), (2, 1, 1), (2, 2, 2)]
