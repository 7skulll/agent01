import pytest
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from app.agent.runtime import build_chat_model
from app.config import Settings


@pytest.mark.parametrize(
    "provider,model_type", [("openai", ChatOpenAI), ("deepseek", ChatDeepSeek)]
)
def test_chat_factory_uses_explicit_provider_endpoint_and_key(provider, model_type):
    config = Settings(
        _env_file=None,
        llm_provider=provider,
        llm_model="custom-chat",
        llm_base_url="https://models.example/v1",
        llm_api_key="test-key",
    )
    model = build_chat_model(config)
    assert isinstance(model, model_type)
    assert model.model_name == "custom-chat"
    assert str(model.openai_api_base).rstrip("/") == "https://models.example/v1"
    assert model.openai_api_key.get_secret_value() == "test-key"
