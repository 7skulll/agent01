from langchain_core.messages import AIMessage, ToolMessage

from app.errors import ChatError, truncated_response


def validate_model_message(message):
    if not isinstance(message, AIMessage):
        return
    if message.response_metadata.get("finish_reason") == "length":
        raise truncated_response()
    if message.invalid_tool_calls:
        raise ChatError("tool_error", "模型返回了格式无效的工具调用，本轮已停止。", 502)
    call_ids = [call["id"] for call in message.tool_calls]
    if any(not call_id for call_id in call_ids) or len(call_ids) != len(set(call_ids)):
        raise ChatError("tool_error", "模型返回了缺失或重复 ID 的工具调用。", 502)


def complete_messages(messages):
    if not messages or not isinstance(messages[-1], AIMessage):
        return False
    if messages[-1].tool_calls or not messages[-1].text.strip():
        return False
    pending = set()
    for message in messages:
        if isinstance(message, AIMessage):
            try:
                validate_model_message(message)
            except ChatError:
                return False
            pending.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
    return not pending
