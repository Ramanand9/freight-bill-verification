"""
agent/llm.py
============
Optional LLM helpers. The whole system MUST work with no API key, so every
function here has a deterministic fallback. The LLM is only ever used for fuzzy,
non-financial tasks: normalising a messy carrier name and prettifying the
human-readable explanation. It NEVER computes money or makes the decision.

If settings.llm_enabled is False (default) or no key is present, the fallbacks
run and the system is fully deterministic.
"""
from __future__ import annotations

import re

from app.config import get_settings

settings = get_settings()

# Common legal/business suffixes we strip when normalising carrier names.
_SUFFIXES = ("logistics", "freight", "aviation", "express", "limited", "ltd", "pvt", "private", "kwe")


def normalize_carrier_name(raw: str) -> str:
    """
    Deterministic carrier-name normalisation (lowercase, strip punctuation and
    common suffixes). With an LLM enabled you could replace this with a call that
    resolves aliases ('Safex' -> 'Safexpress Logistics'); the deterministic
    version is the safe default.
    """
    cleaned = re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()
    tokens = [t for t in cleaned.split() if t not in _SUFFIXES]
    return " ".join(tokens) if tokens else cleaned


def explain(decision: str, evidence: list[str], issues: list[dict]) -> str:
    """
    Produce a one-paragraph explanation. Deterministic by default: stitches the
    evidence and issues into prose. An LLM could make this friendlier, but the
    content (the facts) always comes from the deterministic pipeline.
    """
    issue_text = "; ".join(i["message"] for i in issues) or "no issues detected"
    return (
        f"Decision '{decision}'. Matching trail: {' | '.join(evidence)}. "
        f"Findings: {issue_text}."
    )
