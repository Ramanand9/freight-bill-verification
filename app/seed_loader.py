"""
seed_loader.py
==============
Reads seed_data.json and loads the REFERENCE data into the database:
carriers, contracts, rate cards, shipments, bills of lading.

WHY FREIGHT BILLS ARE NOT LOADED HERE
-------------------------------------
A freight bill is an *incoming event*, not reference data. In production it
arrives over the API/queue. So the loader only seeds the world the agent
matches against; freight bills are submitted later via POST /freight-bills.
We still expose `get_seed_freight_bill(id)` so you can submit a seed bill by
its id alone (convenience for the assignment).

HOW TO LOAD
-----------
`python -m app.seed_loader`  (also runs automatically on app startup).

WHAT TO INSPECT AFTERWARDS
--------------------------
Open logistics.db and check row counts: 5 carriers, 8 contracts, the rate-card
rows split out per lane, 7 shipments, 7 BOLs. Note that CAR001 has 3 contracts
that overlap on DEL-BOM — that overlap is the ambiguity the agent must resolve.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import BillOfLading, Carrier, CarrierContract, ContractRateCard, Shipment

settings = get_settings()


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _load_json() -> dict[str, Any]:
    return json.loads(Path(settings.seed_data_path).read_text(encoding="utf-8"))


def get_seed_freight_bill(bill_id: str) -> dict[str, Any] | None:
    """Return a freight-bill dict from the seed file by id (or None)."""
    data = _load_json()
    for fb in data.get("freight_bills", []):
        if fb.get("id") == bill_id:
            return {k: v for k, v in fb.items() if not k.startswith("_")}
    return None


def list_seed_freight_bill_ids() -> list[str]:
    return [fb["id"] for fb in _load_json().get("freight_bills", [])]


def seed_reference_data(db: Session) -> dict[str, int]:
    """Idempotently insert reference data. Returns counts inserted."""
    data = _load_json()
    counts = {"carriers": 0, "contracts": 0, "rate_cards": 0, "shipments": 0, "bols": 0}

    for c in data.get("carriers", []):
        if db.get(Carrier, c["id"]):
            continue
        db.add(
            Carrier(
                id=c["id"],
                name=c["name"],
                carrier_code=c["carrier_code"],
                gstin=c.get("gstin"),
                bank_account=c.get("bank_account"),
                status=c.get("status", "active"),
                onboarded_on=_date(c.get("onboarded_on")),
            )
        )
        counts["carriers"] += 1

    for ct in data.get("carrier_contracts", []):
        if db.get(CarrierContract, ct["id"]):
            continue
        db.add(
            CarrierContract(
                id=ct["id"],
                carrier_id=ct["carrier_id"],
                effective_date=_date(ct["effective_date"]),
                expiry_date=_date(ct["expiry_date"]),
                status=ct.get("status", "active"),
                notes=ct.get("notes"),
            )
        )
        counts["contracts"] += 1
        for rc in ct.get("rate_card", []):
            db.add(
                ContractRateCard(
                    contract_id=ct["id"],
                    lane=rc["lane"],
                    description=rc.get("description"),
                    rate_per_kg=rc.get("rate_per_kg"),
                    min_charge=rc.get("min_charge"),
                    fuel_surcharge_percent=rc.get("fuel_surcharge_percent"),
                    rate_per_unit=rc.get("rate_per_unit"),
                    unit=rc.get("unit"),
                    unit_capacity_kg=rc.get("unit_capacity_kg"),
                    alternate_rate_per_kg=rc.get("alternate_rate_per_kg"),
                    revised_on=_date(rc.get("revised_on")),
                    revised_fuel_surcharge_percent=rc.get("revised_fuel_surcharge_percent"),
                )
            )
            counts["rate_cards"] += 1

    for s in data.get("shipments", []):
        if db.get(Shipment, s["id"]):
            continue
        db.add(
            Shipment(
                id=s["id"],
                carrier_id=s["carrier_id"],
                contract_id=s.get("contract_id"),
                lane=s["lane"],
                shipment_date=_date(s["shipment_date"]),
                status=s.get("status", "delivered"),
                total_weight_kg=s["total_weight_kg"],
                notes=s.get("notes"),
            )
        )
        counts["shipments"] += 1

    for b in data.get("bills_of_lading", []):
        if db.get(BillOfLading, b["id"]):
            continue
        db.add(
            BillOfLading(
                id=b["id"],
                shipment_id=b["shipment_id"],
                delivery_date=_date(b.get("delivery_date")),
                actual_weight_kg=b["actual_weight_kg"],
                notes=b.get("notes") or b.get("_note"),
            )
        )
        counts["bols"] += 1

    db.commit()
    return counts


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        counts = seed_reference_data(db)
        existing = db.scalar(select(Carrier).limit(1))
        print(f"Seed complete. Inserted this run: {counts}")
        print(f"Sanity: at least one carrier present -> {existing is not None}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
