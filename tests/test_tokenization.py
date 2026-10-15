"""离线真实 BPE、输入边界与完整消息预算的回归验证。"""

import asyncio
import socket

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from support import settings
from test_embedding import FakeEmbeddings

from app.agent.runtime import AgentRuntime
from app.errors import ChatError
from app.rag.chunking import chunk_blocks, split_text
from app.rag.embedding import EmbeddingError, OpenAIEmbeddingModel
from app.rag.parsers import Block
from app.tokenization import TokenCounter, embedding_text, get_encoder
from app.tools import CHAT_TOOLS


@pytest.mark.parametrize("name", ["cl100k_base", "o200k_base"])
def test_bpe_loads_offline_and_matches_official_merge_tables(name, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("tokenizer must not access network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    get_encoder.cache_clear()
    encoder = get_encoder(name)
    assert (
        encoder.encode_ordinary("hello world") == [15339, 1917]
        if name == "cl100k_base"
        else encoder.encode_ordinary("hello world") == [24912, 2375]
    )
    text = "中文😀 <|endoftext|> العربية"
    assert encoder.decode(encoder.encode_ordinary(text)) == text


def test_corrupt_table_fails_without_fallback(tmp_path, monkeypatch):
    import app.tokenization as module

    (tmp_path / "cl100k_base.tiktoken").write_text("bad table")
    monkeypatch.setattr(module, "_DATA", tmp_path)
    get_encoder.cache_clear()
    with pytest.raises(ValueError, match="校验失败"):
        TokenCounter().count("test")
    get_encoder.cache_clear()


def test_bpe_count_differs_from_character_heuristic():
    bpe, heuristic = TokenCounter(), TokenCounter("heuristic")
    assert bpe.count("abcdefghijklmnopqrstuvwxyz") == 1
    assert heuristic.count("abcdefghijklmnopqrstuvwxyz") == 7
    assert bpe.count("😀" * 20) > heuristic.count("😀" * 20)
    assert bpe.count(" ") > 0


@pytest.mark.parametrize("name", ["cl100k_base", "o200k_base"])
def test_unicode_prefix_suffix_and_zero_overlap_are_lossless(name):
    counter = TokenCounter(name)
    text = "😀🧑🏽‍💻中文。 العربية \n1234567890 abcdef\n" * 30
    for budget in [4, 7, 24, 64]:
        prefix = counter.take(text, budget)
        suffix = counter.take(text, budget, suffix=True)
        assert text.startswith(prefix) and text.endswith(suffix)
        assert counter.count(prefix) <= budget and counter.count(suffix) <= budget
        assert "\ufffd" not in prefix + suffix
    chunks = split_text(text, 48, 0, counter=counter)
    assert "".join(chunks) == text
    assert all(counter.count(chunk) <= 48 for chunk in chunks)
    chunks = split_text(text, 48, 8, counter=counter)
    assert all(counter.count(chunk) <= 48 and chunk in text for chunk in chunks)


def test_document_chunking_accounts_for_title_and_preserves_body():
    counter = TokenCounter()
    name, heading = "企业操作手册.md", "操作流程 / 审批事项"
    body = "员工申请需要完成审批。编号0123456789。\n" * 100
    chunks = chunk_blocks(
        [Block(text=body, heading=heading)], 512, 0, counter=counter, name=name, embedding_limit=72
    )
    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == body
    assert all(
        counter.count(embedding_text({"name": name, "heading": heading, "content": chunk.content}))
        <= 72
        for chunk in chunks
    )


def test_metadata_alone_can_exhaust_embedding_budget():
    with pytest.raises(ValueError, match="文件名或标题"):
        chunk_blocks(
            [Block(text="正文", heading="甲" * 100)],
            512,
            0,
            name="document.txt",
            embedding_limit=32,
        )


def test_complete_message_count_includes_tools_arguments_and_results():
    counter = TokenCounter()
    base = [HumanMessage(content="计算")]
    assert counter.messages(base, CHAT_TOOLS) > counter.messages(base)
    call = AIMessage(
        content="",
        tool_calls=[{"name": "multiply_numbers", "args": {"a": 12345, "b": 67890}, "id": "call-1"}],
    )
    assert counter.messages([*base, call]) > counter.messages([*base, AIMessage(content="")])
    assert counter.messages(
        [ToolMessage(content="结果", tool_call_id="id" * 100)]
    ) > counter.messages([ToolMessage(content="结果", tool_call_id="id")])


def test_tool_schema_overflow_is_rejected_before_model_call():
    runtime = AgentRuntime(
        settings(rag_input_tokens=512, llm_max_tokens=100, chat_token_overhead=0), None, None
    )
    messages = [HumanMessage(content=" ")]
    assert runtime.validate_input(messages) > 0
    tool = {
        "type": "function",
        "function": {
            "name": "test",
            "description": "甲" * 500,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    with pytest.raises(ChatError, match="上下文预算"):
        runtime.validate_input(messages, [tool])


def test_unknown_real_embedding_limit_requires_explicit_configuration():
    with pytest.raises(ValueError, match="RAG_EMBEDDING_MAX_INPUT_TOKENS"):
        OpenAIEmbeddingModel.from_settings(settings())


def test_documents_and_query_reject_oversize_without_remote_calls():
    client = FakeEmbeddings()
    model = OpenAIEmbeddingModel(client, model_name="fake", max_input_tokens=8)
    with pytest.raises(EmbeddingError, match="token 预算"):
        asyncio.run(model.embed_documents(["small", "😀" * 20]))
    with pytest.raises(EmbeddingError, match="token 预算"):
        asyncio.run(model.embed_query("😀" * 20))
    assert client.calls == []


def test_embedding_batches_respect_total_tokens_and_keep_exact_input():
    client = FakeEmbeddings()
    counter = TokenCounter()
    texts = ["  abc  ", "数字0123456789", "员工申请流程"] * 3
    model = OpenAIEmbeddingModel(
        client, model_name="fake", max_input_tokens=16, max_batch_tokens=12, batch_size=5
    )
    asyncio.run(model.embed_documents(texts))
    assert [text for batch in client.calls for text in batch] == texts
    assert all(sum(counter.count(text) for text in batch) <= 12 for batch in client.calls)
