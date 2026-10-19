import asyncio
import threading
from pathlib import Path

import pytest

from app.rag.storage import LocalStorage


def test_slow_read_does_not_block_event_loop(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_read(path):
        started.set()
        assert release.wait(3)
        return b"content"

    monkeypatch.setattr(Path, "read_bytes", slow_read)

    async def scenario():
        task = asyncio.create_task(LocalStorage(tmp_path).get("file.txt"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            # A loop callback must run while the disk thread is still blocked.
            tick = asyncio.Event()
            asyncio.get_running_loop().call_soon(tick.set)
            await asyncio.wait_for(tick.wait(), 1)
            assert not task.done()
        finally:
            release.set()
            assert await task == b"content"

    asyncio.run(scenario())


def test_cancel_during_publication_waits_for_disk_and_removes_both_links(tmp_path, monkeypatch):
    published, release = threading.Event(), threading.Event()
    original = Path.hardlink_to

    def delayed_publish(path, target):
        original(path, target)
        published.set()
        assert release.wait(3)

    monkeypatch.setattr(Path, "hardlink_to", delayed_publish)

    async def chunks():
        yield b"content"

    async def scenario():
        task = asyncio.create_task(LocalStorage(tmp_path).put("default/file.txt", chunks()))
        try:
            assert await asyncio.to_thread(published.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert list((tmp_path / "default").iterdir()) == []

    asyncio.run(scenario())
