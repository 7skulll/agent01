"""FastGPT 文本召回流程：逐查询召回 → 路内 RRF → 两路加权 RRF。"""

import asyncio
import heapq
import json
import math
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.errors import ChatError
from app.rag.chunking import chunk_version
from app.rag.embedding import EmbeddingError
from app.rag.vectors import validate_vectors
from app.tokenization import DEFAULT_COUNTER


def recall_limits(mode):
    """FastGPT countRecallLimit：纯模式 100；混合模式向量 80、全文 60。"""
    return (100, 0) if mode == "vector" else (0, 100) if mode == "keyword" else (80, 60)


def ranked_concat(lists):
    """同 FastGPT datasetSearchResultConcat：忽略空/零权重路；单路原样透传。"""
    lists = [(weight, items) for weight, items in lists if weight > 0 and items]
    if not lists:
        return []
    if len(lists) == 1:
        return lists[0][1]
    scores = {}
    for weight, items in lists:
        for rank, (key, _) in enumerate(items, 1):
            scores[key] = scores.get(key, 0) + weight / (60 + rank)
    return sorted(scores.items(), key=lambda item: -item[1])


def unique_queries(values):
    """仅忽略空白和大小写去重，保留编号中的标点及用户原问题。"""
    result, seen = [], set()
    for value in values:
        value = value.strip()
        key = " ".join(value.lower().split())
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(min_length=1, max_length=4000)
    rerank_query: str | None = Field(default=None, min_length=1, max_length=4000)
    queries: list[str] = Field(default_factory=list, max_length=2)
    file_ids: list[str] | None = Field(default=None, max_length=100)
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.2, ge=-1, le=1)
    mode: str = Field(default="hybrid", pattern="^(vector|hybrid|keyword)$")


class Source(BaseModel):
    citation_id: int
    chunk_id: str
    file_id: str
    name: str
    content: str
    heading: str | None
    page: int | None
    kind: str
    chunk_index: int = 0
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    channels: list[str] = Field(default_factory=list)
    matched_queries: list[str] = Field(default_factory=list)
    alternatives: list[dict] = Field(default_factory=list)
    supporting_chunks: list[dict] = Field(default_factory=list)
    truncated: bool = False


class SearchResponse(BaseModel):
    query: str
    index_version: str | None
    sources: list[Source]
    queries: list[str] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)


