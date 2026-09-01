"""
Stage B of the pipeline: everything after Vendor_ID is deterministic, per
the design decision that the LLM's involvement should stop at vendor
identity resolution.

Pipeline inside each vendor group, in order:
  1. Duplicate detection (identical vendor+amount+date+reference invoices/
     transactions) -- removes suspected duplicates from the active pool
     BEFORE any matching happens, so they can't corrupt sum-based matching.
  2. Reference-exact grouping -- covers 1:1, many:1, and 1:many cases where
     both sides share an exact reference. This is where partial payments
     and full multi-payment allocations are resolved, since the reference
     is what proves they belong together.
  3. Amount-only subset-sum allocation -- ONLY for records with NO reference
     on that side (a record that has a reference but didn't match one in
     step 2 must not be silently allowed to match on amount alone; that
     would defeat the point of the reference-contradiction veto).
  4. Pairwise shared-scorer matching -- the general-purpose fallback for
     everything else: fuzzy references, tolerance-only variances,
     reference-missing-on-one-side cases, and reference contradictions.

Every step feeds the same decide() function so the final classification
logic lives in exactly one place.
"""

import itertools
from utils import normalize_reference, days_between
from scoring import score_pair
from config import MATCH_THRESHOLD, MIN_SCORE_GAP, MAX_ALLOCATION_GROUP_SIZE, AMOUNT_TOLERANCE_ABS, AMOUNT_TOLERANCE_PCT


def decide(best_score, second_best_score, has_candidates, has_veto, has_genuine_discrepancy):
    """The single decision function agreed on in the design phase.
    Kept as a pure function (no side effects) so it's independently testable.
    """
    if has_veto:
        return "NOT_RECONCILED"

    if not has_candidates:
        return "NOT_RECONCILED" if has_genuine_discrepancy else "INFO_MISSING"

    if best_score >= MATCH_THRESHOLD and (second_best_score is None or best_score - second_best_score >= MIN_SCORE_GAP):
        return "RECONCILED"

    if has_genuine_discrepancy:
        return "NOT_RECONCILED"

    return "INFO_MISSING"


def amount_within_tolerance(a, b):
    diff = abs(a - b)
    tolerance = max(AMOUNT_TOLERANCE_ABS, AMOUNT_TOLERANCE_PCT * max(a, b))
    return diff <= tolerance, diff


def detect_duplicates(records, id_field, date_field):
    """records: list of dicts already scoped to one vendor. Groups by
    (amount, normalized reference, date) and keeps the lowest-id record as
    active; the rest are flagged as suspected duplicates and pulled out of
    the matching pool entirely (never auto-deleted -- just excluded and
    reported)."""
    groups = {}
    for r in records:
        key = (round(float(r["amount"]), 2), normalize_reference(r.get("reference")), r[date_field])
        groups.setdefault(key, []).append(r)

    active, duplicates = [], []
    for key, group in groups.items():
        if len(group) == 1 or not key[1]:  # no grouping on blank references
            if len(group) == 1:
                active.extend(group)
            else:
                active.extend(group)  # blank-reference collisions are NOT treated as duplicates
            continue
        group_sorted = sorted(group, key=lambda r: r[id_field])
        active.append(group_sorted[0])
        for dup in group_sorted[1:]:
            duplicates.append({
                "record": dup,
                "reason": f"Identical vendor/amount/date/reference to {group_sorted[0][id_field]}; "
                          f"no separate payment found to justify a second record.",
                "duplicate_of": group_sorted[0][id_field],
            })
    return active, duplicates


def reference_group_match(txns, invoices):
    """Groups transactions and invoices that share an exact normalized
    reference (present on both sides). Resolves 1:1, many:1, and 1:many
    cases by comparing summed amounts. Returns (results, matched_txn_ids,
    matched_inv_ids) where results is a list of per-group outcome dicts."""
    groups = {}
    for t in txns:
        ref = normalize_reference(t.get("reference"))
        if ref:
            groups.setdefault(ref, {"txns": [], "invoices": []})["txns"].append(t)
    for i in invoices:
        ref = normalize_reference(i.get("reference"))
        if ref and ref in groups:
            groups[ref]["invoices"].append(i)

    results = []
    matched_txn_ids, matched_inv_ids = set(), set()

    for ref, members in groups.items():
        ts, ivs = members["txns"], members["invoices"]
        if not ts or not ivs:
            continue  # reference only appears on one side -> leave for pairwise fallback

        txn_sum = sum(float(t["amount"]) for t in ts)
        inv_sum = sum(float(i["amount"]) for i in ivs)
        t_ids = [t["txn_id"] for t in ts]
        i_ids = [i["invoice_id"] for i in ivs]

        if abs(txn_sum - inv_sum) < 0.01:
            classification = "RECONCILED"
            reason = "Reference-confirmed link; combined amounts match exactly."
            for t in ts:
                results.append(("transaction", t["txn_id"], classification, i_ids, reason))
            for i in ivs:
                results.append(("invoice", i["invoice_id"], classification, t_ids, reason))
            matched_txn_ids.update(t_ids)
            matched_inv_ids.update(i_ids)
            continue

        within_tol, diff = amount_within_tolerance(txn_sum, inv_sum)
        if within_tol:
            classification = "RECONCILED"
            reason = f"Reference-confirmed link; {diff:.2f} variance within configured tolerance (fee/FX-style)."
            for t in ts:
                results.append(("transaction", t["txn_id"], classification, i_ids, reason))
            for i in ivs:
                results.append(("invoice", i["invoice_id"], classification, t_ids, reason))
            matched_txn_ids.update(t_ids)
            matched_inv_ids.update(i_ids)
            continue

        # Genuine, reference-confirmed amount gap -- partial payment or
        # unexplained shortfall; either way it's a known, real discrepancy.
        remaining = inv_sum - txn_sum
        classification = "NOT_RECONCILED"
        direction = "underpaid" if remaining > 0 else "overpaid"
        reason = (f"Reference confirms the link, but combined amounts differ by {abs(remaining):.2f} "
                  f"({direction}); no other record explains the gap.")
        for t in ts:
            results.append(("transaction", t["txn_id"], classification, i_ids, reason))
        for i in ivs:
            results.append(("invoice", i["invoice_id"], classification, t_ids, reason))
        matched_txn_ids.update(t_ids)
        matched_inv_ids.update(i_ids)

    return results, matched_txn_ids, matched_inv_ids


