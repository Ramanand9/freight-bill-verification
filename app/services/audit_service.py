"""
services/audit_service.py
=========================
A tiny helper for the append-only audit trail.

WHY IT EXISTS
-------------
Every meaningful thing that happens to a freight bill — received, processed,
paused for review, resumed, finalised — is written as an immutable `AuditEvent`.
Nothing here is ever updated or deleted. That append-only timeline is what makes
the system defensible: you can reconstruct exactly what the agent saw and did,
and what a human later overrode, in order.

Keeping this in its own module (rather than scattering `db.add(AuditEvent(...))`
across the codebase) means there is one place that decides how events are shaped.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record_event(
    db: Session,
    *,
    bill_id: str | None,
    event_type: str,
    detail: dict | None = None,
    commit: bool = True,
) -> AuditEvent:
    """Append one audit event. Caller controls commit batching via `commit`."""
    event = AuditEvent(
        freight_bill_id=bill_id,
        event_type=event_type,
        detail=detail or {},
    )
    db.add(event)
    if commit:
        db.commit()
    return event
