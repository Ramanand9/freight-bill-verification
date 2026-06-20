"""
agent/confidence.py
===================
Turns the structured Issues from the rule engine into a single confidence score.

DESIGN
------
Start at 1.0 (a perfectly clean bill) and subtract a fixed penalty for each
*category* of problem, then clamp to [0, 1]. Penalties follow the assignment's
table. Crucially we penalise per CATEGORY, not per issue, so two fuel/base
drift issues don't double-charge the "charge mismatch" penalty.

WHY RULE-BASED (not learned)
----------------------------
The score has to be explainable to an ops reviewer: "0.45 because shipment was
inferred (-0.30) and three contracts overlapped (-0.25)". Every subtraction is
traceable to an Issue. The score feeds triage; it is NOT the sole decider
(see workflow.decide).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agent.rules import Issue

# Penalty table (assignment spec). Keyed by a logical category.
PENALTIES: dict[str, float] = {
    "inferred_shipment": 0.30,
    "multiple_overlapping_contracts": 0.25,
    "charge_mismatch": 0.30,
    "weight_mismatch": 0.35,
    "duplicate_bill": 0.50,
    "unknown_carrier": 0.50,
    "no_valid_contract": 0.40,
    "unit_reconciled": 0.20,
}

# Map raw issue codes -> the penalty category they trigger.
_CODE_TO_CATEGORY: dict[str, str] = {
    "inferred_shipment": "inferred_shipment",
    "multiple_overlapping_contracts": "multiple_overlapping_contracts",
    "base_charge_drift": "charge_mismatch",
    "base_charge_mismatch": "charge_mismatch",
    "fuel_surcharge_drift": "charge_mismatch",
    "fuel_surcharge_mismatch": "charge_mismatch",
    "weight_overbilling_vs_bol": "weight_mismatch",
    "weight_overbilling_cumulative": "weight_mismatch",
    "duplicate_bill": "duplicate_bill",
    "unknown_carrier": "unknown_carrier",
    "no_valid_contract": "no_valid_contract",
    "contract_expired": "no_valid_contract",
    "unit_reconciled": "unit_reconciled",
}


@dataclass
class ConfidenceResult:
    score: float
    breakdown: list[dict]  # [{"category", "penalty"}]


def compute_confidence(issues: list[Issue]) -> ConfidenceResult:
    """Compute confidence in [0,1] from the issue list, penalising per category."""
    triggered: set[str] = set()
    for issue in issues:
        category = _CODE_TO_CATEGORY.get(issue.code)
        if category:
            triggered.add(category)

    score = 1.0
    breakdown: list[dict] = []
    for category in triggered:
        penalty = PENALTIES.get(category, 0.0)
        score -= penalty
        breakdown.append({"category": category, "penalty": penalty})

    score = max(0.0, min(1.0, score))
    breakdown.sort(key=lambda b: b["penalty"], reverse=True)
    return ConfidenceResult(score=round(score, 2), breakdown=breakdown)
