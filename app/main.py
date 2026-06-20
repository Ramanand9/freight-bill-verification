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

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import freight_bills, review
from app.database import SessionLocal, init_db
from app.models import AgentDecision, AuditEvent, FreightBill
from app.seed_loader import seed_reference_data


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

app.include_router(freight_bills.router)
app.include_router(review.router)


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "freight-bill-verification",
        "seed_counts": getattr(app.state, "seed_counts", {}),
        "docs": "/docs",
    }


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
