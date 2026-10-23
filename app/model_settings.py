"""工作空间模型设置：密钥只在服务端保存，空密钥保留已有值。"""

import asyncio
from typing import Literal
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.config import Settings
from app.errors import ChatError
from app.rag.chunking import chunk_version
from app.rag.embedding import build_embedding_model
from app.rag.retrieval import RetrievalService
from app.rag.vectors import VectorService
from app.runs import complete_on_cancel
from app.users import require_admin

router = APIRouter(prefix="/api/v1/settings/models", tags=["model-settings"])
Kind = Literal["llm", "embedding"]


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in ("http", "https")
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
            )
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("请输入不含凭据、查询参数或片段的 HTTP/HTTPS 服务地址。")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("模型名称不能包含控制字符。")
        return value


class LLMInput(ModelInput):
    provider: Literal["openai", "deepseek"] = "openai"
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    input_tokens: int = Field(default=8192, ge=512, le=131072)


class EmbeddingInput(ModelInput):
    dimension: int = Field(default=0, ge=0, le=16000)
    max_input_tokens: int = Field(gt=0, le=1000000)


class ModelInfo(BaseModel):
    model: str
    base_url: str
    key_configured: bool
    source: Literal["environment", "custom"]
    ready: bool


class LLMInfo(ModelInfo):
    provider: Literal["openai", "deepseek"]
    max_tokens: int
    input_tokens: int


class EmbeddingInfo(ModelInfo):
    dimension: int
    max_input_tokens: int


class ModelsInfo(BaseModel):
    llm: LLMInfo
    embedding: EmbeddingInfo
    embedding_reindex_required: bool = False


FIELDS = {
    "llm": {
        "model": "llm_model",
        "base_url": "llm_base_url",
        "api_key": "llm_api_key",
        "provider": "llm_provider",
        "max_tokens": "llm_max_tokens",
        "input_tokens": "rag_input_tokens",
    },
    "embedding": {
        "model": "embedding_model",
        "base_url": "embedding_base_url",
        "api_key": "embedding_api_key",
        "dimension": "embedding_dim",
        "max_input_tokens": "embedding_max_input_tokens",
    },
}


async def read_overrides(database):
    async with database.read():
        cursor = await database.connection.execute(
            "SELECT payload FROM model_settings WHERE singleton=TRUE"
        )
        row = await cursor.fetchone()
    return row["payload"] if row else {}


def effective_settings(base, overrides):
    values = base.model_dump()
    for kind, fields in FIELDS.items():
        for field, target in fields.items():
            if field in overrides.get(kind, {}):
                values[target] = overrides[kind][field]
    return Settings(_env_file=None, **values)


class ModelSettingsService:
    def __init__(self, app, database, base, overrides):
        self.app, self.database, self.base, self.overrides = app, database, base, overrides
        self.lock = asyncio.Lock()

    def info(self, reindex=False):
        settings = self.app.state.runtime.settings
        common = {}
        for kind in FIELDS:
            key = getattr(settings, kind + "_api_key").get_secret_value()
            model, url = getattr(settings, kind + "_model"), getattr(settings, kind + "_base_url")
            common[kind] = {
                "model": model,
                "base_url": url,
                "key_configured": bool(key.strip()),
                "source": "custom" if kind in self.overrides else "environment",
                "ready": self.app.state.runtime.ready
                if kind == "llm"
                else self.app.state.embedding_model is not None,
            }
        return ModelsInfo(
            llm=LLMInfo(
                **common["llm"],
                provider=settings.llm_provider,
                max_tokens=settings.llm_max_tokens,
                input_tokens=settings.rag_input_tokens,
            ),
            embedding=EmbeddingInfo(
                **common["embedding"],
                dimension=settings.embedding_dim,
                max_input_tokens=settings.embedding_max_input_tokens,
            ),
            embedding_reindex_required=reindex,
        )

    @complete_on_cancel
    async def save(self, kind, body=None):
        state = self.app.state
        async with self.lock, state.index_service.schedule_lock, state.run_service.control_lock:
            index, runs = state.index_service, state.run_service
            if any(not task.done() for task in [*index.tasks.values(), *runs.tasks.values()]):
                raise ChatError(
                    "model_settings_busy", "请先完成或取消问答及索引任务，再修改模型。", 409, True
                )
            async with self.database.read():
                cursor = await self.database.connection.execute(
                    "SELECT 1 FROM runs WHERE status IN "
                    "('queued','running','paused','recovering') LIMIT 1"
                )
                if await cursor.fetchone():
                    raise ChatError(
                        "model_settings_busy",
                        "存在未结束的问答，请先完成或取消后再修改模型。",
                        409,
                        True,
                    )
            overrides = {**self.overrides}
            old = state.runtime.settings
            if body is None:
                overrides.pop(kind, None)
            else:
                values = body.model_dump(exclude={"api_key"})
                key = body.api_key.get_secret_value().strip() if body.api_key else ""
                key = key or getattr(old, kind + "_api_key").get_secret_value()
                if not key:
                    raise ChatError("model_key_required", "首次配置时请输入 API Key。", 422)
                overrides[kind] = {**values, "api_key": key}
            settings = effective_settings(self.base, overrides)
            if settings.llm_max_tokens + settings.chat_token_overhead >= int(
                settings.rag_input_tokens * settings.token_safety_ratio
            ):
                raise ChatError(
                    "invalid_model_budget", "输出上限和预留量必须小于模型上下文窗口。", 422
                )
            embedding = state.embedding_model if kind == "llm" else build_embedding_model(settings)
            vector = (
                state.vector_service
                if kind == "llm"
                else VectorService(
                    index.files, index.index, settings, embedding, metrics=state.metrics
                )
            )
            old_vector = state.vector_service.version
            try:
                async with self.database.transaction():
                    await self.database.connection.execute(
                        "INSERT INTO model_settings(singleton,payload) VALUES (TRUE,%s) "
                        "ON CONFLICT(singleton) DO UPDATE SET payload=EXCLUDED.payload",
                        (Jsonb(overrides),),
                    )
            except psycopg.Error:
                # 提交结果不确定时先对账，再决定是否发布内存配置。
                if await read_overrides(self.database) != overrides:
                    raise
            retrieval = RetrievalService(
                state.retrieval_service.vectors,
                vector,
                settings,
                state.retrieval_service.reranker,
                lexical=state.lexical_index,
                metrics=state.metrics,
            )
            index.settings, index.vector_service = settings, vector
            index.chunk_version = chunk_version(settings)
            state.embedding_model, state.vector_service, state.retrieval_service = (
                embedding,
                vector,
                retrieval,
            )
            state.runtime.settings, state.runtime.retrieval_service = settings, retrieval
            # 注入的模型仅用于当前测试/宿主；用户显式保存 LLM 配置后使用所选客户端。
            if kind == "llm":
                state.runtime.model = None
            runs.graph = None
            self.overrides = overrides
            return self.info(kind == "embedding" and old_vector != vector.version)


@router.get("", response_model=ModelsInfo, dependencies=[Depends(require_admin)])
async def get_models(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.model_settings.info()


@router.put("/llm", response_model=ModelsInfo, dependencies=[Depends(require_admin)])
async def save_llm(body: LLMInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.model_settings.save("llm", body)


@router.put("/embedding", response_model=ModelsInfo, dependencies=[Depends(require_admin)])
async def save_embedding(body: EmbeddingInput, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.model_settings.save("embedding", body)


@router.delete("/{kind}", response_model=ModelsInfo, dependencies=[Depends(require_admin)])
async def reset_model(kind: Kind, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.model_settings.save(kind)
