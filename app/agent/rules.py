"""
agent/rules.py
==============
The deterministic core. PURE functions only: no database, no network, no LLM.
Given the same inputs they always return the same outputs, which is exactly why
money math lives here and not in a language model.

WHAT EACH RULE DOES (backend business logic, explained)
-------------------------------------------------------
* contract_date_valid     - is a contract in force on the bill date?
* resolve_fuel_percent    - pick the right fuel surcharge %, honouring mid-term revisions.
* assess_charges          - rebuild the expected base / fuel / gst / total from the
                            contract and compare to what was billed. Handles standard
                            per-kg pricing AND FTL contracts billed on their per-kg
                            alternate (unit reconciliation), and applies min_charge.
* assess_weight           - compare billed weight to the BOL proof-of-delivery and to
                            the shipment total (cumulative across prior bills) to catch
                            over-billing and recognise legitimate partial deliveries.
* is_duplicate            - same carrier + same bill number already seen.

WHY PURE FUNCTIONS
------------------
Determinism + testability. Every penalty the confidence score later applies is
traceable to an Issue produced here. A reviewer (or a court) can replay the math.
"""
from __future__ import annotations

import datetime as dt
import enum
import math
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Issue model: the common currency between rules, confidence, and decisions
# --------------------------------------------------------------------------- #
class Severity(str, enum.Enum):
    INFO = "info"          # we reconciled something; not a problem
    WARNING = "warning"    # needs a human glance, but not clearly wrong
    CRITICAL = "critical"  # a real discrepancy: dispute / escalate


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "data": self.data,
        }


@dataclass(frozen=True)
class RateLine:
    """A single (contract, lane) price line, decoupled from the ORM for testing."""

    contract_id: str
    lane: str
    rate_per_kg: float | None = None
    min_charge: float | None = None
    fuel_surcharge_percent: float | None = None
    rate_per_unit: float | None = None
    unit: str | None = None
    unit_capacity_kg: float | None = None
    alternate_rate_per_kg: float | None = None
    revised_on: dt.date | None = None
    revised_fuel_surcharge_percent: float | None = None


@dataclass
class ChargeAssessment:
    expected_base: float
    expected_fuel: float
    expected_gst: float
    expected_total: float
    fuel_percent_used: float
    method: str                    # "per_kg" | "ftl_alternate_kg" | "ftl_unit"
    min_charge_applied: bool
    issues: list[Issue] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Date rules
# --------------------------------------------------------------------------- #
def contract_date_valid(effective: dt.date, expiry: dt.date, bill_date: dt.date) -> bool:
    """A contract covers a bill only if the bill date falls within its window."""
    return effective <= bill_date <= expiry


# --------------------------------------------------------------------------- #
# Fuel surcharge (with mid-term revision)
# --------------------------------------------------------------------------- #
def resolve_fuel_percent(line: RateLine, bill_date: dt.date) -> tuple[float, bool]:
    """
    Return (percent, revised_applied).

    Contracts can revise the fuel surcharge mid-term. If the bill date is on or
    after `revised_on`, the revised percentage applies. Example: CC-2024-BDA-001
    revises 12% -> 18% on 2024-10-01, so a 2024-11-20 bill must use 18%.
    """
    base_pct = line.fuel_surcharge_percent or 0.0
    if line.revised_on and line.revised_fuel_surcharge_percent is not None and bill_date >= line.revised_on:
        return float(line.revised_fuel_surcharge_percent), True
    return float(base_pct), False


