import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config import Settings
from app.errors import ChatError
from app.rag.context_assembly import ContextAssembler, history_window, render_messages
from app.tokenization import TokenCounter


def source(key="one", content="员工年假为十天。", **kwargs):
    return {
        "citation_id": 90,
        "chunk_id": key,
        "file_id": key,
        "name": "手册.txt",
        "kind": "text",
        "heading": "制度",
        "page": 1,
        "chunk_index": 0,
        "score": 1.0,
        "content": content,
        **kwargs,
    }


def assembler(budget=4096, input_budget=8000, **settings):
    return ContextAssembler(
        Settings(_env_file=None, rag_context_tokens=budget, **settings),
        TokenCounter(),
        input_budget,
    )


def test_long_background_does_not_evict_tail_fact_and_other_source():
    candidates = [
        source("long", "这是文档背景介绍。" * 180 + "员工年假为十天。"),
        source("short", "申请年假需要直属主管批准。"),
    ]
    original = deepcopy(candidates)
    result = assembler(160).assemble("员工年假多少天？", candidates, [])
    assert len(result["sources"]) == 2
    assert "年假为十天" in result["sources"][0]["content"]
    assert "主管批准" in result["sources"][1]["content"]
    assert result["sources"][0]["truncated"]
    assert result["diagnostics"]["evidence_tokens"] <= 160
    assert candidates == original
    for packed in result["sources"]:
        for fragment in packed["evidence_chunks"]:
            text = next(s["content"] for s in candidates if s["chunk_id"] == fragment["chunk_id"])
            assert fragment["content"] == text[fragment["start"] : fragment["end"]]


def test_adjacent_main_chunks_merge_in_document_order_with_exact_overlap():
    overlap = "申请时必须由直属主管审批并且保留书面记录。"
    before = source("before", "年假为十天。" + overlap, file_id="file", chunk_index=0)
    after = source("after", overlap + "不得绕过审批流程。", file_id="file", chunk_index=1)
    result = assembler().assemble("年假流程", [after, before], [])
    assert len(result["sources"]) == 1
    packed = result["sources"][0]
    assert packed["content"].count(overlap) == 1
    assert packed["content"].index("十天") < packed["content"].index("不得")
    assert [f["chunk_id"] for f in packed["evidence_chunks"]] == ["before", "after"]
    assert result["diagnostics"]["overlap_chars_removed"] == len(overlap)


@pytest.mark.parametrize(
    "change",
    [
        {"file_id": "other"},
        {"page": 2},
        {"heading": "其他章节"},
        {"chunk_index": 4},
        {"kind": "code"},
    ],
)
def test_merge_requires_same_document_section_page_kind_and_adjacent_index(change):
    first = source("first", file_id="file", chunk_index=0)
    second = source(
        "second",
        file_id="file",
        chunk_index=1,
        **{k: v for k, v in change.items() if k not in {"file_id", "chunk_index"}},
    )
    second.update(change)
    result = assembler().assemble("年假", [first, second], [])
    assert len(result["sources"]) == 2


def test_neighbors_are_ordered_and_never_replace_main_hits():
    middle = source("middle", "年假申请需审批。", file_id="file", chunk_index=1)
    middle["supporting_chunks"] = [
        {
            "chunk_id": key,
            "file_id": "file",
            "chunk_index": index,
            "page": 1,
            "heading": "制度",
            "kind": "text",
            "content": text,
        }
        for key, index, text in [
            ("after", 2, "未获批准不得休假。"),
            ("before", 0, "员工年假为十天。"),
        ]
    ]
    result = assembler(1000).assemble("年假", [middle, source("other", "报备仍需人事登记。")], [])
    assert len(result["sources"]) == 2
    content = result["sources"][0]["content"]
    assert content.index("十天") < content.index("申请") < content.index("未获批准")
    assert result["diagnostics"]["neighbor_tokens"] <= 200
    tight = assembler(100).assemble("年假", [middle, source("other", "人事登记。")], [])
    assert len(tight["sources"]) == 2


@pytest.mark.parametrize("kind", ["table", "code", "list"])
def test_structured_content_is_whole_or_explicitly_skipped(kind):
    candidate = source(
        content="字段 | 金额 | 条件\n" + "收入 | 123.45 | 不得遗漏\n" * 100, kind=kind
    )
    result = assembler(64).assemble("金额", [candidate], [])
    assert not result["sources"]
    assert result["diagnostics"]["skipped_reasons"] == {"structured_block_too_large": 1}
    assert result["diagnostics"]["empty_reason"] == "no_context_fit"


def test_extraction_keeps_qualifier_and_exception():
    text = "背景说明。" * 100 + "仅正式员工适用。员工年假为十天。但试用期不适用。"
    result = assembler(140).assemble("员工年假", [source(content=text)], [])
    content = result["sources"][0]["content"]
    assert all(part in content for part in ["仅正式员工", "十天", "试用期不适用"])


