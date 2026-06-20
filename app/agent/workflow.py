"""
agent/workflow.py
=================
The LangGraph state machine that processes one freight bill end to end.

PIPELINE
--------
normalize -> find_candidates -> validate_contract -> validate_charges ->
validate_weight_and_bol -> detect_duplicate -> compute_confidence -> decide
       -> (human_review via interrupt) -> finalize -> END

WHY A GRAPH AND NOT ONE BIG FUNCTION
------------------------------------
* Each node does one thing and appends its findings to a shared, typed state.
* The order of reasoning is explicit and reorderable.
* We can PAUSE at `human_review` with a real `interrupt()` and resume later with
  `Command(resume=...)` — the checkpointer persists the exact state in between.
* Every node is unit-testable in isolation by handing it a state dict.

DEPENDENCY INJECTION
--------------------
DB session, the NetworkX graph, and settings are passed per-invocation through
`config["configurable"]` (NOT through state), so they are never checkpointed.
All pre-decision nodes run during the first invocation; on resume only
`human_review` (which returns from the interrupt) and `finalize` execute, so they
need no DB access.
"""
from __future__ import annotations

import datetime as dt

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import llm
from app.agent.confidence import compute_confidence
from app.agent.rules import (
    Issue,
    RateLine,
    Severity,
    assess_charges,
    assess_weight,
    is_duplicate,
)
from app.agent.state import BillState
from app.config import Settings
from app.graph import matcher
from app.models import CarrierContract, ContractRateCard, FreightBill, Shipment


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ctx(config) -> tuple[Session, object, Settings]:
    c = config["configurable"]
    return c["db"], c["domain_graph"], c["settings"]


def _issue(i: Issue) -> dict:
    return i.to_dict()


def _rate_line(db: Session, contract_id: str, lane: str) -> RateLine | None:
    rc = db.execute(
        select(ContractRateCard).where(
            ContractRateCard.contract_id == contract_id, ContractRateCard.lane == lane
        )
    ).scalar_one_or_none()
    if rc is None:
        return None
    return RateLine(
        contract_id=contract_id,
        lane=rc.lane,
        rate_per_kg=rc.rate_per_kg,
        min_charge=rc.min_charge,
        fuel_surcharge_percent=rc.fuel_surcharge_percent,
        rate_per_unit=rc.rate_per_unit,
        unit=rc.unit,
        unit_capacity_kg=rc.unit_capacity_kg,
        alternate_rate_per_kg=rc.alternate_rate_per_kg,
        revised_on=rc.revised_on,
        revised_fuel_surcharge_percent=rc.revised_fuel_surcharge_percent,
    )


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def normalize_freight_bill(state: BillState, config) -> dict:
    """
    First node. Canonicalise the lane and carrier name so every later comparison
    is apples-to-apples. Normalisation comes FIRST because all matching keys
    (carrier, lane) are derived here; matching on raw, inconsistent text would
    miss valid records.
    """
    lane = state["lane"].strip().upper()
    norm_name = llm.normalize_carrier_name(state["carrier_name"])
    return {
        "normalized_lane": lane,
        "normalized_carrier_name": norm_name,
        "evidence": [f"Normalised lane='{lane}', carrier='{norm_name}'."],
    }


