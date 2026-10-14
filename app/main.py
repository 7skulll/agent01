from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import aiosqlite
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.runtime import AgentRuntime
from app.config import Settings
from app.conversations import ConversationNotFoundError, ConversationRepository
from app.errors import ChatError
from app.rag.file_api import router as file_router
from app.rag.files import FileRepository, FileService
from app.rag.indexing import IndexRepository, IndexService
from app.rag.storage import LocalStorage
from app.run_api import router as run_router
from app.run_repository import RunRepository
from app.runs import RunService
from app.schemas import Conversation, ConversationHistory, HealthResponse

router = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request):
    return HealthResponse(
        chat_ready=request.app.state.runtime.ready,
    )


@router.post("/conversations", response_model=Conversation, status_code=201, tags=["chat"])
async def create_conversation(request: Request):
    return await request.app.state.runtime.repository.create()


@router.get("/conversations", response_model=list[Conversation], tags=["chat"])
async def list_conversations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return await request.app.state.runtime.repository.list(limit, offset)


@router.get("/conversations/{thread_id}", response_model=ConversationHistory, tags=["chat"])
async def get_history(thread_id: UUID, request: Request):
    try:
        return await request.app.state.runtime.history(str(thread_id))
    except ConversationNotFoundError as exc:
        error = ChatError("conversation_not_found", str(exc), 404)
        error.thread_id = str(thread_id)
        error.request_id = str(uuid4())
        raise HTTPException(status_code=404, detail=error.detail()) from None
    except ChatError as exc:
        exc.thread_id = str(thread_id)
        exc.request_id = str(uuid4())
        raise HTTPException(status_code=exc.status, detail=exc.detail()) from None


def create_app(
    settings: Settings | None = None,
    chat_model: BaseChatModel | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.chat_database_path.parent.mkdir(parents=True, exist_ok=True)
        database_path = str(settings.chat_database_path)
        async with aiosqlite.connect(database_path) as connection:
            repository = ConversationRepository(connection)
            await repository.setup()
            run_repository = RunRepository(repository)
            await run_repository.setup()
            file_repository = FileRepository(repository)
            await file_repository.setup()
            storage = LocalStorage(settings.file_storage_path)
            app.state.file_service = FileService(file_repository, storage)
            index_repository = IndexRepository(repository)
            await index_repository.setup()
            app.state.index_service = IndexService(
                file_repository, index_repository, storage, settings
            )
            await app.state.index_service.reconcile()
            async with AsyncSqliteSaver.from_conn_string(database_path) as checkpointer:
                await checkpointer.setup()
                app.state.runtime = AgentRuntime(settings, checkpointer, repository, chat_model)
                await app.state.runtime.initialize_memory()
                app.state.run_service = RunService(app.state.runtime, run_repository)
                await app.state.run_service.reconcile_completed()
                try:
                    yield
                finally:
                    await app.state.index_service.close()
                    await app.state.run_service.close()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Agent 对话、LangGraph 可恢复任务与 SQLite 历史；RAG 检索待接入。",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(run_router)
    app.include_router(file_router)
    return app


app = create_app()
