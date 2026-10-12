import asyncio

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from openai import APIError, APITimeoutError

from app.config import Settings
from app.conversations import ConversationRepository
from app.schemas import ChatRequest, ChatResponse, ConversationHistory, StoredMessage
from app.tools import CHAT_TOOLS


class ChatNotReadyError(RuntimeError):
    pass


class ChatTimeoutError(RuntimeError):
    pass


class ChatUpstreamError(RuntimeError):
    pass


class ChatLimitError(RuntimeError):
    pass


class ChatService:
    def __init__(
        self,
        settings: Settings,
        checkpointer: AsyncSqliteSaver,
        repository: ConversationRepository,
        model: BaseChatModel | None = None,
    ):
        self.settings = settings
        self.checkpointer = checkpointer
        self.repository = repository
        self.model = model
        self.agent = None

    @property
    def ready(self) -> bool:
        return self.model is not None or all(
            (
                self.settings.llm_model.strip(),
                self.settings.llm_base_url.strip(),
                self.settings.llm_api_key.get_secret_value().strip(),
            )
        )

    def get_agent(self):
        if self.agent is None:
            model = self.model
            if model is None:
                model = ChatDeepSeek(
                    model=self.settings.llm_model,
                    api_key=self.settings.llm_api_key,
                    api_base=self.settings.llm_base_url,
                    timeout=self.settings.llm_timeout_seconds,
                    max_tokens=self.settings.llm_max_tokens,
                    max_retries=0,
                    streaming=False,
                )
            self.agent = create_agent(
                model=model,
                tools=CHAT_TOOLS,
                checkpointer=self.checkpointer,
                middleware=[
                    ModelCallLimitMiddleware(
                        run_limit=self.settings.agent_max_model_calls, exit_behavior="error"
                    ),
                    ToolCallLimitMiddleware(
                        run_limit=self.settings.agent_max_tool_calls, exit_behavior="error"
                    ),
                ],
                system_prompt=(
                    "你是简洁的中文助手。需要当前日期时间时调用 current_time；"
                    "需要乘法计算时调用 multiply_numbers。根据工具真实返回结果回答，"
                    "不要伪造工具结果。不需要工具时直接回答。当前没有知识库检索工具。"
                ),
            )
        return self.agent

    @staticmethod
    def thread_config(thread_id: str):
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 64}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if not self.ready:
            raise ChatNotReadyError("请配置 LLM API 地址、模型 ID 和 API Key。")
        thread_id = (
            str(request.thread_id)
            if request.thread_id is not None
            else str((await self.repository.create()).thread_id)
        )
        await self.repository.claim(thread_id)
        succeeded = False
        try:
            agent = self.get_agent()
            config = self.thread_config(thread_id)
            snapshot = await agent.aget_state(config)
            previous_ids = {m.id for m in snapshot.values.get("messages", [])}
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=request.message)]}, config=config
                )
            messages = result["messages"]
            last = messages[-1]
            if not isinstance(last, AIMessage) or last.tool_calls or not last.text.strip():
                raise ChatUpstreamError("模型未返回有效的最终回答。")
            response = ChatResponse(
                answer=last.text,
                thread_id=thread_id,
                tools_used=[
                    m.name
                    for m in messages
                    if isinstance(m, ToolMessage) and m.id not in previous_ids and m.name
                ],
            )
            succeeded = True
            return response
        except (APITimeoutError, TimeoutError):
            raise ChatTimeoutError("模型或 Agent 请求超时。") from None
        except APIError:
            raise ChatUpstreamError("模型 API 调用失败，请检查服务地址、凭据和模型 ID。") from None
        except (ModelCallLimitExceededError, ToolCallLimitExceededError, GraphRecursionError):
            raise ChatLimitError("Agent 达到调用次数或步骤上限，已停止。") from None
        finally:
            await asyncio.shield(
                self.repository.finish(thread_id, "ready" if succeeded else "failed")
            )

    async def history(self, thread_id: str) -> ConversationHistory:
        conversation = await self.repository.get(thread_id)
        checkpoint = await self.checkpointer.aget_tuple(self.thread_config(thread_id))
        messages = (
            checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
            if checkpoint
            else []
        )
        return ConversationHistory(
            conversation=conversation,
            messages=[
                StoredMessage(
                    role=m.type,
                    content=m.text,
                    tool_calls=m.tool_calls if isinstance(m, AIMessage) else [],
                    tool_name=m.name if isinstance(m, ToolMessage) else None,
                )
                for m in messages
                if isinstance(m, (HumanMessage, AIMessage, ToolMessage))
            ],
        )