def find_candidates(state: BillState, config) -> dict:
    """Traverse the graph to find carrier, contract, shipment and BOL candidates."""
    db, dg, _ = _ctx(config)
    bill_date = dt.date.fromisoformat(state["bill_date"])

    result = matcher.match(
        dg,
        db,
        carrier_id=state.get("carrier_id"),
        carrier_name=state["carrier_name"],
        lane=state["normalized_lane"],
        bill_date=bill_date,
        shipment_reference=state.get("shipment_reference"),
    )

    issues: list[dict] = []
    evidence: list[str] = []

    if result.carrier_id is None:
        issues.append(_issue(Issue("unknown_carrier", Severity.CRITICAL,
                                    f"Carrier '{state['carrier_name']}' has no master record.")))
        evidence.append("Carrier not found — spot/unknown carrier.")
    else:
        evidence.append(f"Carrier matched by {result.carrier_match_method}: {result.carrier_id}.")

    if result.shipment_inferred:
        issues.append(_issue(Issue("inferred_shipment", Severity.WARNING,
                                   "No shipment reference on bill; shipment inferred from carrier+lane.")))
        evidence.append(f"Shipment inferred: {result.shipment_id}.")
    elif result.shipment_id:
        evidence.append(f"Shipment matched by reference: {result.shipment_id}.")

    if len(result.active_contract_ids) > 1:
        pinned_by_reference = result.shipment_id and not result.shipment_inferred
        if pinned_by_reference:
            evidence.append(
                "Multiple active contracts cover this lane, but the bill's shipment "
                "reference pins the governing contract — no ambiguity."
            )
        else:
            issues.append(_issue(Issue("multiple_overlapping_contracts", Severity.WARNING,
                                       f"{len(result.active_contract_ids)} active contracts cover this lane: "
                                       f"{', '.join(result.active_contract_ids)}.")))
            evidence.append("Multiple overlapping contracts detected.")
    elif result.active_contract_ids:
        evidence.append(f"Active contract candidate: {result.active_contract_ids[0]}.")

    if not result.active_contract_ids and result.expired_contract_ids:
        evidence.append(f"Only expired contracts cover this lane: {', '.join(result.expired_contract_ids)}.")

    matched_bol = result.bol_ids[0] if result.bol_ids else None

    return {
        "matched_carrier_id": result.carrier_id,
        "carrier_match_method": result.carrier_match_method,
        "active_contract_ids": result.active_contract_ids,
        "expired_contract_ids": result.expired_contract_ids,
        "matched_shipment_id": result.shipment_id,
        "shipment_inferred": result.shipment_inferred,
        "matched_bol_id": matched_bol,
        "issues": issues,
        "evidence": evidence,
    }


def _rate_matches(db: Session, contract_id: str, lane: str, billed_rate: float | None) -> bool:
    """True if the billed per-kg rate equals this contract's per-kg or alternate rate."""
    if billed_rate is None:
        return False
    line = _rate_line(db, contract_id, lane)
    if line is None:
        return False
    for candidate in (line.rate_per_kg, line.alternate_rate_per_kg):
        if candidate is not None and abs(candidate - billed_rate) < 1e-6:
            return True
    return False


def validate_contract(state: BillState, config) -> dict:
    """
    Pick the single best contract among candidates and validate it.

    * 0 active contracts: if expired ones exist -> the carrier billed on an
      expired rate (critical); else no contract at all (critical).
    * 1 active contract: use it — UNLESS the billed rate doesn't match it but
      DOES match an expired contract's rate, which means the carrier invoiced on
      a stale contract (critical -> human review, not a simple dispute).
    * >1 active (overlap): if an explicit shipment reference pins a governing
      contract, use it; otherwise disambiguate by matching the billed rate.
    """
    db, _, _ = _ctx(config)
    active = state.get("active_contract_ids", [])
    expired = state.get("expired_contract_ids", [])
    lane = state["normalized_lane"]
    billed_rate = state.get("rate_per_kg")
    issues: list[dict] = []
    evidence: list[str] = []

    if not state.get("matched_carrier_id"):
        return {"chosen_contract_id": None,
                "evidence": ["No carrier -> cannot validate a contract."]}

    if not active:
        if expired:
            issues.append(_issue(Issue("contract_expired", Severity.CRITICAL,
                                       f"Bill date {state['bill_date']} falls outside all contracts; "
                                       f"matching rate only exists on expired contract(s) {expired}.")))
            evidence.append("No active contract on bill date — expired rate apparently used.")
        else:
            issues.append(_issue(Issue("no_valid_contract", Severity.CRITICAL,
                                       "No contract (active or expired) prices this carrier+lane.")))
            evidence.append("No contract prices this lane for this carrier.")
        return {"chosen_contract_id": None, "issues": issues, "evidence": evidence}

    if len(active) == 1:
        chosen = active[0]
        # Active contract exists, but did the carrier bill on an *expired* rate?
        if (
            billed_rate is not None
            and not _rate_matches(db, chosen, lane, billed_rate)
            and any(_rate_matches(db, e, lane, billed_rate) for e in expired)
        ):
            stale = [e for e in expired if _rate_matches(db, e, lane, billed_rate)]
            issues.append(_issue(Issue("contract_expired", Severity.CRITICAL,
                                       f"Billed rate ₹{billed_rate}/kg matches expired contract(s) {stale}, "
                                       f"not the active contract {chosen}. Bill appears to use a stale rate.")))
            evidence.append(
                f"Billed rate matches expired {stale}; active contract is {chosen}. "
                "Escalating: which rate governs is an ops decision."
            )
            return {"chosen_contract_id": None, "issues": issues, "evidence": evidence}
        evidence.append(f"Single active contract {chosen} selected.")
        return {"chosen_contract_id": chosen, "evidence": evidence}

    # >1 active. Prefer the contract the matched shipment is actually under,
    # but only when the shipment was matched by an explicit reference.
    chosen = active[0]
    shipment_id = state.get("matched_shipment_id")
    if shipment_id and not state.get("shipment_inferred"):
        shipment = db.get(Shipment, shipment_id)
        if shipment and shipment.contract_id in active:
            chosen = shipment.contract_id
            evidence.append(f"Overlap resolved by shipment reference: {shipment_id} is under {chosen}.")
            return {"chosen_contract_id": chosen, "evidence": evidence}

    # Otherwise disambiguate by billed-rate match.
    if billed_rate is not None:
        best = next((cid for cid in active if _rate_matches(db, cid, lane, billed_rate)), None)
        if best:
            chosen = best
            evidence.append(f"Overlap resolved: billed rate ₹{billed_rate}/kg matches {chosen}.")
        else:
            evidence.append(f"Overlap unresolved by rate; defaulting to {chosen}. Ambiguity preserved.")
    return {"chosen_contract_id": chosen, "evidence": evidence}