# --------------------------------------------------------------------------- #
# Charge assessment
# --------------------------------------------------------------------------- #
def assess_charges(
    *,
    line: RateLine,
    bill_date: dt.date,
    billed_weight_kg: float,
    billing_unit: str | None,
    billed_base: float,
    billed_fuel: float,
    billed_gst: float,
    gst_rate: float,
    match_tol: float,
    dispute_threshold: float,
) -> ChargeAssessment:
    """
    Rebuild the *expected* charges from the contract and compare to the bill.

    Pricing methods handled:
      * Standard per-kg:   base = weight * rate_per_kg
      * FTL billed per-kg: contract is priced per full-truck-load, but the carrier
                           billed per-kg using the contract's alternate_rate_per_kg.
                           Semantically valid -> we reconcile and emit an INFO issue.
    `min_charge` floors the base in every method.
    """
    issues: list[Issue] = []

    # --- 1. Expected base, choosing the right pricing method ----------------
    if line.rate_per_kg is not None:
        method = "per_kg"
        expected_base = billed_weight_kg * line.rate_per_kg
    elif line.rate_per_unit is not None and line.alternate_rate_per_kg is not None and (
        billing_unit == "kg" or line.unit != billing_unit
    ):
        # FTL contract, but billed on the per-kg alternate. Reconcile.
        method = "ftl_alternate_kg"
        expected_base = billed_weight_kg * line.alternate_rate_per_kg
        issues.append(
            Issue(
                "unit_reconciled",
                Severity.INFO,
                f"Contract priced per {line.unit}; bill is per-kg. Reconciled via "
                f"alternate rate ₹{line.alternate_rate_per_kg}/kg.",
                {"contract_unit": line.unit, "alternate_rate_per_kg": line.alternate_rate_per_kg},
            )
        )
    elif line.rate_per_unit is not None and line.unit_capacity_kg:
        method = "ftl_unit"
        units = math.ceil(billed_weight_kg / line.unit_capacity_kg)
        expected_base = units * line.rate_per_unit
    else:
        # Shouldn't happen with valid data; surface loudly.
        method = "unknown"
        expected_base = billed_base
        issues.append(
            Issue("unpriceable_lane", Severity.CRITICAL, "Rate card has no usable pricing.", {})
        )

    # --- 2. Apply minimum charge -------------------------------------------
    min_applied = False
    if line.min_charge is not None and expected_base < line.min_charge:
        expected_base = float(line.min_charge)
        min_applied = True

    # --- 3. Fuel + GST ------------------------------------------------------
    fuel_pct, revised = resolve_fuel_percent(line, bill_date)
    expected_fuel = expected_base * (fuel_pct / 100.0)
    expected_gst = (expected_base + expected_fuel) * gst_rate
    expected_total = expected_base + expected_fuel + expected_gst
    if revised:
        issues.append(
            Issue(
                "fuel_revision_applied",
                Severity.INFO,
                f"Revised fuel surcharge {fuel_pct}% applied (bill date on/after revision).",
                {"fuel_percent": fuel_pct},
            )
        )

    # --- 4. Compare expected vs billed -------------------------------------
    issues.extend(_compare("base_charge", expected_base, billed_base, match_tol, dispute_threshold))
    issues.extend(_compare("fuel_surcharge", expected_fuel, billed_fuel, match_tol, dispute_threshold))

    # --- 5. Internal self-consistency of the bill (cheap fraud check) ------
    self_gst = (billed_base + billed_fuel) * gst_rate
    if not _within(self_gst, billed_gst, match_tol):
        issues.append(
            Issue(
                "gst_inconsistent",
                Severity.WARNING,
                f"Billed GST ₹{billed_gst} != {gst_rate*100:.0f}% of base+fuel (₹{self_gst:.2f}).",
                {"expected_gst_on_billed": round(self_gst, 2)},
            )
        )

    return ChargeAssessment(
        expected_base=round(expected_base, 2),
        expected_fuel=round(expected_fuel, 2),
        expected_gst=round(expected_gst, 2),
        expected_total=round(expected_total, 2),
        fuel_percent_used=fuel_pct,
        method=method,
        min_charge_applied=min_applied,
        issues=issues,
    )


def _within(expected: float, actual: float, tol: float) -> bool:
    if expected == 0:
        return abs(actual) <= 1.0
    return abs(actual - expected) / abs(expected) <= tol


