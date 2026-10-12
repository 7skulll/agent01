from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatTurn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class QueryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class QueryResponse(BaseModel):
    answer: str
    status: Literal["answered", "insufficient_evidence"]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    rag_ready: bool
    chat_ready: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message: str = Field(min_length=1, max_length=4000)
    thread_id: UUID | None = None


class ChatResponse(BaseModel):
    answer: str
    thread_id: UUID
    tools_used: list[str] = Field(default_factory=list)


class Conversation(BaseModel):
    thread_id: UUID
    created_at: str
    updated_at: str
    status: Literal["ready", "running", "failed"]


class StoredMessage(BaseModel):
    role: Literal["human", "ai", "tool"]
    content: str
    tool_calls: list[dict] = Field(default_factory=list)
    tool_name: str | None = None


class ConversationHistory(BaseModel):
    conversation: Conversation
    messages: list[StoredMessage]
