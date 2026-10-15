"""独立的全文派生索引；本地重建不重切片、不调用 Embedding。"""

import asyncio
import hashlib
import json

from app.rag.terms import TERMS_VERSION, document_terms, search_terms


class LexicalIndex:
    def __init__(self, database):
        self.database = database
        self.version = hashlib.sha256(
            json.dumps(["fastgpt-single-text-v1", TERMS_VERSION]).encode()
        ).hexdigest()

    def query_terms(self, text):
        return search_terms(text)

    async def setup(self):
        async with self.database.transaction():
            await self.database.connection.execute(
                "CREATE TABLE IF NOT EXISTS lexical_indexes (file_id TEXT PRIMARY KEY, "
                "version TEXT NOT NULL)"
            )
            async with self.database.connection.execute("PRAGMA table_info(chunk_terms)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            expected = {"chunk_id", "file_id", "kb_id", "terms"}
            if columns != expected:
                # 仅替换派生全文索引；原文件、chunks 和向量均保留。
                await self.database.connection.execute("DROP TABLE IF EXISTS chunk_terms")
                await self.database.connection.execute("DELETE FROM lexical_indexes")
            await self.database.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_terms USING fts5("
                "chunk_id UNINDEXED, file_id UNINDEXED, kb_id UNINDEXED, "
                "terms, tokenize='unicode61')"
            )

    async def pending(self, kb_id):
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT f.file_id FROM files f LEFT JOIN lexical_indexes l ON l.file_id=f.file_id "
                "WHERE f.kb_id=? AND f.index_status='indexed' AND "
                "(l.version IS NULL OR l.version!=?) ORDER BY f.created_at, f.rowid",
                (kb_id, self.version),
            ) as cursor:
                return [row[0] for row in await cursor.fetchall()]

    async def has_stale_scope(self, kb_id, file_ids, chunk_version, vector_version):
        clauses = [
            "f.kb_id=?",
            "f.index_status='indexed'",
            "f.chunk_version=?",
            "(l.version IS NULL OR l.version!=?)",
            "(? IS NULL OR v.index_version=?)",
        ]
        params = [kb_id, chunk_version, self.version, vector_version, vector_version]
        if file_ids is not None:
            if not file_ids:
                return False
            clauses.append("f.file_id IN (" + ",".join("?" for _ in file_ids) + ")")
            params.extend(file_ids)
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT 1 FROM files f JOIN chunks c ON c.file_id=f.file_id "
                "JOIN chunk_vectors v ON v.chunk_id=c.chunk_id "
                "LEFT JOIN lexical_indexes l ON l.file_id=f.file_id WHERE "
                + " AND ".join(clauses)
                + " LIMIT 1",
                tuple(params),
            ) as cursor:
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

    async def replace_within(self, file_id, rows):
        await self.invalidate_within(file_id)
        await self.database.connection.executemany(
            "INSERT INTO chunk_terms (chunk_id,file_id,kb_id,terms) VALUES (?,?,?,?)",
            rows,
        )
        await self.database.connection.execute(
            "INSERT INTO lexical_indexes(file_id,version) VALUES (?,?)", (file_id, self.version)
        )

    async def invalidate_within(self, file_id):
        await self.database.connection.execute(
            "DELETE FROM chunk_terms WHERE file_id=?", (file_id,)
        )
        await self.database.connection.execute(
            "DELETE FROM lexical_indexes WHERE file_id=?", (file_id,)
        )

    async def rebuild(self, file_id, files, index):
        file = await files.get(file_id)
        if not file or file["index_status"] != "indexed":
            return
        async with self.database.lock:
            async with self.database.connection.execute(
                "SELECT version FROM lexical_indexes WHERE file_id=?", (file_id,)
            ) as cursor:
                current = await cursor.fetchone()
        if current and current[0] == self.version:
            return
        chunks = await index.list_by_file(file_id)
        rows = await self.prepare(file, chunks)
        async with self.database.transaction():
            async with self.database.connection.execute(
                "SELECT chunk_id FROM chunks WHERE file_id=? ORDER BY chunk_index", (file_id,)
            ) as cursor:
                ids = [row[0] for row in await cursor.fetchall()]
            async with self.database.connection.execute(
                "SELECT index_status FROM files WHERE file_id=?", (file_id,)
            ) as cursor:
                current_file = await cursor.fetchone()
            if (
                ids != [chunk["chunk_id"] for chunk in chunks]
                or not current_file
                or current_file[0] != "indexed"
            ):
                raise ValueError("切片或文件状态已变化，请重试全文索引。")
            await self.replace_within(file_id, rows)
