import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from openai import APIError
from pydantic import ValidationError

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.validation import complete_messages, validate_model_message
from app.errors import ChatError, upstream_error
from app.rag.answer_protocol import (
    ANSWER_PROTOCOL,
    invalid,
    parse_answer,
    refusal,
    render_answer,
    repair_messages,
    repair_reserve,
)
from app.rag.context_assembly import ContextAssembler
from app.rag.query_planning import plan_queries
from app.rag.retrieval import SearchRequest
from app.tools import CHAT_TOOLS


class RunState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    run_id: str
    base_message_count: int
    route: str
    answer: str
    tools_used: list[str]
    knowledge_base_id: str | None
    retrieval_mode: str
    file_ids: list[str] | None
    retrieval_done: bool
    evaluation_options: dict
    capture_trace: bool
    rag_history: list[AnyMessage]
    history_scope_dropped: int
    history_window_dropped: int
    retrieved_sources: list[dict]
    retrieval_info: dict
    assembly_done: bool
    rag_model_messages: list[AnyMessage]
    assembly_diagnostics: dict
    assembly_version: str
    prompt_hash: str
    rag_sources: list[dict]
    sources: list[dict]
    search_queries: list[str]
    rerank_query: str
    query_degraded: bool
    query_token_usage: dict
    query_planned: bool
    answer_status: str | None
    answer_reason: str | None
    answer_segments: list[dict]
    answer_protocol: str | None
    rag_raw_message: AIMessage | None
    rag_validated: bool
    rag_validation_error: str | None
    rag_repair_used: bool
    rag_repair_messages: list[AnyMessage]


def pending_tools(messages):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            answered = {m.tool_call_id for m in messages[index + 1 :] if isinstance(m, ToolMessage)}
            return [call for call in message.tool_calls if call["id"] not in answered]
    return []


def emit(event, data):
    get_stream_writer()({"event": event, "data": data})


