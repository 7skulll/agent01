"""Two-case real-model smoke for same-run evaluation; isolated test schema, synthetic text only."""

import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from scripts._database import temporary_database

ROOT = Path(__file__).resolve().parents[1]


async def main():
    settings = Settings()
    config = settings.model_copy(
        update={
            "database_url": SecretStr(
                make_conninfo(settings.database_url.get_secret_value(), dbname="langchain_rag_test")
            )
        }
    )
    report = {"scope": "two_synthetic_cases_real_models_isolated_schema", "checks": []}
    async with temporary_database(config) as database_url:
        with tempfile.TemporaryDirectory(prefix="evaluation-smoke-") as folder:
            runtime = config.model_copy(
                update={"database_url": database_url, "file_storage_path": Path(folder) / "files"}
            )
            app = create_app(runtime)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://evaluation-smoke"
                ) as client:
                    response = await client.post(
                        "/api/v1/knowledge-bases/default/files",
                        files={
                            "file": (
                                "年假制度.md",
                                (
                                    "# 员工年假\n\n公司员工每年享有十天带薪年假。"
                                    "申请需要直属主管审批。"
                                ).encode(),
                            )
                        },
                        headers={"Idempotency-Key": "evaluation-smoke"},
                    )
                    response.raise_for_status()
                    file_id = response.json()["file_id"]
                    response = await client.post("/api/v1/knowledge-bases/default/index")
                    response.raise_for_status()
                    await app.state.index_service.wait()
                    response = await client.post(
                        "/api/v1/evaluations",
                        json={
                            "idempotency_key": str(uuid4()),
                            "knowledge_base_id": "default",
                            "kind": "rag",
                            "mode": "keyword",
                            "k": 3,
                            "cases": [
                                {
                                    "query": "员工每年有多少天带薪年假，谁审批？",
                                    "expected": "answerable",
                                    "relevant_file_ids": [file_id],
                                },
                                {
                                    "query": "员工生日礼金的精确金额是多少？",
                                    "expected": "unanswerable",
                                },
                            ],
                        },
                    )
                    response.raise_for_status()
                    evaluation_id = response.json()["evaluation_id"]
                    async with asyncio.timeout(300):
                        task = app.state.evaluations.tasks.get(evaluation_id)
                        if task:
                            await task
                    response = await client.get("/api/v1/evaluations/" + evaluation_id)
                    response.raise_for_status()
                    result = response.json()
                    assert result["status"] == "completed", result.get("error")
                    assert all(r["status"] == "completed" for r in result["results"]), result[
                        "results"
                    ]
                    assert result["results"][0]["run"]["answer_status"] == "answered"
                    assert result["results"][1]["run"]["answer_status"] == "insufficient_evidence"
                    assert result["summary"]["metrics"]["recall"] == 1
                    assert result["summary"]["metrics"]["precision"] == 1 / 3
                    assert all("trace" in r["diagnostics"] for r in result["results"])
                    report.update(
                        summary=result["summary"],
                        results=[
                            {
                                "expected": r["case"]["expected"],
                                "status": r["status"],
                                "answer_status": r["run"]["answer_status"],
                                "sources": len(r["sources"]),
                                "context_sources": len(r["context_sources"]),
                                "elapsed_ms": r["elapsed_ms"],
                                "token_usage": r["token_usage"],
                            }
                            for r in result["results"]
                        ],
                    )
                    response = await client.get("/api/v1/conversations")
                    assert response.json() == []
                    report["checks"].extend(
                        [
                            "real_retrieval_trace",
                            "same_run_answer_and_sources",
                            "labelled_ir_metrics",
                            "real_refusal",
                            "evaluation_history_isolation",
                        ]
                    )
            restarted = create_app(runtime)
            async with restarted.router.lifespan_context(restarted):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted), base_url="http://evaluation-smoke"
                ) as client:
                    response = await client.get("/api/v1/evaluations/" + evaluation_id)
                    assert response.json()["summary"] == report["summary"]
                    report["checks"].append("restart_persistence")
    report["temporary_schema_cleaned"] = True
    output = ROOT / "evals/results/evaluation-v1-smoke.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
