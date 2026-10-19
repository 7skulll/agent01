import json

import pytest

from app.errors import ChatError
from app.rag.answer_protocol import parse_answer, render_answer, repair_messages, repair_reserve
from app.rag.context_assembly import render_messages
from app.tokenization import TokenCounter

SOURCES = [
    {"citation_id": i, "content": f"实际资料{i}", "chunk_id": str(i), "name": "资料.txt"}
    for i in [1, 2, 3]
]


def payload(text="年假十天。", ids=None):
    return {
        "status": "answered",
        "segments": [{"text": text, "source_ids": [1] if ids is None else ids}],
        "reason": None,
    }


def parse(value, sources=SOURCES):
    return parse_answer(json.dumps(value, ensure_ascii=False), sources)


def test_rendering_uses_only_structured_ids_and_deduplicates_them():
    value = payload("年份[2026]、array[3]和普通编号[1]都属于正文。", [2, 1, 2])
    answer = parse(value)
    result = render_answer(answer, SOURCES)
    assert result["answer"] == value["segments"][0]["text"] + "[2][1]"
    assert result["answer_segments"][0]["source_ids"] == [2, 1]
    assert [s["citation_id"] for s in result["sources"]] == [1, 2]


@pytest.mark.parametrize(
    "ids", [[0], [-1], [True], [False], [1.0], ["1"], [None], [], [99], list(range(1, 22))]
)
def test_non_strict_missing_or_unknown_source_ids_are_rejected(ids):
    with pytest.raises(ChatError) as error:
        parse(payload(ids=ids))
    assert error.value.code == "invalid_citations"


def test_cropped_or_previous_source_id_is_not_allowed():
    with pytest.raises(ChatError) as error:
        parse(payload(ids=[3]), SOURCES[:1])
    assert error.value.code == "invalid_citations"


@pytest.mark.parametrize("reason", ["missing_fact", "conflicting_sources"])
def test_insufficient_evidence_is_a_business_result_without_sources(reason):
    value = {"status": "insufficient_evidence", "segments": [], "reason": reason}
    result = render_answer(parse(value), SOURCES)
    assert result["answer_status"] == "insufficient_evidence"
    assert result["answer_reason"] == reason
    assert result["answer_segments"] == result["sources"] == []
    assert result["answer_protocol"] == "rag-answer-v2"


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        None,
        {"status": "answered", "segments": [], "reason": None},
        {"status": "answered", "segments": [{"text": " ", "source_ids": [1]}], "reason": None},
        {
            "status": "answered",
            "segments": [{"text": "x", "source_ids": [1]}],
            "reason": "missing_fact",
        },
        {
            "status": "insufficient_evidence",
            "segments": [{"text": "x", "source_ids": [1]}],
            "reason": "missing_fact",
        },
        {"status": "insufficient_evidence", "segments": [], "reason": None},
        {"status": "insufficient_evidence", "segments": [], "reason": "unknown"},
        {"status": "partial_answer", "segments": [], "reason": None},
        {**payload(), "extra": "x"},
        payload("x" * 6001),
        payload("\ud800"),
        payload("a\x00b"),
    ],
)
def test_invalid_schema_is_not_silently_treated_as_insufficient(value):
    with pytest.raises(ChatError) as error:
        parse(value)
    assert error.value.code == "invalid_answer_schema"


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        '```json\n{"status":"insufficient_evidence","segments":[],"reason":"missing_fact"}\n```',
        '{"status":"answered","status":"insufficient_evidence","segments":[],"reason":"missing_fact"}',
        '{"status":"answered","segments":[],"reason":NaN}',
        "x" * 64001,
    ],
)
def test_duplicate_keys_code_fences_and_non_json_numbers_are_rejected(raw):
    with pytest.raises(ChatError) as error:
        parse_answer(raw, SOURCES)
    assert error.value.code == "invalid_answer_schema"


def test_segment_and_total_text_limits():
    for count, size in [(33, 1), (5, 6000)]:
        value = payload()
        value["segments"] = [{"text": "x" * size, "source_ids": [1]} for _ in range(count)]
        with pytest.raises(ChatError):
            parse(value)


@pytest.mark.parametrize("tokenizer", ["cl100k_base", "o200k_base", "heuristic"])
def test_repair_keeps_same_evidence_question_and_respects_reserve(tokenizer):
    counter = TokenCounter(tokenizer)
    original = render_messages("😀问题[2026]", [], SOURCES[:1])
    for code in ["invalid_citations", "invalid_answer_schema"]:
        repaired = repair_messages(original, code)
        assert repaired[-1] == original[-1]
        assert code in repaired[0].text and code not in original[0].text
        assert counter.messages(repaired) <= counter.messages(original) + repair_reserve(counter)
