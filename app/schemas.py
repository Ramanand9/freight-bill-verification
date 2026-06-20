"""
schemas.py
==========
Pydantic models that define the API's request/response contract.

WHY A SEPARATE SCHEMA LAYER (not reusing ORM models)
----------------------------------------------------
The ORM models describe how data is *stored*; schemas describe what the API
*accepts and returns*. Keeping them separate means:

* The wire format can differ from the table layout (e.g. we expose a flattened
  "evidence chain" that is actually assembled from several tables).
* We never accidentally leak internal columns or accept fields we don't intend
  to persist.
* FastAPI uses these for validation and the auto-generated OpenAPI docs.

WHAT TO PAY ATTENTION TO
------------------------
`IngestRequest` is deliberately permissive: a caller may submit just a known
seed-bill `id`, OR a full freight-bill payload. The service decides which path
to take. That mirrors the assignment: "accept a freight bill id from seed data
OR a full freight bill payload".
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- #
# Ingest (POST /freight-bills)
# --------------------------------------------------------------------------- #
class FreightBillPayload(BaseModel):
    """A full freight bill as a carrier would submit it (mirrors seed shape)."""

    id: str
    carrier_id: str | None = None
    carrier_name: str
    bill_number: str
    bill_date: dt.date
    shipment_reference: str | None = None
    lane: str
    billed_weight_kg: float
    rate_per_kg: float | None = None
    billing_unit: str | None = None
    base_charge: float
    fuel_surcharge: float = 0.0
    gst_amount: float = 0.0
    total_amount: float


class IngestRequest(BaseModel):
    """
    Accept EITHER a seed-bill id (`bill_id`) OR a full `bill` payload.
    Exactly one must be provided.
    """

    bill_id: str | None = Field(
        default=None, description="Id of a freight bill present in seed_data.json."
    )
    bill: FreightBillPayload | None = Field(
        default=None, description="A full freight-bill payload."
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> "IngestRequest":
        if bool(self.bill_id) == bool(self.bill):
            raise ValueError("Provide exactly one of 'bill_id' or 'bill'.")
        return self


# --------------------------------------------------------------------------- #
# Shared decision / evidence shapes
# --------------------------------------------------------------------------- #
class MatchedEntities(BaseModel):
    carrier_id: str | None = None
    contract_id: str | None = None
    shipment_id: str | None = None
    bol_id: str | None = None


class DecisionView(BaseModel):
    decision: str
    confidence: float
    requires_human_review: bool
    is_final: bool
    matched: MatchedEntities
    issues: list[dict] = []
    evidence: list[str] = []
    created_at: dt.datetime | None = None


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class IngestResponse(BaseModel):
    """Returned immediately after POST /freight-bills."""

    bill_id: str
    status: str
    decision: str | None
    confidence: float | None
    requires_human_review: bool
    evidence: list[str] = []
    issues: list[dict] = []


class FreightBillDetail(BaseModel):
    """Full GET /freight-bills/{id} view: the claim + every decision + audit."""

    id: str
    status: str
    carrier_id: str | None
    carrier_name: str
    bill_number: str
    bill_date: dt.date
    shipment_reference: str | None
    lane: str
    billed_weight_kg: float
    rate_per_kg: float | None
    billing_unit: str | None
    base_charge: float
    fuel_surcharge: float
    gst_amount: float
    total_amount: float

    latest_decision: DecisionView | None = None
    decision_history: list[DecisionView] = []
    audit_trail: list[dict] = []


class ReviewQueueItem(BaseModel):
    bill_id: str
    carrier_name: str
    bill_number: str
    lane: str
    proposed_decision: str
    confidence: float
    issues: list[dict] = []
    created_at: dt.datetime | None = None


class ReviewSubmission(BaseModel):
    """POST /review/{id} body."""

    reviewer_decision: str = Field(description="One of: approve | dispute | modify.")
    reviewer_notes: str | None = None

    @model_validator(mode="after")
    def _valid_decision(self) -> "ReviewSubmission":
        if self.reviewer_decision not in {"approve", "dispute", "modify"}:
            raise ValueError("reviewer_decision must be approve, dispute, or modify.")
        return self


class ReviewResult(BaseModel):
    bill_id: str
    status: str
    final_decision: str
    reviewer_decision: str
    reviewer_notes: str | None = None
    evidence: list[str] = []
