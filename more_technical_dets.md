# Freight Bill Verification Backend

A local backend that processes carrier freight bills (invoices) for a logistics
ops team. For every incoming bill it matches the correct **carrier → contract →
shipment → BOL**, validates **weight, charges, lane, and contract dates** with
deterministic rules, produces a **decision** (`auto_approve` / `flag_for_review`
/ `dispute` / `human_review`) with a **confidence score** and an **evidence
chain**, and **pauses for human review** when needed — resuming after a reviewer
submits a verdict.

The stateful agent is built with **LangGraph**; relationships are modelled as a
**NetworkX** graph; persistence is **SQLAlchemy + SQLite** (structured so Postgres
is a one-line swap). It runs with **no API key** — the optional LLM only touches
name normalisation and explanation text, never money or decisions.

---

## 1. Quick start

```bash
# from the project root (logistics_backend/)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# (optional) configure; defaults work with no .env at all
cp .env.example .env

# run the API
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/docs** for interactive Swagger UI.

### Loading seed data

You don't run a separate import step. On startup the app creates the tables and
loads the **reference** data from `seed_data.json` (carriers, contracts, rate
cards, shipments, BOLs). **Freight bills are deliberately *not* pre-loaded** —
they arrive through `POST /freight-bills`, which is the whole point of the
system. You can submit a seed bill by its id, or post a full payload.

To load reference data manually (e.g. for a script), run:

```bash
python -m app.seed_loader
```

### Running the tests

```bash
pytest -q          # 27 tests: rules, confidence, and end-to-end agent decisions
```

---

## 2. API

| Method & path             | Purpose                                                        |
|---------------------------|----------------------------------------------------------------|
| `POST /freight-bills`     | Ingest a bill (seed id **or** full payload); runs the agent.   |
| `GET  /freight-bills/{id}`| Bill state, decision history, matched entities, evidence, audit.|
| `GET  /review-queue`      | Bills currently paused at `WAITING_FOR_REVIEW`.                 |
| `POST /review/{id}`       | Submit reviewer verdict (`approve`/`dispute`/`modify`); resumes.|
| `GET  /metrics`           | Simple observability: counts by status and final decision.     |

### Curl examples

Ingest a clean bill (auto-approves):

```bash
curl -X POST localhost:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"bill_id": "FB-2025-101"}'
```

Ingest an ambiguous bill (pauses for review):

```bash
curl -X POST localhost:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"bill_id": "FB-2025-110"}'
```

See the queue, then resolve it:

```bash
curl localhost:8000/review-queue

curl -X POST localhost:8000/review/FB-2025-110 \
  -H 'Content-Type: application/json' \
  -d '{"reviewer_decision": "approve", "reviewer_notes": "Onboarded as spot carrier."}'
```

Inspect a bill's full evidence chain:

```bash
curl localhost:8000/freight-bills/FB-2025-110
```

Submit a full payload instead of a seed id (both fields → 422; exactly one is required):

```bash
curl -X POST localhost:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"bill": {"id":"FB-NEW-001","carrier_id":"CAR002","carrier_name":"Delhivery Freight",
       "bill_number":"DEL/25-26/2001","bill_date":"2025-01-25","shipment_reference":"SHP-2025-004",
       "lane":"BLR-CHN","billed_weight_kg":1200,"rate_per_kg":8.00,"base_charge":9600.0,
       "fuel_surcharge":672.0,"gst_amount":1848.96,"total_amount":12120.96}}'
