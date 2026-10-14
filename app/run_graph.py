from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import ValidationError

from app.agent.prompt import SYSTEM_PROMPT
from app.agent.validation import validate_model_message
from app.errors import ChatError
from app.tools import CHAT_TOOLS


class RunState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    run_id: str
    base_message_count: int
    route: str
    answer: str
    tools_used: list[str]


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
        if isinstance(state["messages"][-1], AIMessage) and not state["messages"][-1].tool_calls:
            return {"route": "finalize"}
        return {"route": "tools" if pending_tools(state["messages"]) else "model"}

    async def pause(state: RunState):
        interrupt({"kind": "pause", "run_id": state["run_id"]})
        return {}

    async def model(state: RunState, config: RunnableConfig):
        attempt = await repository.reserve_call(
            state["run_id"], "model", settings.agent_max_model_calls
        )
        message_id = f"{state['run_id']}:model:{attempt}"
        emit("message_start", {"message_id": message_id, "attempt_id": attempt})
        model = runtime.get_model().bind_tools(list(tools.values()))
        result = await model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]],
            config=config,
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
        if not runtime.complete_messages(state["messages"]):
            raise ChatError("invalid_model_response", "模型未返回完整有效回答。", 502)
        return {
            "answer": state["messages"][-1].text,
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
            "model": "model",
            "tools": "tools",
            "finalize": "finalize",
        },
    )
    builder.add_edge("pause", "control")
    builder.add_edge("model", "validate")
    builder.add_edge("validate", "control")
    builder.add_edge("tools", "control")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=runtime.checkpointer)
