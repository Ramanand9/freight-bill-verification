"""
services/freight_bill_service.py
================================
The orchestration layer. This is the ONLY place that knows how to:

* turn an incoming bill (seed id or full payload) into a persisted FreightBill,
* build the domain graph and compile the LangGraph workflow with a durable
  checkpointer,
* run the agent, detect a `interrupt()` pause, and persist what happened,
* resume a paused bill after a human review and persist the finalisation.

WHY BUSINESS LOGIC LIVES HERE AND NOT IN THE ROUTES
---------------------------------------------------
FastAPI routes should only translate HTTP <-> Python and delegate. Keeping the
workflow wiring, DB writes, and checkpointer handling in a service means the same
logic is reusable (tests call it directly, a CLI could too) and the routes stay
thin and readable.

HOW HUMAN-IN-THE-LOOP IS REAL HERE
----------------------------------
We compile the graph with a `SqliteSaver` checkpointer keyed by `thread_id =
bill_id`. The first `invoke` runs every node up to `decide`; if the decision
needs a human, the `human_review` node calls `interrupt()`, which checkpoints the
entire state to SQLite and returns control with an `__interrupt__` marker. Days
later, `resume_after_review` calls `invoke(Command(resume=...))` on the SAME
thread_id; LangGraph reloads the checkpoint and the `interrupt()` call returns the
reviewer's payload, so the graph continues into `finalize`. The pause survives a
process restart because it lives in SQLite, not memory.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.agent.state import BillState
from app.agent.workflow import build_graph_definition
from app.config import Settings, get_settings
from app.graph.builder import build_graph
from app.models import AgentDecision, BillStatus, Decision, FreightBill, HumanReview
from app.services import audit_service

# --------------------------------------------------------------------------- #
# Checkpointer + compiled-graph singletons
# --------------------------------------------------------------------------- #
# The checkpointer must outlive a single request: a bill paused in one HTTP call
# is resumed in another. We hold one long-lived SQLite connection for the whole
# process. A lock serialises graph runs so concurrent requests can't trip the
# single sqlite connection (acceptable for local/dev; see README tradeoffs).
_lock = threading.Lock()
_checkpointer: SqliteSaver | None = None
_compiled = None


def _get_compiled(settings: Settings):
    global _checkpointer, _compiled
    if _compiled is None:
        conn = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
        _checkpointer = SqliteSaver(conn)
        _compiled = build_graph_definition().compile(checkpointer=_checkpointer)
    return _compiled


def reset_engine() -> None:
    """Drop the cached checkpointer/compiled graph (used by tests for isolation)."""
    global _checkpointer, _compiled
    if _checkpointer is not None:
        try:
            _checkpointer.conn.close()
        except Exception:
            pass
    _checkpointer = None
    _compiled = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bill_to_state(fb: FreightBill) -> BillState:
    """Map the persisted claim into the JSON-friendly agent state."""
    return BillState(
        bill_id=fb.id,
        carrier_id=fb.carrier_id,
        carrier_name=fb.carrier_name,
        bill_number=fb.bill_number,
        bill_date=fb.bill_date.isoformat(),
        lane=fb.lane,
        billed_weight_kg=fb.billed_weight_kg,
        billing_unit=fb.billing_unit,
        rate_per_kg=fb.rate_per_kg,
        billed_base=fb.base_charge,
        billed_fuel=fb.fuel_surcharge,
        billed_gst=fb.gst_amount,
        billed_total=fb.total_amount,
        shipment_reference=fb.shipment_reference,
        issues=[],
        evidence=[],
    )


def _upsert_freight_bill(db: Session, data: dict) -> FreightBill:
    """Insert (or replace) the freight bill row, verbatim, in PROCESSING state."""
    bill_date = data["bill_date"]
    if isinstance(bill_date, str):
        bill_date = dt.date.fromisoformat(bill_date)

    fb = db.get(FreightBill, data["id"])
    if fb is None:
        fb = FreightBill(id=data["id"])
        db.add(fb)

    fb.carrier_id = data.get("carrier_id")
    fb.carrier_name = data["carrier_name"]
    fb.bill_number = data["bill_number"]
    fb.bill_date = bill_date
    fb.shipment_reference = data.get("shipment_reference")
    fb.lane = data["lane"]
    fb.billed_weight_kg = data["billed_weight_kg"]
    fb.rate_per_kg = data.get("rate_per_kg")
    fb.billing_unit = data.get("billing_unit")
    fb.base_charge = data["base_charge"]
    fb.fuel_surcharge = data.get("fuel_surcharge", 0.0)
    fb.gst_amount = data.get("gst_amount", 0.0)
    fb.total_amount = data["total_amount"]
    fb.status = BillStatus.PROCESSING
    db.commit()
    db.refresh(fb)
    return fb


def _persist_decision(
    db: Session,
    fb: FreightBill,
    state: dict,
    *,
    decision: str,
    is_final: bool,
    requires_human_review: bool,
) -> AgentDecision:
    rec = AgentDecision(
        freight_bill_id=fb.id,
        decision=Decision(decision),
        confidence=float(state.get("confidence", 0.0)),
        requires_human_review=requires_human_review,
        is_final=is_final,
        matched_carrier_id=state.get("matched_carrier_id"),
        matched_contract_id=state.get("chosen_contract_id"),
        matched_shipment_id=state.get("matched_shipment_id"),
        matched_bol_id=state.get("matched_bol_id"),
        evidence=list(state.get("evidence", [])),
        issues=list(state.get("issues", [])),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def process_bill(db: Session, data: dict) -> dict:
    """
    Ingest + run the agent for one bill. Returns a summary dict shaped for the
    POST /freight-bills response. If the agent pauses, status is
    WAITING_FOR_REVIEW and the proposed decision is recorded (not final).
    """
    settings = get_settings()
    fb = _upsert_freight_bill(db, data)
    audit_service.record_event(
        db, bill_id=fb.id, event_type="received",
        detail={"bill_number": fb.bill_number, "carrier": fb.carrier_name},
    )

    domain_graph = build_graph(db)
    app = _get_compiled(settings)
    config = {
        "configurable": {
            "thread_id": fb.id,
            "db": db,
            "domain_graph": domain_graph,
            "settings": settings,
        }
    }

    with _lock:
        out = app.invoke(_bill_to_state(fb), config)

    paused = "__interrupt__" in out

    if paused:
        proposed = out.get("decision", "human_review")
        _persist_decision(
            db, fb, out,
            decision=proposed, is_final=False, requires_human_review=True,
        )
        fb.status = BillStatus.WAITING_FOR_REVIEW
        db.commit()
        audit_service.record_event(
            db, bill_id=fb.id, event_type="paused_for_review",
            detail={"proposed_decision": proposed, "confidence": out.get("confidence")},
        )
        return {
            "bill_id": fb.id,
            "status": fb.status.value,
            "decision": proposed,
            "confidence": out.get("confidence"),
            "requires_human_review": True,
            "evidence": list(out.get("evidence", [])),
            "issues": list(out.get("issues", [])),
        }

    # Terminal: the agent finished without needing a human.
    final = out.get("final_decision") or out["decision"]
    _persist_decision(
        db, fb, out,
        decision=final, is_final=True, requires_human_review=False,
    )
    fb.status = BillStatus.COMPLETED
    db.commit()
    audit_service.record_event(
        db, bill_id=fb.id, event_type="decided",
        detail={"decision": final, "confidence": out.get("confidence")},
    )
    return {
        "bill_id": fb.id,
        "status": fb.status.value,
        "decision": final,
        "confidence": out.get("confidence"),
        "requires_human_review": False,
        "evidence": list(out.get("evidence", [])),
        "issues": list(out.get("issues", [])),
    }


def resume_after_review(
    db: Session, bill_id: str, reviewer_decision: str, reviewer_notes: str | None
) -> dict:
    """
    Resume a paused bill with the reviewer's verdict. Reloads the checkpoint by
    thread_id, feeds the verdict back into the waiting `interrupt()`, runs
    `finalize`, and persists the human review + final decision.
    """
    settings = get_settings()
    fb = db.get(FreightBill, bill_id)
    if fb is None:
        raise KeyError(f"Freight bill {bill_id} not found.")
    if fb.status != BillStatus.WAITING_FOR_REVIEW:
        raise ValueError(f"Bill {bill_id} is not waiting for review (status={fb.status.value}).")

    app = _get_compiled(settings)
    domain_graph = build_graph(db)
    config = {
        "configurable": {
            "thread_id": fb.id,
            "db": db,
            "domain_graph": domain_graph,
            "settings": settings,
        }
    }
    resume_payload = {"reviewer_decision": reviewer_decision, "reviewer_notes": reviewer_notes}

    with _lock:
        out = app.invoke(Command(resume=resume_payload), config)

    final = out.get("final_decision") or out.get("decision")

    db.add(
        HumanReview(
            freight_bill_id=fb.id,
            reviewer_decision=reviewer_decision,
            reviewer_notes=reviewer_notes,
            final_decision=Decision(final),
        )
    )
    db.commit()

    _persist_decision(
        db, fb, out,
        decision=final, is_final=True, requires_human_review=False,
    )
    fb.status = BillStatus.COMPLETED
    db.commit()
    audit_service.record_event(
        db, bill_id=fb.id, event_type="review_resolved",
        detail={
            "reviewer_decision": reviewer_decision,
            "final_decision": final,
            "notes": reviewer_notes,
        },
    )
    return {
        "bill_id": fb.id,
        "status": fb.status.value,
        "final_decision": final,
        "reviewer_decision": reviewer_decision,
        "reviewer_notes": reviewer_notes,
        "evidence": list(out.get("evidence", [])),
    }
