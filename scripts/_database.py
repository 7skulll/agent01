"""评估脚本在独立 PostgreSQL schema 内运行，结束后清理自己的数据。"""

from contextlib import asynccontextmanager
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr


@asynccontextmanager
async def temporary_database(settings):
    schema = "eval_" + uuid4().hex
    async with await AsyncConnection.connect(
        settings.database_url.get_secret_value(), autocommit=True
    ) as connection:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield SecretStr(
                make_conninfo(
                    settings.database_url.get_secret_value(),
                    options=f"-c search_path={schema},public",
                )
            )
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
