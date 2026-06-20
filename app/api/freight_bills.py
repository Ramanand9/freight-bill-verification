"""
api/freight_bills.py
====================
HTTP surface for ingesting and inspecting freight bills.

These routes are deliberately THIN: they validate input with Pydantic, call the
service, and shape the response. No business logic lives here — that is the
service/agent's job.

ENDPOINTS
---------
POST /freight-bills        -> ingest (seed id or full payload), run the agent.
GET  /freight-bills/{id}   -> the claim + decision history + evidence + audit.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditEvent, FreightBill
from app.schemas import (
    DecisionView,
    FreightBillDetail,
    IngestRequest,
    IngestResponse,
    MatchedEntities,
)
from app.seed_loader import get_seed_freight_bill
from app.services import freight_bill_service

router = APIRouter(prefix="/freight-bills", tags=["freight-bills"])


@router.post("", response_model=IngestResponse)
def ingest_freight_bill(req: IngestRequest, db: Session = Depends(get_db)) -> IngestResponse:
    """
    Ingest a freight bill and trigger the agent.

    Accepts EITHER a seed-bill `bill_id` OR a full `bill` payload (the schema
    enforces exactly one). Returns the resulting status, decision, confidence and
    evidence — or, if the agent paused, status WAITING_FOR_REVIEW.
    """
    if req.bill_id:
        data = get_seed_freight_bill(req.bill_id)
        if data is None:
            raise HTTPException(404, f"No seed freight bill with id '{req.bill_id}'.")
    else:
        data = req.bill.model_dump()

    result = freight_bill_service.process_bill(db, data)
    return IngestResponse(**result)


def _decision_view(d) -> DecisionView:
    return DecisionView(
        decision=d.decision.value,
        confidence=d.confidence,
        requires_human_review=d.requires_human_review,
        is_final=d.is_final,
        matched=MatchedEntities(
            carrier_id=d.matched_carrier_id,
            contract_id=d.matched_contract_id,
            shipment_id=d.matched_shipment_id,
            bol_id=d.matched_bol_id,
        ),
        issues=d.issues or [],
        evidence=d.evidence or [],
        created_at=d.created_at,
    )


@router.get("/{bill_id}", response_model=FreightBillDetail)
def get_freight_bill(bill_id: str, db: Session = Depends(get_db)) -> FreightBillDetail:
    """Full state of a bill: the claim, every decision, the evidence chain, audit."""
    fb = db.get(FreightBill, bill_id)
    if fb is None:
        raise HTTPException(404, f"Freight bill '{bill_id}' not found.")

    decisions = sorted(fb.decisions, key=lambda d: d.created_at or 0)
    views = [_decision_view(d) for d in decisions]
    latest = views[-1] if views else None

    audit = db.execute(
        select(AuditEvent)
        .where(AuditEvent.freight_bill_id == bill_id)
        .order_by(AuditEvent.created_at)
    ).scalars().all()
    audit_trail = [
        {"event": e.event_type, "detail": e.detail, "at": e.created_at.isoformat() if e.created_at else None}
        for e in audit
    ]

    return FreightBillDetail(
        id=fb.id,
        status=fb.status.value,
        carrier_id=fb.carrier_id,
        carrier_name=fb.carrier_name,
        bill_number=fb.bill_number,
        bill_date=fb.bill_date,
        shipment_reference=fb.shipment_reference,
        lane=fb.lane,
        billed_weight_kg=fb.billed_weight_kg,
        rate_per_kg=fb.rate_per_kg,
        billing_unit=fb.billing_unit,
        base_charge=fb.base_charge,
        fuel_surcharge=fb.fuel_surcharge,
        gst_amount=fb.gst_amount,
        total_amount=fb.total_amount,
        latest_decision=latest,
        decision_history=views,
        audit_trail=audit_trail,
    )
