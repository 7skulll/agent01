"""一次性压缩历史评测事件：retrieval_end 不再保留 trace 和来源正文。

新事件已由 run_graph 在 capture_trace 模式下直接精简；这个脚本只处理旧数据。
执行后建议运行 VACUUM (FULL, ANALYZE) run_events 归还文件空间。
"""

import argparse
import asyncio

from app.config import Settings
from app.database import Database

COUNT_SQL = (
    "SELECT COUNT(*) AS n FROM run_events e JOIN runs r ON r.run_id=e.run_id "
    "WHERE r.capture_trace AND e.event='retrieval_end' "
    "AND (e.data::jsonb->'diagnostics' ? 'trace' OR EXISTS ("
    "SELECT 1 FROM jsonb_array_elements(e.data::jsonb->'sources') s "
    "WHERE s ? 'content' OR s ? 'supporting_chunks' OR s ? 'alternatives' "
    "OR s ? 'evidence_chunks'))"
)

COMPACT_SQL = """
UPDATE run_events e
SET data = (
    (e.data::jsonb #- '{diagnostics,trace}')
    || jsonb_build_object(
        'sources',
        COALESCE((
            SELECT jsonb_agg(
                s - 'content' - 'supporting_chunks' - 'alternatives' - 'evidence_chunks'
            )
            FROM jsonb_array_elements(e.data::jsonb->'sources') AS s
        ), '[]'::jsonb)
    )
)::text
FROM runs r
WHERE r.run_id=e.run_id AND r.capture_trace AND e.event='retrieval_end'
AND (e.data::jsonb->'diagnostics' ? 'trace' OR EXISTS (
    SELECT 1 FROM jsonb_array_elements(e.data::jsonb->'sources') s
    WHERE s ? 'content' OR s ? 'supporting_chunks' OR s ? 'alternatives'
    OR s ? 'evidence_chunks'))
"""


async def main(dry_run=False):
    database = Database(Settings())
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(COUNT_SQL)
            count = (await cursor.fetchone())["n"]
        print(f"candidate_events={count}")
        if dry_run or not count:
            return
        async with database.transaction():
            cursor = await database.connection.execute(COMPACT_SQL)
            print(f"updated_events={cursor.rowcount}")
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT pg_size_pretty(pg_total_relation_size('run_events')) AS size"
            )
            print(f"size_before_vacuum={(await cursor.fetchone())['size']}")
        print("run VACUUM (FULL, ANALYZE) run_events; to reclaim file space")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
