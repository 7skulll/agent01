import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".pdf", ".doc", ".docx", ".md", ".txt", ".xls", ".xlsx"})


class FileTooLargeError(ValueError):
    pass


class EmptyFileError(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    path: str
    size: int
    content_hash: str


class Storage(Protocol):
    backend: str

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> StoredFile: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


async def disk_io(operation, *args):
    """等待线程完成再传播取消，避免后台写入与关闭/补偿删除竞争。"""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


class LocalStorage:
    backend = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Invalid storage path")
        return path

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> StoredFile:
        path = await disk_io(self._path, key)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
        size, digest = 0, hashlib.sha256()
        target, published, completed = None, False, False

        def open_target():
            nonlocal target
            path.parent.mkdir(parents=True, exist_ok=True)
            target = temporary.open("xb")

        def publish():
            nonlocal target, published
            target.close()
            target = None
            # UUID keys are immutable: never overwrite an existing object.
            path.hardlink_to(temporary)
            published = True

        def cleanup():
            try:
                if target is not None:
                    target.close()
            finally:
                try:
                    if published and not completed:
                        path.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

        try:
            await disk_io(open_target)
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise FileTooLargeError
                digest.update(chunk)
                await disk_io(target.write, chunk)
            if size == 0:
                raise EmptyFileError
            await disk_io(publish)
            # 清理临时链接成功后才确认发布；取消期间仍需补偿最终对象。
            await disk_io(temporary.unlink)
            completed = True
            return StoredFile(key, size, digest.hexdigest())
        finally:
            await disk_io(cleanup)

    async def get(self, key: str) -> bytes:
        return await disk_io(lambda: self._path(key).read_bytes())

    async def delete(self, key: str) -> None:
        await disk_io(lambda: self._path(key).unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        return await disk_io(lambda: self._path(key).is_file())

    def orphan_candidates(self, min_age_seconds=86400):
        """仅扫描本应用的 UUID 对象与 .part；跳过符号链接、未知文件和最近写入。"""
        if not self.root.exists():
            return []
        uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        suffix = r"(?:pdf|doc|docx|md|txt|xls|xlsx)"
        managed = re.compile(rf"(?:{uuid}\.{suffix}|\.{uuid}\.{suffix}\.[0-9a-f]{{32}}\.part)")
        cutoff = time.time() - min_age_seconds
        result = []
        try:
            folders = list(self.root.iterdir())
        except OSError:
            logger.warning("orphan_root_scan_failed")
            return []
        for folder in folders:
            if folder.is_symlink() or not folder.is_dir():
                continue
            if folder.name != "default" and not re.fullmatch(uuid, folder.name):
                continue
            try:
                paths = list(folder.iterdir())
            except OSError:
                logger.warning("orphan_directory_scan_failed")
                continue
            for path in paths:
                if path.is_symlink() or not path.is_file() or not managed.fullmatch(path.name):
                    continue
                try:
                    if path.stat().st_mtime <= cutoff:
                        result.append(path.relative_to(self.root).as_posix())
                except FileNotFoundError:
                    continue
        return result
