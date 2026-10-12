from contextlib import asynccontextmanager
from uuid import UUID

import aiosqlite
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.chat import (
    ChatLimitError,
    ChatNotReadyError,
    ChatService,
    ChatTimeoutError,
    ChatUpstreamError,
)
from app.config import Settings
from app.conversations import (
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationRepository,
)
from app.pipeline import PipelineNotReadyError, RAGService
from app.schemas import (
    ChatRequest,
    ChatResponse,
    Conversation,
    ConversationHistory,
    HealthResponse,
    QueryRequest,
    QueryResponse,
)

router = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request):
    return HealthResponse(
        rag_ready=request.app.state.rag_service.ready,
        chat_ready=request.app.state.chat_service.ready,
    )


@router.post("/chat", response_model=ChatResponse, tags=["chat"])
async def chat(body: ChatRequest, request: Request):
    try:
        return await request.app.state.chat_service.chat(body)
    except ChatNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ChatTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ChatUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ChatLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConversationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations", response_model=Conversation, status_code=201, tags=["chat"])
async def create_conversation(request: Request):
    return await request.app.state.chat_service.repository.create()


@router.get("/conversations", response_model=list[Conversation], tags=["chat"])
async def list_conversations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return await request.app.state.chat_service.repository.list(limit, offset)


@router.get("/conversations/{thread_id}", response_model=ConversationHistory, tags=["chat"])
async def get_history(thread_id: UUID, request: Request):
    try:
        return await request.app.state.chat_service.history(str(thread_id))
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/query",
    response_model=QueryResponse,
    tags=["rag"],
    responses={503: {"description": "RAG pipeline is not configured"}},
)
async def query(body: QueryRequest, request: Request):
    try:
        return await request.app.state.rag_service.query(body)
    except PipelineNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def create_app(
    settings: Settings | None = None,
    service: RAGService | None = None,
    chat_model: BaseChatModel | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rag_service = service or RAGService(settings)
        settings.chat_database_path.parent.mkdir(parents=True, exist_ok=True)
        database_path = str(settings.chat_database_path)
        async with aiosqlite.connect(database_path) as connection:
            repository = ConversationRepository(connection)
            await repository.setup()
            async with AsyncSqliteSaver.from_conn_string(database_path) as checkpointer:
                await checkpointer.setup()
                app.state.chat_service = ChatService(settings, checkpointer, repository, chat_model)
                yield

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="LangChain Agent 对话与 SQLite 会话历史；RAG 检索待接入。",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()