def validate_charges(state: BillState, config) -> dict:
    """Rebuild expected charges from the chosen contract and compare to billed."""
    db, _, settings = _ctx(config)
    chosen = state.get("chosen_contract_id")
    if not chosen:
        return {"expected": None,
                "evidence": ["No contract chosen -> charges not validated."]}

    line = _rate_line(db, chosen, state["normalized_lane"])
    if line is None:
        return {"expected": None,
                "evidence": [f"Contract {chosen} has no rate line for {state['normalized_lane']}."]}

    assessment = assess_charges(
        line=line,
        bill_date=dt.date.fromisoformat(state["bill_date"]),
        billed_weight_kg=state["billed_weight_kg"],
        billing_unit=state.get("billing_unit"),
        billed_base=state["billed_base"],
        billed_fuel=state["billed_fuel"],
        billed_gst=state["billed_gst"],
        gst_rate=settings.gst_rate,
        match_tol=settings.charge_match_tolerance,
        dispute_threshold=settings.charge_dispute_threshold,
    )
    expected = {
        "base": assessment.expected_base,
        "fuel": assessment.expected_fuel,
        "gst": assessment.expected_gst,
        "total": assessment.expected_total,
        "fuel_percent_used": assessment.fuel_percent_used,
        "method": assessment.method,
        "min_charge_applied": assessment.min_charge_applied,
    }
    evidence = [
        f"Expected via {assessment.method}: base ₹{assessment.expected_base}, "
        f"fuel ₹{assessment.expected_fuel} @ {assessment.fuel_percent_used}%, "
        f"total ₹{assessment.expected_total}. Billed total ₹{state['billed_total']}."
    ]
    return {"expected": expected,
            "issues": [_issue(i) for i in assessment.issues],
            "evidence": evidence}


