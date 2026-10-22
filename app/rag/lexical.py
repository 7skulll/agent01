"""独立的全文派生索引；本地重建不重切片、不调用 Embedding。"""

import asyncio
import hashlib
import json
import unicodedata
from collections import defaultdict

from psycopg.types.json import Jsonb

from app.rag.terms import TERMS_VERSION, document_terms, search_terms


class LexicalIndex:
    def __init__(self, database):
        self.database = database
        self.version = hashlib.sha256(
            json.dumps(["postgres-positional-bm25-kb-v2", TERMS_VERSION]).encode()
        ).hexdigest()

    def query_terms(self, text):
        return search_terms(text)

    async def pending(self, kb_id):
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT f.file_id FROM files f LEFT JOIN lexical_indexes l ON l.file_id=f.file_id "
                "WHERE f.kb_id=%s AND f.index_status IN ('recorded','indexing','indexed') AND "
                "EXISTS (SELECT 1 FROM chunks c WHERE c.file_id=f.file_id) AND "
                "(l.version IS DISTINCT FROM %s OR "
                "l.chunk_version IS DISTINCT FROM f.chunk_version) "
                "ORDER BY f.created_at, f.id",
                (kb_id, self.version),
            )
            return [row["file_id"] for row in await cursor.fetchall()]

    async def has_stale_scope(self, kb_id, file_ids):
        clauses = [
            "f.kb_id=%s",
            "f.index_status IN ('recorded','indexing','indexed')",
            "(l.version IS DISTINCT FROM %s OR l.chunk_version IS DISTINCT FROM f.chunk_version)",
        ]
        params = [kb_id, self.version]
        if file_ids is not None:
            if not file_ids:
                return False
            clauses.append("f.file_id IN (" + ",".join("%s" for _ in file_ids) + ")")
            params.extend(file_ids)
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT 1 FROM files f JOIN chunks c ON c.file_id=f.file_id "
                "LEFT JOIN lexical_indexes l ON l.file_id=f.file_id WHERE "
                + " AND ".join(clauses)
                + " LIMIT 1",
                tuple(params),
            )
            return await cursor.fetchone() is not None

    async def prepare(self, file, chunks):
        def build():
            return [
                (
                    chunk["chunk_id"],
                    file["file_id"],
                    file["kb_id"],
                    " ".join(
                        document_terms(
                            "\n".join(part for part in (chunk["heading"], chunk["content"]) if part)
                        )
                    ),
                )
                for chunk in chunks
            ]

        return await asyncio.to_thread(build)

    async def replace_within(self, file_id, rows, chunk_version):
        await self.invalidate_within(file_id)
        documents, postings = [], []
        for chunk_id, file_id, kb_id, terms in rows:
            tokens = fulltext_tokens(terms)
            documents.append((chunk_id, file_id, kb_id, terms, tokens, len(tokens)))
            positions = defaultdict(list)
            for position, term in enumerate(tokens, 1):
                positions[term].append(position)
            postings.extend((chunk_id, term, indexes) for term, indexes in positions.items())
        await self.database.connection.executemany(
            "INSERT INTO chunk_terms(chunk_id,file_id,kb_id,terms,tokens,token_count) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            documents,
        )
        await self.database.connection.executemany(
            "INSERT INTO lexical_postings(chunk_id,term,positions) VALUES (%s,%s,%s)", postings
        )
        await self.database.connection.execute(
            "INSERT INTO lexical_indexes(file_id,version,chunk_version,chunk_count,token_count) "
            "VALUES (%s,%s,%s,%s,%s)",
            (file_id, self.version, chunk_version, len(documents), sum(d[-1] for d in documents)),
        )

    async def invalidate_within(self, file_id):
        await self.database.connection.execute(
            "DELETE FROM chunk_terms WHERE file_id=%s", (file_id,)
        )
        await self.database.connection.execute(
            "DELETE FROM lexical_indexes WHERE file_id=%s", (file_id,)
        )

    async def search(self, kb_id, file_ids, terms, limit):
        return (await self.search_many(kb_id, file_ids, [terms], limit))[0]

    async def search_many(self, kb_id, file_ids, queries, limit):
        """一次统计语料；每条查询独立评分、限额，供调用方执行路内 RRF。"""
        phrases = [
            [tokens for term in terms if (tokens := fulltext_tokens(term))] for terms in queries
        ]
        results = [[] for _ in queries]
        if not any(phrases) or file_ids == []:
            return results
        async with self.database.read():
            cursor = await self.database.connection.execute(
                BM25_SQL, (Jsonb(phrases), kb_id, self.version, file_ids, limit)
            )
            for row in await cursor.fetchall():
                results[row.pop("query_id") - 1].append(row)
        return results

    async def character_fallback(self, kb_id, file_ids, queries, limit):
        """仅补充无 BM25 命中的查询；使用 GIN 字符词项，不扫描 LIKE。"""
        async with self.database.read():
            cursor = await self.database.connection.execute(
                CHARACTER_SQL, (Jsonb(queries), kb_id, file_ids, limit)
            )
            results = [[] for _ in queries]
            for row in await cursor.fetchall():
                results[row.pop("query_id") - 1].append(row)
        return results

    async def rebuild(self, file_id, files, index):
        file = await files.get(file_id)
        if not file or file["index_status"] not in ("recorded", "indexing", "indexed"):
            return
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT version,chunk_version FROM lexical_indexes WHERE file_id=%s", (file_id,)
            )
            current = await cursor.fetchone()
        if (
            current
            and current["version"] == self.version
            and current["chunk_version"] == file["chunk_version"]
        ):
            return
        chunks = await index.list_by_file(file_id)
        if not chunks:
            return
        rows = await self.prepare(file, chunks)
        async with self.database.transaction():
            cursor = await self.database.connection.execute(
                "SELECT chunk_id FROM chunks WHERE file_id=%s ORDER BY chunk_index", (file_id,)
            )
            ids = [row["chunk_id"] for row in await cursor.fetchall()]
            cursor = await self.database.connection.execute(
                "SELECT index_status,chunk_version FROM files WHERE file_id=%s", (file_id,)
            )
            current_file = await cursor.fetchone()
            if (
                ids != [chunk["chunk_id"] for chunk in chunks]
                or not current_file
                or current_file["index_status"] not in ("recorded", "indexing", "indexed")
                or current_file["chunk_version"] != file["chunk_version"]
            ):
                raise ValueError("切片或文件状态已变化，请重试全文索引。")
            await self.replace_within(file_id, rows, file["chunk_version"])
            await self.database.connection.execute(
                "UPDATE files SET lexical_error=NULL WHERE file_id=%s", (file_id,)
            )

    async def ready_files(self, file_ids):
        """就绪仅依赖全文版本和已发布切片，不检查向量或当前 Embedding 配置。"""
        async with self.database.read():
            cursor = await self.database.connection.execute(
                "SELECT f.file_id FROM files f JOIN lexical_indexes l ON l.file_id=f.file_id "
                "WHERE f.file_id=ANY(%s) AND f.index_status IN ('recorded','indexing','indexed') "
                "AND l.version=%s AND l.chunk_version=f.chunk_version",
                (file_ids, self.version),
            )
            return {row["file_id"] for row in await cursor.fetchall()}


