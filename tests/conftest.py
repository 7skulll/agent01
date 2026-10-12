import pytest


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    """Never write test sessions into the user's local database."""
    monkeypatch.chdir(tmp_path)
