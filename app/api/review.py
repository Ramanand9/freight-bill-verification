"""
api/review.py
=============
HTTP surface for the human-in-the-loop review step.

ENDPOINTS
---------
GET  /review-queue    -> bills currently paused at WAITING_FOR_REVIEW.
POST /review/{id}     -> submit a reviewer verdict; resumes the paused agent.

The reviewer's verdict (approve / dispute / modify) is fed back into the agent's
waiting `interrupt()` via the service, which reloads the checkpoint and runs the
graph to `finalize`. The route itself stays thin.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AgentDecision, BillStatus, FreightBill
from app.schemas import ReviewQueueItem, ReviewResult, ReviewSubmission
from app.services import freight_bill_service

router = APIRouter(tags=["review"])


@router.get("/review-queue", response_model=list[ReviewQueueItem])
def review_queue(db: Session = Depends(get_db)) -> list[ReviewQueueItem]:
    """List bills paused for human review, with the agent's proposed decision."""
    bills = db.execute(
        select(FreightBill).where(FreightBill.status == BillStatus.WAITING_FOR_REVIEW)
    ).scalars().all()

    items: list[ReviewQueueItem] = []
    for fb in bills:
        # the most recent (non-final) decision carries the proposal
        proposal = db.execute(
            select(AgentDecision)
            .where(AgentDecision.freight_bill_id == fb.id)
            .order_by(AgentDecision.created_at.desc())
        ).scalars().first()
        items.append(
            ReviewQueueItem(
                bill_id=fb.id,
                carrier_name=fb.carrier_name,
                bill_number=fb.bill_number,
                lane=fb.lane,
                proposed_decision=proposal.decision.value if proposal else "human_review",
                confidence=proposal.confidence if proposal else 0.0,
                issues=proposal.issues if proposal else [],
                created_at=proposal.created_at if proposal else None,
            )
        )
    return items


@router.post("/review/{bill_id}", response_model=ReviewResult)
def submit_review(
    bill_id: str, body: ReviewSubmission, db: Session = Depends(get_db)
) -> ReviewResult:
    """Submit a reviewer decision and resume the agent for this bill."""
    try:
        result = freight_bill_service.resume_after_review(
            db, bill_id, body.reviewer_decision, body.reviewer_notes
        )
    except KeyError:
        raise HTTPException(404, f"Freight bill '{bill_id}' not found.")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return ReviewResult(**result)
