import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations():
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def unique_doc_type():
    return f"test_doc_{uuid.uuid4().hex}"
