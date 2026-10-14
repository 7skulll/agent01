from pathlib import Path

from pydantic import Field, SecretStr
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
    file_storage_path: Path = Path("data/files")
    chunk_size_tokens: int = Field(default=512, ge=64, le=4096)
    chunk_overlap_tokens: int = Field(default=64, ge=0, le=1024)
    agent_max_model_calls: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=30)
    agent_timeout_seconds: float = Field(default=120, gt=0, le=600)
    stream_heartbeat_seconds: float = Field(default=15, gt=0, le=60)
    cors_allow_origins: str = "*"
