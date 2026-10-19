"""PostgreSQL 连接池与显式事务作用域；连接不在协程之间共享。"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Connection:
    """稳定的事务连接入口，便于统一提交、回滚与故障注入。"""

    def __init__(self, database):
        self.database = database

    @property
    def raw(self):
        conn = self.database.bound_connection()
        if conn is None:
            raise RuntimeError("数据库操作必须位于 read() 或 transaction() 作用域内。")
        return conn

    @property
    def in_transaction(self):
        conn = self.database.bound_connection()
        return conn is not None and conn.info.transaction_status != TransactionStatus.IDLE

    async def execute(self, statement, params=None, **kwargs):
        return await self.raw.execute(statement, params, **kwargs)

    async def executemany(self, statement, params):
        async with self.raw.cursor() as cursor:
            await cursor.executemany(statement, params)

    async def commit(self):
        await self.raw.commit()

    async def rollback(self):
        await self.raw.rollback()


class Database:
    def __init__(self, settings):
        self.current = ContextVar("postgres_connection", default=None)
        self.connection = Connection(self)
        self.pool = AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            open=False,
            min_size=1,
            max_size=settings.database_pool_size,
            timeout=10,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        )

    def bound_connection(self):
        value = self.current.get()
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return None
        return value[1] if value and value[0] is task else None

    @asynccontextmanager
    async def read(self):
        if self.bound_connection() is not None:
            yield
            return
        async with self.pool.connection() as conn:
            token = self.current.set((asyncio.current_task(), conn))
            try:
                yield
            finally:
                self.current.reset(token)

    @asynccontextmanager
    async def transaction(self):
        if self.connection.in_transaction:
            raise RuntimeError("不允许嵌套业务事务。")
        async with self.read():
            try:
                await self.connection.execute("BEGIN")
                yield
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def setup(self):
        async with self.transaction():
            await self.connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(current_database() || ':migrate',0))"
            )
            await self.connection.execute(
                "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"
            )
            await self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY)"
            )
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                cursor = await self.connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)
                )
                if await cursor.fetchone() is None:
                    await self.connection.execute(path.read_text(), prepare=False)
                    await self.connection.execute(
                        "INSERT INTO schema_migrations VALUES (%s)", (path.name,)
                    )

    @asynccontextmanager
    async def worker_ownership(self):
        """现有调度器是单进程模型；数据库锁阻止第二实例误恢复仍在运行的任务。"""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(current_database() || ':' || "
                "current_schema() || ':worker',0)) AS acquired"
            )
            if not (await cursor.fetchone())["acquired"]:
                raise RuntimeError("该数据库已有运行中的应用实例；当前仅支持单 worker。")
            try:
                yield
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(current_database() || ':' "
                    "|| current_schema() || ':worker',0))"
                )
