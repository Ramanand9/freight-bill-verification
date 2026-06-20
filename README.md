# Freight Bill Verification

This project checks a transport company’s freight invoice before it is paid.

Think of it as a careful accounts clerk: it reads the bill, finds the matching
carrier, contract, shipment, and proof of delivery, recalculates the price, and
then explains whether the bill should be approved, disputed, or shown to a
person.

It includes both parts of the application:

- a browser-based operations screen (the **UI**), and
- a Python/FastAPI verification service (the **backend**).

You do not need an API key, a cloud account, a separate frontend server, or a
separate database server. Everything runs on one computer.

---

## What problem does it solve?

A freight bill is a carrier’s request for payment. Before paying it, a company
normally has to answer questions such as:

- Is this a carrier we know?
- Was its contract valid on the bill date?
- Does the contract cover this route, such as Delhi to Mumbai?
- Does the rate on the bill match the agreed rate?
- Is the fuel surcharge correct?
- Was this much weight actually delivered?
- Has the same invoice already been submitted?

This application performs those checks, keeps the evidence, and gives one of
four results:

| Result | Plain-English meaning | What happens next |
| --- | --- | --- |
| **Auto approve** (green) | The bill agrees with the records. | The bill is completed automatically. |
| **Flag for review** (amber) | Something is unclear or slightly different. | The bill waits for a person. |
| **Dispute** (red) | There is a serious problem, such as duplicate billing or excess weight. | The bill is completed as disputed. |
| **Human review** (blue) | The system cannot safely decide, for example because the carrier is unknown. | The bill waits for a person. |

The “confidence” percentage is a transparent trust score. It begins at 100%
and falls when the system has to infer information or finds problems. The
severity of the actual problem is more important than the percentage alone.

---

## Run the complete application

### What you need

- A computer with **Python 3.11 or newer**
- A terminal: Terminal on macOS/Linux, or PowerShell on Windows
- A web browser
- Git, if you are downloading the project from GitHub

### macOS or Linux

Copy and run these commands one at a time:

```bash
git clone https://github.com/Ramanand9/freight-bill-verification.git
cd freight-bill-verification
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### Windows PowerShell

Copy and run these commands one at a time:

```powershell
git clone https://github.com/Ramanand9/freight-bill-verification.git
cd freight-bill-verification
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

When the terminal says that Uvicorn is running, leave that window open and go
to:

**http://127.0.0.1:8000/ui/**

That is the complete application. The same server supplies both the web page
and the backend. On its first start, it creates the local database and loads
the example carriers, contracts, shipments, and delivery records automatically.

To stop the application, return to the terminal and press **Ctrl+C**. Start it
again later with the final `uvicorn` command. Previous decisions and unfinished
reviews survive a normal restart.

### Optional: prove the backend is working

Run the automated tests from the project folder:

```bash
python -m pytest -q
```

The expected result is `27 passed`.

---

## How to use the UI

The top-right status should say **connected** with a green dot. The page has six
main areas.

### 1. Summary numbers at the top

These show how many bills were approved, disputed, are still in review, are
completed, and how many audit events have been recorded.

### 2. “Submit a bill” on the left

Choose one of the ten sample bills from **Seed scenario**. Its invoice data
appears in the large box below. Press **Verify bill**.

The invoice is shown as JSON, which is simply a labelled form written as text.
For example, `"billed_weight_kg": 850` means “the carrier billed for 850 kg.”
You may edit a number before pressing **Verify bill** to see how the decision
changes. **Reset to seed** restores the original sample.

### 3. “Verdict” on the right

This shows:

- the colour-coded decision;
- whether processing is complete or waiting for a person;
- the confidence percentage;
- which carrier, contract, shipment, and BOL were matched;
- every issue found; and
- the evidence used to reach the decision.

**BOL** means Bill of Lading: the delivery document that proves what was
actually transported.

### 4. “Run all 10 seeds”

This button processes all ten prepared examples in order. A results table
appears. Click any row to inspect that bill.

Starting from a clean demo, the examples show this spread:

