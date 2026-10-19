"""纯内存单测保留工作目录隔离，不创建 PostgreSQL schema。"""

import pytest


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