def _combo_score(members, target_date):
    """Scores a candidate subset-sum combination: exact-sum amount evidence
    plus average date proximity across members. Used only to rank competing
    equal-sum combinations against each other -- never to justify a sum
    that doesn't exactly match."""
    from scoring import date_signal
    date_pts = [date_signal(target_date, m["_date"])[1] for m in members]
    avg_date_pts = sum(date_pts) / len(date_pts) if date_pts else 0
    return 30 + avg_date_pts  # 30 = amount_exact weight for the whole combo


def subset_sum_allocation(anchor_records, pool_records, anchor_date_field, pool_date_field, anchor_id_field, pool_id_field):
    """For each anchor record (e.g. a transaction) with NO reference, look
    for combinations of pool records (e.g. invoices, also reference-absent)
    whose amounts sum exactly to the anchor's amount. Returns
    (results, matched_anchor_ids, matched_pool_ids)."""
    results = []
    matched_anchor_ids, matched_pool_ids = set(), set()
    # NOTE: the pool side is NOT filtered by reference presence. The
    # contradiction veto only applies when BOTH sides present a reference
    # that disagrees; here the anchor has none (checked below), so a pool
    # record having its own per-line reference is not a contradiction --
    # it's simply not being used as the matching key for this allocation.
    available_pool = list(pool_records)

    for anchor in anchor_records:
        if anchor.get("reference"):
            continue  # has a reference -> must go through reference-group or pairwise, not blind sum
        target = round(float(anchor["amount"]), 2)
        candidates_left = [p for p in available_pool if p[pool_id_field] not in matched_pool_ids]

        valid_combos = []
        for size in range(1, min(MAX_ALLOCATION_GROUP_SIZE, len(candidates_left)) + 1):
            for combo in itertools.combinations(candidates_left, size):
                if abs(sum(float(c["amount"]) for c in combo) - target) < 0.01:
                    valid_combos.append(combo)

        if not valid_combos:
            continue

        scored = []
        for combo in valid_combos:
            members = [{"_date": c[pool_date_field]} for c in combo]
            scored.append((combo, _combo_score(members, anchor[anchor_date_field])))
        scored.sort(key=lambda x: -x[1])

        best_combo, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else None

        # An exact-sum match is itself the strong evidence here -- it plays
        # the same role a reference match does in the reference-grouped
        # tier. So a UNIQUE combo is treated as reconciled outright, rather
        # than being held to MATCH_THRESHOLD (which was calibrated for full
        # reference+amount+date+identifier scoring, a different scale than
        # this amount+date-only combo score). Competing combos still need a
        # clear gap to avoid forcing a choice between equally valid options.
        if second_score is None:
            classification = "RECONCILED"
        elif best_score - second_score >= MIN_SCORE_GAP:
            classification = "RECONCILED"
        else:
            classification = "INFO_MISSING"

        combo_ids = [c[pool_id_field] for c in best_combo]
        all_considered_ids = sorted({c[pool_id_field] for combo, _ in scored for c in combo})

        if classification == "RECONCILED":
            reason = f"Unique amount-only combination sums exactly to {target:.2f}."
            results.append(("transaction", anchor[anchor_id_field], "RECONCILED", combo_ids, reason))
            for cid in combo_ids:
                results.append(("invoice", cid, "RECONCILED", [anchor[anchor_id_field]], reason))
            matched_anchor_ids.add(anchor[anchor_id_field])
            matched_pool_ids.update(combo_ids)
        else:
            reason = ("Multiple equally valid amount-only allocations exist with no reference/date "
                      "evidence to distinguish them -- not forcing a match.")
            results.append(("transaction", anchor[anchor_id_field], "INFO_MISSING", all_considered_ids, reason))
            for cid in all_considered_ids:
                results.append(("invoice", cid, "INFO_MISSING", [anchor[anchor_id_field]], reason))
            matched_anchor_ids.add(anchor[anchor_id_field])
            matched_pool_ids.update(all_considered_ids)

    return results, matched_anchor_ids, matched_pool_ids