| Bill | Expected result | What it demonstrates |
| --- | --- | --- |
| FB-2025-101 | Auto approve | Everything matches. |
| FB-2025-102 | Flag for review | Shipment was inferred and contracts overlap. |
| FB-2025-103 | Auto approve | A legitimate partial delivery. |
| FB-2025-104 | Dispute | Billed weight exceeds delivery evidence. |
| FB-2025-105 | Flag for review | The rate has drifted from the contract. |
| FB-2025-106 | Human review | The bill appears to use an expired contract. |
| FB-2025-107 | Auto approve | Per-truck and per-kg pricing reconcile. |
| FB-2025-108 | Auto approve | A revised fuel surcharge is applied correctly. |
| FB-2025-109 | Dispute | It duplicates bill 101. |
| FB-2025-110 | Human review | The carrier is not in the master records. |

The order matters for duplicate and cumulative-weight checks, just as it does
when invoices arrive at a real accounts department.

### 5. “Review queue”

Amber and blue decisions wait here. A reviewer can add a note and select:

- **Approve** — accept and complete the bill;
- **Dispute** — reject and complete the bill as disputed; or
- **Modify** — accept it after adjustment and record the adjustment in the note.

In this demo, **Modify** finishes as approved and preserves the reviewer note; it
does not replace the original invoice amounts. A production version would save
the corrected amount as a separate adjustment record.

The workflow is genuinely paused while it waits. Its saved state is loaded and
continued when the reviewer makes a choice, even if the server was restarted in
between.

### 6. “Evidence chain”

This is the history of the bill. It shows the machine’s proposed decision, the
final decision, and an append-only audit trail. A later human answer does not
erase the machine’s earlier answer.

### Resetting the demonstration

Press **Reset demo** and confirm. This removes submitted sample bills,
decisions, reviews, and their audit history, then reloads the original reference
records. It is useful before showing the ten examples again.

---

## A five-minute demonstration

1. Open **http://127.0.0.1:8000/ui/** and check for the green connected dot.
2. Press **Reset demo** so all counters begin at zero.
3. Press **Run all 10 seeds** and compare the results with the table above.
4. Select **FB-2025-101**, change `"billed_weight_kg": 850` to `5000`, and press
   **Verify bill**. The same clean bill should turn into a weight dispute.
5. In the **Review queue**, add a note to a waiting bill and approve or dispute
   it.
6. Look at **Evidence chain** to see both the proposed and final decisions.

For a more detailed presentation script, see [DEMO.md](DEMO.md).

---

## How the backend works — the full journey

The easiest way to understand the backend is to follow one bill through it:

```text
Browser
  │ sends an invoice
  ▼
FastAPI route validates its shape
  │
  ▼
Service saves the original claim and starts the workflow
  │
  ▼
Relationship graph finds carrier → contract → shipment → BOL
  │
  ▼
Pure rules recalculate dates, charges, tax, weight, and duplicates
  │
  ▼
Confidence scorer explains its penalties
  │
  ▼
Decision step ── safe result ──────────────► save final decision
  │
  └──────── needs a person ► save checkpoint ► review queue
                                               │
                                  reviewer acts│
                                               ▼
                                      resume and finalize
```

### 1. The browser UI

`app/static/index.html` contains the HTML, CSS, and JavaScript for the entire
screen. It uses the browser’s built-in `fetch` function to call the backend.
There is no Node.js build, React installation, or second web server.

On page load it calls:

- `GET /` to check the connection;
- `GET /seed-bills` to fill the sample picker;
- `GET /metrics` to fill the counters; and
- `GET /review-queue` to show waiting work.

### 2. FastAPI receives and validates the request

`app/main.py` creates the application, serves the UI, creates database tables at
startup, and loads reference data. It also supplies health, sample-data,
metrics, and demo-reset endpoints.

`app/schemas.py` describes exactly what a valid request and response looks like.
Pydantic rejects missing or malformed fields before business logic runs.

The route modules are intentionally small:

- `app/api/freight_bills.py` accepts and retrieves bills;
- `app/api/review.py` lists waiting bills and accepts human decisions.

