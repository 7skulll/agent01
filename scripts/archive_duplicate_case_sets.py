"""归档重复测评集；默认只预演，加 --apply 才写入。

当前整理决定：CRUD-RAG-60-v1 为主库，CRUD-RAG-reviewed-v1 作为历史副本归档。
归档只改 archived 标记，不删除记录，历史评测仍可按 case_set_id 读取和重试。
"""

import argparse
import asyncio

from app.config import Settings
from app.database import Database

DEFAULT_KB = "CRUD-RAG-reviewed-v1"


async def main(kb_name: str, apply: bool):
    database = Database(Settings())
    async with database.pool:
        async with database.read():
            cursor = await database.connection.execute(
                "SELECT c.case_set_id,c.name,c.archived,"
                "(SELECT COUNT(*) FROM evaluations e "
                " WHERE e.request->>'case_set_id'=c.case_set_id) AS refs "
                "FROM evaluation_case_sets c JOIN knowledge_bases k ON k.kb_id=c.kb_id "
                "WHERE k.name=%s ORDER BY c.name",
                (kb_name,),
            )
            rows = await cursor.fetchall()
        print(f"kb={kb_name} sets={len(rows)}")
        for row in rows:
            print(
                f"  {row['name']} archived={row['archived']} refs={row['refs']}"
            )
        if not apply:
            print("dry-run; add --apply to archive")
            return
        async with database.transaction():
            cursor = await database.connection.execute(
                "UPDATE evaluation_case_sets c SET archived=TRUE "
                "FROM knowledge_bases k "
                "WHERE k.kb_id=c.kb_id AND k.name=%s AND c.archived=FALSE",
                (kb_name,),
            )
            print(f"archived={cursor.rowcount}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", default=DEFAULT_KB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.kb, args.apply))
