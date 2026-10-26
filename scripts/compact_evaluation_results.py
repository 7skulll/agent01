"""一次性压缩历史评测结果：trace 只保留候选、排名和分数。

新结果在保存时已经压缩；这个脚本只处理旧数据。执行后建议运行
VACUUM (FULL, ANALYZE) evaluation_results 归还文件空间。
"""

import argparse
import asyncio
import json

from psycopg.types.json import Jsonb

from app.config import Settings
from app.database import Database
from app.evaluations import compact_trace


async def main(dry_run=False):
    database = Database(Settings())
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT evaluation_id,case_index,payload FROM evaluation_results "
                "ORDER BY evaluation_id,case_index"
            )
            rows = await cursor.fetchall()

        candidates, saved = [], 0
        for row in rows:
            payload = row["payload"]
            before = len(json.dumps(payload, ensure_ascii=False))
            if compact_trace(payload.get("diagnostics")):
                after = len(json.dumps(payload, ensure_ascii=False))
                candidates.append((row["evaluation_id"], row["case_index"], payload))
                saved += before - after
        print(f"candidate_rows={len(candidates)} estimated_bytes_saved={saved}")
        if dry_run or not candidates:
            return

        for start in range(0, len(candidates), 50):
            batch = candidates[start : start + 50]
            async with database.transaction():
                await database.connection.executemany(
                    "UPDATE evaluation_results SET payload=%s "
                    "WHERE evaluation_id=%s AND case_index=%s",
                    [
                        (Jsonb(payload), evaluation_id, index)
                        for evaluation_id, index, payload in batch
                    ],
                )
            print(f"updated_rows={min(start + 50, len(candidates))}")
        print("run VACUUM (FULL, ANALYZE) evaluation_results; to reclaim file space")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
