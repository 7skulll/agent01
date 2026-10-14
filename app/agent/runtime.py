from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.validation import complete_messages as _complete_messages
from app.config import Settings
from app.conversations import ConversationRepository
from app.errors import ChatError
from app.schemas import ConversationHistory, StoredMessage


class AgentRuntime:
    """Shared agent dependencies and helpers for the chat engines.

    Owns the model, checkpointer, conversation repository and the small set of
    read-only helpers that both the prebuilt-agent /chat engine and the explicit
    /runs graph need. Engine-specific orchestration stays in the respective
    service so this object never depends on either engine.
    """

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
        return (
            self.model
            if self.model is not None
            else ChatDeepSeek(
                model=self.settings.llm_model,
                api_key=self.settings.llm_api_key,
                api_base=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout_seconds,
                max_tokens=self.settings.llm_max_tokens,
                max_retries=0,
            )
        )

    @staticmethod
    def complete_messages(messages):
        return _complete_messages(messages)

    async def initialize_memory(self):
        # Upgrade old single-thread checkpoints without replaying pending tasks.
        for thread_id in self.repository.legacy_ids:
            config = {"configurable": {"thread_id": thread_id}}
            async for checkpoint in self.checkpointer.alist(config):
                messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
                if self.complete_messages(messages):
                    await self.repository.migrate_checkpoint(
                        thread_id, checkpoint.config["configurable"]["checkpoint_id"]
                    )
                    break
        await self.repository.prune_unreferenced_runs()

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
