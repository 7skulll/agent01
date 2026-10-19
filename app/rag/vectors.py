"""PostgreSQL/pgvector 向量持久化与单进程索引；小型知识库使用精确余弦检索。"""

import asyncio
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone

from psycopg import sql

from app.errors import ChatError
from app.observability import Metrics
from app.rag.chunking import chunk_version
from app.rag.embedding import EmbeddingError
from app.tokenization import TokenCounter, embedding_limit, embedding_text

logger = logging.getLogger(__name__)
INPUT_VERSION = "title-body-v1"


def index_version(settings, model):
    value = [
        INPUT_VERSION,
        chunk_version(settings),
        model.model_name,
        settings.embedding_base_url,
        settings.embedding_dim,
        settings.embedding_normalize,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def validate_vectors(vectors, count, dim=None):
    """拒绝数量错误、维度变化、非有限值和零向量，保护入库及检索。"""
    if len(vectors) != count or not vectors:
        raise EmbeddingError("Embedding 返回向量数量与输入不一致。")
    actual = len(vectors[0])
    if not actual or (dim and actual != dim):
        raise EmbeddingError("Embedding 返回向量维度不符。")
    for vector in vectors:
        if len(vector) != actual or not all(math.isfinite(v) for v in vector):
            raise EmbeddingError("Embedding 返回了无效向量。")
        norm = math.hypot(*vector)
        if not norm or not math.isfinite(norm):
            raise EmbeddingError("Embedding 返回了零向量或超出有限范围。")
    return actual


class VectorRepository:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    async def ensure_hnsw(self, version, dimension):
        """精确路径不建 ANN；显式启用时为模型版本/维度创建独立表达式索引。"""
        if self.settings.vector_search_mode != "hnsw":
            return
        if not 1 <= dimension <= 2000:
            raise ValueError("HNSW vector 索引要求明确配置 1–2000 维；其他维度先使用 exact。")
        name = "vectors_hnsw_" + hashlib.sha256(f"{version}:{dimension}".encode()).hexdigest()[:24]
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            )
            installed = tuple(
                int(x) for x in (await cursor.fetchone())["extversion"].split(".")[:2]
            )
            if installed < (0, 8):
                raise ValueError("带过滤的 HNSW 需要 pgvector >= 0.8 的迭代扫描。")
            cursor = await self.database.connection.execute(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() AND c.relname=%s",
                (name,),
            )
            existing = await cursor.fetchone()
            if existing is not None and not existing["indisvalid"]:
                # CONCURRENTLY 建索引中断会留下 invalid 索引，必须先移除再重试。
                await self.database.connection.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY {}").format(sql.Identifier(name))
                )
            await self.database.connection.execute(
                sql.SQL(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON chunk_vectors USING hnsw "
                    "((vector::public.vector({})) public.vector_cosine_ops) "
                    "WHERE dimension={} AND index_version={}"
                ).format(
                    sql.Identifier(name),
                    sql.Literal(dimension),
                    sql.Literal(dimension),
                    sql.Literal(version),
                )
            )

    async def search(self, kb_id, version, vectors, limit, file_ids=None):
        """默认精确余弦；HNSW 使用类型匹配的表达式和迭代过滤扫描。"""
        if file_ids == []:
            return [[] for _ in vectors], {}
        lists, rows = [], {}
        approximate = self.settings.vector_search_mode == "hnsw"
        for vector in vectors:
            dimension = len(vector)
            if approximate and not 1 <= dimension <= 2000:
                raise EmbeddingError("HNSW 查询向量维度超出支持范围。")
            distance = (
                f"(v.vector::public.vector({dimension})) <=> %s::public.vector({dimension})"
                if approximate
                else "v.vector <=> %s::public.vector"
            )
            clauses = [
                sql.SQL("f.kb_id=%s"),
                sql.SQL("f.index_status='indexed'"),
                sql.SQL("v.dimension=%s"),
                sql.SQL("v.index_version={}").format(sql.Literal(version)),
            ]
            params = [json.dumps(vector), kb_id, dimension]
            if file_ids is not None:
                clauses.append(sql.SQL("f.file_id=ANY(%s)"))
                params.append(file_ids)
            if approximate:
                params.append(json.dumps(vector))
            params.append(limit)
            order = distance if approximate else "vector_score DESC,c.chunk_id"
            statement = sql.SQL(
                "SELECT c.*,f.name,1-(" + distance + ") AS vector_score "
                "FROM chunk_vectors v JOIN chunks c ON c.chunk_id=v.chunk_id "
                "JOIN files f ON f.file_id=c.file_id WHERE {} ORDER BY " + order + " LIMIT %s"
            ).format(sql.SQL(" AND ").join(clauses))
            scope = self.database.transaction if approximate else self.database.read
            async with scope():
                if approximate:
                    await self.database.connection.execute(
                        "SELECT set_config('hnsw.ef_search',%s,true)",
                        (str(self.settings.hnsw_ef_search),),
                    )
                    await self.database.connection.execute(
                        "SET LOCAL hnsw.iterative_scan='strict_order'"
                    )
                cursor = await self.database.connection.execute(statement, tuple(params))
                hits = await cursor.fetchall()
            if approximate:
                hits.sort(key=lambda row: (-row["vector_score"], row["chunk_id"]))
            rows.update({row["chunk_id"]: row for row in hits})
            lists.append([(row["chunk_id"], row["vector_score"]) for row in hits])
        return lists, rows