### 3. The service coordinates the work

`app/services/freight_bill_service.py` is the conductor. It:

1. saves the invoice exactly as submitted;
2. records a “received” audit event;
3. builds the relationship graph;
4. starts the LangGraph workflow;
5. saves either a final decision or a proposed decision;
6. marks uncertain bills as waiting; and
7. later resumes a saved workflow with the reviewer’s answer.

`app/services/audit_service.py` appends timestamped events. Existing events are
never rewritten by normal processing.

### 4. The relationship graph finds matching records

The main database is relational, but matching is naturally a relationship
problem. `app/graph/builder.py` builds a small in-memory NetworkX graph:

```text
Carrier ──has──► Contract ──prices──► Lane
   │
   └──moved──► Shipment ──proved by──► Bill of Lading
                    │
                    └──uses──► Contract
```

`app/graph/matcher.py` walks those connections. It can expose several contracts
for the same lane, infer a shipment when exactly one sensible match exists, and
find every delivery document attached to a shipment. It finds candidates; it
does not decide whether money is correct.

NetworkX is sufficient because this demo graph is small and rebuilt from the
database. A separate graph server would add complexity without improving this
local application.

### 5. LangGraph runs the checks in a fixed order

`app/agent/workflow.py` wires the verification steps into this state machine:

```text
normalize
  → find candidates
  → validate contract
  → validate charges
  → validate weight and BOL
  → detect duplicate
  → compute confidence
  → decide
  → human review when needed
  → finalize
```

`app/agent/state.py` defines the shared information passed between those steps.
Each step adds structured issues and readable evidence. The order is explicit,
testable, and resumable.

The human-review step uses LangGraph’s `interrupt()`. A separate SQLite
checkpoint file stores the whole workflow state under the bill ID. Review uses
`Command(resume=...)` to continue the same run instead of pretending to start a
new one.

### 6. Deterministic rules do the money calculations

`app/agent/rules.py` contains pure Python functions: the same inputs always
produce the same outputs. No language model is allowed to decide money.

The important calculations are:

```text
expected base charge = billed weight × contract rate per kg
expected fuel charge = expected base × contract fuel percentage
expected GST         = (expected base + expected fuel) × 18%
expected total       = expected base + expected fuel + expected GST
```

The rules also support:

- a minimum charge;
- full-truck-load pricing and its permitted per-kg alternative;
- a fuel percentage revised part-way through a contract;
- 1% normal charge tolerance;
- a serious dispute when charge drift exceeds 25%;
- bill weight versus the BOL’s delivered weight;
- cumulative bill weight versus the shipment’s total weight; and
- duplicate carrier-and-invoice-number detection.

These settings live in `app/config.py` and may be overridden in a local `.env`
file. `.env.example` documents every option.

### 7. Confidence is explainable

`app/agent/confidence.py` starts at `1.0` (100%) and subtracts a fixed penalty
once per problem category:

| Problem category | Penalty |
| --- | ---: |
| Inferred shipment | 0.30 |
| Multiple overlapping contracts | 0.25 |
| Charge mismatch | 0.30 |
| Weight mismatch | 0.35 |
| Duplicate bill | 0.50 |
| Unknown carrier | 0.50 |
| No valid contract | 0.40 |
| Reconciled unit difference | 0.20 |

Decision routing is severity-first. For example, a proven duplicate is disputed
even if a different scoring arrangement might leave a moderate percentage. An
informational unit conversion can still auto-approve because it is not a fault.

### 8. Data is saved in two SQLite databases

`app/database.py` configures SQLAlchemy and opens/closes a session for each API
request. `app/models.py` defines the tables.

| Table | What it represents |
| --- | --- |
| `carriers` | Known transport companies. |
| `carrier_contracts` | Date-bound agreements with carriers. |
| `contract_rate_cards` | Prices for each contract and lane. |
| `shipments` | The actual movement of goods. |
| `bills_of_lading` | Delivery proof and actual weight. |
| `freight_bills` | The invoice exactly as submitted. |
| `agent_decisions` | Machine proposals and final decisions. |
| `human_reviews` | Reviewer choices and notes. |
| `audit_events` | The append-only event history. |

