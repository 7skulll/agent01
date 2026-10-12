from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", extra="ignore")

    app_name: str = "Enterprise RAG"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=30, gt=0, le=300)
    llm_max_tokens: int = Field(default=256, ge=1, le=8192)
    chat_database_path: Path = Path("data/chat.sqlite3")
    agent_max_model_calls: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=30)
    agent_timeout_seconds: float = Field(default=120, gt=0, le=600)
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    chunk_size: int = Field(default=1000, ge=1)
    chunk_overlap: int = Field(default=150, ge=0)
    retrieval_top_k: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def validate_chunking(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self
