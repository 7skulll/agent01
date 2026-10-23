import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.evaluation_ragas import RagasJudge, RagasUnavailable


@pytest.mark.parametrize("timeout", [False, True])
def test_worker_is_killed_and_reaped_on_cancel_or_timeout(monkeypatch, timeout):
    async def scenario():
        started = asyncio.Event()

        class Process:
            returncode = None
            killed = False
            reaped = False

            async def communicate(self, payload):
                started.set()
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True

            async def wait(self):
                self.reaped = True
                self.returncode = -9

        process = Process()

        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        settings = Settings(_env_file=None, ragas_worker_timeout_seconds=0.01 if timeout else 30)
        judge = RagasJudge(SimpleNamespace(settings=settings))
        judge._available = True
        task = asyncio.create_task(judge.score([]))
        await started.wait()
        if not timeout:
            task.cancel()
        with pytest.raises(RagasUnavailable if timeout else asyncio.CancelledError):
            await task
        assert process.killed
        assert process.reaped

    asyncio.run(scenario())