def build_run_graph(runtime, repository):
    settings = runtime.settings
    tools = {tool.name: tool for tool in CHAT_TOOLS}

    async def control(state: RunState):
        row = await repository.get(state["run_id"])
        if row["pause_requested"]:
            return {"route": "pause"}
        if state.get("knowledge_base_id"):
            if not state.get("query_planned"):
                return {"route": "plan_query"}
            if not state.get("retrieval_done"):
                return {"route": "retrieve"}
            if not state.get("assembly_done"):
                return {"route": "assemble_context"}
            if state.get("rag_validated"):
                return {"route": "finalize"}
            if state.get("rag_raw_message") is not None:
                return {"route": "validate_rag"}
            if state.get("rag_validation_error"):
                return {"route": "repair_rag" if state.get("rag_repair_used") else "prepare_repair"}
            return {"route": "rag_model"}
        if isinstance(state["messages"][-1], AIMessage) and not state["messages"][-1].tool_calls:
            return {"route": "finalize"}
        return {"route": "tools" if pending_tools(state["messages"]) else "model"}

    async def pause(state: RunState):
        interrupt({"kind": "pause", "run_id": state["run_id"]})
        return {}

    async def plan_query(state: RunState, config: RunnableConfig):
        plan = await plan_queries(runtime, repository, state, config)
        emit("query_plan", plan)
        return {**plan, "query_planned": True}

    async def retrieve(state: RunState):
        # 检索结果随节点一起存入检查点，模型失败后恢复不会重新调用 Embedding。
        emit("retrieval_start", {"knowledge_base_id": state["knowledge_base_id"]})
        query = state["messages"][-1].text
        result = await runtime.retrieval_service.search(
            state["knowledge_base_id"],
            SearchRequest(
                query=query,
                mode=state.get("retrieval_mode", "hybrid"),
                queries=state.get("search_queries", [query])[1:],
                rerank_query=state.get("rerank_query"),
                top_k=settings.retrieval_answer_candidates,
                file_ids=state.get("file_ids"),
            ),
            **({"capture_trace": True} if state.get("capture_trace") else {}),
            **(
                {"rerank": state["evaluation_options"]["rerank"] == "on"}
                if state.get("evaluation_options", {}).get("rerank", "inherit") != "inherit"
                else {}
            ),
        )

        return {
            "retrieval_done": True,
            "retrieved_sources": [
                {
                    **source.model_dump(),
                    "kb_id": state["knowledge_base_id"],
                    "index_version": result.index_version,
                    "lexical_version": result.lexical_version,
                }
                for source in result.sources
            ],
            "retrieval_info": {
                "index_version": result.index_version,
                "queries": result.queries,
                "diagnostics": result.diagnostics,
                "query_degraded": state.get("query_degraded", False),
            },
        }

    async def assemble_context(state: RunState):
        started = time.perf_counter()
        with runtime.metrics.span("context_assembly"):
            reserve = repair_reserve(runtime.counter)
            context_tokens = state.get("evaluation_options", {}).get("context_tokens")
            assembly_settings = (
                settings.model_copy(update={"rag_context_tokens": context_tokens})
                if context_tokens is not None
                else settings
            )
            result = ContextAssembler(
                assembly_settings, runtime.counter, runtime.input_budget - reserve
            ).assemble(
                state["messages"][-1].text,
                state["retrieved_sources"],
                state.get("rag_history", []),
                state.get("rerank_query"),
            )
        diagnostics = result["diagnostics"]
        diagnostics["repair_reserved_tokens"] = reserve
        diagnostics["model_input_budget"] = runtime.input_budget
        diagnostics["history_scope_dropped"] = state.get("history_scope_dropped", 0)
        diagnostics["history_turns_dropped"] += state.get("history_window_dropped", 0)
        diagnostics["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        diagnostics["prompt_hash"] = result["prompt_hash"]
        info = state["retrieval_info"]
        emit(
            "retrieval_end",
            {
                **info,
                "sources": result["sources"],
                "diagnostics": {**info["diagnostics"], "context_assembly": diagnostics},
            },
        )
        emit("context_assembly", diagnostics)
        update = {
            "assembly_done": True,
            "rag_model_messages": result["messages"],
            "rag_sources": result["sources"],
            "assembly_diagnostics": diagnostics,
            "assembly_version": result["assembly_version"],
            "prompt_hash": result["prompt_hash"],
        }
        if not result["sources"]:
            outcome = refusal(
                "no_context_fit" if state["retrieved_sources"] else "no_retrieval_hits"
            )
            update.update(outcome)
            update["rag_validated"] = True
            update["messages"] = [
                AIMessage(content=outcome["answer"], id=f"{state['run_id']}:answer")
            ]
        return update

    async def invoke_rag(state: RunState, config, repair=False):
        messages = state["rag_repair_messages"] if repair else state["rag_model_messages"]
        estimated = runtime.validate_input(messages)
        attempt = await repository.reserve_call(
            state["run_id"], "repair" if repair else "model", settings.agent_max_model_calls
        )
        stage = "answer_repair" if repair else "answer"
        emit(
            "answer_repair" if repair else "answer_start",
            {
                "attempt_id": attempt,
                "answer_protocol": ANSWER_PROTOCOL,
                **({"error_code": state["rag_validation_error"]} if repair else {}),
            },
        )
        model = runtime.get_model()
        if settings.rag_answer_json_mode:
            model = model.bind(response_format={"type": "json_object"})
        runtime.metrics.tokens(stage, estimated_input=estimated)
        model_started = time.perf_counter()
        try:
            with runtime.metrics.span("citation_repair" if repair else "answer_model"):
                message = await model.ainvoke(messages, config=config)
        except APIError as exc:
            if not repair:
                raise
            error = upstream_error(exc)
            error.retryable = False  # 已消耗唯一一次修复调用，禁止恢复后再次调用。
            raise error from None
        except (ChatError, TimeoutError):
            raise
        except Exception:
            if not repair:
                raise
            raise ChatError("internal_error", "回答修复调用失败，请重新提问。", 502) from None
        runtime.metrics.tokens(stage, usage=message.usage_metadata)
        emit(
            "token_usage",
            {
                "stage": stage,
                "duration_ms": round((time.perf_counter() - model_started) * 1000, 2),
                "estimated_input_tokens": estimated,
                "tokenizer": runtime.counter.name,
                "actual": message.usage_metadata or None,
            },
        )
        return {"rag_raw_message": message}

    async def rag_model(state: RunState, config: RunnableConfig):
        return await invoke_rag(state, config)

    async def prepare_repair(state: RunState):
        messages = repair_messages(state["rag_model_messages"], state["rag_validation_error"])
        runtime.validate_input(messages)
        return {"rag_repair_used": True, "rag_repair_messages": messages}

    async def repair_rag(state: RunState, config: RunnableConfig):
        return await invoke_rag(state, config, repair=True)

    async def validate_rag(state: RunState):
        message = state["rag_raw_message"]
        if not isinstance(message, AIMessage):
            raise ChatError("invalid_model_response", "模型未返回有效回答。", 502)
        validate_model_message(message)
        try:
            if message.tool_calls:
                raise invalid("invalid_answer_schema")
            payload = parse_answer(message.text, state["rag_sources"])
        except ChatError as exc:
            runtime.metrics.increment("citation_validation", outcome="error", reason=exc.code)
            emit("answer_validation", {"valid": False, "error_code": exc.code})
            if state.get("rag_repair_used"):
                raise
            return {"rag_validation_error": exc.code, "rag_raw_message": None}
        outcome = render_answer(payload, state["rag_sources"])
        runtime.metrics.increment("citation_validation", outcome="success", reason="none")
        emit(
            "answer_validation",
            {
                "valid": True,
                "answer_status": outcome["answer_status"],
                "source_count": len(outcome["sources"]),
            },
        )
        return {
            **outcome,
            "rag_validated": True,
            "rag_validation_error": None,
            "rag_raw_message": None,
            "messages": [AIMessage(content=outcome["answer"], id=f"{state['run_id']}:answer")],
        }

    async def model(state: RunState, config: RunnableConfig):
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        model_tools = list(tools.values())
        estimated_tokens = runtime.validate_input(messages, model_tools)
        attempt = await repository.reserve_call(
            state["run_id"], "model", settings.agent_max_model_calls
        )
        message_id = f"{state['run_id']}:model:{attempt}"
        emit("message_start", {"message_id": message_id, "attempt_id": attempt})
        model = runtime.get_model().bind_tools(model_tools)
        runtime.metrics.tokens("answer", estimated_input=estimated_tokens)
        with runtime.metrics.span("answer_model"):
            result = await model.ainvoke(messages, config=config)
        runtime.metrics.tokens("answer", usage=result.usage_metadata)
        emit(
            "token_usage",
            {
                "stage": "answer",
                "estimated_input_tokens": estimated_tokens,
                "tokenizer": runtime.counter.name,
                "actual": result.usage_metadata or None,
            },
        )
        emit("message_end", {"message_id": message_id, "attempt_id": attempt})
        return {"messages": [result]}

    async def validate(state: RunState):
        message = state["messages"][-1]
        validate_model_message(message)
        if not isinstance(message, AIMessage) or (
            not message.tool_calls and not message.text.strip()
        ):
            raise ChatError("invalid_model_response", "模型未返回完整有效回答。", 502)
        return {}

    async def execute_tool(state: RunState, config: RunnableConfig):
        # One tool per node execution: each successful tool result gets its own
        # checkpoint before the next tool/model call can begin.
        call = pending_tools(state["messages"])[0]
        if call["name"] not in tools:
            raise ChatError("tool_error", "模型请求了不存在的工具。", 502)
        attempt = await repository.reserve_call(
            state["run_id"], "tool", settings.agent_max_tool_calls
        )
        details = {"tool_name": call["name"], "tool_run_id": call["id"], "attempt_id": attempt}
        emit("tool_start", details)
        try:
            result = await tools[call["name"]].ainvoke(call, config=config)
        except ValidationError:
            raise ChatError("tool_error", "工具调用参数无效，请创建新任务。", 502) from None
        except Exception:
            raise ChatError("tool_error", "工具执行失败，本轮已停止。", 502, True) from None
        if not isinstance(result, ToolMessage) or result.status == "error":
            raise ChatError("tool_error", "工具调用参数或执行结果无效。", 502)
        emit("tool_end", details)
        return {"messages": [result]}

    async def finalize(state: RunState):
        if not complete_messages(state["messages"]):
            raise ChatError("invalid_model_response", "模型未返回完整有效回答。", 502)
        if state.get("knowledge_base_id"):
            if not state.get("rag_validated"):
                raise ChatError("invalid_answer_schema", "回答尚未通过校验。", 502)
            runtime.metrics.increment(
                "rag_answer_status",
                status=state["answer_status"],
                reason=state.get("answer_reason") or "none",
            )
            return {"tools_used": []}
        return {
            "answer": state["messages"][-1].text,
            "sources": [],
            "tools_used": [
                m.name
                for m in state["messages"][state["base_message_count"] :]
                if isinstance(m, ToolMessage) and m.name
            ],
        }

    builder = StateGraph(RunState)
    for name, node in (
        ("control", control),
        ("pause", pause),
        ("plan_query", plan_query),
        ("retrieve", retrieve),
        ("assemble_context", assemble_context),
        ("rag_model", rag_model),
        ("validate_rag", validate_rag),
        ("prepare_repair", prepare_repair),
        ("repair_rag", repair_rag),
        ("model", model),
        ("validate", validate),
        ("tools", execute_tool),
        ("finalize", finalize),
    ):
        builder.add_node(name, node)
    builder.add_edge(START, "control")
    builder.add_conditional_edges(
        "control",
        lambda state: state["route"],
        {
            "pause": "pause",
            "plan_query": "plan_query",
            "retrieve": "retrieve",
            "assemble_context": "assemble_context",
            "rag_model": "rag_model",
            "validate_rag": "validate_rag",
            "prepare_repair": "prepare_repair",
            "repair_rag": "repair_rag",
            "model": "model",
            "tools": "tools",
            "finalize": "finalize",
        },
    )
    builder.add_edge("pause", "control")
    builder.add_edge("plan_query", "control")
    builder.add_edge("retrieve", "control")
    builder.add_edge("assemble_context", "control")
    for node in ("rag_model", "validate_rag", "prepare_repair", "repair_rag"):
        builder.add_edge(node, "control")
    builder.add_edge("model", "validate")
    builder.add_edge("validate", "control")
    builder.add_edge("tools", "control")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=runtime.checkpointer)
