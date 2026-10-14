import asyncio
import logging
import sqlite3
from functools import wraps

from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from openai import APIError

from app.agent.runtime import AgentRuntime
from app.errors import ChatError, upstream_error
from app.run_graph import build_run_graph
from app.run_repository import GRAPH_VERSION, TERMINAL_STATUSES, RunRepository

logger = logging.getLogger(__name__)


def complete_on_cancel(operation):
    @wraps(operation)
    async def protected(*args, **kwargs):
        task = asyncio.create_task(operation(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    return protected


class RunService:
    def __init__(self, runtime: AgentRuntime, repository: RunRepository):
        self.runtime = runtime
        self.repository = repository
        self.tasks: dict[str, asyncio.Task] = {}
        self.control_lock = asyncio.Lock()
        self.graph = None
        self.closing = False

    def get_graph(self):
        if self.graph is None:
            self.graph = build_run_graph(self.runtime, self.repository)
        return self.graph

    async def reconcile_completed(self):
        # Startup reconciliation only publishes already checkpointed final answers.
        # No unfinished node, model or tool is automatically executed.
        for row in await self.repository.recovering():
            if row["graph_version"] != GRAPH_VERSION:
                continue
            snapshot = await self.get_graph().aget_state(self.config(row["run_id"]))
            if snapshot.next or not snapshot.values.get("answer"):
                continue
            try:
                await self.settle(
                    row["run_id"],
                    "completed",
                    checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                    answer=snapshot.values["answer"],
                    tools_used=snapshot.values["tools_used"],
                )
            except (ChatError, sqlite3.Error) as exc:
                logger.warning(
                    "run_publication_deferred run_id=%s type=%s", row["run_id"], type(exc).__name__
                )

    @staticmethod
    def config(run_id):
        # There are more nodes per loop than create_agent. Call budgets, persisted
        # separately before external calls, remain the primary execution limit.
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 256}

    def start(self, run_id, inputs=None):
        task = asyncio.create_task(self.execute(run_id, inputs), name=f"run:{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda done: self.task_done(run_id, done))

    def task_done(self, run_id, task):
        if self.tasks.get(run_id) is task:
            self.tasks.pop(run_id)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "run_worker_failed run_id=%s type=%s", run_id, type(task.exception()).__name__
            )

    @complete_on_cancel
    async def create(self, conversation_id, body, request_id):
        async with self.control_lock:
            if self.closing:
                raise ChatError("service_stopping", "服务正在关闭。", 503, True)
            if not self.runtime.ready:
                raise ChatError("model_not_configured", "请先配置模型。", 503)
            row, created = await self.repository.create(conversation_id, body, request_id)
            task = self.tasks.get(row["run_id"])
            if created:
                self.start(row["run_id"], "new")
            elif row["status"] == "queued" and (task is None or task.done()):
                # A commit may have succeeded despite an error being reported.
                # Idempotent retries must attach a worker to that queued task.
                snapshot = await self.get_graph().aget_state(self.config(row["run_id"]))
                self.start(row["run_id"], self.resume_input(snapshot))
            return self.repository.response(row)

    @staticmethod
    def resume_input(snapshot):
        if not snapshot.values:
            return "new"
        if any(task.interrupts for task in snapshot.tasks):
            return Command(resume=True)
        return None

    async def settle(self, run_id, status, **kwargs):
        for attempt in range(3):
            try:
                await self.repository.finish(run_id, status, **kwargs)
                return
            except sqlite3.Error:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))

    async def inspect(self, run_id):
        async with self.control_lock:
            row = await self.repository.get(run_id)
            task = self.tasks.get(run_id)
            if row["status"] in ("queued", "running") and (task is None or task.done()):
                await self.repository.mark_recovering(run_id)
                row = await self.repository.get(run_id)
            return row

    async def advance(self, run_id, graph, inputs, config, row, deadline):
        if inputs is None:
            snapshot = await graph.aget_state(config)
            if not snapshot.next and snapshot.values.get("answer"):
                # Publishing an already completed graph consumes no model/tool budget.
                return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise ChatError("agent_timeout", "任务累计执行时间已达到上限。", 504)
        message_id, attempt_id = None, None
        async with asyncio.timeout_at(deadline):
            if inputs == "new":
                history = await self.runtime.committed_messages(row["conversation_id"])
                inputs = {
                    "run_id": run_id,
                    "base_message_count": len(history),
                    "messages": [*history, HumanMessage(content=row["message"])],
                }
            async for part in graph.astream(
                inputs,
                config=config,
                stream_mode=["messages", "updates", "custom"],
                version="v2",
                durability="sync",
            ):
                if part["type"] == "custom":
                    event, data = part["data"]["event"], part["data"]["data"]
                    if event == "message_start":
                        message_id, attempt_id = data["message_id"], data["attempt_id"]
                    await self.repository.event(run_id, event, data)
                elif part["type"] == "updates":
                    for node in part["data"]:
                        if not node.startswith("__"):
                            await self.repository.event(run_id, "state_update", {"node": node})
                elif part["type"] == "messages":
                    chunk, metadata = part["data"]
                    if (
                        metadata.get("langgraph_node") == "model"
                        and isinstance(chunk, AIMessageChunk)
                        and chunk.text
                    ):
                        await self.repository.event(
                            run_id,
                            "text_delta",
                            {
                                "text": chunk.text,
                                "message_id": message_id,
                                "attempt_id": attempt_id,
                            },
                        )
        return await graph.aget_state(config)

    async def execute(self, run_id, inputs):
        clock = asyncio.get_running_loop().time
        started = clock()
        elapsed_saved = False
        row = await self.repository.get(run_id)
        try:
            await self.repository.set_running(run_id)
            graph = self.get_graph()
            config = self.config(run_id)
            deadline = (
                started + self.runtime.settings.agent_timeout_seconds - row["elapsed_seconds"]
            )
            snapshot = await self.advance(run_id, graph, inputs, config, row, deadline)
            elapsed = clock() - started
            if any(task.interrupts for task in snapshot.tasks):
                await self.repository.add_elapsed(run_id, elapsed)
                elapsed_saved = True
                await self.repository.paused(run_id)
                return
            if snapshot.next or not snapshot.values.get("answer"):
                raise ChatError("invalid_model_response", "任务未生成完整结果。", 502, True)
            await self.settle(
                run_id,
                "completed",
                checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                answer=snapshot.values["answer"],
                tools_used=snapshot.values["tools_used"],
                elapsed=elapsed,
            )
            elapsed_saved = True
        except asyncio.CancelledError:
            # Explicit cancellation is settled by cancel(); lifecycle shutdown
            # keeps a recoverable checkpoint instead of discarding graph state.
            raise
        except Exception as exc:
            if isinstance(exc, ChatError):
                error = exc
            elif isinstance(exc, APIError):
                error = upstream_error(exc)
            elif isinstance(exc, TimeoutError):
                error = ChatError("agent_timeout", "任务累计执行时间已达到上限。", 504)
            elif isinstance(exc, GraphRecursionError):
                error = ChatError("agent_limit_exceeded", "任务达到步骤上限。", 429)
            elif isinstance(exc, sqlite3.Error):
                error = ChatError("history_publish_failed", "任务存储失败，可稍后恢复。", 503, True)
            else:
                error = ChatError("internal_error", "任务处理失败，可稍后恢复。", 500, True)
            error.thread_id, error.request_id = row["conversation_id"], row["request_id"]
            logger.warning(
                "run_failed run_id=%s code=%s type=%s", run_id, error.code, type(exc).__name__
            )
            await self.settle(
                run_id,
                "failed",
                error={**error.detail(), "http_status": error.status},
                elapsed=0 if elapsed_saved else clock() - started,
            )
            elapsed_saved = True
        finally:
            if not elapsed_saved:
                await self.repository.add_elapsed(run_id, clock() - started)

    @complete_on_cancel
    async def resume(self, run_id):
        async with self.control_lock:
            if self.closing:
                raise ChatError("service_stopping", "服务正在关闭。", 503, True)
            row = await self.repository.get(run_id)
            task = self.tasks.get(run_id)
            if task and not task.done():
                if row["status"] not in ("paused", "failed"):
                    raise ChatError("run_busy", "任务仍在执行。", 409)
                await asyncio.gather(task, return_exceptions=True)
            if row["status"] in ("queued", "running"):
                await self.repository.mark_recovering(run_id)
                row = await self.repository.get(run_id)
            if row["graph_version"] != GRAPH_VERSION:
                raise ChatError("graph_version_mismatch", "任务图版本不兼容。", 409)
            snapshot = await self.get_graph().aget_state(self.config(run_id))
            inputs = self.resume_input(snapshot)
            reset_message = (
                await self.repository.last_message_id(run_id) if "model" in snapshot.next else None
            )
            row = await self.repository.resume(run_id, reset_message)
            self.start(run_id, inputs)
            return self.repository.response(row)

    @complete_on_cancel
    async def cancel(self, run_id):
        async with self.control_lock:
            row = await self.repository.get(run_id)
            if row["status"] in TERMINAL_STATUSES:
                return self.repository.response(row)
            task = self.tasks.get(run_id)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self.settle(run_id, "cancelled")
            return self.repository.response(await self.repository.get(run_id))

    async def close(self):
        async with self.control_lock:
            self.closing = True
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
