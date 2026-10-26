"""归档重复知识库；默认只预演，加 --apply 才写入。

只改 knowledge_bases.archived，不删除文件、索引、测评集或历史评测。
归档后默认不出现在知识库选择器，新检索/问答/评测会被 require_knowledge_base 拒绝。
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
                "SELECT kb_id,name,archived FROM knowledge_bases WHERE name=%s", (kb_name,)
            )
            row = await cursor.fetchone()
        if not row:
            raise SystemExit(f"knowledge base not found: {kb_name}")
        print(f"kb={row['kb_id']} name={row['name']} archived={row['archived']}")
        if not apply:
            print("dry-run; add --apply to archive")
            return
        async with database.transaction():
            await database.connection.execute(
                "UPDATE knowledge_bases SET archived=TRUE WHERE kb_id=%s", (row["kb_id"],)
            )
        print("archived=true")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", default=DEFAULT_KB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.kb, args.apply))
