import asyncio
from types import SimpleNamespace

from test_context_packing import assembler, source

from app.config import Settings
from app.rag.retrieval import RetrievalService, SearchRequest, Source


def hit(key, content, **kwargs):
    return Source(
        citation_id=0,
        chunk_id=key,
        file_id=key,
        name="manual.txt",
        content=content,
        heading=None,
        page=None,
        kind="text",
        score=0.01,
        **kwargs,
    )


def test_whitespace_duplicates_keep_original_text_and_provenance():
    first = hit("a", "姓名：张三\u00a0\u00a0金额：100 元")
    second = hit("b", "姓名：张三 金额：100 元")
    third = hit("c", "姓名：张三 金额：1000 元")
    assert RetrievalService._dedupe([first, second, third]) == [first, third]
    assert "\u00a0" in first.content
    assert first.alternatives[0]["chunk_id"] == "b"


def test_redundancy_penalty_keeps_independent_evidence_and_rerank_boundary():
    body = "这是同一份文档中反复出现的背景介绍和审批流程。" * 20
    first = hit("a", body, rank_source="rerank")
    duplicate = hit("b", body + "补充说明。", rank_source="rerank")
    independent = hit("c", "另一条独立的证据说明申请所需材料。" * 20, rank_source="rerank")
    tail = hit("d", "未经过重排的内容。")
    assert RetrievalService._diversify([first, duplicate, independent, tail]) == [
        first,
        independent,
        duplicate,
        tail,
    ]


def test_rerank_scores_are_explicitly_typed_and_original_candidates_untouched():
    class Model:
        async def rerank(self, query, documents):
            return [0.9] * len(documents)

    settings = Settings(_env_file=None, retrieval_rerank_candidates=20)
    service = RetrievalService(None, None, settings, reranker=Model())
    candidates = [hit(str(i), f"片段 {i}") for i in range(21)]
    result = asyncio.run(service._rerank("问题", candidates, {"degraded": [], "stage_ms": {}}))
    assert result[0].score_type == "rerank_model"
    assert result[0].rank_source == "rerank"
    assert result[-1].score_type == "recall_rrf"
    assert result[-1].rank_source == "recall"
    assert all(s.rank_source == "recall" for s in candidates)


def test_keyword_multiquery_is_one_batch_with_separate_rankings():
    class Lexical:
        calls = 0

        async def has_stale_scope(self, *args):
            return False

        def query_terms(self, query):
            return [query]

        async def search_many(self, kb, files, queries, limit):
            self.calls += 1
            assert queries == [["one"], ["two"]]
            return [
                [{"chunk_id": "a", "lexical_score": -1}],
                [{"chunk_id": "b", "lexical_score": -2}],
            ]

    lexical = Lexical()
    service = RetrievalService(None, None, Settings(_env_file=None), lexical=lexical)
    lists, rows, _ = asyncio.run(
        service._keywords(
            "kb",
            SearchRequest(query="one", queries=["two"]),
            ["one", "two"],
        )
    )
    assert lexical.calls == 1
    assert lists == [[("a", 1)], [("b", 2)]]
    assert set(rows) == {"a", "b"}


def test_table_extraction_keeps_header_complete_row_and_original_ranges():
    text = (
        "姓名 | 金额 | 条件\n--- | --- | ---\n"
        + "".join(f"员工{i} | {i}元 | 正式员工\n" for i in range(80))
        + "张三 | 314元 | 仅正式员工适用\n"
    )
    result = assembler(200).assemble("张三的金额和条件", [source(content=text, kind="table")], [])
    assert len(result["sources"]) == 1
    packed = result["sources"][0]
    assert "姓名 | 金额 | 条件" in packed["content"]
    assert "张三 | 314元 | 仅正式员工适用" in packed["content"]
    assert packed["truncated"]
    for fragment in packed["evidence_chunks"]:
        assert text[fragment["start"] : fragment["end"]] == fragment["content"]
    assert result["diagnostics"]["evidence_tokens"] <= 200


def test_evidence_token_cache_still_counts_exact_json():
    class Counter:
        calls = 0

        def count(self, text):
            self.calls += 1
            return len(text)

    from app.rag.context_assembly import ContextAssembler, evidence_payload, serialize

    counter = Counter()
    packer = ContextAssembler(SimpleNamespace(), counter, 10000)
    candidates = [source()]
    expected = len(serialize(evidence_payload(candidates)))
    assert packer.evidence_tokens(candidates) == expected
    assert packer.evidence_tokens(candidates) == expected
    assert counter.calls == 1
