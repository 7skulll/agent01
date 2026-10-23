import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rag.answer_protocol import AnswerReason, AnswerSegment, AnswerStatus


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    chat_ready: bool = False
    embedding_ready: bool = False


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value):
        if any(unicodedata.category(c) in {"Cc", "Cs"} for c in value):
            raise ValueError("会话名称不能包含控制字符或无效 Unicode。")
        return value


class ConversationDelete(BaseModel):
    thread_id: UUID
    deleted: bool = True
    cleanup_pending: int = 0


class Conversation(BaseModel):
    title: str | None = None
    thread_id: UUID
    created_at: str
    updated_at: str
    status: Literal["ready", "running", "failed"]
    last_error_code: str | None = None
    request_id: UUID | None = None
    active_run_id: UUID | None = None


class StoredMessage(BaseModel):
    role: Literal["human", "ai", "tool"]
    content: str
    tool_calls: list[dict] = Field(default_factory=list)
    tool_name: str | None = None


class ConversationHistory(BaseModel):
    conversation: Conversation
    messages: list[StoredMessage]


RunStatus = Literal["queued", "running", "paused", "recovering", "completed", "failed", "cancelled"]


class EvaluationOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rerank: Literal["inherit", "on", "off"] = "inherit"
    context_tokens: int | None = Field(default=None, ge=256, le=8192)
    judge: Literal["none", "ragas"] = "none"


class RunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    knowledge_base_id: str | None = Field(default=None, min_length=1, max_length=128)
    capture_trace: bool = False
    evaluation_options: EvaluationOptions | None = None
    retrieval_mode: Literal["hybrid", "keyword", "vector"] = "hybrid"
    file_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = Field(
        default=None, max_length=100
    )

    @field_validator("file_ids")
    @classmethod
    def canonical_files(cls, value):
        return sorted(set(value)) if value is not None else None

    @model_validator(mode="after")
    def scope_requires_knowledge_base(self):
        if self.file_ids is not None and self.knowledge_base_id is None:
            raise ValueError("指定文件范围时必须选择知识库。")
        return self


class RunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    request_id: UUID
    status: RunStatus
    knowledge_base_id: str | None = None
    capture_trace: bool = False
    evaluation_options: EvaluationOptions | None = None
    retrieval_mode: Literal["hybrid", "keyword", "vector"] = "hybrid"
    file_ids: list[str] | None = None
    graph_version: str
    created_at: str
    updated_at: str
    pause_requested: bool = False
    model_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0
    event_seq: int = 0
    answer: str | None = None
    answer_status: AnswerStatus | None = None
    answer_reason: AnswerReason | None = None
    answer_protocol: Literal["rag-answer-v2"] | None = None
    answer_segments: list[AnswerSegment] = Field(default_factory=list)
    answer_repair_calls: int = 0
    tools_used: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    error: dict | None = None


class ConversationTurn(RunResponse):
    message: str
    knowledge_base_id: str | None = None
    committed: bool = False


class ConversationTurns(BaseModel):
    items: list[ConversationTurn]
    total: int
    limit: int
    offset: int
