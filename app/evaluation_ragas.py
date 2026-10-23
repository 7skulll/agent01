"""RAGAS worker client.

The main service never imports ragas, pandas, datasets or pyarrow.  RAGAS runs in
an isolated Python environment under ``tools/ragas-worker`` and communicates via
a JSON stdin/stdout protocol.  This keeps the serving environment free of the
judge dependency graph and prevents judge failures from taking down the API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "tools" / "ragas-worker"
WORKER_SCRIPT = WORKER_DIR / "worker.py"
DEFAULT_PYTHON = WORKER_DIR / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)


class RagasUnavailable(RuntimeError):
    pass


def worker_command(settings: Settings) -> list[str]:
    custom = settings.ragas_worker_command.strip()
    if custom:
        return shlex.split(custom)
    return [str(DEFAULT_PYTHON), str(WORKER_SCRIPT)]


def _executable_exists(command: list[str]) -> bool:
    if not command:
        return False
    executable = command[0]
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        return Path(executable).is_file()
    return shutil.which(executable) is not None


def ragas_available(settings: Settings | None = None) -> bool:
    settings = settings or Settings()
    command = worker_command(settings)
    if not _executable_exists(command):
        return False
    if settings.ragas_worker_command.strip():
        return True
    return WORKER_SCRIPT.is_file()


class RagasJudge:
    """Client-side adapter; the actual judge runs in a separate interpreter."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.command = worker_command(self.settings)
        self._available: bool | None = None
        self.metadata: dict = {}

    @property
    def settings(self) -> Settings:
        # Model settings can change at runtime; read them per request.
        return self.runtime.settings

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        if not _executable_exists(self.command):
            return False
        if not self.settings.ragas_worker_command.strip() and not WORKER_SCRIPT.is_file():
            return False
        try:
            result = subprocess.run(
                [*self.command, "--probe"],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            metadata = json.loads(result.stdout)
        except (ValueError, TypeError):
            return False
        self.metadata = metadata if isinstance(metadata, dict) else {}
        return bool(self.metadata.get("ragas_version"))

    def _payload(self, records):
        return {
            "records": [
                {
                    "question": row.get("question"),
                    "answer": row.get("answer"),
                    "contexts": row.get("contexts") or [],
                    "reference": row.get("reference") or "",
                }
                for row in records
            ],
            "llm": {
                "model": self.settings.llm_model,
                "base_url": self.settings.llm_base_url,
                "api_key": self.settings.llm_api_key.get_secret_value(),
                "max_tokens": self.settings.llm_max_tokens,
                "temperature": 0,
            },
            "embeddings": {
                "model": self.settings.embedding_model,
                "base_url": self.settings.embedding_base_url,
                "api_key": self.settings.embedding_api_key.get_secret_value(),
            },
        }

    async def score(self, records):
        if not self.available:
            raise RagasUnavailable("RAGAS worker 未安装或不可用。")
        payload = json.dumps(self._payload(records), ensure_ascii=False).encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self.settings.ragas_worker_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RagasUnavailable("RAGAS worker 执行超时。") from None
        except asyncio.CancelledError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise
        if process.returncode != 0:
            # Do not log judge prompts or answers; they may contain sensitive text.
            logger.warning("ragas_worker_failed code=%s", process.returncode)
            raise RagasUnavailable("RAGAS worker 执行失败。")
        try:
            result = json.loads(stdout)
        except (ValueError, TypeError):
            raise RagasUnavailable("RAGAS worker 返回了无效 JSON。") from None
        scores = result.get("scores") if isinstance(result, dict) else None
        if not isinstance(scores, list) or len(scores) != len(records):
            raise RagasUnavailable("RAGAS worker 返回数量与案例不一致。")
        self.metadata = {
            key: value for key, value in result.items() if key != "scores"
        }
        return scores