class VectorService:
    """外部调用在事务之外；完整文件向量和 indexed 状态一次提交。

    向量化失败回到 recorded，保留稳定 chunk_id；重启也只重试向量阶段。
    """

    def __init__(self, files, index, settings, model, *, metrics=None):
        self.files, self.index = files, index
        self.settings, self.model = settings, model
        self.version = index_version(settings, model) if model else None
        self.metrics = metrics or Metrics()
        self.counter = TokenCounter(settings.embedding_tokenizer)

    async def reconcile(self):
        async with self.files.database.transaction():
            await self.files.database.connection.execute(
                "UPDATE files SET index_status='recorded', index_error=NULL "
                "WHERE index_status='indexing'"
            )

    async def pending(self, kb_id, file_id=None):
        async with self.files.database.read():
            cursor = await self.files.database.connection.execute(
                "SELECT f.file_id FROM files f WHERE f.kb_id=%s AND "
                "(f.index_status='recorded' OR (f.index_status='indexed' AND EXISTS "
                "(SELECT 1 FROM chunks c LEFT JOIN chunk_vectors v ON v.chunk_id=c.chunk_id "
                "WHERE c.file_id=f.file_id AND (v.chunk_id IS NULL OR v.index_version!=%s "
                "OR (%s > 0 AND v.dimension!=%s))))) "
                "AND (%s::text IS NULL OR f.file_id=%s) ORDER BY f.created_at, f.id",
                (kb_id, self.version, self.model.dim or 0, self.model.dim or 0, file_id, file_id),
            )
            return [row["file_id"] for row in await cursor.fetchall()]

    async def index_file(self, file_id):
        if self.model is None:
            return
        row = await self.files.get(file_id)
        if row is None or row["index_status"] not in ("recorded", "indexed"):
            return
        if row["index_status"] == "indexed" and not await self.pending(row["kb_id"], file_id):
            return
        await self.files.set_status(file_id, "indexing")
        started, outcome = time.perf_counter(), "error"
        try:
            chunks = await self.index.list_by_file(file_id)
            if not chunks:
                raise EmbeddingError("文件没有可向量化的切片，请重新解析。")
            texts = [embedding_text({**chunk, "name": row["name"]}) for chunk in chunks]
            limit = embedding_limit(self.settings)
            counter = TokenCounter(self.settings.embedding_tokenizer)
            if limit and any(counter.count(text) > limit for text in texts):
                raise EmbeddingError("最终 Embedding 输入超过预算，请重新切片。")
            vectors = []
            dimension = self.model.dim
            for start in range(0, len(texts), self.settings.embedding_batch_size):
                batch = texts[start : start + self.settings.embedding_batch_size]
                self.metrics.tokens(
                    "embedding_documents", estimated_input=sum(counter.count(t) for t in batch)
                )
                with self.metrics.span("embedding_documents"):
                    result = await self.model.embed_documents(batch)
                dimension = validate_vectors(result, len(batch), dimension)
                vectors.extend(result)
            timestamp = datetime.now(timezone.utc).isoformat()
            async with self.files.database.transaction():
                connection = self.files.database.connection
                # 防止外部调用期间切片被重新解析；本服务仅支持单 worker。
                cursor = await connection.execute(
                    "SELECT chunk_id FROM chunks WHERE file_id=%s ORDER BY chunk_index", (file_id,)
                )
                ids = [record["chunk_id"] for record in await cursor.fetchall()]
                if ids != [chunk["chunk_id"] for chunk in chunks]:
                    raise EmbeddingError("切片已变化，请重试向量化。")
                await connection.execute("DELETE FROM chunk_vectors WHERE file_id=%s", (file_id,))
                await connection.executemany(
                    "INSERT INTO chunk_vectors VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            chunk["chunk_id"],
                            file_id,
                            self.model.model_name,
                            dimension,
                            self.version,
                            hashlib.sha256(text.encode()).hexdigest(),
                            json.dumps(vector),
                            timestamp,
                        )
                        for chunk, text, vector in zip(chunks, texts, vectors)
                    ],
                )
                await self.files.set_status_within(file_id, "indexed")
            outcome = "ok"
        except asyncio.CancelledError:
            outcome = "cancelled"
            await self.files.set_status(file_id, "recorded", "向量化中断，可重试。")
            raise
        except Exception as exc:
            logger.warning("vector_index_failed file_id=%s type=%s", file_id, type(exc).__name__)
            await self.files.set_status(
                file_id,
                "recorded",
                str(exc) if isinstance(exc, EmbeddingError) else "向量保存失败。",
            )
        finally:
            self.metrics.observe("vector_index", time.perf_counter() - started, outcome)

    async def invalidate_within(self, file_id):
        """与新切片一起提交，移除原有向量；全文索引由索引编排层独立管理。"""
        connection = self.files.database.connection
        await connection.execute("DELETE FROM chunk_vectors WHERE file_id=%s", (file_id,))

    def require_model(self):
        if self.model is None:
            raise ChatError("embedding_not_configured", "请先配置 Embedding。", 503)
