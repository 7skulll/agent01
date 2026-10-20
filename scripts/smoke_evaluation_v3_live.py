"""Real Chrome -> Vite -> FastAPI -> configured models; isolated schema, synthetic text."""

import argparse
import asyncio
import json
import os
import shutil
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from scripts._database import temporary_database

ROOT = Path(__file__).resolve().parents[1]


async def wait_url(url, process=None):
    async with httpx.AsyncClient() as client:
        for _ in range(150):
            if process is not None and process.returncode is not None:
                raise RuntimeError("前端服务提前退出。")
            try:
                response = await client.get(url, timeout=1)
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError("本地验收服务未启动。")


async def main(mode="keyword", rerank="off", report_prefix="evaluation-v3"):
    started_at = datetime.now(timezone.utc).isoformat()
    settings = Settings()
    config = settings.model_copy(
        update={
            "database_url": SecretStr(
                make_conninfo(settings.database_url.get_secret_value(), dbname="langchain_rag_test")
            )
        }
    )
    output = ROOT / "evals/results"
    output.mkdir(exist_ok=True)
    async with temporary_database(config) as database_url:
        with tempfile.TemporaryDirectory(prefix="v3-live-") as folder:
            runtime = config.model_copy(
                update={"database_url": database_url, "file_storage_path": Path(folder) / "files"}
            )
            app = create_app(runtime)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            api_port = sock.getsockname()[1]
            api_url = f"http://127.0.0.1:{api_port}"
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
            )
            api_task = asyncio.create_task(server.serve(sockets=[sock]))
            vite = None
            try:
                await wait_url(api_url + "/api/v1/health")
                before = app.state.runtime.settings.rag_context_tokens
                async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
                    uploaded = await client.post(
                        "/api/v1/knowledge-bases/default/files",
                        files={
                            "file": (
                                "v3验收制度.md",
                                (
                                    "# 年假制度\n公司员工每年享有十天带薪年假。"
                                    "申请需要直属主管审批。"
                                ).encode(),
                            )
                        },
                        headers={"Idempotency-Key": "v3-live"},
                    )
                    uploaded.raise_for_status()
                    response = await client.post("/api/v1/knowledge-bases/default/index")
                    response.raise_for_status()
                    await app.state.index_service.wait()
                with socket.socket() as picker:
                    picker.bind(("127.0.0.1", 0))
                    ui_port = picker.getsockname()[1]
                ui_url = f"http://127.0.0.1:{ui_port}"
                env = {
                    **os.environ,
                    "API_PROXY_TARGET": api_url,
                    "V3_URL": ui_url,
                    "V3_RESULTS": str(output),
                    "V3_MODE": mode,
                    "V3_RERANK": rerank,
                    "V3_PREFIX": report_prefix,
                }
                vite = await asyncio.create_subprocess_exec(
                    shutil.which("npm"),
                    "run",
                    "dev",
                    "--",
                    "--port",
                    str(ui_port),
                    cwd=ROOT / "frontend",
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await wait_url(ui_url, vite)
                browser = await asyncio.create_subprocess_exec(
                    shutil.which("node"),
                    "scripts/evaluation-live.mjs",
                    cwd=ROOT / "frontend",
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(browser.communicate(), 300)
                except BaseException:
                    browser.terminate()
                    await browser.wait()
                    raise
                if browser.returncode:
                    raise RuntimeError(stderr.decode()[-5000:])
                report = json.loads(stdout)
                report["started_at"] = started_at
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                assert app.state.runtime.settings.rag_context_tokens == before
                report["checks"].append("workspace_unchanged")
            finally:
                if vite is not None:
                    vite.terminate()
                    await vite.wait()
                server.should_exit = True
                await api_task
                sock.close()
            restarted = create_app(runtime)
            async with restarted.router.lifespan_context(restarted):
                row = await restarted.state.evaluations.row(report["baseline"])
                assert (
                    row["results"][0]["review"]["notes"] == "自动验收流程验证，回答仍待人工审核。"
                )
                report["checks"].append("restart_persistence")
    report["temporary_schema_cleaned"] = True
    (output / f"{report_prefix}-live.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {"checks": report["checks"], "temporary_schema_cleaned": True}, ensure_ascii=False
        )
    )


async def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", action="store_true", help="三种检索模式，均强制真实重排")
    args = parser.parse_args()
    if args.matrix:
        for mode in ("keyword", "vector", "hybrid"):
            await main(mode, "on", f"evaluation-v3-{mode}-rerank")
    else:
        await main()


if __name__ == "__main__":
    asyncio.run(run())
