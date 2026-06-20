# DEMO.md — Freight Bill Verification, live walkthrough

A second-monitor checklist for demoing the project. Primary path is the **web console**;
a terminal fallback is at the bottom in case the eval machine has no browser.

**Golden rule:** start from a clean database in ascending order. Duplicate detection and
cumulative-weight checks are intentionally arrival-ordered, so a stale DB or reshuffled order
moves which bill carries a flag. The **Reset demo** button (or `rm -f *.db`) puts you back to a
known state any time.

---

## 0 · Before they arrive (one-time)

```bash
cd logistics_backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # expect: 27 passed
```

Leave the server running and the console open:

```bash
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/ui/** — the status dot (top-right) should be green.
Have the Swagger docs at **http://127.0.0.1:8000/docs** open in a second tab as backup.

---

## 1 · Framing (~30 sec, say this first)

> "A carrier invoice is an untrusted claim. This backend matches each bill against contracts,
> rate cards, and proof-of-delivery, runs deterministic rules, and reaches one of four
> decisions — auto-approve, flag, dispute, or escalate to a human. The interesting part is a
> real human-in-the-loop pause built on LangGraph that survives a server restart. Let me show
> it on ten seed scenarios."

Click **Reset demo** so the metric chips read all zeros.

---

## 2 · The spread (one click)

Click **Run all 10 seeds**. The results table fills in:

| Bill        | Decision        | Conf | Why (headline)                             |
| ----------- | --------------- | ---- | ------------------------------------------ |
| FB-2025-101 | Auto approve    | 100% | clean — everything reconciles              |
| FB-2025-102 | Flag for review | 45%  | no shipment ref + overlapping contracts    |
| FB-2025-103 | Auto approve    | 100% | partial delivery, legitimately under total |
| FB-2025-104 | Dispute         | 65%  | billed > delivered AND > shipment total    |
| FB-2025-105 | Flag for review | 70%  | rate drift vs contract                     |
| FB-2025-106 | Human review    | 30%  | billed on an expired contract's rate       |
| FB-2025-107 | Auto approve    | 80%  | FTL/per-kg reconciliation (info only)      |
| FB-2025-108 | Auto approve    | 100% | fuel-surcharge revision correctly applied  |
| FB-2025-109 | Dispute         | 15%  | duplicate of 101                           |
| FB-2025-110 | Human review    | 50%  | unknown carrier, no contract on file       |

> "Six resolved automatically — four approvals, two disputes — and four parked for a human.
> Nothing it was unsure about got auto-anything."

The metric chips now show 6 auto-resolved, 4 in review.

---

## 3 · Cause and effect (the part the UI unlocks)

This beats any static list — show the engine _reacting_.

1. Left picker → **FB-2025-101**. It verifies as a green **Auto approve**, 100%, zero issues.
2. In the payload box, change `"billed_weight_kg": 850` to `5000`.
3. Click **Verify bill**.

The stamp flips to red **Dispute**, confidence drops, a `weight_overbilling_vs_bol` issue
appears with its evidence line.

> "Same bill, one field changed. Nothing is hard-coded per scenario — it recomputes against the
> contract and the delivery proof every time."

(Optional second tweak: set `rate_per_kg` well above contract to show a charge-drift flag.)

---

## 4 · The four parked bills + human-in-the-loop

Scroll to **Review queue**.

1. Click **FB-2025-106**'s id → the Evidence chain (right) loads. Read the evidence line: billed
   rate matches an **expired** contract while an active one exists, so it refused to pick a
   contract and escalated.
2. In its note field type `stale TCI rate`, click **Dispute**.
3. The queue shrinks; the chain now shows two timeline nodes —
   **Human review (proposed) → Dispute (final)** — and the audit trail reads
   `received → paused_for_review → review_resolved`.

> "Nothing is overwritten. Every bill keeps the agent's proposal, the human's final call, and a
> timestamped append-only audit log. That's what makes a dispute defensible."

Resolve the others to taste: **FB-2025-110** → Approve ("onboard spot carrier"),
**FB-2025-102** / **FB-2025-105** → your call.

---

## 5 · Durability proof (optional, lands hard)

After ingesting but _before_ resolving a queued bill:

1. `Ctrl-C` the uvicorn process.
2. Restart: `uvicorn app.main:app --reload`.
3. Reload `/ui/`, open the queue, resolve the bill — it still completes.

> "The pause wasn't a status flag. The whole workflow state was checkpointed to SQLite
> mid-execution, so it resumes across a restart."

---

## 6 · Close on metrics

Point at the header chips: ten in, six auto-resolved, four reviewed, full audit coverage.

> "The system absorbs the easy volume and routes only genuine ambiguity to a person. That ~60%
> auto-clear rate is the business value."

---

## Likely questions — short answers

- **Why doesn't confidence decide routing?** The assignment's penalty table and confidence
  bands contradict each other on this seed data (FB-107 scores 0.80 yet must approve; FB-102
  scores 0.45 yet must only flag). So decisions are _severity-first_; confidence is the
  triage/trust signal and a sub-0.60 safety net.
- **Is it deterministic?** Yes, given a clean DB and ascending order. Duplicate and
  cumulative-weight checks are arrival-ordered by design — that mirrors how AP teams approve
  invoices as they land. The Reset button restores a known state.
- **Why NetworkX, not Neo4j?** Small, read-mostly graph rebuilt from SQL each run; no second
  datastore to operate. The matcher is isolated, so it can be swapped if it ever needs scale.
- **Where would an LLM go?** Carrier-name normalization and evidence summaries — the seams are
  in `app/agent/llm.py`, stubbed deterministically so the system runs offline.

---

## Terminal fallback (no browser)

```bash
rm -f logistics.db agent_checkpoints.db*          # clean slate
uvicorn app.main:app --reload                      # in one terminal

# in a second terminal:
for n in 01 02 03 04 05 06 07 08 09 10; do
  printf "FB-2025-1%s -> " $n
  curl -s -X POST http://127.0.0.1:8000/freight-bills \
    -H 'Content-Type: application/json' -d "{\"bill_id\":\"FB-2025-1$n\"}" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print('%-18s %-16s conf=%s'%(d['status'],d['decision'],d['confidence']))"
done

curl -s http://127.0.0.1:8000/review-queue | python3 -m json.tool

curl -s -X POST http://127.0.0.1:8000/review/FB-2025-106 \
  -H 'Content-Type: application/json' \
  -d '{"reviewer_decision":"dispute","reviewer_notes":"Stale TCI rate."}' | python3 -m json.tool

curl -s http://127.0.0.1:8000/freight-bills/FB-2025-106 | python3 -m json.tool   # evidence chain
curl -s http://127.0.0.1:8000/metrics | python3 -m json.tool
```

---

## Quick reference

| Endpoint                  | Purpose                                          |
| ------------------------- | ------------------------------------------------ | ------- | -------------------------------- |
| `GET /ui/`                | the console                                      |
| `GET /docs`               | Swagger / OpenAPI                                |
| `POST /freight-bills`     | ingest `{"bill_id": "..."}` or `{"bill": {...}}` |
| `GET /freight-bills/{id}` | full evidence chain + audit                      |
| `GET /review-queue`       | bills awaiting a human                           |
| `POST /review/{id}`       | `{"reviewer_decision":"approve                   | dispute | modify","reviewer_notes":"..."}` |
| `GET /metrics`            | counts by status / decision                      |
| `GET /seed-bills`         | seed payloads (powers the picker)                |
| `POST /admin/reset`       | wipe bills, keep reference data                  |
