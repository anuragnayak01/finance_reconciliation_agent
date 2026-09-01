"""Builds the final report: overall + per-vendor match rate, both
record-level and amount-level, per the design decision that record count
alone can hide a small number of high-value unresolved records."""

from collections import defaultdict


def build_report(pipeline_result):
    outcomes = pipeline_result["record_outcomes"]
    transactions = pipeline_result["transactions"]
    invoices = pipeline_result["invoices"]

    amount_by_id = {t["txn_id"]: float(t["amount"]) for t in transactions}
    amount_by_id.update({i["invoice_id"]: float(i["amount"]) for i in invoices})
    vendor_by_id = {t["txn_id"]: t.get("vendor_id") for t in transactions}
    vendor_by_id.update({i["invoice_id"]: i.get("vendor_id") for i in invoices})

    per_vendor = defaultdict(lambda: {"total": 0, "reconciled": 0, "not_reconciled": 0,
                                       "info_missing": 0, "total_amount": 0.0, "reconciled_amount": 0.0})

    overall = {"total": 0, "reconciled": 0, "not_reconciled": 0, "info_missing": 0,
               "total_amount": 0.0, "reconciled_amount": 0.0}

    for record_id, outcome in outcomes.items():
        amt = amount_by_id.get(record_id, 0.0)
        vendor_id = outcome["vendor_id"] or vendor_by_id.get(record_id) or "UNRESOLVED_VENDOR"
        cls = outcome["classification"]

        for bucket in (overall, per_vendor[vendor_id]):
            bucket["total"] += 1
            bucket["total_amount"] += amt
            if cls == "RECONCILED":
                bucket["reconciled"] += 1
                bucket["reconciled_amount"] += amt
            elif cls == "NOT_RECONCILED":
                bucket["not_reconciled"] += 1
            else:
                bucket["info_missing"] += 1

    def pct(numerator, denominator):
        return round(100 * numerator / denominator, 1) if denominator else 0.0

    overall_summary = {
        **overall,
        "record_match_rate_pct": pct(overall["reconciled"], overall["total"]),
        "amount_match_rate_pct": pct(overall["reconciled_amount"], overall["total_amount"]),
    }

    per_vendor_summary = {}
    for vendor_id, stats in per_vendor.items():
        per_vendor_summary[vendor_id] = {
            **stats,
            "record_match_rate_pct": pct(stats["reconciled"], stats["total"]),
            "amount_match_rate_pct": pct(stats["reconciled_amount"], stats["total_amount"]),
        }

    return {
        "overall": overall_summary,
        "per_vendor": per_vendor_summary,
        "exception_count": len(pipeline_result["exceptions"]),
    }


def print_report(report):
    o = report["overall"]
    print("=" * 70)
    print("OVERALL RECONCILIATION SUMMARY")
    print("=" * 70)
    print(f"Total records          : {o['total']}")
    print(f"Reconciled              : {o['reconciled']}")
    print(f"Not Reconciled          : {o['not_reconciled']}")
    print(f"Info Missing            : {o['info_missing']}")
    print(f"Record Match Rate       : {o['record_match_rate_pct']}%")
    print(f"Amount Match Rate       : {o['amount_match_rate_pct']}%  "
          f"(₹{o['reconciled_amount']:,.0f} of ₹{o['total_amount']:,.0f})")
    print()
    print("-" * 70)
    print(f"{'Vendor':<10}{'Total':>7}{'Recon':>7}{'NotRec':>8}{'InfoMiss':>10}{'RecMatch%':>11}{'AmtMatch%':>11}")
    print("-" * 70)
    for vendor_id, stats in sorted(report["per_vendor"].items()):
        print(f"{vendor_id:<10}{stats['total']:>7}{stats['reconciled']:>7}{stats['not_reconciled']:>8}"
              f"{stats['info_missing']:>10}{stats['record_match_rate_pct']:>10}%{stats['amount_match_rate_pct']:>10}%")
    print("-" * 70)
