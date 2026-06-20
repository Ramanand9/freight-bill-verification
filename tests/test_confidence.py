"""
tests/test_confidence.py
========================
Unit tests for confidence scoring (app/agent/confidence.py).

Key properties under test:
* a clean bill scores 1.0,
* each penalty subtracts its prescribed amount,
* penalties are applied PER CATEGORY (two charge issues penalise once),
* the score is clamped to [0, 1].
"""
from __future__ import annotations

from app.agent.confidence import compute_confidence
from app.agent.rules import Issue, Severity


def _issue(code: str, severity: Severity = Severity.WARNING) -> Issue:
    return Issue(code=code, severity=severity, message=code)


def test_clean_bill_scores_one():
    assert compute_confidence([]).score == 1.0


def test_single_duplicate_penalty():
    result = compute_confidence([_issue("duplicate_bill", Severity.CRITICAL)])
    assert result.score == 0.50


def test_unit_reconciled_is_small_penalty():
    result = compute_confidence([_issue("unit_reconciled", Severity.INFO)])
    assert result.score == 0.80


def test_inferred_plus_overlap_stacks():
    # FB-2025-102: -0.30 (inferred) -0.25 (overlap) = 0.45
    result = compute_confidence([
        _issue("inferred_shipment"),
        _issue("multiple_overlapping_contracts"),
    ])
    assert round(result.score, 2) == 0.45


def test_charge_issues_penalise_once_per_category():
    # base + fuel drift both map to the "charge_mismatch" category -> one penalty.
    result = compute_confidence([
        _issue("base_charge_drift"),
        _issue("fuel_surcharge_drift"),
    ])
    assert round(result.score, 2) == 0.70


def test_score_is_clamped_to_zero():
    result = compute_confidence([
        _issue("unknown_carrier", Severity.CRITICAL),
        _issue("no_valid_contract", Severity.CRITICAL),
        _issue("duplicate_bill", Severity.CRITICAL),
    ])
    assert result.score == 0.0