def validate_weight_and_bol(state: BillState, config) -> dict:
    """Compare billed weight to BOL proof and shipment total (cumulative)."""
    db, _, settings = _ctx(config)
    shipment_id = state.get("matched_shipment_id")
    if not shipment_id:
        return {"evidence": ["No shipment matched -> weight not cross-checked against delivery."]}

    shipment = db.get(Shipment, shipment_id)
    bol_actual = None
    if state.get("matched_bol_id"):
        from app.models import BillOfLading
        bol = db.get(BillOfLading, state["matched_bol_id"])
        bol_actual = bol.actual_weight_kg if bol else None

    # Sum billed weight of OTHER freight bills already ingested for this shipment.
    # IMPORTANT: only do this cross-bill cumulative check when the shipment link is
    # CONFIRMED (explicit reference). If the shipment was merely *inferred*, we are
    # not sure this bill even belongs to it, so stacking a cumulative-overbilling
    # dispute on top of a guess would be a false positive — and would make the
    # verdict depend on ingestion order. With an inferred link we still check this
    # bill's own weight against the shipment total and the BOL, just not the sum
    # across sibling bills.
    inferred = bool(state.get("shipment_inferred"))
    if inferred:
        prior_billed = 0.0
    else:
        prior = db.execute(
            select(FreightBill).where(
                FreightBill.shipment_reference == shipment_id, FreightBill.id != state["bill_id"]
            )
        ).scalars().all()
        prior_billed = sum(fb.billed_weight_kg for fb in prior)

    weight_issues = assess_weight(
        billed_weight_kg=state["billed_weight_kg"],
        shipment_total_kg=shipment.total_weight_kg if shipment else None,
        prior_billed_kg=prior_billed,
        bol_actual_kg=bol_actual,
        shipment_status=shipment.status if shipment else None,
        tol_kg=settings.weight_match_tolerance_kg,
    )
    evidence = [
        f"Weight: billed {state['billed_weight_kg']}kg, BOL {bol_actual}kg, "
        f"prior-billed {prior_billed}kg "
        f"{'(cumulative skipped — shipment inferred)' if inferred else ''}, shipment total "
        f"{shipment.total_weight_kg if shipment else '?'}kg."
    ]
    return {"issues": [_issue(i) for i in weight_issues], "evidence": evidence}


def detect_duplicate(state: BillState, config) -> dict:
    """Duplicate = same carrier (id or name) + same bill_number already ingested."""
    db, _, _ = _ctx(config)
    carrier_key = state.get("matched_carrier_id") or state["normalized_carrier_name"]

    others = db.execute(
        select(FreightBill).where(
            FreightBill.bill_number == state["bill_number"], FreightBill.id != state["bill_id"]
        )
    ).scalars().all()
    existing = set()
    for fb in others:
        key = fb.carrier_id or llm.normalize_carrier_name(fb.carrier_name)
        existing.add((key, fb.bill_number))

    dup = is_duplicate(carrier_key, state["bill_number"], existing)
    issues: list[dict] = []
    evidence: list[str] = []
    if dup:
        issues.append(_issue(Issue("duplicate_bill", Severity.CRITICAL,
                                   f"Bill number {state['bill_number']} already ingested for this carrier.")))
        evidence.append("Duplicate bill number detected.")
    else:
        evidence.append("No duplicate bill number found.")
    return {"is_duplicate": dup, "issues": issues, "evidence": evidence}


def compute_confidence_node(state: BillState, config) -> dict:
    """Score confidence from the accumulated issues."""
    issues = [
        Issue(code=i["code"], severity=Severity(i["severity"]), message=i["message"], data=i.get("data", {}))
        for i in state.get("issues", [])
    ]
    result = compute_confidence(issues)
    return {
        "confidence": result.score,
        "confidence_breakdown": result.breakdown,
        "evidence": [f"Confidence {result.score} (penalties: {result.breakdown or 'none'})."],
    }


def _codes(state: BillState) -> set[str]:
    return {i["code"] for i in state.get("issues", [])}


def _has_severity(state: BillState, severity: str) -> bool:
    return any(i.get("severity") == severity for i in state.get("issues", []))