def pairwise_scorer_match(txns, invoices):
    """Final fallback: 1:1 shared-scorer matching with decide(), for
    everything not resolved by reference groups or subset-sum allocation.
    Handles fuzzy references, tolerance-only variances, reference-missing-
    on-one-side cases, reference contradictions, and true orphans."""
    results = []
    matched_txn_ids, matched_inv_ids = set(), set()
    remaining_invoices = list(invoices)

    for t in txns:
        candidates = []
        for inv in remaining_invoices:
            s = score_pair(t, inv, t["_date"], inv["_date"])
            candidates.append((inv, s))

        if not candidates:
            # No invoice left at all for this vendor -> confirmed orphan.
            classification = decide(0, None, False, False, True)
            results.append(("transaction", t["txn_id"], classification, [],
                            "No candidate invoice remains for this vendor anywhere in the batch."))
            continue

        # Secondary sort key: among tied scores (most commonly several
        # vetoed candidates all flattened to 0), prefer the one whose amount
        # actually agrees -- that's the one the contradiction is really
        # about, not an arbitrary same-vendor record with a differing amount.
        candidates.sort(key=lambda x: (-x[1]["score"], 0 if x[1]["signals"]["amount"] in ("exact", "tolerance") else 1))
        best_inv, best = candidates[0]
        second = candidates[1][1]["score"] if len(candidates) > 1 else None

        if best["veto"]:
            classification = decide(0, None, True, True, False)
            reason = f"Best candidate ({best_inv['invoice_id']}) is vetoed: {best['signals']}."
            results.append(("transaction", t["txn_id"], classification, [best_inv["invoice_id"]], reason))
            results.append(("invoice", best_inv["invoice_id"], classification, [t["txn_id"]], reason))
            remaining_invoices = [i for i in remaining_invoices if i["invoice_id"] != best_inv["invoice_id"]]
            continue

        genuine_discrepancy = best["signals"]["amount"] == "mismatch" and best["signals"]["reference"] in ("exact", "partial")
        classification = decide(best["score"], second, True, False, genuine_discrepancy)

        if classification == "RECONCILED":
            reason = f"Best candidate score {best['score']} clears threshold with a clear gap. Signals: {best['signals']}"
            results.append(("transaction", t["txn_id"], "RECONCILED", [best_inv["invoice_id"]], reason))
            results.append(("invoice", best_inv["invoice_id"], "RECONCILED", [t["txn_id"]], reason))
            matched_txn_ids.add(t["txn_id"])
            matched_inv_ids.add(best_inv["invoice_id"])
            remaining_invoices = [i for i in remaining_invoices if i["invoice_id"] != best_inv["invoice_id"]]
        elif classification == "NOT_RECONCILED":
            reason = f"Reference-confirmed candidate ({best_inv['invoice_id']}) but amount does not reconcile. Signals: {best['signals']}"
            results.append(("transaction", t["txn_id"], "NOT_RECONCILED", [best_inv["invoice_id"]], reason))
            # Mirror onto the invoice side rather than letting it fall through
            # to a weaker, independently-guessed leftover classification --
            # this invoice's strongest available evidence IS this transaction.
            results.append(("invoice", best_inv["invoice_id"], "NOT_RECONCILED", [t["txn_id"]], reason))
        else:
            considered = [c[0]["invoice_id"] for c in candidates if c[1]["score"] >= best["score"] - MIN_SCORE_GAP]
            reason = f"No candidate clears the confidence bar with a clear enough margin. Top scores: {[(c[0]['invoice_id'], c[1]['score']) for c in candidates[:3]]}"
            results.append(("transaction", t["txn_id"], "INFO_MISSING", considered, reason))
            for inv_id in considered:
                results.append(("invoice", inv_id, "INFO_MISSING", [t["txn_id"]], reason))

    matched_inv_id_set = matched_inv_ids
    unmatched_invoices = [i for i in invoices if i["invoice_id"] not in matched_inv_id_set]
    for inv in unmatched_invoices:
        already_reported = any(r[0] == "invoice" and r[1] == inv["invoice_id"] for r in results)
        if already_reported:
            continue
        remaining_txns = [t for t in txns if t["txn_id"] not in matched_txn_ids]
        if not remaining_txns:
            classification = decide(0, None, False, False, True)
            results.append(("invoice", inv["invoice_id"], classification, [],
                            "No candidate transaction remains for this vendor anywhere in the batch."))
        else:
            results.append(("invoice", inv["invoice_id"], "INFO_MISSING", [],
                            "No transaction matched this invoice directly; see linked transaction exceptions for detail."))

    return results, matched_txn_ids, matched_inv_ids
