"""Orchestrates the full agent flow, vendor group by vendor group."""

from collections import defaultdict
from utils import parse_date
from vendor_resolution import resolve_vendors
from reconciliation import detect_duplicates, reference_group_match, subset_sum_allocation, pairwise_scorer_match


def load_rows(path, date_field):
    import csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["_date"] = parse_date(row[date_field])
            rows.append(row)
    return rows


def run_pipeline(transactions_path, invoices_path, llm_call_fn=None):
    transactions = load_rows(transactions_path, "txn_date")
    invoices = load_rows(invoices_path, "invoice_date")

    # --- Stage A: vendor identity resolution (the only LLM-touching step) ---
    vendor_map, needs_review_groups = resolve_vendors(transactions, invoices, llm_call_fn=llm_call_fn)

    exceptions = []          # every non-RECONCILED record, with full reasoning
    record_outcomes = {}     # record_id -> {"classification", "matched_ids", "reason", "type", "vendor_id"}

    needs_review_names = {n for g in needs_review_groups for n in g["names"]}
    for group in needs_review_groups:
        for name in group["names"]:
            for t in transactions:
                if t["raw_vendor_name"] == name:
                    record_outcomes[t["txn_id"]] = dict(
                        type="transaction", vendor_id=None, classification="INFO_MISSING",
                        matched_ids=[], edge_case_tag="vendor_needs_review",
                        reason=f"Vendor identity unresolved: {group['names']} -- {group['reason']}",
                    )
            for i in invoices:
                if i["raw_vendor_name"] == name:
                    record_outcomes[i["invoice_id"]] = dict(
                        type="invoice", vendor_id=None, classification="INFO_MISSING",
                        matched_ids=[], edge_case_tag="vendor_needs_review",
                        reason=f"Vendor identity unresolved: {group['names']} -- {group['reason']}",
                    )

    for t in transactions:
        t["vendor_id"] = vendor_map.get(t["raw_vendor_name"])
    for i in invoices:
        i["vendor_id"] = vendor_map.get(i["raw_vendor_name"])

    txns_by_vendor = defaultdict(list)
    invs_by_vendor = defaultdict(list)
    for t in transactions:
        if t["vendor_id"]:
            txns_by_vendor[t["vendor_id"]].append(t)
    for i in invoices:
        if i["vendor_id"]:
            invs_by_vendor[i["vendor_id"]].append(i)

    all_vendor_ids = set(txns_by_vendor) | set(invs_by_vendor)

    for vendor_id in sorted(all_vendor_ids):
        vt = txns_by_vendor.get(vendor_id, [])
        vi = invs_by_vendor.get(vendor_id, [])

        # --- Stage B.1: duplicate detection, before any matching ---
        active_txns, dup_txns = detect_duplicates(vt, "txn_id", "_date")
        active_invs, dup_invs = detect_duplicates(vi, "invoice_id", "_date")

        for d in dup_txns:
            record_outcomes[d["record"]["txn_id"]] = dict(
                type="transaction", vendor_id=vendor_id, classification="NOT_RECONCILED",
                matched_ids=[d["duplicate_of"]], edge_case_tag="duplicate_transaction_suspected",
                reason=d["reason"],
            )
        for d in dup_invs:
            record_outcomes[d["record"]["invoice_id"]] = dict(
                type="invoice", vendor_id=vendor_id, classification="NOT_RECONCILED",
                matched_ids=[d["duplicate_of"]], edge_case_tag="duplicate_invoice_suspected",
                reason=d["reason"],
            )

        remaining_txns, remaining_invs = list(active_txns), list(active_invs)

        # --- Stage B.2: reference-exact grouping (1:1, many:1, 1:many) ---
        ref_results, matched_t, matched_i = reference_group_match(remaining_txns, remaining_invs)
        for rtype, rid, classification, matched_ids, reason in ref_results:
            record_outcomes[rid] = dict(type=rtype, vendor_id=vendor_id, classification=classification,
                                         matched_ids=matched_ids, edge_case_tag="reference_grouped", reason=reason)
        remaining_txns = [t for t in remaining_txns if t["txn_id"] not in matched_t]
        remaining_invs = [i for i in remaining_invs if i["invoice_id"] not in matched_i]

        # --- Stage B.3: amount-only subset-sum allocation (reference-absent only) ---
        alloc_results, matched_t2, matched_i2 = subset_sum_allocation(
            remaining_txns, remaining_invs, "_date", "_date", "txn_id", "invoice_id")
        for rtype, rid, classification, matched_ids, reason in alloc_results:
            record_outcomes[rid] = dict(type=rtype, vendor_id=vendor_id, classification=classification,
                                         matched_ids=matched_ids, edge_case_tag="amount_allocation", reason=reason)
        remaining_txns = [t for t in remaining_txns if t["txn_id"] not in matched_t2]
        remaining_invs = [i for i in remaining_invs if i["invoice_id"] not in matched_i2]

        # --- Stage B.4: pairwise shared-scorer fallback ---
        pw_results, matched_t3, matched_i3 = pairwise_scorer_match(remaining_txns, remaining_invs)
        for rtype, rid, classification, matched_ids, reason in pw_results:
            record_outcomes[rid] = dict(type=rtype, vendor_id=vendor_id, classification=classification,
                                         matched_ids=matched_ids, edge_case_tag="pairwise_scored", reason=reason)

    # Any transaction/invoice whose vendor was never assigned at all (should
    # not happen given needs_review handling above, but guarded defensively)
    for t in transactions:
        record_outcomes.setdefault(t["txn_id"], dict(
            type="transaction", vendor_id=None, classification="INFO_MISSING",
            matched_ids=[], edge_case_tag="unresolved_vendor", reason="Vendor could not be resolved."))
    for i in invoices:
        record_outcomes.setdefault(i["invoice_id"], dict(
            type="invoice", vendor_id=None, classification="INFO_MISSING",
            matched_ids=[], edge_case_tag="unresolved_vendor", reason="Vendor could not be resolved."))

    # Build exceptions for everything not RECONCILED
    amount_by_id = {t["txn_id"]: float(t["amount"]) for t in transactions}
    amount_by_id.update({i["invoice_id"]: float(i["amount"]) for i in invoices})
    date_by_id = {t["txn_id"]: t["txn_date"] for t in transactions}
    date_by_id.update({i["invoice_id"]: i["invoice_date"] for i in invoices})
    vendor_display = {**{t["txn_id"]: t.get("vendor_id") for t in transactions},
                       **{i["invoice_id"]: i.get("vendor_id") for i in invoices}}

    for record_id, outcome in record_outcomes.items():
        if outcome["classification"] != "RECONCILED":
            exceptions.append({
                "vendor_id": outcome["vendor_id"] or vendor_display.get(record_id),
                "record_id": record_id,
                "record_type": outcome["type"],
                "exception_type": outcome["classification"],
                "amount": amount_by_id.get(record_id),
                "date": str(date_by_id.get(record_id)) if date_by_id.get(record_id) else None,
                "candidate_matches": outcome["matched_ids"],
                "edge_case_tag": outcome["edge_case_tag"],
                "reason": outcome["reason"],
                "recommended_action": _recommend_action(outcome),
            })

    return {
        "transactions": transactions,
        "invoices": invoices,
        "record_outcomes": record_outcomes,
        "exceptions": exceptions,
        "needs_review_groups": needs_review_groups,
        "vendor_map": vendor_map,
    }


def _recommend_action(outcome):
    tag = outcome["edge_case_tag"]
    if tag == "vendor_needs_review":
        return "Verify vendor master / tax ID to confirm whether these names represent one vendor or several."
    if "duplicate" in tag:
        return "Confirm with vendor whether this is a genuine duplicate invoice or a distinct charge."
    if outcome["classification"] == "NOT_RECONCILED":
        return "Review remittance advice / contact vendor to resolve the amount or reference discrepancy."
    return "Review candidate matches manually; available evidence is insufficient to auto-resolve."
