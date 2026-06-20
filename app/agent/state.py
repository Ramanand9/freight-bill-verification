"""
agent/state.py
==============
The single state object that flows through every LangGraph node.

WHY A SHARED TYPED STATE
------------------------
Each node receives the state, reads what it needs, and returns a partial update
that LangGraph merges in. That is what makes the pipeline a graph instead of one
giant function: nodes are small, independently testable, and the data contract
between them is explicit and serialisable (so it can be checkpointed and resumed
after a human review).

`issues` and `evidence` use additive reducers: a node returns only the NEW items
and LangGraph appends them, so the audit trail accumulates across nodes.

Everything here is plain JSON-friendly data (strings, numbers, lists, dicts) —
no ORM objects, no datetimes — precisely so the persistent checkpointer can
serialise it and resume days later.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class BillState(TypedDict, total=False):
    # ---- Raw bill (as submitted) ----
    bill_id: str
    carrier_id: str | None
    carrier_name: str
    bill_number: str
    bill_date: str          # ISO date string
    lane: str
    billed_weight_kg: float
    billing_unit: str | None
    rate_per_kg: float | None
    billed_base: float
    billed_fuel: float
    billed_gst: float
    billed_total: float
    shipment_reference: str | None   # explicit shipment id on the bill, if any

    # ---- Normalisation ----
    normalized_lane: str
    normalized_carrier_name: str

    # ---- Candidate matching ----
    matched_carrier_id: str | None
    carrier_match_method: str
    active_contract_ids: list[str]
    expired_contract_ids: list[str]
    chosen_contract_id: str | None
    matched_shipment_id: str | None
    shipment_inferred: bool
    matched_bol_id: str | None

    # ---- Validation outputs ----
    expected: dict | None       # charge assessment summary
    is_duplicate: bool

    # ---- Scoring & decision ----
    confidence: float
    confidence_breakdown: list[dict]
    decision: str
    requires_human_review: bool

    # ---- Accumulating audit ----
    issues: Annotated[list[dict], operator.add]
    evidence: Annotated[list[str], operator.add]

    # ---- Human-in-the-loop ----
    reviewer_decision: str | None
    reviewer_notes: str | None
    final_decision: str | None
