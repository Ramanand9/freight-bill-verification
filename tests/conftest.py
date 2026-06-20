"""
tests/conftest.py
=================
Shared pytest fixtures. Critically, this sets the storage paths to a temp
directory BEFORE any `app.*` module is imported, so the SQLAlchemy engine and the
LangGraph checkpointer (both built at import time from Settings) point at
throwaway files instead of the real ones.
"""
from __future__ import annotations

import os
import tempfile

# --- Must happen before importing app modules -----------------------------
_TMPDIR = tempfile.mkdtemp(prefix="freight_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["CHECKPOINT_DB"] = f"{_TMPDIR}/test_ckpt.db"
# seed_data.json lives at the project root; tests run from there.
os.environ.setdefault("SEED_DATA_PATH", "./seed_data.json")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.seed_loader import seed_reference_data  # noqa: E402
from app.services import freight_bill_service as svc  # noqa: E402


def _fresh_storage():
    """Reset relational tables and the checkpoint store to empty."""
    svc.reset_engine()
    ckpt = os.environ["CHECKPOINT_DB"]
    if os.path.exists(ckpt):
        os.remove(ckpt)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture
def db():
    """A fresh, reference-seeded DB session with an empty checkpoint store."""
    init_db()
    _fresh_storage()
    session = SessionLocal()
    seed_reference_data(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    """A FastAPI TestClient against fresh storage (startup re-seeds reference data)."""
    from fastapi.testclient import TestClient

    init_db()
    _fresh_storage()
    from app.main import app

    with TestClient(app) as c:
        yield c