```

---

## 3. Architecture

The system is layered so that each concern lives in exactly one place and
business logic never leaks into HTTP handlers.

```
            HTTP request
                 │
        ┌────────▼─────────┐   app/api/*          thin: validate I/O, delegate
        │   API layer      │
        └────────┬─────────┘
                 │
        ┌────────▼─────────┐   app/services/*     orchestration: persist, run the
        │  service layer   │                      agent, handle interrupt/resume
        └────────┬─────────┘
                 │
        ┌────────▼─────────┐   app/agent/workflow LangGraph state machine: the
        │  agent workflow  │                      ordered reasoning pipeline
        └────┬────────┬────┘
             │        │
   ┌─────────▼──┐  ┌──▼──────────┐   app/agent/rules.py + confidence.py
   │ rules layer│  │ graph match │   app/graph/*    PURE deterministic logic;
   └─────────┬──┘  └──┬──────────┘                  candidate matching over NetworkX
             │        │
        ┌────▼────────▼────┐   app/models.py + database.py
        │  database layer  │   SQLAlchemy ORM, SQLite (Postgres-ready)
        └──────────────────┘
```

**Why business logic is not inside FastAPI routes.** Routes should only translate
HTTP ↔ Python and delegate. Keeping the workflow wiring, DB writes, and
checkpointer handling in a *service* means the same logic is reusable (tests call
it directly, a CLI or a queue worker could too), routes stay trivially readable,
and we can change transport (REST → gRPC → batch) without touching the rules.

**Why the agent is a graph, not one big function.** Each node does one thing and
returns a partial update to a shared, typed state. The order of reasoning is
explicit and reorderable, every node is unit-testable in isolation, and — crucially
— we can **pause at a node with a real `interrupt()`** and resume later from a
persisted checkpoint. A single 300-line function can't be paused mid-way and
rehydrated days later.

---

## 4. Database schema

SQLAlchemy 2.0 typed models (`app/models.py`). SQLite for local dev; the
`DATABASE_URL` is structured so switching to Postgres is one line.

| Table               | Why it exists                                                                 |
|---------------------|-------------------------------------------------------------------------------|
| `carriers`          | Master record we match incoming bills against.                                |
| `carrier_contracts` | A dated agreement. A carrier can have **many**, and they can **overlap** in time/lane — the core ambiguity in the data. |
| `contract_rate_cards` | One row per **(contract, lane)** price line. Split out of the contract because a contract prices several lanes, and each line carries its own fuel %, min charge, FTL/alternate-rate fields, and mid-term revisions. (Don't bury a list inside a row.) |
| `shipments`         | A physical movement of goods under a contract.                                |
| `bills_of_lading`   | Proof-of-delivery for (part of) a shipment. One shipment can have several BOLs (multi-truck / partial delivery). |
| `freight_bills`     | The invoice **as submitted**, stored verbatim. We never mutate the claimed numbers; we only judge them. |
| `agent_decisions`   | What the agent **concluded** (decision, confidence, matched entities, evidence, issues). Kept separate from the bill so "what was claimed" and "what we decided" never mix — and we keep a history (initial run + post-review finalisation). |
| `human_reviews`     | A reviewer's verdict that resumes a paused bill.                              |
| `audit_events`      | Append-only timeline of everything that happened to a bill. Never overwritten — this is what makes the system defensible. |

The backbone is the split between **claim** (`freight_bills`, immutable), **machine
verdict** (`agent_decisions`, possibly several), and **human verdict**
(`human_reviews`). That mirrors how a real ops/finance team reasons: claim →
assessment → override.

---

## 5. Graph model

We build an in-memory **NetworkX** `DiGraph` from the reference data
(`app/graph/builder.py`) with namespaced nodes and typed edges:

```
carrier:<id>  ──has_contract──▶  contract:<id>  ──prices_lane──▶  lane:<contract>::<lane>
carrier:<id>  ──shipped──────▶  shipment:<id>  ──delivered_via─▶  bol:<id>
shipment:<id> ──under_contract─▶ contract:<id>
```

**Why graph thinking helps the ambiguous cases** (`app/graph/matcher.py`):

- **Overlapping contracts.** A single lane like `DEL-BOM` is reachable from three
  different contract nodes. The traversal naturally yields all three candidates;
  we then filter by date validity and surface them rather than silently picking one.
- **Missing shipment reference.** When a bill has no shipment id, we walk
  `carrier → shipment` edges filtered by lane to *infer* a shipment instead of
  giving up — and we mark it inferred so confidence drops.
- **Shipment ↔ BOL linkage.** `successors(shipment)` gives every BOL for free, so
  partial deliveries (multiple BOLs on one shipment) fall out of the same traversal.

The matcher only finds **candidates**. Choosing among them and validating money is
done deterministically afterward. We chose NetworkX over Neo4j because the graph is
small, read-mostly, and rebuilt from the source of truth (the relational DB) on each
run — an external graph DB would add ops overhead with no payoff at this scale, while
still demonstrating the graph-traversal approach the task asks for.

---

## 6. The LangGraph agent

Pipeline (`app/agent/workflow.py`):

```
normalize → find_candidates → validate_contract → validate_charges →
validate_weight_and_bol → detect_duplicate → compute_confidence → decide
        → (human_review via interrupt) → finalize → END
```

What flows between nodes is a single typed `BillState` (`app/agent/state.py`).
Each node reads what it needs and returns a partial update; `issues` and
`evidence` use additive reducers so the audit trail accumulates across the whole
run. The state holds only JSON-friendly data (ISO date strings, dicts, lists) so
the checkpointer can serialise and resume it.

Node responsibilities:

1. **normalize** — canonicalise lane + carrier name first, so every later match is
   apples-to-apples (matching on raw inconsistent text would miss valid records).
2. **find_candidates** — graph traversal for carrier, contract(s), shipment, BOL.
3. **validate_contract** — pick the governing contract: a shipment *reference* pins
   it; otherwise disambiguate overlaps by the billed rate. Detects bills priced on an
   **expired** contract's rate even when an active contract also exists.
4. **validate_charges** — rebuild expected base/fuel/GST from the contract, applying
   `min_charge`, the **revised** fuel surcharge after `revised_on`, and FTL ↔ per-kg
   **alternate** reconciliation; compare to billed with tolerance.
5. **validate_weight_and_bol** — compare billed weight to BOL proof and to the
   cumulative total across prior bills for the shipment; catch overbilling; treat
   genuine partial deliveries as fine.
6. **detect_duplicate** — same carrier + bill number already ingested.
7. **compute_confidence** — score from accumulated issues (below).
8. **decide** — map issues + confidence to a decision (below).
9. **human_review** — `interrupt()` to pause; resumes with the reviewer's payload.
10. **finalize** — settle the final decision (reviewer's, or the agent's if terminal).

**Dependency injection.** The DB session, the NetworkX graph, and settings are
passed per-invocation through `config["configurable"]`, *not* through state — so
they are never checkpointed. All pre-decision nodes run on the first invocation; on
resume only `human_review` and `finalize` execute.

---

## 7. Confidence scoring

`app/agent/confidence.py`. Start at **1.0** and subtract a penalty for each
**category** of problem detected (penalties apply once per category, then the score
is clamped to `[0, 1]`):

| Category                        | Penalty |
|---------------------------------|---------|
| shipment inferred (no reference)| 0.30    |
| multiple overlapping contracts  | 0.25    |
| charge mismatch / drift         | 0.30    |
| weight mismatch / overbilling   | 0.35    |
| duplicate bill                  | 0.50    |
| unknown carrier                 | 0.50    |
| expired / no valid contract     | 0.40    |
| unit mismatch but reconciled    | 0.20    |

The score is a *signal*, but the **decision is issue-severity-first**. Each issue
has a severity — `INFO` (we reconciled something; not a problem), `WARNING` (needs a
human glance), or `CRITICAL` (a real discrepancy). The `decide` node routes:

- `duplicate` → **dispute**; `unknown_carrier` → **human_review**;
- weight overbilling → **dispute**; expired / no contract → **human_review**;
- charge mismatch (critical) → **dispute**;
- any remaining `WARNING` (rate drift, overlap, inferred shipment) → **flag_for_review**;
- otherwise → **auto_approve** (an `INFO`-only reconciliation like FTL-billed-per-kg
  lowers the score but is not a problem, so it still approves);
- a very low score with no routing issue is a defensive **human_review**.

**Why severity-first?** The prescribed penalty table and the prescribed confidence
*bands* cannot both hold for every seed bill: a clean FTL/kg reconciliation scores
0.80 yet should approve, while a no-reference overlap scores 0.45 yet should only
flag. Rather than fudge the numbers, the severity of *what was actually found* drives
the decision and confidence is reported alongside as an explainable signal. This is a
deliberate, documented design choice (see §10).

---

## 8. Human-in-the-loop (real interrupt/resume)

This is a genuine LangGraph pattern, not a workaround.

- The graph is compiled with a **`SqliteSaver` checkpointer** keyed by
  `thread_id = bill_id`.
- The first `invoke` runs every node up to `decide`. If the decision needs a human,
  the `human_review` node calls **`interrupt(payload)`**, which **checkpoints the
  entire state to SQLite** and returns control with an `__interrupt__` marker. The
  service then sets the bill to `WAITING_FOR_REVIEW` and records the *proposed*
  (non-final) decision.
- `GET /review-queue` lists these paused bills.
- `POST /review/{id}` calls `invoke(Command(resume=verdict))` on the **same
  thread_id**. LangGraph reloads the checkpoint, the waiting `interrupt()` call
  returns the reviewer's payload, and the graph continues into `finalize`. We persist
  a `HumanReview` row and a final `AgentDecision(is_final=True)`, then mark the bill
  `COMPLETED`.

Because the pause lives in SQLite rather than memory, **it survives a process
restart** — you can stop the server between the pause and the review and still
resume correctly. Reviewer verdicts map as: `approve → auto_approve`,
`dispute → dispute`, `modify → auto_approve` (with notes recorded for audit).

---

## 9. Scenario-by-scenario behaviour

All ten seed bills are verified (see `tests/test_agent_decisions.py` and the table
below). Decision priority is severity-first, so the *reason* matters as much as the
label.

| Bill | Scenario | Decision | Why |
|------|----------|----------|-----|
| FB-2025-101 | Clean match | **auto_approve** (conf 1.0) | Shipment, weight, charges all reconcile. |
| FB-2025-102 | No reference + 3 overlapping DEL-BOM contracts | **flag_for_review** (0.45) | Shipment inferred (−0.30) + overlap (−0.25); billed ₹13.20 resolves to CC-2025-SFX-003, but the ambiguity warrants a glance. |
| FB-2025-103 | Partial delivery (2nd truck, 800kg) | **auto_approve** (1.0) | Shipment *reference* pins CC-2024-SFX-001; cumulative weight within shipment total. |
| FB-2025-104 | Overbilling (claims 1500kg) | **dispute** | 1500kg > BOL 1200kg **and** prior 800 + 1500 > shipment total 2000. |
| FB-2025-105 | Rate drift (₹8.70 vs ₹8.00, ~8.75%) | **flag_for_review** (0.70) | Within tolerance..dispute-threshold band → WARNING. |
| FB-2025-106 | Billed on expired TCI rate (₹7.50) | **human_review** (0.30) | Active contract is FTL @ ₹6.50; billed rate matches only the *expired* contract → escalate. |
| FB-2025-107 | FTL contract billed per-kg (₹6.50 alternate) | **auto_approve** (0.80) | Valid alternate billing; reconciliation is INFO, not a problem. |
| FB-2025-108 | Fuel surcharge revision (18% after revised_on) | **auto_approve** (1.0) | Bill correctly uses the revised 18%. |
| FB-2025-109 | Duplicate of FB-2025-101 | **dispute** | Same carrier + bill number already ingested. |
| FB-2025-110 | Unknown carrier (Gati KWE) | **human_review** (0.50) | No master record; needs an onboarding decision. |

> Note on FB-104 vs FB-109: both have order-independent guards, but processing
> bills in id order (101 → 110) is the natural flow and makes the cumulative-weight
> and duplicate checks deterministic.

---

## 10. Trade-offs made deliberately

- **Severity-first decisions over literal confidence bands.** As explained in §7,
  the prescribed penalties and bands conflict on real seed bills. I kept the
  prescribed penalty *formula* (it produces the expected scores) but let issue
  severity drive the decision, with confidence as an explainable signal. This is the
  single most consequential design call.
- **NetworkX, not Neo4j.** Small, read-mostly graph rebuilt from the relational
  source of truth each run. Demonstrates graph traversal without standing up another
  database. The matcher is isolated, so swapping in Neo4j later is contained.
- **SQLite + a single long-lived checkpointer connection**, guarded by a process
  lock that serialises graph runs. Correct and simple for local/dev; it trades
  write concurrency for clarity (see below).
- **LLM strictly optional and side-of-plate.** Name normalisation and explanation
  text only, with deterministic fallbacks, so the system is fully reproducible and
  testable with no key. All money and decisions are pure Python — as required.
- **Reference data auto-seeded on startup; freight bills only via the API.** Mirrors
  reality: master data is provisioned, invoices arrive as events.

---

## 11. What I'd do with more time

- **Postgres + Alembic migrations**, and move the LangGraph checkpointer to the
  Postgres saver so HITL state and business state share one durable store.
- **Concurrency:** per-bill locking and a connection pool instead of the global lock,
  so independent bills process in parallel safely.
- **Richer matching:** fuzzy carrier-name resolution (the LLM hook is already there),
  and confidence-weighted candidate ranking when the billed rate matches more than
  one overlapping contract.
- **Observability:** structured per-node logging with timings, and a `/dashboard`
  endpoint summarising agent throughput, decision mix, and review SLAs.
- **AuthN/Z** on the review endpoints (who approved what), and idempotency keys on
  ingest.
- **Deployment:** Dockerfile + `docker-compose` (API + Postgres), GCP Cloud Run +
  Cloud SQL, and Secret Manager for the optional API key.

---

## Project layout

```
logistics_backend/
  app/
    main.py                 FastAPI app, startup seeding, /metrics
    config.py               typed Settings (env-driven)
    database.py             engine, session, Base, get_db
    models.py               9 SQLAlchemy tables
    schemas.py              Pydantic request/response contracts
    seed_loader.py          loads reference data; exposes seed freight bills
    api/
      freight_bills.py      POST /freight-bills, GET /freight-bills/{id}
      review.py             GET /review-queue, POST /review/{id}
    services/
      freight_bill_service.py  orchestration + interrupt/resume
      audit_service.py         append-only audit events
    graph/
      builder.py            build NetworkX graph from the DB
      matcher.py            candidate carrier/contract/shipment/BOL search
    agent/
      state.py              the typed BillState
      workflow.py           the LangGraph nodes + wiring
      rules.py              PURE deterministic rule engine
      confidence.py         rule-based confidence scoring
      llm.py                optional LLM helpers (deterministic fallbacks)
  tests/
    conftest.py             isolated temp DB + checkpoint fixtures
    test_rules.py           rule-engine unit tests
    test_confidence.py      scoring unit tests
    test_agent_decisions.py end-to-end agent + HITL tests
  seed_data.json
  requirements.txt
  .env.example
  README.md
```
