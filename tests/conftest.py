from pathlib import Path

import pytest

from jarvis.security.permissions import SecurityPolicy
from jarvis.storage.database import Database


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    return root


@pytest.fixture
def policy(sandbox: Path, tmp_path: Path) -> SecurityPolicy:
    return SecurityPolicy(
        allowed_paths=[str(sandbox)],
        deny_patterns=[str(sandbox / "secret") + "/*", str(sandbox / "*.key")],
    )


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()
