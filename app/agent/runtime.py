from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import Settings
from app.conversations import ConversationRepository
from app.errors import ChatError
from app.observability import Metrics
from app.schemas import ConversationHistory, StoredMessage
from app.tokenization import TokenCounter


class AgentRuntime:
    """任务图共享的模型、检查点、会话仓库和检索服务依赖。"""

    def __init__(
        self,
        settings: Settings,
        checkpointer: BaseCheckpointSaver,
        repository: ConversationRepository,
        model: BaseChatModel | None = None,
        retrieval_service=None,
    ):
        self.settings = settings
        self.counter = TokenCounter(settings.chat_tokenizer)
        self.checkpointer = checkpointer
        self.repository = repository
        self.model = model
        self.retrieval_service = retrieval_service
        self.metrics = retrieval_service.metrics if retrieval_service else Metrics()

    @property
    def input_budget(self):
        return (
            int(self.settings.rag_input_tokens * self.settings.token_safety_ratio)
            - self.settings.llm_max_tokens
            - self.settings.chat_token_overhead
        )

    def validate_input(self, messages, tools=()):
        count = self.counter.messages(messages, tools)
        if count > self.input_budget:
            raise ChatError("context_limit_exceeded", "会话超出上下文预算，请创建新会话。", 422)
        return count

    @property
    def ready(self) -> bool:
        return self.model is not None or all(
            (
                self.settings.llm_model.strip(),
                self.settings.llm_base_url.strip(),
                self.settings.llm_api_key.get_secret_value().strip(),
            )
        )

    def get_model(self):
        if self.model is not None:
            return self.model
        return build_chat_model(self.settings)

    async def committed_messages(self, thread_id):
        # A concurrent successful turn may prune the previously committed run
        # between reading its config and loading it. Retry once to pick up the
        # newly committed checkpoint before giving up.
        for _ in range(2):
            config = await self.repository.checkpoint_config(thread_id)
            if config is None:
                return []
            checkpoint = await self.checkpointer.aget_tuple(config)
            if checkpoint is not None:
                return checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
        raise ChatError("history_unavailable", "会话历史暂时无法读取，请稍后重试。", 503, True)

    async def history(self, thread_id: str) -> ConversationHistory:
        conversation = await self.repository.get(thread_id)
        messages = await self.committed_messages(thread_id)
        return ConversationHistory(
            conversation=conversation,
            messages=[
                StoredMessage(
                    role=m.type,
                    content=m.text,
                    tool_calls=[dict(call) for call in m.tool_calls]
                    if isinstance(m, AIMessage)
                    else [],
                    tool_name=m.name if isinstance(m, ToolMessage) else None,
                )
                for m in messages
                if isinstance(m, (HumanMessage, AIMessage, ToolMessage))
            ],
        )


def build_chat_model(settings):
    factory = ChatDeepSeek if settings.llm_provider == "deepseek" else ChatOpenAI
    return factory(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
        max_retries=0,
        **({"use_responses_api": False} if settings.llm_provider == "openai" else {}),
    )
