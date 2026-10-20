from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_", env_file=".env", extra="ignore", populate_by_name=True
    )

    app_name: str = "Enterprise RAG"
    # Empty disables the shared-token gate; set RAG_API_TOKEN to enable it.
    api_token: SecretStr = SecretStr("")
    llm_provider: Literal["deepseek", "openai"] = "deepseek"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=30, gt=0, le=300)
    llm_max_tokens: int = Field(default=256, ge=1, le=8192)
    database_url: SecretStr = SecretStr(
        "postgresql://rag:rag_local_dev@127.0.0.1:5432/langchain_rag"
    )
    database_pool_size: int = Field(default=10, ge=3, le=100)
    file_storage_path: Path = Path("data/files")
    chunk_size_tokens: int = Field(default=512, ge=64, le=4096)
    chunk_overlap_tokens: int = Field(default=64, ge=0, le=1024)
    chunk_max_per_file: int = Field(default=20000, ge=1, le=1000000)
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dim: int = Field(default=0, ge=0, le=16000)
    embedding_batch_size: int = Field(default=10, ge=1, le=256)
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=300)
    embedding_max_retries: int = Field(default=3, ge=0, le=10)
    embedding_normalize: bool = True
    # 通用 BPE 是估算编码；不根据兼容 API 的模型名猜测 tokenizer。
    embedding_tokenizer: Literal["cl100k_base", "o200k_base", "heuristic"] = "cl100k_base"
    chat_tokenizer: Literal["cl100k_base", "o200k_base", "heuristic"] = "cl100k_base"
    # 0 表示未知；真实 Embedding 客户端要求显式配置单条上限。
    embedding_max_input_tokens: int = Field(default=0, ge=0)
    embedding_max_batch_tokens: int = Field(default=0, ge=0)
    token_safety_ratio: float = Field(default=0.95, gt=0, le=1)
    chat_token_overhead: int = Field(default=256, ge=0)
    vector_search_mode: Literal["exact", "hnsw"] = "exact"
    hnsw_ef_search: int = Field(default=100, ge=10, le=1000)
    retrieval_answer_candidates: int = Field(default=10, ge=1, le=20)
    retrieval_dense_weight: float = Field(default=0.5, ge=0, le=1)
    rerank_enabled: bool = False
    rerank_provider: Literal["siliconflow"] = "siliconflow"
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_api_key: SecretStr = SecretStr("")
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_tokenizer: Literal["cl100k_base", "o200k_base", "heuristic"] = "cl100k_base"
    # 0 将窗口处理交给服务端；配置后使用本地计数器预估查询与正文的总预算。
    rerank_max_input_tokens: int = Field(default=0, ge=0)
    rerank_window_overlap_tokens: int = Field(default=64, ge=0, le=1024)
    rerank_batch_size: int = Field(default=64, ge=1, le=256)
    rerank_max_windows: int = Field(default=1024, ge=1, le=4096)
    retrieval_rerank_candidates: int = Field(default=40, ge=20, le=500)
    retrieval_rerank_weight: float = Field(default=1, ge=0, le=1)
    retrieval_rerank_min_score: float | None = Field(default=None, allow_inf_nan=False)
    retrieval_rerank_timeout_seconds: float = Field(default=10, gt=0, le=60)
    retrieval_max_per_file: int = Field(default=20, ge=1, le=20)
    retrieval_neighbor_chunks: int = Field(default=0, ge=0, le=2)
    rag_history_tokens: int = Field(
        default=1536,
        ge=0,
        le=16384,
        validation_alias=AliasChoices("RAG_HISTORY_TOKENS", "RAG_RAG_HISTORY_TOKENS"),
    )
    rag_answer_json_mode: bool = Field(
        default=True,
        validation_alias=AliasChoices("RAG_ANSWER_JSON_MODE", "RAG_RAG_ANSWER_JSON_MODE"),
    )
    rag_history_turns: int = Field(
        default=3,
        ge=0,
        le=20,
        validation_alias=AliasChoices("RAG_HISTORY_TURNS", "RAG_RAG_HISTORY_TURNS"),
    )
    rag_neighbor_budget_ratio: float = Field(
        default=0.2,
        ge=0,
        le=0.5,
        validation_alias=AliasChoices("RAG_NEIGHBOR_BUDGET_RATIO", "RAG_RAG_NEIGHBOR_BUDGET_RATIO"),
    )
    rag_query_rewrite: bool = Field(
        default=False, validation_alias=AliasChoices("RAG_QUERY_REWRITE", "RAG_RAG_QUERY_REWRITE")
    )
    rag_query_expand: bool = Field(
        default=False, validation_alias=AliasChoices("RAG_QUERY_EXPAND", "RAG_RAG_QUERY_EXPAND")
    )
    rag_query_timeout_seconds: float = Field(
        default=10,
        gt=0,
        le=60,
        validation_alias=AliasChoices("RAG_QUERY_TIMEOUT_SECONDS", "RAG_RAG_QUERY_TIMEOUT_SECONDS"),
    )
    rag_context_tokens: int = Field(
        default=2048,
        ge=64,
        le=16384,
        validation_alias=AliasChoices("RAG_CONTEXT_TOKENS", "RAG_RAG_CONTEXT_TOKENS"),
    )
    rag_input_tokens: int = Field(
        default=8192,
        ge=512,
        le=131072,
        validation_alias=AliasChoices("RAG_INPUT_TOKENS", "RAG_RAG_INPUT_TOKENS"),
    )
    parse_pdf_layout_mode: bool = True
    parse_remove_header_footer: bool = True
    parse_remove_toc: bool = True
    table_context_chars: int = Field(default=0, ge=0, le=2000)
    agent_max_model_calls: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=30)
    agent_timeout_seconds: float = Field(default=120, gt=0, le=600)
    stream_heartbeat_seconds: float = Field(default=15, gt=0, le=60)
    cors_allow_origins: str = "*"
