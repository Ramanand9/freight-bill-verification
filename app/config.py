"""
config.py
=========
Central place for all runtime configuration.

WHY THIS EXISTS
---------------
Hard-coding paths, tolerances, and feature flags inside business logic makes a
system impossible to tune or deploy to different environments. We funnel every
"knob" through a single typed Settings object loaded from environment variables
(with sane defaults), so the rest of the code never reads os.environ directly.

We use pydantic-settings so the values are *validated and typed* at startup —
a bad value fails loudly here instead of deep inside the agent.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Storage ---------------------------------------------------------
    # SQLite for local dev. The URL is structured so swapping to Postgres later
    # is a one-line change (e.g. postgresql+psycopg://user:pass@host/db).
    database_url: str = "sqlite:///./logistics.db"

    # Path to the LangGraph checkpoint store (durable interrupt/resume state).
    checkpoint_db: str = "./agent_checkpoints.db"

    # Path to seed data the loader ingests.
    seed_data_path: str = "./seed_data.json"

    # --- Business tolerances (the "deterministic rules" knobs) -----------
    # How close a billed value must be to the expected value to count as a
    # clean match. 0.01 == 1%. These live in config so ops can tune them
    # without touching rule code.
    charge_match_tolerance: float = 0.01          # 1% -> treated as "matches"
    charge_dispute_threshold: float = 0.25        # >25% drift -> dispute, not flag
    weight_match_tolerance_kg: float = 1.0        # absolute kg slack on weight checks
    gst_rate: float = 0.18                         # standard GST applied to base+fuel

    # --- Decision thresholds --------------------------------------------
    auto_approve_min_confidence: float = 0.85
    human_review_max_confidence: float = 0.60

    # --- LLM (optional, system must work without a key) -----------------
    # If unset, every LLM-flavoured helper falls back to deterministic Python.
    anthropic_api_key: str | None = None
    llm_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so we build Settings exactly once per process."""
    return Settings()
