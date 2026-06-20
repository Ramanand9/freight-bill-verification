"""
models.py
=========
The relational schema, expressed as SQLAlchemy 2.0 typed ORM models.

WHY EACH TABLE EXISTS
---------------------
* carriers            - the company sending invoices. Master record we match against.
* carrier_contracts   - a dated agreement with a carrier. A carrier can have MANY,
                        and they can OVERLAP in time/lane (the core ambiguity).
* contract_rate_cards - one row per (contract, lane) price line. Split out from the
                        contract because a contract prices several lanes, and a lane
                        line carries its own fuel %, min charge, FTL/alt-rate fields,
                        and mid-term revisions. 1-NF: don't bury a list inside a row.
* shipments           - a physical movement of goods under a contract.
* bills_of_lading     - proof-of-delivery for (part of) a shipment. One shipment can
                        have several BOLs (multi-truck / partial delivery).
* freight_bills       - the invoice as SUBMITTED by the carrier. Stored verbatim;
                        we never mutate the claimed numbers, we only judge them.
* agent_decisions     - what the agent CONCLUDED about a bill (decision, confidence,
                        the matched entities, evidence, issues). Kept separate from
                        the bill so "what was claimed" and "what we decided" never mix,
                        and so we keep a history (initial run + post-review finalise).
* human_reviews       - a reviewer's verdict that resumes a paused bill.
* audit_events        - append-only timeline of everything that happened to a bill.
                        This is what makes the system defensible: every state change
                        is recorded, never overwritten.

WHAT TO PAY ATTENTION TO
------------------------
The split between freight_bills (immutable claim), agent_decisions (machine
verdict, possibly many), and human_reviews (human verdict) is the schema's
backbone. It mirrors how a real ops/finance team reasons: claim -> assessment
-> override.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class BillStatus(str, enum.Enum):
    """Lifecycle of a freight bill inside the system."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    COMPLETED = "COMPLETED"


class Decision(str, enum.Enum):
    """Possible agent / final decisions."""

    AUTO_APPROVE = "auto_approve"
    FLAG_FOR_REVIEW = "flag_for_review"
    DISPUTE = "dispute"
    HUMAN_REVIEW = "human_review"


# --------------------------------------------------------------------------- #
# Master / reference data
# --------------------------------------------------------------------------- #
class Carrier(Base):
    __tablename__ = "carriers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    carrier_code: Mapped[str] = mapped_column(String, nullable=False)
    gstin: Mapped[str | None] = mapped_column(String)
    bank_account: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    onboarded_on: Mapped[dt.date | None] = mapped_column(Date)

    contracts: Mapped[list["CarrierContract"]] = relationship(back_populates="carrier")
    shipments: Mapped[list["Shipment"]] = relationship(back_populates="carrier")


class CarrierContract(Base):
    __tablename__ = "carrier_contracts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_id: Mapped[str] = mapped_column(ForeignKey("carriers.id"), nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, default="active")
    notes: Mapped[str | None] = mapped_column(String)

    carrier: Mapped[Carrier] = relationship(back_populates="contracts")
    rate_cards: Mapped[list["ContractRateCard"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class ContractRateCard(Base):
    __tablename__ = "contract_rate_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("carrier_contracts.id"), nullable=False)
    lane: Mapped[str] = mapped_column(String, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String)

    # Standard per-kg pricing
    rate_per_kg: Mapped[float | None] = mapped_column(Float)
    min_charge: Mapped[float | None] = mapped_column(Float)
    fuel_surcharge_percent: Mapped[float | None] = mapped_column(Float)

    # FTL (full-truck-load) pricing with a per-kg alternate
    rate_per_unit: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)
    unit_capacity_kg: Mapped[float | None] = mapped_column(Float)
    alternate_rate_per_kg: Mapped[float | None] = mapped_column(Float)

    # Mid-term fuel surcharge revision
    revised_on: Mapped[dt.date | None] = mapped_column(Date)
    revised_fuel_surcharge_percent: Mapped[float | None] = mapped_column(Float)

    contract: Mapped[CarrierContract] = relationship(back_populates="rate_cards")


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    carrier_id: Mapped[str] = mapped_column(ForeignKey("carriers.id"), nullable=False)
    contract_id: Mapped[str | None] = mapped_column(ForeignKey("carrier_contracts.id"))
    lane: Mapped[str] = mapped_column(String, nullable=False, index=True)
    shipment_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, default="delivered")
    total_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(String)

    carrier: Mapped[Carrier] = relationship(back_populates="shipments")
    bills_of_lading: Mapped[list["BillOfLading"]] = relationship(back_populates="shipment")


class BillOfLading(Base):
    __tablename__ = "bills_of_lading"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id"), nullable=False)
    delivery_date: Mapped[dt.date | None] = mapped_column(Date)
    actual_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(String)

    shipment: Mapped[Shipment] = relationship(back_populates="bills_of_lading")


# --------------------------------------------------------------------------- #
# Transactional data: the invoice and everything we decide about it
# --------------------------------------------------------------------------- #
class FreightBill(Base):
    __tablename__ = "freight_bills"

    id: Mapped[str] = mapped_column(String, primary_key=True)

    # ---- The claim, stored verbatim (never mutated) ----
    carrier_id: Mapped[str | None] = mapped_column(String)          # may be null (spot carrier)
    carrier_name: Mapped[str] = mapped_column(String, nullable=False)
    bill_number: Mapped[str] = mapped_column(String, nullable=False, index=True)
    bill_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    shipment_reference: Mapped[str | None] = mapped_column(String)
    lane: Mapped[str] = mapped_column(String, nullable=False)
    billed_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    rate_per_kg: Mapped[float | None] = mapped_column(Float)
    billing_unit: Mapped[str | None] = mapped_column(String)
    base_charge: Mapped[float] = mapped_column(Float, nullable=False)
    fuel_surcharge: Mapped[float] = mapped_column(Float, default=0.0)
    gst_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)

    # ---- Processing state ----
    status: Mapped[BillStatus] = mapped_column(
        Enum(BillStatus), default=BillStatus.RECEIVED, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    decisions: Mapped[list["AgentDecision"]] = relationship(
        back_populates="freight_bill", order_by="AgentDecision.created_at"
    )
    reviews: Mapped[list["HumanReview"]] = relationship(back_populates="freight_bill")


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id: Mapped[str] = mapped_column(ForeignKey("freight_bills.id"), nullable=False)

    decision: Mapped[Decision] = mapped_column(Enum(Decision), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)  # post-review finalisation?

    # Matched entities (the graph traversal result)
    matched_carrier_id: Mapped[str | None] = mapped_column(String)
    matched_contract_id: Mapped[str | None] = mapped_column(String)
    matched_shipment_id: Mapped[str | None] = mapped_column(String)
    matched_bol_id: Mapped[str | None] = mapped_column(String)

    # Rich, human-readable explanation and the structured issue list
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    freight_bill: Mapped[FreightBill] = relationship(back_populates="decisions")


class HumanReview(Base):
    __tablename__ = "human_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id: Mapped[str] = mapped_column(ForeignKey("freight_bills.id"), nullable=False)

    reviewer_decision: Mapped[str] = mapped_column(String, nullable=False)  # approve/dispute/modify
    reviewer_notes: Mapped[str | None] = mapped_column(String)
    final_decision: Mapped[Decision] = mapped_column(Enum(Decision), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    freight_bill: Mapped[FreightBill] = relationship(back_populates="reviews")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id: Mapped[str | None] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
