from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.runtime import AgentRuntime
from app.config import Settings
from app.conversations import ConversationNotFoundError, ConversationRepository
from app.database import Database
from app.errors import ChatError
from app.evaluation_api import router as evaluation_router
from app.evaluations import EvaluationService
from app.http_errors import ErrorResponse, register_error_handlers
from app.knowledge_bases import KnowledgeBaseService
from app.knowledge_bases import router as knowledge_base_router
from app.model_settings import ModelSettingsService, effective_settings, read_overrides
from app.model_settings import router as model_settings_router
from app.observability import Metrics, MetricsMiddleware
from app.rag.embedding import EmbeddingModel, build_embedding_model
from app.rag.file_api import router as file_router
from app.rag.files import FileRepository, FileService
from app.rag.indexing import IndexRepository, IndexService
from app.rag.lexical import LexicalIndex
from app.rag.reranking import Reranker, configured_reranker
from app.rag.retrieval import RetrievalService
from app.rag.retrieval import router as retrieval_router
from app.rag.storage import LocalStorage
from app.rag.vectors import VectorRepository, VectorService
from app.run_api import router as run_router
from app.run_repository import RunRepository
from app.runs import RunService
from app.schemas import (
    Conversation,
    ConversationDelete,
    ConversationHistory,
    ConversationUpdate,
    HealthResponse,
)

router = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(request: Request):
    return HealthResponse(
        chat_ready=request.app.state.runtime.ready,
        embedding_ready=request.app.state.embedding_model is not None,
    )


@router.get("/metrics", tags=["observability"])
async def metrics_snapshot(request: Request):
    return request.app.state.metrics.snapshot()


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


@router.patch("/conversations/{thread_id}", response_model=Conversation, tags=["chat"])
async def rename_conversation(thread_id: UUID, body: ConversationUpdate, request: Request):
    return await request.app.state.runtime.repository.rename(str(thread_id), body.title)


@router.delete("/conversations/{thread_id}", response_model=ConversationDelete, tags=["chat"])
async def delete_conversation(thread_id: UUID, request: Request):
    return await request.app.state.run_service.delete_conversation(str(thread_id))


def create_app(
    settings: Settings | None = None,
    chat_model: BaseChatModel | None = None,
    embedding_model: EmbeddingModel | None = None,
    reranker: Reranker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时验证本地编码资源，不在首个用户请求中触发初始化或网络下载。
        from app.tokenization import TokenCounter

        for name in {settings.chat_tokenizer, settings.embedding_tokenizer}:
            TokenCounter(name).count("预热")
        database = Database(settings)
        async with (
            configured_reranker(settings, reranker, metrics=metrics) as active_reranker,
            database.pool,
        ):
            await database.setup()
            async with database.worker_ownership():
                overrides = await read_overrides(database)
                active_settings = effective_settings(settings, overrides)
                app.state.embedding_model = (
                    embedding_model
                    if embedding_model is not None
                    else build_embedding_model(active_settings)
                )
                repository = ConversationRepository(database)
                await repository.setup()
                run_repository = RunRepository(repository)
                await run_repository.setup()
                file_repository = FileRepository(database)
                storage = LocalStorage(settings.file_storage_path)
                app.state.file_service = FileService(file_repository, storage)
                index_repository = IndexRepository(database)
                vectors = VectorRepository(database, settings)
                lexical = LexicalIndex(database)
                vector_service = VectorService(
                    file_repository,
                    index_repository,
                    active_settings,
                    app.state.embedding_model,
                    metrics=metrics,
                )
                if app.state.embedding_model is not None:
                    await vectors.ensure_hnsw(
                        vector_service.version,
                        active_settings.embedding_dim or app.state.embedding_model.dim or 0,
                    )
                app.state.vector_service = vector_service
                app.state.lexical_index = lexical
                app.state.retrieval_service = RetrievalService(
                    vectors,
                    vector_service,
                    active_settings,
                    active_reranker,
                    lexical=lexical,
                    metrics=metrics,
                )
                app.state.index_service = IndexService(
                    file_repository,
                    index_repository,
                    storage,
                    active_settings,
                    vector_service,
                    lexical,
                    metrics=metrics,
                )
                await app.state.index_service.reconcile()
                async with AsyncPostgresSaver.from_conn_string(
                    active_settings.database_url.get_secret_value()
                ) as checkpointer:
                    await checkpointer.setup()
                    app.state.runtime = AgentRuntime(
                        active_settings,
                        checkpointer,
                        repository,
                        chat_model,
                        app.state.retrieval_service,
                    )
                    await repository.cleanup_checkpoints(checkpointer)
                    await repository.prune_unreferenced_runs(checkpointer)
                    app.state.run_service = RunService(app.state.runtime, run_repository)
                    await app.state.run_service.reconcile_completed()
                    app.state.knowledge_base_service = KnowledgeBaseService(
                        database,
                        storage,
                        app.state.index_service,
                        app.state.run_service,
                        app.state.file_service,
                    )
                    try:
                        await app.state.knowledge_base_service.collect_orphans()
                    except OSError:
                        import logging

                        logging.getLogger(__name__).warning("startup_orphan_scan_failed")
                    app.state.model_settings = ModelSettingsService(
                        app, database, settings, overrides
                    )
                    app.state.evaluations = EvaluationService(app, database)
                    await app.state.evaluations.setup()
                    try:
                        yield
                    finally:
                        await app.state.evaluations.close()
                        await app.state.index_service.close()
                        await app.state.run_service.close()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Agent 对话、LangGraph 可恢复任务与 PostgreSQL 历史；"
            "pgvector 精确向量与 BM25 全文混合检索。"
        ),
        lifespan=lifespan,
        responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.metrics = metrics
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.include_router(router)
    app.include_router(run_router)
    app.include_router(file_router)
    app.include_router(knowledge_base_router)
    app.include_router(retrieval_router)
    app.include_router(model_settings_router)
    app.include_router(evaluation_router)
    return app


app = create_app()
