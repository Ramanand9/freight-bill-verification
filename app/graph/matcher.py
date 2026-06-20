"""
graph/matcher.py
================
Traverses the domain graph to find candidate Carrier, Contract, Shipment, and
BOL for an incoming freight bill. This is the "find_candidates" brain.

WHY GRAPH MATCHING HELPS THE AMBIGUOUS CASES
--------------------------------------------
* Overlapping contracts: a single lane (e.g. DEL-BOM) is reachable from three
  contract nodes. The traversal naturally yields all three as candidates; we
  then filter by date validity and surface "multiple_overlapping_contracts".
* Missing shipment reference: we walk Carrier -> Shipment edges filtered by lane
  to *infer* a shipment instead of giving up.
* Shipment/BOL linkage: successors(shipment) gives BOLs for free — partial
  deliveries (multiple BOLs) fall out of the same traversal.

The matcher only finds CANDIDATES. Choosing among them and validating money is
done deterministically later (validate_contract / validate_charges).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.rules import contract_date_valid
from app.graph import builder as gb
from app.models import Carrier, CarrierContract, Shipment


@dataclass
class MatchResult:
    carrier_id: str | None = None
    carrier_match_method: str = "none"            # "id" | "name" | "none"
    active_contract_ids: list[str] = field(default_factory=list)
    expired_contract_ids: list[str] = field(default_factory=list)
    shipment_id: str | None = None
    shipment_inferred: bool = False
    bol_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def find_carrier(db: Session, carrier_id: str | None, carrier_name: str) -> tuple[str | None, str]:
    """Resolve the carrier by id first, then by normalized-name match."""
    if carrier_id and db.get(Carrier, carrier_id):
        return carrier_id, "id"

    target = _normalize(carrier_name)
    for carrier in db.execute(select(Carrier)).scalars():
        name = _normalize(carrier.name)
        # match if either contains the other's significant token set
        if name == target or target in name or name in target:
            return carrier.id, "name"
    return None, "none"


def find_contract_candidates(
    graph: nx.DiGraph, carrier_id: str, lane: str, bill_date: dt.date, db: Session
) -> tuple[list[str], list[str]]:
    """
    Return (active_contract_ids, expired_contract_ids) for this carrier that
    price `lane`. Active = bill_date within the contract window. We keep the
    expired ones separately so the agent can explain "you used an expired rate".
    """
    active: list[str] = []
    expired: list[str] = []
    cnode = gb.carrier_node(carrier_id)
    if cnode not in graph:
        return active, expired

    for contract_n in graph.successors(cnode):
        if graph.nodes[contract_n].get("type") != "contract":
            continue
        # does this contract price the lane?
        prices_lane = any(
            graph.nodes[ln].get("lane") == lane
            for ln in graph.successors(contract_n)
            if graph.nodes[ln].get("type") == "lane"
        )
        if not prices_lane:
            continue
        contract = db.get(CarrierContract, graph.nodes[contract_n]["id"])
        if contract_date_valid(contract.effective_date, contract.expiry_date, bill_date):
            active.append(contract.id)
        else:
            expired.append(contract.id)
    return active, expired


def find_shipment(
    db: Session, shipment_reference: str | None, carrier_id: str | None, lane: str
) -> tuple[str | None, bool]:
    """Use the explicit reference if present; otherwise infer by carrier + lane."""
    if shipment_reference and db.get(Shipment, shipment_reference):
        return shipment_reference, False
    if carrier_id:
        candidates = db.execute(
            select(Shipment).where(Shipment.carrier_id == carrier_id, Shipment.lane == lane)
        ).scalars().all()
        if len(candidates) == 1:
            return candidates[0].id, True
    return None, False


def find_bols(graph: nx.DiGraph, shipment_id: str | None) -> list[str]:
    """All BOLs hanging off the shipment (successors in the graph)."""
    if not shipment_id:
        return []
    snode = gb.shipment_node(shipment_id)
    if snode not in graph:
        return []
    return [
        graph.nodes[b]["id"]
        for b in graph.successors(snode)
        if graph.nodes[b].get("type") == "bol"
    ]


def match(
    graph: nx.DiGraph,
    db: Session,
    *,
    carrier_id: str | None,
    carrier_name: str,
    lane: str,
    bill_date: dt.date,
    shipment_reference: str | None,
) -> MatchResult:
    """Full candidate search for one freight bill."""
    result = MatchResult()

    result.carrier_id, result.carrier_match_method = find_carrier(db, carrier_id, carrier_name)
    if result.carrier_id is None:
        result.notes.append(f"Carrier '{carrier_name}' not found in master data.")
        return result

    result.active_contract_ids, result.expired_contract_ids = find_contract_candidates(
        graph, result.carrier_id, lane, bill_date, db
    )
    result.shipment_id, result.shipment_inferred = find_shipment(
        db, shipment_reference, result.carrier_id, lane
    )
    result.bol_ids = find_bols(graph, result.shipment_id)
    return result
