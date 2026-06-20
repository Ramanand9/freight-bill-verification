"""
main.py
=======
The FastAPI application entrypoint.

RESPONSIBILITIES
----------------
* Create tables on startup (init_db) and load REFERENCE seed data (carriers,
  contracts, rate cards, shipments, BOLs). Freight bills are NOT loaded here —
  they arrive via POST /freight-bills, which is the whole point of the system.
* Mount the two routers (freight-bills, review).
* Expose a tiny health/metrics surface.

This file stays small on purpose: wiring only, no business logic.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import freight_bills, review
from app.config import get_settings
from app.database import Base, SessionLocal, engine, init_db
from app.models import AgentDecision, AuditEvent, FreightBill
from app.seed_loader import seed_reference_data

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db: Session = SessionLocal()
    try:
        counts = seed_reference_data(db)
        app.state.seed_counts = counts
    finally:
        db.close()
    yield


app = FastAPI(
    title="Freight Bill Verification Backend",
    version="1.0.0",
    description="Processes carrier freight bills with a LangGraph agent: matches "
    "contracts/shipments/BOLs over a NetworkX graph, validates charges with "
    "deterministic rules, scores confidence, and pauses for human review.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local dev tool; the console may be opened off-origin
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(freight_bills.router)
app.include_router(review.router)

# Serve the demo console (static single-page app) at /ui.
if _STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "freight-bill-verification",
        "seed_counts": getattr(app.state, "seed_counts", {}),
        "docs": "/docs",
    }


@app.get("/seed-bills", tags=["meta"])
def seed_bills() -> list[dict]:
    """List the seed freight bills with their scenario note and editable payload.

    Powers the console's bill picker: selecting one pre-fills the editor, so a
    reviewer can submit it as-is or tweak a field and watch the verdict change.
    """
    path = get_settings().seed_data_path
    with open(path) as fh:
        data = json.load(fh)
    out = []
    for fb in data.get("freight_bills", []):
        payload = {k: v for k, v in fb.items() if not k.startswith("_")}
        out.append(
            {
                "id": fb.get("id"),
                "scenario": fb.get("_scenario", ""),
                "payload": payload,
            }
        )
    return out


@app.post("/admin/reset", tags=["meta"])
def admin_reset() -> dict:
    """Dev-only: wipe all freight bills/decisions/audit and re-seed reference data.

    Lets you re-run the demo from a clean slate without restarting the server.
    Reference data (carriers, contracts, rate cards, shipments, BOLs) is restored;
    only the ingested bills and their decision/audit history are cleared.
    """
    from app.services.freight_bill_service import reset_engine

    # Release the checkpointer's SQLite handle before deleting its file.
    reset_engine()
    ckpt = get_settings().checkpoint_db
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(ckpt + suffix)
        except FileNotFoundError:
            pass

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        counts = seed_reference_data(db)
        app.state.seed_counts = counts
    finally:
        db.close()
    return {"reset": True, "seed_counts": counts}


@app.get("/metrics", tags=["meta"])
def metrics() -> dict:
    """Simple observability: counts of bills by status and decisions by type."""
    db: Session = SessionLocal()
    try:
        by_status = dict(
            db.execute(select(FreightBill.status, func.count()).group_by(FreightBill.status)).all()
        )
        by_decision = dict(
            db.execute(
                select(AgentDecision.decision, func.count())
                .where(AgentDecision.is_final.is_(True))
                .group_by(AgentDecision.decision)
            ).all()
        )
        audit_total = db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()
        return {
            "bills_by_status": {getattr(k, "value", k): v for k, v in by_status.items()},
            "final_decisions": {getattr(k, "value", k): v for k, v in by_decision.items()},
            "audit_events": audit_total,
        }
    finally:
        db.close()
