"""
tests/test_rules.py
===================
Unit tests for the PURE deterministic rule engine (app/agent/rules.py).

These are the calculations the assignment insists must never touch an LLM:
date validity, rate/fuel/min-charge math, FTL-vs-kg reconciliation, weight
comparison, and duplicate detection. They take plain inputs and return issues,
so they are trivial to test in isolation.
"""
from __future__ import annotations

import datetime as dt

from app.agent.rules import (
    RateLine,
    Severity,
    assess_charges,
    assess_weight,
    contract_date_valid,
    is_duplicate,
    resolve_fuel_percent,
)


def _codes(issues):
    return {i.code for i in issues}


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def test_contract_date_valid_within_window():
    assert contract_date_valid(dt.date(2024, 1, 1), dt.date(2025, 12, 31), dt.date(2025, 2, 15))


def test_contract_date_invalid_after_expiry():
    assert not contract_date_valid(dt.date(2023, 7, 1), dt.date(2024, 6, 30), dt.date(2025, 3, 20))


# --------------------------------------------------------------------------- #
# Fuel surcharge revision
# --------------------------------------------------------------------------- #
def test_resolve_fuel_uses_original_before_revision():
    line = RateLine(
        contract_id="C", lane="DEL-BOM-AIR", rate_per_kg=85.0, min_charge=12000.0,
        fuel_surcharge_percent=12.0, revised_on=dt.date(2024, 10, 1),
        revised_fuel_surcharge_percent=18.0,
    )
    pct, revised = resolve_fuel_percent(line, dt.date(2024, 9, 30))
    assert pct == 12.0 and revised is False


def test_resolve_fuel_uses_revised_on_or_after():
    line = RateLine(
        contract_id="C", lane="DEL-BOM-AIR", rate_per_kg=85.0, min_charge=12000.0,
        fuel_surcharge_percent=12.0, revised_on=dt.date(2024, 10, 1),
        revised_fuel_surcharge_percent=18.0,
    )
    pct, revised = resolve_fuel_percent(line, dt.date(2024, 11, 20))
    assert pct == 18.0 and revised is True


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #
def test_clean_per_kg_charge_has_no_issues():
    # FB-2025-101: 850kg @ 15.00, fuel 8%, gst 18%
    line = RateLine(contract_id="CC-2024-SFX-001", lane="DEL-BLR",
                    rate_per_kg=15.0, min_charge=6000.0, fuel_surcharge_percent=8.0)
    a = assess_charges(
        line=line, bill_date=dt.date(2025, 2, 15), billed_weight_kg=850,
        billing_unit=None, billed_base=12750.0, billed_fuel=1020.0, billed_gst=2479.0,
        gst_rate=0.18, match_tol=0.01, dispute_threshold=0.25,
    )
    assert a.expected_base == 12750.0
    assert a.expected_fuel == 1020.0
    assert _codes(a.issues) == set()


def test_ftl_alternate_per_kg_reconciles_as_info():
    # FB-2025-107: FTL contract billed on the per-kg alternate (6.50).
    line = RateLine(contract_id="CC-2024-TCI-002", lane="BOM-AHM",
                    rate_per_kg=None, min_charge=48000.0, fuel_surcharge_percent=6.0,
                    rate_per_unit=48000.0, unit="FTL", unit_capacity_kg=8000,
                    alternate_rate_per_kg=6.50)
    a = assess_charges(
        line=line, bill_date=dt.date(2025, 3, 1), billed_weight_kg=7800,
        billing_unit="kg", billed_base=50700.0, billed_fuel=3042.0, billed_gst=9673.56,
        gst_rate=0.18, match_tol=0.01, dispute_threshold=0.25,
    )
    assert a.method == "ftl_alternate_kg"
    assert a.expected_base == 50700.0
    # The only issue is the informational reconciliation, not a discrepancy.
    assert "unit_reconciled" in _codes(a.issues)
    assert all(i.severity != Severity.CRITICAL for i in a.issues)


def test_rate_drift_flags_as_warning():
    # FB-2025-105: billed 8.70 vs contracted 8.00 (~8.75% drift).
    line = RateLine(contract_id="CC-2024-DEL-001", lane="BLR-CHN",
                    rate_per_kg=8.0, min_charge=3500.0, fuel_surcharge_percent=7.0)
    a = assess_charges(
        line=line, bill_date=dt.date(2025, 1, 25), billed_weight_kg=1200,
        billing_unit=None, billed_base=10440.0, billed_fuel=730.80, billed_gst=2010.74,
        gst_rate=0.18, match_tol=0.01, dispute_threshold=0.25,
    )
    assert "base_charge_drift" in _codes(a.issues)
    assert any(i.severity == Severity.WARNING for i in a.issues)


# --------------------------------------------------------------------------- #
# Weight
# --------------------------------------------------------------------------- #
def test_weight_overbilling_vs_bol_is_critical():
    # billed 1500 but BOL proves 1200 delivered on that truck
    issues = assess_weight(
        billed_weight_kg=1500, shipment_total_kg=2000, prior_billed_kg=800,
        bol_actual_kg=1200, shipment_status="partially_delivered", tol_kg=1.0,
    )
    codes = _codes(issues)
    assert "weight_overbilling_vs_bol" in codes or "weight_overbilling_cumulative" in codes
    assert any(i.severity == Severity.CRITICAL for i in issues)


def test_partial_delivery_is_not_overbilling():
    # FB-2025-103: 800kg remaining on second truck, cumulative 800+ (prior 0) <= 2000
    issues = assess_weight(
        billed_weight_kg=800, shipment_total_kg=2000, prior_billed_kg=0,
        bol_actual_kg=1200, shipment_status="partially_delivered", tol_kg=1.0,
    )
    assert all(i.severity != Severity.CRITICAL for i in issues)


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #
def test_duplicate_detected_on_same_carrier_and_number():
    existing = {("CAR001", "SFX/2025/00234")}
    assert is_duplicate("CAR001", "SFX/2025/00234", existing) is True


def test_not_duplicate_when_number_differs():
    existing = {("CAR001", "SFX/2025/00234")}
    assert is_duplicate("CAR001", "SFX/2025/00999", existing) is False
