"""SQLite 向量持久化与单进程索引；小型知识库使用精确余弦检索。"""

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timezone

from app.errors import ChatError
from app.rag.chunking import chunk_version
from app.rag.embedding import EmbeddingError
from app.tokenization import TokenCounter, embedding_limit
from app.tokenization import embedding_text as embedding_text

logger = logging.getLogger(__name__)
INPUT_VERSION = "title-body-v1"


def index_version(settings, model, legacy_terms=None):
    value = [
        INPUT_VERSION,
        chunk_version(settings),
        model.model_name,
        settings.embedding_base_url,
        settings.embedding_dim,
        settings.embedding_normalize,
    ]
    if legacy_terms:
        value.insert(1, legacy_terms)
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
    def __init__(self, database):
        self.database = database

    async def setup(self):
        async with self.database.transaction():
            await self.database.connection.execute(
                "CREATE TABLE IF NOT EXISTS chunk_vectors ("
                "chunk_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, model TEXT NOT NULL, "
                "dimension INTEGER NOT NULL, index_version TEXT NOT NULL, "
                "input_hash TEXT NOT NULL, "
                "vector TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            await self.database.connection.execute(
                "CREATE INDEX IF NOT EXISTS vectors_file ON chunk_vectors(file_id)"
            )

    async def candidates(self, kb_id, version, file_ids=None):
        if file_ids == []:
            return []
        scope = ""
        params = [kb_id, version]
        if file_ids is not None:
            scope = " AND f.file_id IN (" + ",".join("?" for _ in file_ids) + ")"
            params.extend(file_ids)
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT c.*, f.name, v.vector, v.dimension, v.input_hash "
                "FROM chunk_vectors v JOIN chunks c ON c.chunk_id=v.chunk_id "
                "JOIN files f ON f.file_id=c.file_id "
                "WHERE f.kb_id=? AND f.index_status='indexed' AND v.index_version=? "
                + scope
                + " ORDER BY c.file_id, c.chunk_index",
                tuple(params),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]


class VectorService:
    """外部调用在事务之外；完整文件向量和 indexed 状态一次提交。

    向量化失败回到 recorded，保留稳定 chunk_id；重启也只重试向量阶段。
    """

    def __init__(self, files, index, repository, settings, model, lexical):
        self.files, self.index, self.repository = files, index, repository
        self.settings, self.model = settings, model
        self.lexical = lexical
        self.version = index_version(settings, model) if model else None

    async def reconcile(self):
        async with self.files.database.transaction():
            if self.model:
                # 兼容基础版仅把词项版本混入向量指纹的旧数据；内容/模型/切片完全一致。
                legacy = index_version(self.settings, self.model, "ordered-bigrams-v2")
                await self.files.database.connection.execute(
                    "UPDATE chunk_vectors SET index_version=? WHERE index_version=?",
                    (self.version, legacy),
                )
            await self.files.database.connection.execute(
                "UPDATE files SET index_status='recorded', index_error=NULL "
                "WHERE index_status='indexing'"
            )

    async def pending(self, kb_id, file_id=None):
        async with self.files.database.lock:
            async with self.files.database.connection.execute(
                "SELECT f.file_id FROM files f WHERE f.kb_id=? AND "
                "(f.index_status='recorded' OR (f.index_status='indexed' AND EXISTS "
                "(SELECT 1 FROM chunks c LEFT JOIN chunk_vectors v ON v.chunk_id=c.chunk_id "
                "WHERE c.file_id=f.file_id AND (v.chunk_id IS NULL OR v.index_version!=? "
                "OR (? > 0 AND v.dimension!=?))))) "
                "AND (? IS NULL OR f.file_id=?) ORDER BY f.created_at, f.rowid",
                (kb_id, self.version, self.model.dim or 0, self.model.dim or 0, file_id, file_id),
            ) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def index_file(self, file_id):
        if self.model is None:
            return
        row = await self.files.get(file_id)
        if row is None or row["index_status"] not in ("recorded", "indexed"):
            return
        if row["index_status"] == "indexed" and not await self.pending(row["kb_id"], file_id):
            return
        await self.files.set_status(file_id, "indexing")
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
                result = await self.model.embed_documents(batch)
                dimension = validate_vectors(result, len(batch), dimension)
                vectors.extend(result)
            lexical_rows = await self.lexical.prepare(row, chunks)
            timestamp = datetime.now(timezone.utc).isoformat()
            async with self.files.database.transaction():
                connection = self.files.database.connection
                # 防止外部调用期间切片被重新解析；本服务仅支持单 worker。
                async with connection.execute(
                    "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY chunk_index", (file_id,)
                ) as cursor:
                    ids = [record[0] for record in await cursor.fetchall()]
                if ids != [chunk["chunk_id"] for chunk in chunks]:
                    raise EmbeddingError("切片已变化，请重试向量化。")
                await connection.execute("DELETE FROM chunk_vectors WHERE file_id=?", (file_id,))
                await connection.executemany(
                    "INSERT INTO chunk_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                await self.lexical.replace_within(file_id, lexical_rows)
                await self.files.set_status_within(file_id, "indexed")
        except asyncio.CancelledError:
            await self.files.set_status(file_id, "recorded", "向量化中断，可重试。")
            raise
        except Exception as exc:
            logger.warning("vector_index_failed file_id=%s type=%s", file_id, type(exc).__name__)
            await self.files.set_status(
                file_id,
                "recorded",
                str(exc) if isinstance(exc, EmbeddingError) else "向量保存失败。",
            )

    async def invalidate_within(self, file_id):
        """与新切片一起提交，移除原有向量和词项，防止重解析留下孤儿索引。"""
        connection = self.files.database.connection
        await connection.execute("DELETE FROM chunk_vectors WHERE file_id=?", (file_id,))
        await self.lexical.invalidate_within(file_id)

    def require_model(self):
        if self.model is None:
            raise ChatError("embedding_not_configured", "请先配置 Embedding。", 503)
