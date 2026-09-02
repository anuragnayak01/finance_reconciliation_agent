"""
Entry point. Run with:

    python3 run_agent.py transactions.csv invoices.csv

By default this runs OFFLINE (no LLM call) using the deterministic fallback
in vendor_resolution.py, so it can be executed without API access. To use a
real LLM (via Groq's free tier) for vendor resolution, set the GROQ_API_KEY
environment variable (get one free at https://console.groq.com/keys) and
pass --online, or import make_groq_llm_call_fn directly:

    from vendor_resolution import make_groq_llm_call_fn
    llm_call_fn = make_groq_llm_call_fn()  # requires GROQ_API_KEY env var
    result = run_pipeline(txn_path, inv_path, llm_call_fn=llm_call_fn)
"""

import sys
import json
from pipeline import run_pipeline
from report import build_report, print_report
from vendor_resolution import make_groq_llm_call_fn


def main():
    txn_path = sys.argv[1] if len(sys.argv) > 1 else "transactions.csv"
    inv_path = sys.argv[2] if len(sys.argv) > 2 else "invoices.csv"
    use_online = "--online" in sys.argv

    llm_call_fn = make_groq_llm_call_fn() if use_online else None
    result = run_pipeline(txn_path, inv_path, llm_call_fn=llm_call_fn)
    report = build_report(result)

    print_report(report)

    print(f"\nVendor groups needing human review before reconciliation: {len(result['needs_review_groups'])}")
    for g in result["needs_review_groups"]:
        print(f"  - {g['names']}: {g['reason']}")

    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    with open("exceptions.json", "w", encoding="utf-8") as f:
        json.dump(result["exceptions"], f, indent=2, default=str)

    import csv
    with open("reconciliation_results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["record_id", "record_type", "vendor_id", "classification", "matched_ids", "edge_case_tag", "reason"])
        for record_id, outcome in sorted(result["record_outcomes"].items()):
            writer.writerow([record_id, outcome["type"], outcome["vendor_id"], outcome["classification"],
                              ";".join(outcome["matched_ids"]), outcome["edge_case_tag"], outcome["reason"]])

    print("\nWrote report.json, exceptions.json, reconciliation_results.csv")


if __name__ == "__main__":
    main()