The files created at runtime are:

- `logistics.db` — business records and decisions;
- `agent_checkpoints.db` — paused workflow state.

They are local generated files and are intentionally excluded from Git.

`app/seed_loader.py` reads `seed_data.json`. It automatically inserts reference
records, but does not pre-insert freight bills: a bill must arrive through the UI
or API so the real processing path is exercised.

---

## Backend API

FastAPI provides interactive documentation at:

**http://127.0.0.1:8000/docs**

| Method and path | Purpose |
| --- | --- |
| `GET /` | Connection/health check. |
| `GET /seed-bills` | List editable sample invoices. |
| `POST /freight-bills` | Submit a sample ID or a complete invoice. |
| `GET /freight-bills/{id}` | Read a bill, decisions, evidence, and audit history. |
| `GET /review-queue` | List bills paused for a person. |
| `POST /review/{id}` | Submit a reviewer decision and resume the workflow. |
| `GET /metrics` | Return status, decision, and audit counts. |
| `POST /admin/reset` | Reset local demo data. |

Example: process the first sample from a second terminal while the server runs:

```bash
curl -X POST http://127.0.0.1:8000/freight-bills \
  -H 'Content-Type: application/json' \
  -d '{"bill_id":"FB-2025-101"}'
```

Example: see everything recorded for it:

```bash
curl http://127.0.0.1:8000/freight-bills/FB-2025-101
```

---

## Project map

```text
app/
├── main.py                       application startup and shared endpoints
├── config.py                     settings and business tolerances
├── database.py                   SQLAlchemy engine and sessions
├── models.py                     database tables
├── schemas.py                    API request/response validation
├── seed_loader.py                sample reference-data loader
├── api/
│   ├── freight_bills.py          submit/read bill endpoints
│   └── review.py                 human-review endpoints
├── services/
│   ├── freight_bill_service.py   persistence + workflow orchestration
│   └── audit_service.py          append-only audit recording
├── graph/
│   ├── builder.py                relationship graph construction
│   └── matcher.py                carrier/contract/shipment/BOL matching
├── agent/
│   ├── state.py                  shared typed workflow state
│   ├── workflow.py               ordered verification state machine
│   ├── rules.py                  deterministic bill calculations
│   ├── confidence.py             explainable score penalties
│   └── llm.py                    optional text/name helper with offline fallback
└── static/
    └── index.html                complete browser UI

tests/                             27 rule, confidence, and end-to-end tests
seed_data.json                     reference records and ten sample invoices
DEMO.md                            detailed presentation script
requirements.txt                  pinned Python packages
.env.example                      optional configuration template
```

---

## Troubleshooting

### The browser says “backend offline”

The terminal running Uvicorn must remain open. Check it for an error, then open
exactly **http://127.0.0.1:8000/ui/**.

### `python` or `python3` is not found

Install Python 3.11 or newer, close and reopen the terminal, and retry. On
Windows, use `py -3` as shown above.

### Port 8000 is already in use

Run on a different port:

```bash
python -m uvicorn app.main:app --reload --port 8001
```

Then open **http://127.0.0.1:8001/ui/**.

### Results differ after repeating the demo

Press **Reset demo** before **Run all 10 seeds**. Duplicate and cumulative
weight checks correctly remember earlier submissions.

### I changed `.env` but nothing happened

Stop the server with **Ctrl+C** and start it again. Settings are loaded when the
process starts.

---

## Important production note

This repository is a local demonstration, not a production payment system. It
deliberately has open CORS, no login, an exposed reset button, SQLite storage,
and a single-process workflow lock. Before real use, add authentication and
permissions, protect or remove the reset route, validate organisation-specific
tax/rate rules, move to managed durable storage, add monitoring and backups, and
complete a security review.

The optional LLM helper is disabled by default. Even when enabled, it is only a
seam for text normalization and explanations; contract selection, money,
confidence, and final routing remain deterministic Python logic.
