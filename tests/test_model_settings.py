import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from support import ScriptModel, settings, test_database, wait_run
from test_rag_pipeline import Embeddings, index, upload

from app.main import create_app

URL = "/api/v1/settings/models"
LLM = {
    "provider": "openai",
    "model": "custom-chat",
    "base_url": "https://models.example/v1",
    "api_key": "private-chat-secret",
    "max_tokens": 128,
    "input_tokens": 8192,
}
EMBEDDING = {
    "model": "custom-embedding",
    "base_url": "https://vectors.example/v1",
    "api_key": "private-embedding-secret",
    "dimension": 3,
    "max_input_tokens": 8192,
}


def test_llm_save_preserves_secret_runs_with_selected_model_and_survives_restart(monkeypatch):
    chosen = []
    model = ScriptModel(outputs=[AIMessage(content="custom reply")])

    def build(config):
        chosen.append(
            (config.llm_model, config.llm_base_url, config.llm_api_key.get_secret_value())
        )
        return model

    monkeypatch.setattr("app.agent.runtime.build_chat_model", build)
    config = settings()
    with TestClient(create_app(config)) as client:
        response = client.put(URL + "/llm", json=LLM)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "private-chat-secret" not in response.text
        assert "api_key" not in response.json()["llm"]
        assert response.json()["llm"]["key_configured"]
        assert response.json()["llm"]["provider"] == "openai"
        assert client.app.state.runtime.ready
        thread = client.post("/api/v1/conversations").json()["thread_id"]
        row = client.post(
            f"/api/v1/conversations/{thread}/runs",
            json={"message": "hello", "idempotency_key": "custom"},
        ).json()
        done = wait_run(client, row["run_id"], {"completed", "failed"})
        assert done["status"] == "completed" and done["answer"] == "custom reply"
        assert chosen == [(LLM["model"], LLM["base_url"], LLM["api_key"])]
        changed = {**LLM, "model": "new-model", "api_key": ""}
        assert client.put(URL + "/llm", json=changed).status_code == 200
        assert client.app.state.runtime.settings.llm_api_key.get_secret_value() == LLM["api_key"]
    with TestClient(create_app(config)) as client:
        response = client.get(URL)
        assert response.json()["llm"]["model"] == "new-model"
        assert LLM["api_key"] not in response.text
        assert client.app.state.runtime.settings.llm_api_key.get_secret_value() == LLM["api_key"]
        restored = client.delete(URL + "/llm")
        assert restored.status_code == 200
        assert restored.json()["llm"]["source"] == "environment"
        assert restored.json()["llm"]["model"] == config.llm_model


def test_embedding_config_is_used_for_index_and_query_and_key_rotation_keeps_version(monkeypatch):
    models = []

    class CustomEmbedding(Embeddings):
        model_name = "custom-embedding"

    def build(config):
        model = CustomEmbedding()
        models.append((config, model))
        return model

    monkeypatch.setattr("app.model_settings.build_embedding_model", build)
    with TestClient(create_app(settings())) as client:
        before = client.app.state.index_service.chunk_version
        response = client.put(URL + "/embedding", json=EMBEDDING)
        assert response.status_code == 200 and response.json()["embedding_reindex_required"]
        assert EMBEDDING["api_key"] not in response.text
        assert client.app.state.index_service.chunk_version != before
        upload(client, "员工年假十天。")
        index(client)
        result = client.post(
            "/api/v1/knowledge-bases/default/search",
            json={"query": "年假", "mode": "vector", "min_score": -1},
        )
        assert result.status_code == 200 and result.json()["sources"]
        assert models[0][1].calls and models[0][1].queries
        version = client.app.state.vector_service.version
        changed = client.put(URL + "/embedding", json={**EMBEDDING, "api_key": "rotated-secret"})
        assert changed.status_code == 200
        assert not changed.json()["embedding_reindex_required"]
        assert client.app.state.vector_service.version == version
        assert client.app.state.vector_service.model is models[-1][1]


def test_settings_switch_does_not_mix_old_vectors_and_new_model(monkeypatch):
    model = Embeddings()
    with TestClient(create_app(settings(), embedding_model=model)) as client:
        upload(client, "员工年假十天。")
        index(client)
        monkeypatch.setattr("app.model_settings.build_embedding_model", lambda _: Embeddings())
        saved = client.put(URL + "/embedding", json=EMBEDDING)
        assert saved.status_code == 200
        result = client.post(
            "/api/v1/knowledge-bases/default/search",
            json={"query": "年假", "mode": "vector", "min_score": -1},
        )
        assert result.json()["sources"] == []
        keyword = client.post(
            "/api/v1/knowledge-bases/default/search", json={"query": "年假", "mode": "keyword"}
        )
        assert keyword.status_code == 200 and keyword.json()["sources"]