def decide(state: BillState, config) -> dict:
    """
    Map issues + confidence to a decision.

    Decision is issue-severity-FIRST, confidence-SECOND. The prescribed penalty
    table and the prescribed confidence bands can't both be satisfied for every
    seed bill (e.g. a clean FTL/kg reconciliation scores 0.80 yet should approve;
    a no-shipment overlap scores 0.45 yet should only flag). So severity drives
    the decision and confidence is a reported, tie-breaking signal:

      * CRITICAL issues route by their kind (dispute / human_review).
      * WARNING issues (drift, overlap, inferred shipment) -> flag_for_review.
      * INFO-only (e.g. unit reconciled) is NOT a problem -> auto_approve, even
        though it lowered the score.
      * A very low score with no routing issue is a defensive human_review.
    """
    _, _, settings = _ctx(config)
    codes = _codes(state)
    conf = state["confidence"]

    if "duplicate_bill" in codes:
        decision, reason = "dispute", "Duplicate bill."
    elif "unknown_carrier" in codes:
        decision, reason = "human_review", "Unknown/spot carrier needs onboarding decision."
    elif codes & {"weight_overbilling_vs_bol", "weight_overbilling_cumulative"}:
        decision, reason = "dispute", "Billed weight exceeds delivered/shipment weight."
    elif codes & {"contract_expired", "no_valid_contract"}:
        decision, reason = "human_review", "No valid contract governs the bill date/rate."
    elif codes & {"base_charge_mismatch", "fuel_surcharge_mismatch"}:
        decision, reason = "dispute", "Charge mismatch beyond dispute threshold."
    elif _has_severity(state, Severity.WARNING.value):
        decision, reason = "flag_for_review", "Ambiguity or rate drift requires a human glance."
    elif conf < settings.human_review_max_confidence:
        decision, reason = "human_review", f"Confidence {conf} below review floor."
    else:
        decision, reason = "auto_approve", "Clean match; charges, weight and dates reconcile."

    requires_review = decision in {"flag_for_review", "human_review"}
    explanation = llm.explain(decision, state.get("evidence", []), state.get("issues", []))
    return {
        "decision": decision,
        "requires_human_review": requires_review,
        "evidence": [f"Decision: {decision} — {reason}", f"Summary: {explanation}"],
    }


def human_review(state: BillState, config) -> dict:
    """
    Pause for a human. `interrupt()` checkpoints the full state and returns control
    to the caller. When POST /review resumes with Command(resume=payload), the same
    interrupt() call returns that payload and the graph continues to finalize.
    """
    payload = interrupt(
        {
            "bill_id": state["bill_id"],
            "proposed_decision": state["decision"],
            "confidence": state["confidence"],
            "issues": state.get("issues", []),
            "evidence": state.get("evidence", []),
        }
    )
    reviewer_decision = (payload or {}).get("reviewer_decision", "approve")
    reviewer_notes = (payload or {}).get("reviewer_notes")
    mapping = {"approve": "auto_approve", "dispute": "dispute", "modify": "auto_approve"}
    final = mapping.get(reviewer_decision, "flag_for_review")
    return {
        "reviewer_decision": reviewer_decision,
        "reviewer_notes": reviewer_notes,
        "final_decision": final,
        "evidence": [f"Reviewer '{reviewer_decision}' -> final '{final}'. Notes: {reviewer_notes or '-'}"],
    }


def finalize(state: BillState, config) -> dict:
    """Settle the final decision (reviewer's verdict, or the agent's if terminal)."""
    final = state.get("final_decision") or state["decision"]
    return {"final_decision": final, "evidence": [f"Finalised as '{final}'."]}


# --------------------------------------------------------------------------- #
# Graph wiring
# --------------------------------------------------------------------------- #
def _route_after_decide(state: BillState) -> str:
    return "human_review" if state.get("requires_human_review") else "finalize"


def build_graph_definition() -> StateGraph:
    g = StateGraph(BillState)
    g.add_node("normalize", normalize_freight_bill)
    g.add_node("find_candidates", find_candidates)
    g.add_node("validate_contract", validate_contract)
    g.add_node("validate_charges", validate_charges)
    g.add_node("validate_weight_and_bol", validate_weight_and_bol)
    g.add_node("detect_duplicate", detect_duplicate)
    g.add_node("compute_confidence", compute_confidence_node)
    g.add_node("decide", decide)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)

    g.add_edge(START, "normalize")
    g.add_edge("normalize", "find_candidates")
    g.add_edge("find_candidates", "validate_contract")
    g.add_edge("validate_contract", "validate_charges")
    g.add_edge("validate_charges", "validate_weight_and_bol")
    g.add_edge("validate_weight_and_bol", "detect_duplicate")
    g.add_edge("detect_duplicate", "compute_confidence")
    g.add_edge("compute_confidence", "decide")
    g.add_conditional_edges("decide", _route_after_decide,
                            {"human_review": "human_review", "finalize": "finalize"})
    g.add_edge("human_review", "finalize")
    g.add_edge("finalize", END)
    return g
