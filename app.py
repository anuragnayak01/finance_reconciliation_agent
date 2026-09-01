"""
FastAPI backend for the finance reconciliation agent.

Wraps the existing pipeline (pipeline.py / vendor_resolution.py /
reconciliation.py) behind a small HTTP API so a frontend (e.g. the
dashboard in frontend/index.html, deployed on Vercel) can upload
transactions/invoices CSVs and get a reconciliation report back.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload

Deployed on Render with:
    uvicorn app:app --host 0.0.0.0 --port $PORT
"""

import os
import csv
import io
import tempfile
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pipeline import run_pipeline
from report import build_report
from vendor_resolution import make_grok_llm_call_fn

app = FastAPI(title="Finance Reconciliation Agent API")

# CORS: kept permissive here because the recommended deploy pattern below
# uses a Vercel rewrite proxy (frontend/vercel.json) so the browser calls
# same-origin and CORS never actually applies in production. This still
# allows direct cross-origin calls (e.g. testing the Render URL directly).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

REQUIRED_TXN_COLUMNS = {"txn_id", "txn_date", "raw_vendor_name", "amount", "reference"}
REQUIRED_INV_COLUMNS = {"invoice_id", "invoice_date", "raw_vendor_name", "amount", "reference"}


def _validate_csv(file_bytes: bytes, required_columns: set, label: str):
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(400, f"{label}: file is empty or not valid CSV.")
    missing = required_columns - set(reader.fieldnames)
    if missing:
        raise HTTPException(
            400,
            f"{label}: missing required column(s): {sorted(missing)}. "
            f"Found columns: {reader.fieldnames}",
        )
    return text


@app.get("/api/health")
def health():
    return {"status": "ok", "grok_configured": bool(os.environ.get("XAI_API_KEY"))}


@app.post("/api/reconcile")
async def reconcile(
    transactions: UploadFile = File(...),
    invoices: UploadFile = File(...),
    mode: str = Form("offline"),  # "offline" | "online"
):
    if mode not in ("offline", "online"):
        raise HTTPException(400, "mode must be 'offline' or 'online'")

    txn_bytes = await transactions.read()
    inv_bytes = await invoices.read()

    txn_text = _validate_csv(txn_bytes, REQUIRED_TXN_COLUMNS, "transactions.csv")
    inv_text = _validate_csv(inv_bytes, REQUIRED_INV_COLUMNS, "invoices.csv")

    llm_call_fn = None
    if mode == "online":
        if not os.environ.get("XAI_API_KEY"):
            raise HTTPException(
                400,
                "mode=online requires XAI_API_KEY to be set on the server.",
            )
        llm_call_fn = make_grok_llm_call_fn()

    with tempfile.TemporaryDirectory() as tmp:
        txn_path = os.path.join(tmp, "transactions.csv")
        inv_path = os.path.join(tmp, "invoices.csv")
        with open(txn_path, "w", encoding="utf-8", newline="") as f:
            f.write(txn_text)
        with open(inv_path, "w", encoding="utf-8", newline="") as f:
            f.write(inv_text)

        try:
            result = run_pipeline(txn_path, inv_path, llm_call_fn=llm_call_fn)
        except Exception as exc:
            raise HTTPException(500, f"Pipeline failed: {exc}") from exc

        report = build_report(result)

    return {
        "mode": mode,
        "report": report,
        "needs_review_groups": result["needs_review_groups"],
        "exceptions": result["exceptions"],
        "vendor_map_size": len(result["vendor_map"]),
    }
