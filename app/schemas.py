from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    chat_ready: bool = False
    embedding_ready: bool = False


class Conversation(BaseModel):
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


class RunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    knowledge_base_id: Literal["default"] | None = None


class RunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    request_id: UUID
    status: RunStatus
    graph_version: str
    created_at: str
    updated_at: str
    pause_requested: bool = False
    model_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0
    event_seq: int = 0
    answer: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    error: dict | None = None
