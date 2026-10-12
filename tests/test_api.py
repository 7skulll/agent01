import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.pipeline import RAGService, build_answer_chain, build_rewrite_chain, split_documents


class RecordingRetriever:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    async def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return self.documents


def make_settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_health_and_unconfigured_query():
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/v1/health").json() == {
            "status": "ok",
            "rag_ready": False,
            "chat_ready": False,
        }
        response = client.post(
            "/api/v1/query", json={"knowledge_base_id": "hr", "question": "年假？"}
        )
        assert response.status_code == 503
        assert "尚未接入" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"knowledge_base_id": "hr", "question": "   "},
        {"knowledge_base_id": "", "question": "年假？"},
        {
            "knowledge_base_id": "hr",
            "question": "年假？",
            "history": [{"role": "system", "content": "x"}],
        },
    ],
)
def test_invalid_query(body):
    with TestClient(create_app(make_settings())) as client:
        assert client.post("/api/v1/query", json=body).status_code == 422


def test_follow_up_rewrites_before_scoped_retrieval():
    retriever = RecordingRetriever(
        [Document(page_content="年假申请需提前三天。", metadata={"knowledge_base_id": "hr"})]
    )
    prompts = []

    def answer(prompt):
        prompts.append(prompt.to_messages())
        return AIMessage(content="年假申请需提前三天。")

    service = RAGService(
        make_settings(),
        retriever=retriever,
        answer_chain=build_answer_chain(RunnableLambda(answer)),
        rewrite_chain=build_rewrite_chain(
            RunnableLambda(lambda prompt: AIMessage(content="年假申请需要提前几天？"))
        ),
    )
    with TestClient(create_app(make_settings(), service)) as client:
        response = client.post(
            "/api/v1/query",
            json={
                "knowledge_base_id": "hr",
                "question": "要提前几天？",
                "history": [{"role": "user", "content": "我想申请年假"}],
            },
        )
        assert response.json() == {"answer": "年假申请需提前三天。", "status": "answered"}
        assert retriever.calls == [
            {"query": "年假申请需要提前几天？", "knowledge_base_id": "hr", "top_k": 6}
        ]
        assert "年假申请需提前三天" in prompts[0][0].content
        assert prompts[0][-1].content == "要提前几天？"


@pytest.mark.parametrize(
    "documents",
    [[], [Document(page_content="财务机密", metadata={"knowledge_base_id": "finance"})]],
)
def test_no_evidence_or_wrong_knowledge_base_never_calls_answer_model(documents):
    def forbidden_model(_):
        raise AssertionError("No answer model call is allowed without evidence")

    service = RAGService(
        make_settings(),
        retriever=RecordingRetriever(documents),
        answer_chain=build_answer_chain(RunnableLambda(forbidden_model)),
        rewrite_chain=build_rewrite_chain(RunnableLambda(forbidden_model)),
    )
    with TestClient(create_app(make_settings(), service)) as client:
        result = client.post(
            "/api/v1/query", json={"knowledge_base_id": "hr", "question": "年假？"}
        )
        assert result.status_code == 200
        assert result.json()["status"] == "insufficient_evidence"


def test_chunking_preserves_document_metadata():
    metadata = {"knowledge_base_id": "hr", "file_id": "policy.docx", "page": 2}
    docs = [Document(page_content="这是员工制度。" * 20, metadata=metadata)]
    chunks = split_documents(docs, make_settings(chunk_size=30, chunk_overlap=5))
    assert len(chunks) > 1
    assert all(chunk.metadata == metadata and len(chunk.page_content) <= 30 for chunk in chunks)


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValidationError):
        make_settings(chunk_size=10, chunk_overlap=10)
