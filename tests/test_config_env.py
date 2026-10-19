"""使用文档中的环境变量名，防止 rag_ 字段与全局前缀意外重复。"""

import pytest

from app.config import Settings


@pytest.mark.parametrize("prefix", ["RAG_", "RAG_RAG_"])
def test_rag_settings_read_documented_and_legacy_dotenv_names(tmp_path, prefix):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                f"{prefix}INPUT_TOKENS=16384",
                f"{prefix}CONTEXT_TOKENS=4096",
                f"{prefix}QUERY_REWRITE=false",
                f"{prefix}QUERY_EXPAND=true",
                f"{prefix}QUERY_TIMEOUT_SECONDS=7",
                f"{prefix}HISTORY_TOKENS=1000",
                f"{prefix}HISTORY_TURNS=2",
                f"{prefix}NEIGHBOR_BUDGET_RATIO=0.1",
                f"{prefix}ANSWER_JSON_MODE=false",
            ]
        )
    )
    config = Settings(_env_file=env)
    assert config.rag_input_tokens == 16384
    assert config.rag_context_tokens == 4096
    assert config.rag_query_rewrite is False
    assert config.rag_query_expand is True
    assert config.rag_query_timeout_seconds == 7
    assert config.rag_history_tokens == 1000
    assert config.rag_history_turns == 2
    assert config.rag_neighbor_budget_ratio == 0.1
    assert config.rag_answer_json_mode is False


def test_rag_settings_read_process_environment(monkeypatch):
    monkeypatch.setenv("RAG_INPUT_TOKENS", "16384")
    monkeypatch.setenv("RAG_CONTEXT_TOKENS", "4096")
    config = Settings(_env_file=None)
    assert config.rag_input_tokens == 16384
    assert config.rag_context_tokens == 4096


def test_settings_accept_explicit_python_field_names():
    config = Settings(_env_file=None, rag_input_tokens=1024, rag_context_tokens=128)
    assert config.rag_input_tokens == 1024
    assert config.rag_context_tokens == 128