@pytest.mark.parametrize("status", ["running", "paused", "recovering"])
def test_unfinished_run_prevents_model_switch(status):
    with TestClient(
        create_app(settings(), chat_model=ScriptModel(outputs=[AIMessage(content="OK")]))
    ) as client:
        thread = client.post("/api/v1/conversations").json()["thread_id"]
        from app.schemas import RunRequest

        row, _ = client.portal.call(
            client.app.state.run_service.repository.create,
            thread,
            RunRequest(message="question", idempotency_key="busy"),
            "request",
        )
        with test_database() as db:
            db.execute("UPDATE runs SET status=%s WHERE run_id=%s", (status, row["run_id"]))
        response = client.put(URL + "/llm", json=LLM)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "model_settings_busy"
        assert client.get(URL).json()["llm"]["source"] == "environment"


def test_running_index_prevents_model_switch():
    import asyncio

    async def scenario(service):
        task = asyncio.create_task(asyncio.sleep(60))
        service.tasks["default"] = task

    with TestClient(create_app(settings())) as client:
        client.portal.call(scenario, client.app.state.index_service)
        assert client.put(URL + "/llm", json=LLM).status_code == 409


@pytest.mark.parametrize(
    "body,code",
    [
        ({**LLM, "api_key": ""}, "model_key_required"),
        ({**LLM, "base_url": "file:///tmp/key"}, "validation_error"),
        ({**LLM, "base_url": "https://user:password@example.com/v1"}, "validation_error"),
        ({**LLM, "max_tokens": 8192, "input_tokens": 512}, "invalid_model_budget"),
    ],
)
def test_invalid_llm_settings_are_rejected_without_disclosing_keys(body, code):
    with TestClient(create_app(settings())) as client:
        response = client.put(URL + "/llm", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == code
        assert LLM["api_key"] not in response.text
        assert client.get(URL).json()["llm"]["source"] == "environment"


def test_missing_embedding_token_limit_is_rejected():
    with TestClient(create_app(settings())) as client:
        response = client.put(URL + "/embedding", json={**EMBEDDING, "max_input_tokens": 0})
        assert response.status_code == 422
        assert EMBEDDING["api_key"] not in response.text


def test_failed_database_write_does_not_publish_config(monkeypatch):
    import psycopg

    with TestClient(create_app(settings())) as client:
        connection = client.app.state.model_settings.database.connection
        original = connection.execute

        async def fail(statement, *args, **kwargs):
            if str(statement).startswith("INSERT INTO model_settings"):
                raise psycopg.OperationalError("private diagnostic")
            return await original(statement, *args, **kwargs)

        monkeypatch.setattr(connection, "execute", fail)
        response = client.put(URL + "/llm", json=LLM)
        assert response.status_code == 503
        assert "private diagnostic" not in response.text
        assert client.get(URL).json()["llm"]["source"] == "environment"
        assert not client.app.state.runtime.ready


def test_keys_are_absent_from_checkpointed_run_data(monkeypatch):
    model = ScriptModel(outputs=[AIMessage(content="OK")])
    monkeypatch.setattr("app.agent.runtime.build_chat_model", lambda _: model)
    with TestClient(create_app(settings())) as client:
        assert client.put(URL + "/llm", json=LLM).status_code == 200
        thread = client.post("/api/v1/conversations").json()["thread_id"]
        row = client.post(
            f"/api/v1/conversations/{thread}/runs",
            json={"message": "hi", "idempotency_key": "safe"},
        ).json()
        wait_run(client, row["run_id"], {"completed"})
        snapshot = client.portal.call(
            client.app.state.run_service.get_graph().aget_state,
            client.app.state.run_service.config(row["run_id"]),
        )
        assert LLM["api_key"] not in str(snapshot.values)
        events = client.get(f"/api/v1/runs/{row['run_id']}/events").text
        assert LLM["api_key"] not in events
        with test_database() as db:
            payload = db.execute("SELECT payload FROM model_settings").fetchone()[0]
            assert json.loads(json.dumps(payload))["llm"]["api_key"] == LLM["api_key"]


def test_real_embedding_client_config_persists_without_external_calls():
    config = settings()
    with TestClient(create_app(config)) as client:
        response = client.put(URL + "/embedding", json=EMBEDDING)
        assert response.status_code == 200
        first_version = client.app.state.vector_service.version
        assert client.app.state.embedding_model.model_name == EMBEDDING["model"]
        assert client.app.state.embedding_model.dim == EMBEDDING["dimension"]
    with TestClient(create_app(config)) as client:
        assert client.app.state.vector_service.version == first_version
        assert client.get(URL).json()["embedding"]["source"] == "custom"
        assert (
            client.app.state.embedding_model._client.openai_api_key.get_secret_value()
            == EMBEDDING["api_key"]
        )
        reset = client.delete(URL + "/embedding")
        assert reset.status_code == 200
        assert reset.json()["embedding"]["source"] == "environment"
        assert not client.app.state.embedding_model