def _compare(field_name: str, expected: float, billed: float, tol: float, dispute_threshold: float) -> list[Issue]:
    """Classify a single expected-vs-billed comparison into 0 or 1 Issue."""
    if expected == 0:
        drift = 0.0 if abs(billed) <= 1.0 else 1.0
    else:
        drift = abs(billed - expected) / abs(expected)

    if drift <= tol:
        return []  # clean match
    over = billed > expected
    if drift <= dispute_threshold:
        return [
            Issue(
                f"{field_name}_drift",
                Severity.WARNING,
                f"{field_name}: billed ₹{billed:.2f} vs expected ₹{expected:.2f} "
                f"({drift*100:.1f}% {'over' if over else 'under'}).",
                {"expected": round(expected, 2), "billed": round(billed, 2), "drift_pct": round(drift * 100, 2)},
            )
        ]
    return [
        Issue(
            f"{field_name}_mismatch",
            Severity.CRITICAL,
            f"{field_name}: billed ₹{billed:.2f} vs expected ₹{expected:.2f} "
            f"({drift*100:.1f}% {'over' if over else 'under'}) — beyond dispute threshold.",
            {"expected": round(expected, 2), "billed": round(billed, 2), "drift_pct": round(drift * 100, 2)},
        )
    ]


# --------------------------------------------------------------------------- #
# Weight assessment
# --------------------------------------------------------------------------- #
def assess_weight(
    *,
    billed_weight_kg: float,
    shipment_total_kg: float | None,
    prior_billed_kg: float,
    bol_actual_kg: float | None,
    shipment_status: str | None,
    tol_kg: float,
) -> list[Issue]:
    """
    Compare billed weight against delivery reality.

    Two independent over-billing guards (order-independent):
      1. Billed weight must not exceed the BOL-confirmed delivery (you cannot bill
         for more than was proven delivered).
      2. Cumulative billed weight across all bills for a shipment must not exceed
         the shipment's total weight.
    A shipment marked partially_delivered with billed < total is normal -> INFO.
    """
    issues: list[Issue] = []

    if bol_actual_kg is not None and billed_weight_kg > bol_actual_kg + tol_kg:
        issues.append(
            Issue(
                "weight_overbilling_vs_bol",
                Severity.CRITICAL,
                f"Billed {billed_weight_kg}kg exceeds BOL-confirmed {bol_actual_kg}kg.",
                {"billed_kg": billed_weight_kg, "bol_kg": bol_actual_kg},
            )
        )

    if shipment_total_kg is not None:
        cumulative = prior_billed_kg + billed_weight_kg
        if cumulative > shipment_total_kg + tol_kg:
            issues.append(
                Issue(
                    "weight_overbilling_cumulative",
                    Severity.CRITICAL,
                    f"Cumulative billed {cumulative}kg (prior {prior_billed_kg} + this "
                    f"{billed_weight_kg}) exceeds shipment total {shipment_total_kg}kg.",
                    {"cumulative_kg": cumulative, "shipment_total_kg": shipment_total_kg},
                )
            )
        elif (shipment_status == "partially_delivered" or prior_billed_kg > 0) and cumulative <= shipment_total_kg + tol_kg:
            issues.append(
                Issue(
                    "partial_delivery",
                    Severity.INFO,
                    f"Partial delivery: this bill {billed_weight_kg}kg, prior {prior_billed_kg}kg, "
                    f"shipment total {shipment_total_kg}kg. Within limit.",
                    {"cumulative_kg": cumulative, "shipment_total_kg": shipment_total_kg},
                )
            )

    return issues


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #
def is_duplicate(carrier_key: str, bill_number: str, existing_keys: set[tuple[str, str]]) -> bool:
    """A bill is a duplicate if (carrier, bill_number) was already ingested."""
    return (carrier_key, bill_number) in existing_keys
