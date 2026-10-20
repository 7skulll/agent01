"""FastGPT 文本召回流程：逐查询召回 → 路内 RRF → 两路加权 RRF。"""

import asyncio
import logging
import math
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.errors import ChatError
from app.knowledge_bases import require_knowledge_base
from app.observability import Metrics, measured
from app.rag.embedding import EmbeddingError
from app.rag.reranking import RerankError
from app.rag.vectors import validate_vectors

logger = logging.getLogger(__name__)


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
    lexical_version: str | None = None
    sources: list[Source]
    queries: list[str] = Field(default_factory=list)
    diagnostics: dict = Field(default_factory=dict)


class RetrievalService:
    def __init__(
        self, vectors, vector_service, settings, reranker=None, *, lexical=None, metrics=None
    ):
        self.vectors, self.vector_service, self.settings = vectors, vector_service, settings
        self.reranker = reranker
        self.lexical = lexical
        self.metrics = metrics or Metrics()

    @measured("retrieval")
    async def search(self, kb_id, body, *, capture_trace=False, rerank=None):
        try:
            result = await self._search(kb_id, body, capture_trace=capture_trace, rerank=rerank)
        except Exception:
            self.metrics.increment("retrieval_results", outcome="error")
            raise
        for reason in result.diagnostics["degraded"]:
            self.metrics.increment("retrieval_degradations", reason=reason)
        self.metrics.increment(
            "retrieval_results", outcome="empty" if not result.sources else "nonempty"
        )
        return result

    async def _search(self, kb_id, body, *, capture_trace=False, rerank=None):
        """两路独立执行；可选步骤失败降级，全部启用的召回路径失败时报告错误。"""
        await require_knowledge_base(self.vectors.database, kb_id)
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
            "vector_search_mode": self.settings.vector_search_mode,
        }
        # 空范围是确定性的空结果，不依赖模型配置或外部服务的可用性。
        if body.file_ids == []:
            diagnostics.update({"scope_empty": True, "stage_ms": {"total": 0.0}})
            return SearchResponse(
                query=body.query,
                queries=queries,
                sources=[],
                index_version=self.vector_service.version,
                lexical_version=self.lexical.version if self.lexical else None,
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
                diagnostics["vector_returned_count"] = len(rows)
            lookup.update(rows)
            lists.extend((channel, query, items) for query, items in zip(queries, recall_lists))
        if len(errors) == len(names):
            if isinstance(errors[0], ChatError):
                raise errors[0]
            raise ChatError("retrieval_unavailable", "检索暂时不可用。", 503, True)
        fusion_started = time.perf_counter()
        candidates = self._fuse(lists, lookup, body)
        if errors and not candidates:
            raise ChatError("retrieval_unavailable", "部分检索路径不可用，请稍后重试。", 503, True)
        diagnostics["fused_count"] = len(candidates)
        candidates = self._dedupe(candidates)
        diagnostics["deduplicated_count"] = len(candidates)
        diagnostics["stage_ms"]["fusion"] = round((time.perf_counter() - fusion_started) * 1000, 2)
        self.metrics.observe("fusion", (time.perf_counter() - fusion_started))
        if capture_trace:
            diagnostics["trace"] = {"before_rerank": [s.model_dump() for s in candidates]}
        if rerank is True and (self.reranker is None or self.settings.retrieval_rerank_weight == 0):
            raise ChatError(
                "rerank_not_configured", "开启重排实验需要已配置且权重大于零的重排服务。", 422
            )
        if rerank is False:
            diagnostics["rerank_skipped"] = "evaluation_disabled"
        else:
            candidates = await self._rerank(
                body.rerank_query or body.query, candidates, diagnostics
            )
        if capture_trace:
            diagnostics["trace"]["after_rerank"] = [s.model_dump() for s in candidates]
        # 向量门槛独立于重排，不能因接入重排而无提示失效。
        diagnostics["using_similarity_filter"] = body.mode == "vector"
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
                with self.metrics.span("neighbors"):
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
            lexical_version=self.lexical.version if self.lexical else None,
            sources=selected,
            diagnostics=diagnostics,
        )

    @measured("vector_recall")
    async def _dense(self, kb_id, body, queries):
        started = time.perf_counter()
        self.vector_service.require_model()
        try:
            self.metrics.tokens(
                "embedding_query",
                estimated_input=sum(self.vector_service.counter.count(query) for query in queries),
            )
            # 保留 embed_query 语义，使非对称 Embedding 模型能区分问题和文档。
            vectors = await asyncio.gather(
                *(self.vector_service.model.embed_query(query) for query in queries)
            )
            for vector in vectors:
                validate_vectors([vector], 1, self.vector_service.model.dim)
        except EmbeddingError as exc:
            raise ChatError("embedding_error", str(exc), 502, exc.retryable) from None
        lists, rows = await self.vectors.search(
            kb_id, self.vector_service.version, vectors, recall_limits(body.mode)[0], body.file_ids
        )
        return lists, rows, round((time.perf_counter() - started) * 1000, 2)

    @measured("keyword_recall")
    async def _keywords(self, kb_id, body, queries):
        started = time.perf_counter()
        lists, rows = [], {}
        if await self.lexical.has_stale_scope(kb_id, body.file_ids):
            raise ChatError("keyword_index_outdated", "全文索引需要本地重建，请调用 index。", 503)
        for query in queries:
            terms = await asyncio.to_thread(self.lexical.query_terms, query)
            if not terms:
                lists.append([])
                continue
            hits = await self.lexical.search(
                kb_id,
                body.file_ids,
                terms,
                recall_limits(body.mode)[1],
            )
            rows.update({row["chunk_id"]: row for row in hits})
            lists.append([(row["chunk_id"], -row["lexical_score"]) for row in hits])
        return lists, rows, round((time.perf_counter() - started) * 1000, 2)

    def _fuse(self, lists, lookup, body):
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
        """按类型、标题和正文去重，保留符号、内部空白及代码缩进和来源信息。"""

        def normalize(text):
            return text.replace("\r\n", "\n").replace("\r", "\n")

        result, seen = [], {}
        for source in sources:
            content = normalize(source.content)
            key = (
                source.kind,
                normalize(source.heading or ""),
                content if source.kind == "code" else content.strip(),
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
        """重排成功后原子替换候选；纯模型排序或 RRF，阈值只解释模型原始分数。"""
        if self.reranker is None or not candidates:
            return candidates
        weight = self.settings.retrieval_rerank_weight
        threshold = self.settings.retrieval_rerank_min_score
        if weight == 0 and threshold is None:
            diagnostics["rerank_skipped"] = "zero_weight"
            return candidates
        started = time.perf_counter()
        outcome = "error"
        pool = candidates[: self.settings.retrieval_rerank_candidates]
        documents = [
            "\n".join(part for part in (s.name, s.heading, s.content) if part) for s in pool
        ]
        try:
            async with asyncio.timeout(self.settings.retrieval_rerank_timeout_seconds):
                scores = await self.reranker.rerank(query, documents)
            if len(scores) != len(pool) or not all(
                type(score) in (int, float) and math.isfinite(score) for score in scores
            ):
                raise ValueError("invalid rerank scores")
            # 不修改原候选，后续处理发生异常时仍能完整回退；同分保留召回顺序。
            pool = [
                source.model_copy(update={"rerank_score": float(score)})
                for source, score in zip(pool, scores)
            ]
            ranked = sorted(pool, key=lambda source: -source.rerank_score)
            ranks = {source.chunk_id: rank for rank, source in enumerate(ranked, 1)}
            for rank, source in enumerate(pool, 1):
                source.score = (
                    source.rerank_score
                    if weight == 1
                    else (1 - weight) / (60 + rank) + weight / (60 + ranks[source.chunk_id])
                )
            ranked = sorted(pool, key=lambda source: -source.score)
            if threshold is not None:
                ranked = [source for source in ranked if source.rerank_score >= threshold]
            outcome = "ok"
            diagnostics["rerank_applied"] = True
            diagnostics["rerank_strategy"] = "model" if weight == 1 else "rrf"
            diagnostics["rerank_threshold"] = threshold
            diagnostics["rerank_filtered_count"] = len(pool) - len(ranked)
            diagnostics["rerank_model"] = getattr(self.reranker, "model_name", "injected")
            diagnostics["rerank_count"] = len(pool)
            diagnostics["unreranked_count"] = len(candidates) - len(pool)
            # 阈值开启后禁止未评分尾部补位，避免绕过过滤；否则仍保留尾部作多样性补位。
            return ranked + (candidates[len(pool) :] if threshold is None else [])
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            diagnostics["degraded"].append("rerank_failed")
            reason = (
                exc.code
                if isinstance(exc, RerankError)
                else (
                    "timeout"
                    if isinstance(exc, TimeoutError)
                    else "invalid_response_or_adapter_error"
                )
            )
            diagnostics["rerank_error"] = reason
            logger.warning("rerank_failed reason=%s", reason)
            return candidates
        finally:
            diagnostics["stage_ms"]["rerank"] = round((time.perf_counter() - started) * 1000, 2)
            self.metrics.observe("rerank", time.perf_counter() - started, outcome)

    async def _expand_neighbors(self, kb_id, sources):
        """一次回查同文件、同标题、同页的相邻正文，保留补充片段 ID。"""
        radius = self.settings.retrieval_neighbor_chunks
        seeds = [source for source in sources if source.kind == "text"]
        if not seeds:
            return sources
        clauses, params = [], [kb_id]
        for source in seeds:
            clauses.append(
                "(c.file_id=%s AND c.heading IS NOT DISTINCT FROM %s AND c.page IS NOT "
                "DISTINCT FROM %s AND c.chunk_index BETWEEN %s AND %s "
                "AND EXISTS (SELECT 1 FROM chunks seed WHERE seed.chunk_id=%s "
                "AND seed.file_id=c.file_id))"
            )
            params.extend(
                [
                    source.file_id,
                    source.heading,
                    source.page,
                    source.chunk_index - radius,
                    source.chunk_index + radius,
                    source.chunk_id,
                ]
            )
        async with self.vectors.database.read():
            cursor = await self.vectors.database.connection.execute(
                "SELECT c.* FROM chunks c JOIN files f ON f.file_id=c.file_id "
                "WHERE f.kb_id=%s AND f.index_status IN ('recorded','indexing','indexed') "
                "AND c.kind='text' AND ("
                + " OR ".join(clauses)
                + ") ORDER BY c.file_id, c.chunk_index",
                tuple(params),
            )
            neighbors = await cursor.fetchall()
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
                    {
                        "chunk_id": row["chunk_id"],
                        "page": row["page"],
                        "content": row["content"],
                        "file_id": row["file_id"],
                        "chunk_index": row["chunk_index"],
                        "kind": row["kind"],
                        "heading": row["heading"],
                    }
                )
        return sources


router = APIRouter(prefix="/api/v1", tags=["retrieval"])


@router.post("/knowledge-bases/{kb_id}/search", response_model=SearchResponse)
async def search(kb_id: str, body: SearchRequest, request: Request):
    try:
        return await request.app.state.retrieval_service.search(kb_id, body)
    except ChatError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None
