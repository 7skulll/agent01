"""真实上传、索引、重排、引用问答和重启持久化验证；只使用测试库隔离 schema。"""

import argparse
import asyncio
import json
import re
import tempfile
from io import BytesIO
from pathlib import Path

import httpx
from docx import Document
from openpyxl import Workbook
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from scripts._database import temporary_database

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/v1"
KB = BASE + "/knowledge-bases/default"


def documents():
    """构造四种小文件，包含两个空表头回归场景。"""
    word = Document()
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(1, 0).text = "预算"
    table.cell(1, 1).text = "5000元"
    word_bytes = BytesIO()
    word.save(word_bytes)
    excel = Workbook()
    excel.active.append(["设备型号", None])
    excel.active.append(["ZX-314", "24伏"])
    excel_bytes = BytesIO()
    excel.save(excel_bytes)
    excel.close()
    return [
        (
            "年假制度.md",
            "# 员工年假\n\n公司员工每年享有十天带薪年假。年假申请需要直属主管审批。".encode(),
        ),
        ("预算表.docx", word_bytes.getvalue()),
        ("设备参数.xlsx", excel_bytes.getvalue()),
        ("食堂通知.txt", "公司食堂每天中午十二点开餐。".encode()),
    ]


async def body(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def main(output: Path | None = None):
    settings = Settings()
    if not settings.rerank_enabled:
        raise SystemExit("完整验证要求 .env 已启用 Rerank。")
    settings = settings.model_copy(
        update={
            "database_url": SecretStr(
                make_conninfo(
                    settings.database_url.get_secret_value(),
                    dbname="langchain_rag_test",
                )
            )
        }
    )
    report = {"scope": "isolated_test_schema_real_embedding_rerank_chat", "checks": []}

    def passed(name, **details):
        report["checks"].append({"name": name, **details})
        print(json.dumps({"passed": name, **details}, ensure_ascii=False), flush=True)

    async with temporary_database(settings) as database_url:
        with tempfile.TemporaryDirectory(prefix="rag-e2e-") as directory:
            config = settings.model_copy(
                update={
                    "database_url": database_url,
                    "file_storage_path": Path(directory) / "files",
                }
            )
            app = create_app(config)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://smoke"
                ) as client:
                    health = await body(client, "GET", BASE + "/health")
                    assert health["chat_ready"] and health["embedding_ready"], health
                    passed("startup_and_health")
                    for index, (name, data) in enumerate(documents()):
                        uploaded = await body(
                            client,
                            "POST",
                            KB + "/files",
                            files={"file": (name, data)},
                            headers={"Idempotency-Key": f"smoke-file-{index}"},
                        )
                        assert uploaded["index_status"] == "not_indexed"
                    passed("upload_four_formats")
                    await body(client, "POST", KB + "/index")
                    await app.state.index_service.wait()
                    files = (await body(client, "GET", KB + "/files"))["items"]
                    assert len(files) == 4 and all(f["index_status"] == "indexed" for f in files), (
                        files
                    )
                    assert all(f["keyword_ready"] and not f["keyword_error"] for f in files)
                    passed(
                        "parse_chunk_embed_publish",
                        chunk_count=sum(f["chunk_count"] for f in files),
                    )
                    for mode in ("keyword", "vector", "hybrid"):
                        found = await body(
                            client,
                            "POST",
                            KB + "/search",
                            json={
                                "query": "公司员工年假有多少天，需要谁审批？",
                                "mode": mode,
                            },
                        )
                        diag = found["diagnostics"]
                        assert found["sources"] and any(
                            "十天" in s["content"] for s in found["sources"]
                        )
                        assert diag["rerank_applied"] and not diag["degraded"], diag
                        passed(
                            mode + "_search_with_real_rerank", rerank_ms=diag["stage_ms"]["rerank"]
                        )
                    for query, expected in (("预算", "第2列: 5000元"), ("ZX-314", "第2列: 24伏")):
                        found = await body(
                            client,
                            "POST",
                            KB + "/search",
                            json={
                                "query": query,
                                "mode": "keyword",
                            },
                        )
                        assert any(expected in s["content"] for s in found["sources"]), found
                    passed("blank_header_values_searchable")
                    conversation = await body(client, "POST", BASE + "/conversations")
                    cid = conversation["thread_id"]
                    run = await body(
                        client,
                        "POST",
                        BASE + f"/conversations/{cid}/runs",
                        json={
                            "message": (
                                "公司员工每年有多少天带薪年假，申请由谁审批？"
                                "请依据资料回答并给出引用。"
                            ),
                            "idempotency_key": "real-rag-smoke",
                            "knowledge_base_id": "default",
                        },
                    )
                    run_path = BASE + "/runs/" + run["run_id"]
                    async with asyncio.timeout(settings.agent_timeout_seconds + 20):
                        while True:
                            result = await body(client, "GET", run_path)
                            if result["status"] in ("completed", "failed", "cancelled"):
                                break
                            await asyncio.sleep(0.1)
                    assert result["status"] == "completed", result.get("error")
                    answer = result["answer"]
                    assert re.search(r"十天|10\s*天", answer) and "主管" in answer, answer
                    assert result["sources"] and re.search(r"\[\d+\]", answer), result
                    assert result["answer_status"] == "answered"
                    assert (
                        result["answer_protocol"] == "rag-answer-v2" and result["answer_segments"]
                    )
                    response = await client.get(run_path + "/events")
                    response.raise_for_status()
                    assert "\nevent: text_delta\n" not in response.text
                    events = []
                    for frame in response.text.split("\n\n"):
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            None,
                        )
                        if data:
                            events.append(json.loads(data))
                    assert any(e.get("diagnostics", {}).get("rerank_applied") for e in events)
                    assembly = next(e for e in events if e.get("assembly_version") == "context-v1")
                    assert assembly["source_count"] > 0
                    assert assembly["total_estimated_tokens"] <= assembly["input_budget"]
                    passed("context_assembly_budget_and_snapshot", diagnostics=assembly)
                    history = await body(client, "GET", BASE + f"/conversations/{cid}")
                    assert history["messages"][-1]["content"] == answer
                    passed("real_chat_citations_sse_and_history", answer=answer)
                    passed(
                        "structured_answer_protocol",
                        protocol=result["answer_protocol"],
                        segments=result["answer_segments"],
                        repair_calls=result["answer_repair_calls"],
                    )
                    missing_cid = (await body(client, "POST", BASE + "/conversations"))["thread_id"]
                    missing = await body(
                        client,
                        "POST",
                        BASE + f"/conversations/{missing_cid}/runs",
                        json={
                            "message": (
                                "员工年假制度的精确生效日期是哪年哪月哪日？请仅依据资料确认。"
                            ),
                            "idempotency_key": "missing-fact",
                            "knowledge_base_id": "default",
                            "retrieval_mode": "keyword",
                        },
                    )
                    missing_path = BASE + "/runs/" + missing["run_id"]
                    async with asyncio.timeout(settings.agent_timeout_seconds + 20):
                        while True:
                            refused = await body(client, "GET", missing_path)
                            if refused["status"] in ("completed", "failed", "cancelled"):
                                break
                            await asyncio.sleep(0.1)
                    assert (
                        refused["status"] == "completed"
                        and refused["answer_status"] == "insufficient_evidence"
                    ), refused
                    assert refused["answer_reason"] == "missing_fact" and not refused["sources"]
                    passed(
                        "real_model_insufficient_evidence",
                        answer_status=refused["answer_status"],
                        reason=refused["answer_reason"],
                    )
                    snapshot = await body(client, "GET", BASE + "/metrics")
                    stages = {row["stage"] for row in snapshot["timings"]}
                    assert {
                        "parse",
                        "chunk",
                        "lexical_index",
                        "embedding_documents",
                        "vector_index",
                        "keyword_recall",
                        "vector_recall",
                        "rerank",
                        "answer_model",
                    } <= stages
                    assert any(
                        row["name"] == "rerank_http_attempts" and row["value"] > 0
                        for row in snapshot["counters"]
                    )
                    report["metrics"] = snapshot
                    passed("pipeline_metrics_available")
            restarted = create_app(config)
            async with restarted.router.lifespan_context(restarted):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted), base_url="http://smoke"
                ) as client:
                    persisted = await body(client, "GET", run_path)
                    assert persisted["answer"] == answer
                    assert (
                        persisted["answer_status"] == "answered"
                        and persisted["answer_segments"] == result["answer_segments"]
                    )
                    persisted_refusal = await body(client, "GET", missing_path)
                    assert (
                        persisted_refusal["answer_status"] == "insufficient_evidence"
                        and persisted_refusal["answer_reason"] == "missing_fact"
                    )
                    assert (await body(client, "GET", BASE + f"/conversations/{cid}"))["messages"][
                        -1
                    ]["content"] == answer
                    assert all(
                        f["index_status"] == "indexed"
                        for f in (await body(client, "GET", KB + "/files"))["items"]
                    )
                    snapshot = await body(client, "GET", BASE + "/metrics")
                    assert not any(t["stage"] == "rerank" for t in snapshot["timings"])
                    passed("restart_persistence_and_metrics_reset")
    report["temporary_schema_cleaned"] = True
    output = output or ROOT / "evals/results/rag-e2e-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("完整链路通过；隔离 schema 和文件已清理，结果已保存。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="保存验证报告的位置")
    asyncio.run(main(parser.parse_args().output))