def fulltext_tokens(text):
    """FTS 风格 Unicode 字母/数字切词，统一小写及去音调，不新增中文分词。"""
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn"
    )
    tokens, word = [], []
    for char in normalized + " ":
        if char.isalnum() or unicodedata.category(char) == "Co":
            word.append(char)
        elif word:
            tokens.append("".join(word))
            word = []
    return tokens


# IDF/平均长度限定在目标知识库的有效全文版本；临时 file_ids 只过滤候选，不改变统计。
# 使用位置列表计算短语频次，避免把 3.14 等原短语条件放宽成任意词 OR。
BM25_SQL = """
WITH queries AS (
 SELECT ordinality AS query_id, row_number() OVER () AS qid,
 ARRAY(SELECT jsonb_array_elements_text(phrase)) AS tokens
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS q(value,ordinality)
 CROSS JOIN LATERAL jsonb_array_elements(q.value) AS phrase
), eligible_files AS MATERIALIZED (
 SELECT f.file_id,f.kb_id,l.chunk_count,l.token_count FROM files f
 JOIN lexical_indexes l ON l.file_id=f.file_id
 WHERE f.kb_id=%s
 AND f.index_status IN ('recorded','indexing','indexed')
 AND l.version=%s AND l.chunk_version=f.chunk_version
), matches AS (
 SELECT q.query_id,q.qid,t.chunk_id,t.token_count,count(*)::double precision AS tf
 FROM queries q JOIN lexical_postings p ON p.term=q.tokens[1]
 JOIN chunk_terms t ON t.chunk_id=p.chunk_id
 JOIN eligible_files e ON e.file_id=t.file_id AND e.kb_id=t.kb_id
 CROSS JOIN LATERAL unnest(p.positions) AS pos
 WHERE t.tokens[pos:pos+cardinality(q.tokens)-1]=q.tokens
 GROUP BY q.query_id,q.qid,t.chunk_id,t.token_count
), dfs AS (SELECT qid,count(*)::double precision AS df FROM matches GROUP BY qid),
corpus AS (
 SELECT sum(chunk_count)::double precision AS n,
 sum(token_count)::double precision/NULLIF(sum(chunk_count),0) AS avgdl FROM eligible_files
),
scores AS (
 SELECT m.query_id,m.chunk_id,-sum(
 GREATEST(ln((s.n-d.df+0.5)/(d.df+0.5)),0.000001)
 * (m.tf*2.2)/(m.tf+1.2*(0.25+0.75*m.token_count/NULLIF(s.avgdl,0)))) AS lexical_score
 FROM matches m JOIN dfs d ON d.qid=m.qid
 CROSS JOIN corpus s GROUP BY m.query_id,m.chunk_id
), ranked AS (
 SELECT s.*,row_number() OVER (
 PARTITION BY s.query_id ORDER BY s.lexical_score,s.chunk_id) AS position
 FROM scores s JOIN chunks c ON c.chunk_id=s.chunk_id
 WHERE c.file_id=ANY(COALESCE(%s::text[],ARRAY[c.file_id]))
)
SELECT c.*,f.name,r.lexical_score,r.query_id FROM ranked r
JOIN chunks c ON c.chunk_id=r.chunk_id JOIN files f ON f.file_id=c.file_id
WHERE r.position<=%s ORDER BY r.query_id,r.position
"""
CHARACTER_SQL = """
WITH queries AS (
 SELECT ordinality AS query_id,rag_character_terms(value #>> '{}') AS terms
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY
), ranked AS (
 SELECT q.query_id,c.chunk_id,-overlap.hits::float/cardinality(q.terms) AS lexical_score,
 row_number() OVER (PARTITION BY q.query_id
 ORDER BY overlap.hits DESC,
 overlap.hits::float/cardinality(c.character_terms) DESC,c.chunk_id) AS position
 FROM queries q JOIN chunks c ON c.character_terms && q.terms
 JOIN files f ON f.file_id=c.file_id
 CROSS JOIN LATERAL (
 SELECT count(*) AS hits FROM unnest(q.terms) AS term WHERE term=ANY(c.character_terms)
 ) AS overlap
 WHERE f.kb_id=%s AND f.index_status IN ('recorded','indexing','indexed')
 AND c.file_id=ANY(COALESCE(%s::text[],ARRAY[c.file_id]))
 AND overlap.hits>=GREATEST(1,ceil(cardinality(q.terms)*0.5))
)
SELECT c.*,f.name,r.lexical_score,r.query_id FROM ranked r
JOIN chunks c ON c.chunk_id=r.chunk_id JOIN files f ON f.file_id=c.file_id
WHERE r.position<=%s ORDER BY r.query_id,r.position
"""