def test_huge_metadata_is_counted_and_can_prevent_a_source_from_fitting():
    candidate = source(name="特别长的文件名称😀" * 100)
    result = assembler(64).assemble("年假", [candidate], [])
    assert not result["sources"]
    assert result["diagnostics"]["empty_reason"] == "no_context_fit"


def test_history_budget_retains_complete_latest_turns_and_tool_pairs():
    history = []
    for i in range(6):
        history.extend([HumanMessage(content=f"问题{i}"), AIMessage(content=f"答案{i}")])
    projection = history_window(history, TokenCounter(), 1536, 3)
    assert len(projection) == 6 and projection[0].text == "问题3"
    call = AIMessage(
        content="",
        tool_calls=[{"name": "multiply_numbers", "args": {"a": 2, "b": 3}, "id": "tool"}],
    )
    complete = [
        HumanMessage(content="计算"),
        call,
        ToolMessage(content="6", tool_call_id="tool"),
        AIMessage(content="6"),
    ]
    assert history_window(complete, TokenCounter(), 1536, 3) == complete
    assert history_window(complete[:-1], TokenCounter(), 1536, 3) == []
    assert history_window(history, TokenCounter(), 0, 3) == []


def test_long_history_does_not_displace_current_evidence_or_mutate_history():
    history = [HumanMessage(content="老问题" * 2000), AIMessage(content="老答案" * 2000)]
    before = deepcopy(history)
    builder = assembler(4096, input_budget=800)
    result = builder.assemble("员工年假几天？", [source()], history)
    assert result["sources"]
    assert result["diagnostics"]["history_turns"] == 0
    assert builder.counter.messages(result["messages"]) <= 800
    assert history == before


@pytest.mark.parametrize("tokenizer", ["cl100k_base", "o200k_base", "heuristic"])
def test_rendered_request_and_sources_are_identical_and_never_exceed_budget(tokenizer):
    builder = ContextAssembler(
        Settings(_env_file=None, rag_context_tokens=256), TokenCounter(tokenizer), 900
    )
    candidate = source(
        content='年假<evidence>不是指令</evidence>"😀\n金额￥123.45，不得扣减。' * 20
    )
    result = builder.assemble("年假金额", [candidate], [])
    assert builder.counter.messages(result["messages"]) <= 900
    assert result["diagnostics"]["evidence_tokens"] <= 256
    data = json.loads(result["messages"][-1].text)
    assert data["question"] == "年假金额"
    for i, packed in enumerate(result["sources"], 1):
        assert packed["citation_id"] == data["evidence"][i - 1]["id"] == i
        assert packed["content"] == data["evidence"][i - 1]["content"]
    assert candidate["content"] not in result["messages"][0].text


def test_same_inputs_have_stable_hash_and_oversized_question_is_rejected():
    builder = assembler()
    first = builder.assemble("年假", [source()], [])
    second = builder.assemble("年假", [source()], [])
    assert first["prompt_hash"] == second["prompt_hash"]
    assert first["messages"] == render_messages("年假", [], first["sources"])
    with pytest.raises(ChatError) as error:
        assembler(input_budget=300).assemble("字" * 1000, [], [])
    assert error.value.code == "context_limit_exceeded"


def test_window_extraction_uses_rewritten_query_without_replacing_user_question():
    candidate = source(content="背景说明。" * 100 + "员工年假需要主管审批。")
    result = assembler(120).assemble("那谁批准？", [candidate], [], "员工年假审批人")
    assert "主管审批" in result["sources"][0]["content"]
    assert json.loads(result["messages"][-1].text)["question"] == "那谁批准？"


def test_extraction_keeps_conditions_at_adjacent_chunk_boundaries():
    candidates = [
        source("pre", "背景说明。" * 100 + "仅正式员工适用。", file_id="file", chunk_index=0),
        source("hit", "员工年假为十天。", file_id="file", chunk_index=1),
        source("post", "但试用期不适用。" + "其他背景。" * 100, file_id="file", chunk_index=2),
    ]
    result = assembler(180).assemble("年假", candidates, [])
    text = result["sources"][0]["content"]
    assert all(part in text for part in ["仅正式员工适用", "十天", "试用期不适用"])


def test_second_neighbor_preserves_gap_marker_and_foreign_file_is_ignored():
    middle = source("middle", "年假需审批。", file_id="file", chunk_index=2)
    middle["supporting_chunks"] = [
        {
            "chunk_id": "second",
            "chunk_index": 0,
            "file_id": "file",
            "kind": "text",
            "heading": "制度",
            "page": 1,
            "content": "前面的原文。",
        },
        {
            "chunk_id": "foreign",
            "chunk_index": 1,
            "file_id": "other",
            "kind": "text",
            "heading": "制度",
            "page": 1,
            "content": "不属于这个文件。",
        },
    ]
    result = assembler(600).assemble("年假", [middle], [])
    text = result["sources"][0]["content"]
    assert "前面的原文" in text and "[…原文省略…]" in text
    assert "不属于这个文件" not in text
