"""Write reviewer-supplied judgments into one evaluation; dry-run by default.

The input file is a JSON object with `reviewer`, `reviewed_at`, `review_method` and a
`reviews` list of `{index, answer_sha256, review}`. Each entry is checked against the
stored answer before writing, so a judgment made while looking at a different answer is
rejected instead of silently attached to this run.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from app.config import Settings
from app.database import Database
from app.evaluation_models import Review

PROVENANCE = ("reviewer", "reviewed_at", "review_method", "carried_from")


def answer_hash(result):
    answer = ((result.get("run") or {}).get("answer") or "").strip()
    return hashlib.sha256(answer.encode()).hexdigest()


def is_reviewed(result):
    return (result.get("review") or {}).get("verdict", "pending") != "pending"


async def main(evaluation: str, path: Path, apply: bool, overwrite: bool):
    document = json.loads(path.read_text(encoding="utf-8"))
    database = Database(Settings())
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT evaluation_id,status FROM evaluations "
                "WHERE evaluation_id::text LIKE %s ORDER BY created_at DESC",
                (evaluation + "%",),
            )
            rows = await cursor.fetchall()
            if len(rows) != 1:
                raise SystemExit("评测 ID 前缀需唯一匹配一条记录")
            evaluation_id = rows[0]["evaluation_id"]
            if rows[0]["status"] != "completed":
                raise SystemExit("评测尚未结束，不能写入审核。")
            cursor = await database.connection.execute(
                "SELECT case_index,payload FROM evaluation_results "
                "WHERE evaluation_id=%s ORDER BY case_index",
                (evaluation_id,),
            )
            results = {r["case_index"]: r["payload"] for r in await cursor.fetchall()}

        writes, rejected = [], []
        for entry in document["reviews"]:
            index = entry["index"]
            payload = results.get(index)
            if payload is None:
                rejected.append((index, "案例不存在"))
                continue
            if not (payload.get("run") or {}).get("answer"):
                rejected.append((index, "没有生成回答，不能审核"))
                continue
            if answer_hash(payload) != entry["answer_sha256"]:
                rejected.append((index, "回答已变化，判定对应的是旧答案"))
                continue
            if is_reviewed(payload) and not overwrite:
                rejected.append((index, "已有审核记录，需要 --overwrite"))
                continue
            review = Review.model_validate(entry["review"]).model_dump()
            for key in PROVENANCE:
                review[key] = entry.get(key, document.get(key, review.get(key)))
            writes.append((index, review))

        print(f"评测 {evaluation_id}：待写入 {len(writes)} 条，拒绝 {len(rejected)} 条")
        for index, review in writes:
            print(
                f"  {index}: {review['verdict']} / {review['fact_coverage']}"
                f" ({review['review_method']})"
            )
        for index, reason in rejected:
            print(f"  拒绝 {index}: {reason}")
        if not apply:
            print("预演结束；加 --apply 写入。")
            return
        async with database.transaction():
            for index, review in writes:
                await database.connection.execute(
                    "UPDATE evaluation_results SET payload=jsonb_set(payload,'{review}',%s::jsonb),"
                    "updated_at=now() WHERE evaluation_id=%s AND case_index=%s",
                    (Jsonb(review), evaluation_id, index),
                )
        print(f"已写入 {len(writes)} 条审核。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", required=True, help="目标评测 ID（可只用前缀）")
    parser.add_argument("--file", required=True, type=Path, help="审核 JSON 文件")
    parser.add_argument("--apply", action="store_true", help="写入数据库")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有审核记录")
    args = parser.parse_args()
    asyncio.run(main(args.evaluation, args.file, args.apply, args.overwrite))
