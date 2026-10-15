import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

MAX_FILE_BYTES = 50 * 1024 * 1024
# 数据库只做宽松兜底，真实限额由 MAX_FILE_BYTES 在接收期控制，避免每次调整都重建表。
MAX_STORED_FILE_BYTES = 200 * 1024 * 1024
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


class LocalStorage:
    backend = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()

    @staticmethod
    def key(kb_id: str, file_id: str, extension: str) -> str:
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in (kb_id, file_id)):
            raise ValueError("Invalid storage identifier")
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("Invalid storage extension")
        return f"{kb_id}/{file_id}{extension}"

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Invalid storage path")
        return path

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> StoredFile:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
        size, digest = 0, hashlib.sha256()
        published = False
        try:
            with temporary.open("xb") as target:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise FileTooLargeError
                    digest.update(chunk)
                    target.write(chunk)
                if size == 0:
                    raise EmptyFileError
            # UUID keys are immutable: never overwrite an existing object.
            path.hardlink_to(temporary)
            published = True
            return StoredFile(key, size, digest.hexdigest())
        except BaseException:
            if published:
                path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()
