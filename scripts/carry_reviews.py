"""Reuse a finished review on a later run of the same case set; dry-run by default.

A review is stored per evaluation result, so every new run starts as `pending` even
when the questions and the answers are unchanged. Copying a verdict onto a different
answer would fabricate evidence, so a case is only carried when the answer text is
byte-identical; everything else is reported for a real re-review.

The copied review keeps its original reviewer and adds `review_method=carried` plus
`carried_from=<evaluation_id>:<index>`, so a carried record never looks like somebody
re-read this run.
"""

import argparse
import asyncio
import hashlib

from psycopg.types.json import Jsonb

from app.config import Settings
from app.database import Database

PROVENANCE = ("reviewer", "reviewed_at", "review_method", "carried_from")


def answer_hash(result):
    answer = ((result.get("run") or {}).get("answer") or "").strip()
    return hashlib.sha256(answer.encode()).hexdigest()


def is_reviewed(result):
    return (result.get("review") or {}).get("verdict", "pending") != "pending"


async def load(database, evaluation_id):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT evaluation_id,status,request,created_at FROM evaluations "
            "WHERE evaluation_id::text LIKE %s ORDER BY created_at DESC",
            (evaluation_id + "%",),
        )
        evaluations = await cursor.fetchall()
        if not evaluations:
            raise SystemExit(f"未找到评测 {evaluation_id}")
        if len(evaluations) > 1:
            raise SystemExit("前缀匹配到多个评测，请给出更长的 ID")
        row = evaluations[0]
        cursor = await database.connection.execute(
            "SELECT case_index,payload FROM evaluation_results "
            "WHERE evaluation_id=%s ORDER BY case_index",
            (row["evaluation_id"],),
        )
        results = {r["case_index"]: r["payload"] for r in await cursor.fetchall()}
    return row, results


async def sources(database, row):
    """Completed runs of the same case set, newest first, fully reviewed ones first."""
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT e.evaluation_id,e.created_at FROM evaluations e "
            "WHERE e.request->>'case_set_id'=%s AND e.evaluation_id<>%s AND e.status='completed' "
            "ORDER BY e.created_at DESC",
            (row["request"].get("case_set_id"), row["evaluation_id"]),
        )
        candidates = await cursor.fetchall()
        found = []
        for candidate in candidates:
            cursor = await database.connection.execute(
                "SELECT case_index,payload FROM evaluation_results "
                "WHERE evaluation_id=%s ORDER BY case_index",
                (candidate["evaluation_id"],),
            )
            payloads = {r["case_index"]: r["payload"] for r in await cursor.fetchall()}
            if payloads and all(is_reviewed(p) for p in payloads.values()):
                found.append((candidate, payloads))
    return found


def build(targets, reviewed_runs):
    carried, missing = [], []
    for index, payload in sorted(targets.items()):
        if is_reviewed(payload):
            continue
        if not answer_hash(payload):
            # Nothing was generated, so no reviewer judged this answer; do not fabricate one.
            missing.append(index)
            continue
        query = payload["case"]["query"]
        digest = answer_hash(payload)
        match = None
        for candidate, payloads in reviewed_runs:
            for source_index, source in payloads.items():
                same_case = source["case"]["query"] == query and (
                    not payload["case"].get("case_id")
                    or source["case"].get("case_id") == payload["case"].get("case_id")
                )
                if same_case and source.get("run") and answer_hash(source) == digest:
                    match = (candidate, source_index, source)
                    break
            if match:
                break
        if match:
            candidate, source_index, source = match
            review = {k: v for k, v in source["review"].items() if k not in PROVENANCE}
            review.update(
                reviewer=source["review"].get("reviewer"),
                reviewed_at=source["review"].get("reviewed_at"),
                review_method="carried",
                carried_from=f"{candidate['evaluation_id']}:{source_index}",
            )
            carried.append((index, review))
        else:
            missing.append(index)
    return carried, missing


async def main(evaluation: str, apply: bool):
    database = Database(Settings())
    async with database.pool:
        row, targets = await load(database, evaluation)
        if row["status"] != "completed":
            raise SystemExit("评测尚未结束，不能继承审核。")
        reviewed_runs = await sources(database, row)
        if not reviewed_runs:
            raise SystemExit("同一案例集没有已完成且全部审核的评测可供继承。")
        carried, missing = build(targets, reviewed_runs)

        print(f"评测 {row['evaluation_id']}（{len(targets)} 题）")
        for index, review in carried:
            print(f"  继承 {index}: {review['carried_from']} -> {review['verdict']}")
        if missing:
            print(f"  需重新审核 {len(missing)} 题: {missing}（回答已变化，不能复制判定）")
        if not apply:
            print("预演结束；加 --apply 写入继承结果。")
            return
        if not carried:
            print("没有可写入的继承结果。")
            return
        async with database.transaction():
            for index, review in carried:
                await database.connection.execute(
                    "UPDATE evaluation_results SET payload=jsonb_set(payload,'{review}',%s::jsonb),"
                    "updated_at=now() WHERE evaluation_id=%s AND case_index=%s",
                    (Jsonb(review), row["evaluation_id"], index),
                )
        print(f"已写入 {len(carried)} 条继承审核。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", required=True, help="目标评测 ID（可只用前缀）")
    parser.add_argument("--apply", action="store_true", help="写入数据库")
    args = parser.parse_args()
    asyncio.run(main(args.evaluation, args.apply))
