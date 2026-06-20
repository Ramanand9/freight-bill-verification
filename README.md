# Freight Bill Verification

This is a small backend project I built to show how freight bills can be checked before a company pays them.

A freight bill is basically an invoice sent by a transport company after moving goods from one place to another. In a real logistics team, someone has to compare that bill with the agreed contract, shipment details, delivery proof, weight, and charges. Doing all of that by hand takes time, and it is easy to miss a duplicate bill or an incorrect amount.

My project does those checks automatically and keeps a clear record of how it reached the result.

## What the project does

When a freight bill comes in, the system:

1. Finds the transport company that sent it.
2. Looks for the contract and price agreed with that company.
3. Connects the bill to the correct shipment and Bill of Lading (the delivery record).
4. Checks the weight, route, dates, tax, fuel charge, and final amount.
5. Checks whether the same invoice has already been submitted.
6. Gives the bill a confidence score and suggests what should happen next.

The result can be:

- **Auto approve** – everything looks correct.
- **Flag for review** – something looks unusual and a person should check it.
- **Dispute** – there is a serious mismatch, such as overbilling or a duplicate invoice.
- **Human review** – the system does not have enough information to make a safe decision.

The important part is that the program does not quietly guess when it is unsure. It pauses and places the bill in a review queue. A person can then approve it, dispute it, or correct it. The system also saves the reasons and evidence behind every decision.

## A simple example

Imagine a carrier submits a bill for 1,200 kg on the Bengaluru-to-Chennai route.

The system finds the matching shipment and contract, checks the actual delivered weight, applies the agreed rate and fuel charge, calculates GST, and compares its expected total with the carrier's total. If they match, the bill can be approved. If the carrier charged for more weight than was delivered, the bill is disputed. If two contracts could apply, the bill is sent to a person instead of making a risky choice.

## How I approached it

I divided the work into a few clear parts:

- The **API** receives bills and shows their results.
- The **database** stores carriers, contracts, shipments, delivery records, bills, reviews, and the decision history.
- A **matching step** connects each bill with the most likely carrier, contract, shipment, and delivery record.
- A **rules step** performs the important calculations. The financial decision is based on fixed rules, not on an AI making up an answer.
- A **workflow** runs the checks in order. It can stop for a human review and continue later after a reviewer responds.
- An **audit trail** keeps a history of what happened, which is useful if someone asks why a bill was approved or disputed.

I used FastAPI for the backend, SQLite for local storage, NetworkX for connecting related records, and LangGraph for the step-by-step workflow. The project works locally without any paid API key.

## How to run the backend

You will need Python installed on your computer. Python 3.10 or newer is recommended.

Open a terminal in this project folder and run the following commands.

### 1. Create a virtual environment

This gives the project its own private set of Python packages.

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 2. Install the required packages

```bash
pip install -r requirements.txt
```

### 3. Start the backend

```bash
uvicorn app.main:app --reload --port 8000
```

Once it starts, open this page in a browser:

**http://localhost:8000/docs**

That page is an interactive screen where you can try the backend without building a separate website.

The first startup creates the local database and loads sample carriers, contracts, shipments, and delivery records automatically.

## A quick demo

With the backend running, open **http://localhost:8000/docs** and try these steps:

1. Open `POST /freight-bills`.
2. Click **Try it out**.
3. Use this sample input:

```json
{
  "bill_id": "FB-2025-101"
}
```

4. Click **Execute**. This is a clean sample bill, so it should be automatically approved.
5. Try `FB-2025-110` next. This bill has an unknown carrier, so the system should pause it for human review.
6. Open `GET /review-queue` to see bills waiting for a person.
7. Use `POST /review/{bill_id}` to submit the person's decision and finish the process.

You can also use `GET /freight-bills/{bill_id}` to see the bill, the checks performed, the decision history, and the audit trail.

## Running the tests

The project includes automated tests for correct bills, duplicate bills, overbilling, unusual rates, and the human review process.

```bash
pytest -q
```

## Main project folders

- `app/api` contains the backend endpoints.
- `app/agent` contains the workflow and checking rules.
- `app/graph` contains the matching logic.
- `app/services` connects the API, workflow, and database.
- `tests` contains the automated tests.
- `seed_data.json` contains the sample logistics data used for the demo.

The earlier, more technical version of this documentation is still available in [`README_old.md`](README_old.md).
