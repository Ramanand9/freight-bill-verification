"""
graph/builder.py
================
Builds an in-memory NetworkX directed graph of the domain from the database.

WHY A GRAPH
-----------
Matching a freight bill is a multi-hop relationship problem:
    FreightBill -> (its lane/carrier) -> Contract -> RateCard
    FreightBill -> Shipment -> BOL
Expressing this as a graph means "find candidate BOLs" is literally
`successors(shipment_node)`, and the ambiguity in the data (a lane reachable
through THREE contract nodes) is visible as multiple paths rather than hidden
inside SQL. NetworkX is the right tool for a local, read-mostly graph of this
size — no server, no driver, just traversal. (Neo4j would be the move if the
graph were large, shared, and queried with Cypher across services.)

NODE NAMESPACES
---------------
carrier:<id> | contract:<id> | lane:<contract_id>::<lane> | shipment:<id> | bol:<id>
"""
from __future__ import annotations

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BillOfLading, Carrier, CarrierContract, ContractRateCard, Shipment


def carrier_node(cid: str) -> str:
    return f"carrier:{cid}"


def contract_node(cid: str) -> str:
    return f"contract:{cid}"


def lane_node(contract_id: str, lane: str) -> str:
    return f"lane:{contract_id}::{lane}"


def shipment_node(sid: str) -> str:
    return f"shipment:{sid}"


def bol_node(bid: str) -> str:
    return f"bol:{bid}"


def build_graph(db: Session) -> nx.DiGraph:
    """Construct the domain graph from current DB state."""
    g = nx.DiGraph()

    for carrier in db.execute(select(Carrier)).scalars():
        g.add_node(carrier_node(carrier.id), type="carrier", id=carrier.id, name=carrier.name)

    for contract in db.execute(select(CarrierContract)).scalars():
        g.add_node(
            contract_node(contract.id),
            type="contract",
            id=contract.id,
            carrier_id=contract.carrier_id,
            effective_date=contract.effective_date,
            expiry_date=contract.expiry_date,
            status=contract.status,
        )
        g.add_edge(carrier_node(contract.carrier_id), contract_node(contract.id), rel="has_contract")

    for rc in db.execute(select(ContractRateCard)).scalars():
        ln = lane_node(rc.contract_id, rc.lane)
        g.add_node(ln, type="lane", lane=rc.lane, contract_id=rc.contract_id, rate_card_id=rc.id)
        g.add_edge(contract_node(rc.contract_id), ln, rel="prices_lane")

    for s in db.execute(select(Shipment)).scalars():
        g.add_node(
            shipment_node(s.id),
            type="shipment",
            id=s.id,
            carrier_id=s.carrier_id,
            contract_id=s.contract_id,
            lane=s.lane,
            total_weight_kg=s.total_weight_kg,
            status=s.status,
        )
        g.add_edge(carrier_node(s.carrier_id), shipment_node(s.id), rel="shipped")
        if s.contract_id:
            g.add_edge(shipment_node(s.id), contract_node(s.contract_id), rel="under_contract")

    for b in db.execute(select(BillOfLading)).scalars():
        g.add_node(
            bol_node(b.id),
            type="bol",
            id=b.id,
            shipment_id=b.shipment_id,
            actual_weight_kg=b.actual_weight_kg,
        )
        g.add_edge(shipment_node(b.shipment_id), bol_node(b.id), rel="delivered_via")

    return g
