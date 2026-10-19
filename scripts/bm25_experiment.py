"""在同一临时库、相同向量上做 BM25 消融；不进入生产检索链路。"""

import asyncio
import hashlib
import json
import re

from app.rag.lexical import LexicalIndex
from app.tokenization import embedding_text


def baseline_terms(text):
    """仅在离线实验中保留旧分析器，用于重现基础版结果。"""
    terms = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.lower())
    return [
        token
        for term in terms
        for token in (
            [term]
            if not re.fullmatch(r"[\u3400-\u9fff]+", term) or len(term) < 2
            else [term[i : i + 2] for i in range(len(term) - 1)]
        )
    ]


class BaselineLexicalIndex(LexicalIndex):
    """只用于历史双字词项对照；其余检索设置与当前流程一致。"""

    def __init__(self, database):
        super().__init__(database)
        self.version = "experiment:baseline-bigrams-v1"

    def query_terms(self, text):
        return list(dict.fromkeys(baseline_terms(text)))

    async def prepare(self, file, chunks):
        return await asyncio.to_thread(
            lambda: [
                (
                    chunk["chunk_id"],
                    file["file_id"],
                    file["kb_id"],
                    " ".join(baseline_terms(embedding_text({**chunk, "name": file["name"]}))),
                )
                for chunk in chunks
            ]
        )


async def vector_fingerprint(database):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT chunk_id,vector,input_hash,index_version FROM chunk_vectors ORDER BY chunk_id"
        )
        rows = [tuple(row.values()) for row in await cursor.fetchall()]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


async def compare_bm25(app, client, base, dataset, file_ids, min_score):
    from scripts.eval_retrieval import measure_modes

    service = app.state.retrieval_service
    index_service = app.state.index_service
    database = service.vectors.database
    original = await vector_fingerprint(database)
    results = {}
    for label, lexical in [
        ("baseline", BaselineLexicalIndex(database)),
        ("fastgpt", LexicalIndex(database)),
    ]:
        async with database.transaction():
            await database.connection.execute("DELETE FROM chunk_terms")
            await database.connection.execute("DELETE FROM lexical_indexes")
        service.lexical = lexical
        for file_id in file_ids:
            await lexical.rebuild(file_id, index_service.files, index_service.index)
        print("Variant:", label, flush=True)
        runs = await measure_modes(client, base, dataset, file_ids, min_score)
        current = await vector_fingerprint(database)
        if current != original:
            raise AssertionError("BM25 实验不得改变向量。")
        results[label] = {
            "analyzer": "historical_bigrams" if label == "baseline" else "jieba-rs-hmm",
            "pipeline": "fastgpt_text_recall",
            "vector_fingerprint": current,
            "runs": runs,
        }
    return results
