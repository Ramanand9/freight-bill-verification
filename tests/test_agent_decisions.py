"""
tests/test_agent_decisions.py
=============================
End-to-end tests for the agent's core decision logic. Each test ingests a real
seed freight bill through the full pipeline (graph match -> rules -> confidence
-> decide) and asserts the resulting decision, plus the human-in-the-loop
interrupt/resume cycle.

These exercise the assignment's required scenarios: clean approve, duplicate
dispute, unknown-carrier review, rate-drift review, and fuel-revision approve.
"""
from __future__ import annotations

import pytest

from app.models import BillStatus, Decision, FreightBill, HumanReview
from app.seed_loader import get_seed_freight_bill
from app.services import freight_bill_service as svc


def _run(db, bill_id: str) -> dict:
    return svc.process_bill(db, get_seed_freight_bill(bill_id))


# --------------------------------------------------------------------------- #
# Required scenario coverage
# --------------------------------------------------------------------------- #
def test_clean_bill_auto_approves(db):
    res = _run(db, "FB-2025-101")
    assert res["decision"] == "auto_approve"
    assert res["status"] == BillStatus.COMPLETED.value
    assert res["confidence"] == 1.0


def test_duplicate_bill_disputes(db):
    _run(db, "FB-2025-101")               # original must exist first
    res = _run(db, "FB-2025-109")          # same carrier + bill_number
    assert res["decision"] == "dispute"
    assert any(i["code"] == "duplicate_bill" for i in res["issues"])


def test_unknown_carrier_goes_to_human_review(db):
    res = _run(db, "FB-2025-110")
    assert res["decision"] == "human_review"
    assert res["requires_human_review"] is True
    assert res["status"] == BillStatus.WAITING_FOR_REVIEW.value


def test_rate_drift_flags_for_review(db):
    res = _run(db, "FB-2025-105")
    assert res["decision"] == "flag_for_review"
    assert res["requires_human_review"] is True


def test_fuel_revision_auto_approves(db):
    # bill date after revised_on -> must use revised 18% surcharge
    res = _run(db, "FB-2025-108")
    assert res["decision"] == "auto_approve"
    assert res["confidence"] == 1.0


def test_overbilling_disputes(db):
    _run(db, "FB-2025-103")                # prior 800kg for the same shipment
    res = _run(db, "FB-2025-104")          # claims 1500kg -> cumulative overbilling
    assert res["decision"] == "dispute"


def test_ftl_kg_reconciliation_auto_approves(db):
    res = _run(db, "FB-2025-107")
    assert res["decision"] == "auto_approve"
    assert any(i["code"] == "unit_reconciled" for i in res["issues"])


# --------------------------------------------------------------------------- #
# Human-in-the-loop interrupt/resume
# --------------------------------------------------------------------------- #
def test_pause_and_resume_with_dispute(db):
    res = _run(db, "FB-2025-106")
    assert res["status"] == BillStatus.WAITING_FOR_REVIEW.value

    out = svc.resume_after_review(db, "FB-2025-106", "dispute", "Stale TCI rate.")
    assert out["final_decision"] == "dispute"
    assert out["status"] == BillStatus.COMPLETED.value

    fb = db.get(FreightBill, "FB-2025-106")
    assert fb.status == BillStatus.COMPLETED
    reviews = db.query(HumanReview).filter_by(freight_bill_id="FB-2025-106").all()
    assert len(reviews) == 1
    assert reviews[0].final_decision == Decision.DISPUTE

    # the final agent_decision is marked final
    final = [d for d in fb.decisions if d.is_final]
    assert final and final[-1].decision == Decision.DISPUTE


def test_resume_rejected_when_not_waiting(db):
    _run(db, "FB-2025-101")                # completes immediately, not waiting
    with pytest.raises(ValueError):
        svc.resume_after_review(db, "FB-2025-101", "approve", None)


# --------------------------------------------------------------------------- #
# Full API surface
# --------------------------------------------------------------------------- #
def test_review_queue_and_endpoint_flow(client):
    client.post("/freight-bills", json={"bill_id": "FB-2025-110"})
    queue = client.get("/review-queue").json()
    assert any(item["bill_id"] == "FB-2025-110" for item in queue)

    resp = client.post("/review/FB-2025-110",
                       json={"reviewer_decision": "approve", "reviewer_notes": "Spot carrier OK."})
    assert resp.status_code == 200
    assert resp.json()["final_decision"] == "auto_approve"
    assert client.get("/review-queue").json() == []