class RetrievalService:
    def __init__(self, vectors, vector_service, settings, reranker=None):
        self.vectors, self.vector_service, self.settings = vectors, vector_service, settings
        self.reranker = reranker

    async def search(self, kb_id, body):
        """两路独立执行；可选步骤失败降级，全部启用的召回路径失败时报告错误。"""
        if kb_id != "default":
            raise ChatError("knowledge_base_not_found", "知识库不存在。", 404)
        queries = unique_queries([body.query, *body.queries])
        if any(len(query) > 4000 for query in queries):
            raise ChatError("invalid_query", "检索问题过长。", 422)
        started = time.perf_counter()
        diagnostics = {
            "degraded": [],
            "stage_ms": {},
            "rerank_applied": False,
            "fusion": "channel_rrf_then_weighted_rrf",
            "recall_limits": dict(zip(("vector", "keyword"), recall_limits(body.mode))),
            "rrf_k": 60,
        }
        # 空范围是确定性的空结果，不依赖模型配置或外部服务的可用性。
        if body.file_ids == []:
            diagnostics.update({"scope_empty": True, "stage_ms": {"total": 0.0}})
            return SearchResponse(
                query=body.query,
                queries=queries,
                sources=[],
                index_version=self.vector_service.version,
                diagnostics=diagnostics,
            )
        tasks, names = [], []
        if body.mode != "keyword" and (
            body.mode != "hybrid" or self.settings.retrieval_dense_weight > 0
        ):
            tasks.append(self._dense(kb_id, body, queries))
            names.append("vector")
        if body.mode != "vector" and (
            body.mode != "hybrid" or self.settings.retrieval_dense_weight < 1
        ):
            tasks.append(self._keywords(kb_id, body, queries))
            names.append("keyword")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        lists, lookup, errors = [], {}, []
        for channel, result in zip(names, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                errors.append(result)
                diagnostics["degraded"].append(channel + "_failed")
                continue
            recall_lists, rows, elapsed = result
            diagnostics["stage_ms"][channel] = elapsed
            diagnostics[channel + "_count"] = len(
                {key for items in recall_lists for key, _ in items}
            )
            if channel == "vector":
                diagnostics["vector_scanned_count"] = len(rows)
            lookup.update(rows)
            lists.extend((channel, query, items) for query, items in zip(queries, recall_lists))
        if len(errors) == len(names):
            if isinstance(errors[0], ChatError):
                raise errors[0]
            raise ChatError("retrieval_unavailable", "检索暂时不可用。", 503, True)
        fusion_started = time.perf_counter()
        candidates = self._fuse(lists, lookup, body, queries)
        if errors and not candidates:
            raise ChatError("retrieval_unavailable", "部分检索路径不可用，请稍后重试。", 503, True)
        diagnostics["fused_count"] = len(candidates)
        candidates = self._dedupe(candidates)
        diagnostics["deduplicated_count"] = len(candidates)
        diagnostics["stage_ms"]["fusion"] = round((time.perf_counter() - fusion_started) * 1000, 2)
        candidates = await self._rerank(body.rerank_query or body.query, candidates, diagnostics)
        # FastGPT 只在纯向量模式或 rerank 模式应用相似度；混合原始分数没有统一阈值。
        diagnostics["using_similarity_filter"] = (
            body.mode == "vector" and not diagnostics["rerank_applied"]
        )
        if diagnostics["using_similarity_filter"]:
            candidates = [source for source in candidates if source.vector_score >= body.min_score]
        selected, file_counts = [], {}
        for source in candidates:
            count = file_counts.get(source.file_id, 0)
            if count >= self.settings.retrieval_max_per_file:
                continue
            file_counts[source.file_id] = count + 1
            selected.append(source.model_copy(update={"citation_id": len(selected) + 1}))
            if len(selected) == body.top_k:
                break
        if self.settings.retrieval_neighbor_chunks and selected:
            neighbor_started = time.perf_counter()
            try:
                selected = await self._expand_neighbors(kb_id, selected)
            except Exception:
                diagnostics["degraded"].append("neighbors_failed")
            diagnostics["stage_ms"]["neighbors"] = round(
                (time.perf_counter() - neighbor_started) * 1000, 2
            )
        diagnostics["stage_ms"]["total"] = round((time.perf_counter() - started) * 1000, 2)
        return SearchResponse(
            query=body.query,
            queries=queries,
            index_version=self.vector_service.version,
            sources=selected,
            diagnostics=diagnostics,
        )

    async def _dense(self, kb_id, body, queries):
        started = time.perf_counter()
        self.vector_service.require_model()
        try:
            # 保留 embed_query 语义，使非对称 Embedding 模型能区分问题和文档。
            vectors = await asyncio.gather(
                *(self.vector_service.model.embed_query(query) for query in queries)
            )
            for vector in vectors:
                validate_vectors([vector], 1, self.vector_service.model.dim)
        except EmbeddingError as exc:
            raise ChatError("embedding_error", str(exc), 502, exc.retryable) from None
        rows = await self.vectors.candidates(kb_id, self.vector_service.version, body.file_ids)
        lists = await asyncio.to_thread(
            self._cosine_lists, rows, vectors, recall_limits(body.mode)[0]
        )
        return (
            lists,
            {row["chunk_id"]: row for row in rows},
            round((time.perf_counter() - started) * 1000, 2),
        )

    def _cosine_lists(self, rows, queries, limit):
        lists = []
        # 本次多查询共用一次 JSON 解码，不为每个问题重复解析向量。
        vectors = []
        for row in rows:
            vector = json.loads(row["vector"])
            norm = math.hypot(*vector)
            if norm and math.isfinite(norm):
                vectors.append((row, [value / norm for value in vector]))
        for query in queries:
            query_norm = math.hypot(*query)
            normalized_query = [value / query_norm for value in query]
            scored = []
            for row, vector in vectors:
                if len(vector) != len(query):
                    continue
                score = sum(a * b for a, b in zip(vector, normalized_query))
                scored.append((row["chunk_id"], score))
            lists.append(
                heapq.nsmallest(
                    limit,
                    scored,
                    key=lambda item: (-item[1], item[0]),
                )
            )
        return lists

    async def _keywords(self, kb_id, body, queries):
        started = time.perf_counter()
        lists, rows = [], {}
        version = chunk_version(self.settings)
        if await self.vector_service.lexical.has_stale_scope(
            kb_id, body.file_ids, version, self.vector_service.version
        ):
            raise ChatError("keyword_index_outdated", "全文索引需要本地重建，请调用 index。", 503)
        for query in queries:
            terms = await asyncio.to_thread(self.vector_service.lexical.query_terms, query)
            if not terms:
                lists.append([])
                continue
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            clauses = [
                "chunk_terms MATCH ?",
                "f.kb_id=?",
                "f.index_status='indexed'",
                "f.chunk_version=?",
                "l.version=?",
            ]
            params = [
                expression,
                kb_id,
                version,
                self.vector_service.lexical.version,
            ]
            # 状态、版本和用户文件范围先过滤，再 LIMIT；旧索引不得挤占候选池。
            if self.vector_service.version:
                clauses.append("v.index_version=?")
                params.append(self.vector_service.version)
            if body.file_ids is not None:
                clauses.append("f.file_id IN (" + ",".join("?" for _ in body.file_ids) + ")")
                params.extend(body.file_ids)
            params.append(recall_limits(body.mode)[1])
            async with self.vectors.database.lock:
                async with self.vectors.database.connection.execute(
                    "SELECT c.*, f.name, bm25(chunk_terms) AS lexical_score "
                    "FROM chunk_terms JOIN chunks c ON c.chunk_id=chunk_terms.chunk_id "
                    "JOIN files f ON f.file_id=c.file_id "
                    "JOIN lexical_indexes l ON l.file_id=f.file_id "
                    "JOIN chunk_vectors v ON v.chunk_id=c.chunk_id WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY lexical_score, c.chunk_id LIMIT ?",
                    tuple(params),
                ) as cursor:
                    hits = [dict(row) for row in await cursor.fetchall()]
            rows.update({row["chunk_id"]: row for row in hits})
            lists.append([(row["chunk_id"], -row["lexical_score"]) for row in hits])
        return lists, rows, round((time.perf_counter() - started) * 1000, 2)

    def _fuse(self, lists, lookup, body, queries):
        evidence, grouped = {}, {"vector": [], "keyword": []}
        for channel, query, items in lists:
            grouped[channel].append(items)
            for key, raw in items:
                item = evidence.setdefault(key, {"channels": [], "matched_queries": []})
                if channel not in item["channels"]:
                    item["channels"].append(channel)
                if query not in item["matched_queries"]:
                    item["matched_queries"].append(query)
                field = "vector_score" if channel == "vector" else "keyword_score"
                item[field] = max(raw, item.get(field, -math.inf))
        channels = []
        for (channel, query_lists), limit in zip(grouped.items(), recall_limits(body.mode)):
            merged = ranked_concat([(1, items) for items in query_lists])[:limit]
            weight = (
                self.settings.retrieval_dense_weight
                if channel == "vector"
                else 1 - self.settings.retrieval_dense_weight
            )
            channels.append((weight if body.mode == "hybrid" else 1, merged))
        ranked = ranked_concat(channels)
        result = []
        for key, score in ranked:
            row = lookup[key]
            if not row["content"].strip():
                continue
            result.append(
                Source(
                    citation_id=0,
                    chunk_id=key,
                    file_id=row["file_id"],
                    name=row["name"],
                    content=row["content"],
                    heading=row["heading"],
                    page=row["page"],
                    kind=row["kind"],
                    chunk_index=row["chunk_index"],
                    score=score,
                    **evidence[key],
                )
            )
        return result

    @staticmethod
    def _dedupe(sources):
        """同 FastGPT 按 q+a 的字母/数字归一化去重；额外保留来源回溯信息。"""
        result, seen = [], {}
        for source in sources:
            key = "".join(
                char for char in (source.heading or "") + source.content if char.isalnum()
            )
            if key in seen:
                seen[key].alternatives.append(
                    {
                        "chunk_id": source.chunk_id,
                        "file_id": source.file_id,
                        "name": source.name,
                        "heading": source.heading,
                        "page": source.page,
                    }
                )
                continue
            seen[key] = source
            result.append(source)
        return result

    async def _rerank(self, query, candidates, diagnostics):
        if self.reranker is None or not candidates:
            return candidates
        started = time.perf_counter()
        pool = candidates[: self.settings.retrieval_rerank_candidates]
        documents = [
            "\n".join(part for part in (s.name, s.heading, s.content) if part) for s in pool
        ]
        try:
            async with asyncio.timeout(self.settings.retrieval_rerank_timeout_seconds):
                scores = await self.reranker.rerank(query, documents)
            if len(scores) != len(pool) or not all(math.isfinite(score) for score in scores):
                raise ValueError("invalid rerank scores")
            for source, score in zip(pool, scores):
                source.rerank_score = score
            # 使用名次融合，保留原始分数，避免批内归一化制造虚假的置信度。
            ranked = sorted(pool, key=lambda s: (-s.rerank_score, s.chunk_id))
            ranks = {source.chunk_id: rank for rank, source in enumerate(ranked, 1)}
            weight = self.settings.retrieval_rerank_weight
            for rank, source in enumerate(pool, 1):
                source.score = (1 - weight) / (60 + rank) + weight / (60 + ranks[source.chunk_id])
            diagnostics["rerank_applied"] = True
            diagnostics["rerank_count"] = len(pool)
            diagnostics["unreranked_count"] = len(candidates) - len(pool)
            # 仅重排候选池前缀；尾部保留原有顺序，供每文件数量限制后的补位使用。
            # 尾部没有 rerank_score，不与新的融合分数直接比较。
            return (
                sorted(pool, key=lambda source: (-source.score, source.chunk_id))
                + candidates[len(pool) :]
            )
        except Exception:
            diagnostics["degraded"].append("rerank_failed")
            return candidates
        finally:
            diagnostics["stage_ms"]["rerank"] = round((time.perf_counter() - started) * 1000, 2)

    async def _expand_neighbors(self, kb_id, sources):
        """一次回查同文件、同标题、同页的相邻正文，保留补充片段 ID。"""
        radius = self.settings.retrieval_neighbor_chunks
        seeds = [source for source in sources if source.kind == "text"]
        if not seeds:
            return sources
        clauses, params = [], [kb_id, self.vector_service.version, self.vector_service.version]
        for source in seeds:
            clauses.append(
                "(c.file_id=? AND c.heading IS ? AND c.page IS ? AND c.chunk_index BETWEEN ? AND ?)"
            )
            params.extend(
                [
                    source.file_id,
                    source.heading,
                    source.page,
                    source.chunk_index - radius,
                    source.chunk_index + radius,
                ]
            )
        async with self.vectors.database.lock:
            async with self.vectors.database.connection.execute(
                "SELECT c.* FROM chunks c JOIN files f ON f.file_id=c.file_id "
                "JOIN chunk_vectors v ON v.chunk_id=c.chunk_id "
                "WHERE f.kb_id=? AND f.index_status='indexed' AND c.kind='text' "
                "AND (? IS NULL OR v.index_version=?) AND ("
                + " OR ".join(clauses)
                + ") ORDER BY c.file_id, c.chunk_index",
                tuple(params),
            ) as cursor:
                neighbors = [dict(row) for row in await cursor.fetchall()]
        used = {source.chunk_id for source in sources}
        for source in seeds:
            for row in neighbors:
                if (
                    row["chunk_id"] in used
                    or row["file_id"] != source.file_id
                    or row["heading"] != source.heading
                    or row["page"] != source.page
                    or abs(row["chunk_index"] - source.chunk_index) > radius
                ):
                    continue
                used.add(row["chunk_id"])
                source.supporting_chunks.append(
                    {"chunk_id": row["chunk_id"], "page": row["page"], "content": row["content"]}
                )
        return sources

    @staticmethod
    def context(sources, budget, counter=DEFAULT_COUNTER):
        """完整块计预算；首条超长时裁剪正文，标注实际引用内容，绝不超限。"""
        blocks, selected = [], []
        for source in sources:
            source = source.model_copy(deep=True, update={"citation_id": len(selected) + 1})
            prefix = (
                f"[{source.citation_id}] 文件：{source.name}；标题：{source.heading or ''}；"
                f"页码：{source.page or ''}\n"
            )
            block = prefix + source.content
            if counter.count("\n\n".join([*blocks, block])) > budget:
                if blocks or counter.count(prefix) >= budget or source.kind != "text":
                    continue
                content = counter.take(source.content, budget, format_text=lambda t: prefix + t)
                if not content.strip():
                    continue
                source.content = content
                source.truncated = True
                # 裁剪后不宣称所有扩展片段仍完整参与回答。
                source.supporting_chunks = []
                block = prefix + source.content
            included = []
            for neighbor in source.supporting_chunks:
                addition = "\n\n补充同节片段：\n" + neighbor["content"]
                if counter.count("\n\n".join([*blocks, block + addition])) > budget:
                    continue
                block += addition
                source.content += addition
                included.append(neighbor)
            source.supporting_chunks = included
            blocks.append(block)
            selected.append(source.model_dump())
        return "\n\n".join(blocks), selected


router = APIRouter(prefix="/api/v1", tags=["retrieval"])


@router.post("/knowledge-bases/{kb_id}/search", response_model=SearchResponse)
async def search(kb_id: str, body: SearchRequest, request: Request):
    try:
        return await request.app.state.retrieval_service.search(kb_id, body)
    except ChatError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
