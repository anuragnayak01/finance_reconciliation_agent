"""
The ONE shared scorer, per the design decision: tiers are deterministic
gates that decide WHEN scoring is needed; this module is what gets called
whenever there are multiple plausible candidates to rank.

Key principle carried over from the design conversation: contradiction is
different from missing. A wrong reference or a mismatched tax ID is a hard
veto -- it actively disqualifies a candidate regardless of how well other
signals line up. A missing reference just contributes zero points; it never
disqualifies anything.
"""

from utils import normalize_reference, string_similarity, days_between
from config import (
    SCORE_WEIGHTS, FUZZY_REFERENCE_THRESHOLD, AMOUNT_TOLERANCE_ABS,
    AMOUNT_TOLERANCE_PCT, DATE_VERY_CLOSE_DAYS, DATE_FAR_DAYS,
)


def reference_signal(txn_ref, inv_ref):
    """Returns (label, points, is_veto)."""
    a, b = normalize_reference(txn_ref), normalize_reference(inv_ref)
    if not a or not b:
        return "absent", SCORE_WEIGHTS["reference_absent"], False
    if a == b:
        return "exact", SCORE_WEIGHTS["reference_exact"], False
    sim = string_similarity(a, b)
    if sim >= FUZZY_REFERENCE_THRESHOLD:
        return "partial", SCORE_WEIGHTS["reference_partial"], False
    # Both present, clearly different, not just a formatting variant ->
    # contradictory evidence, not merely "no match".
    return "contradiction", 0, True


def amount_signal(txn_amount, inv_amount):
    """Returns (label, points, is_veto). Amount mismatches are never a veto
    on their own -- a genuine amount gap is a NOT_RECONCILED finding, not a
    reason to throw the candidate out of consideration."""
    diff = abs(txn_amount - inv_amount)
    if diff == 0:
        return "exact", SCORE_WEIGHTS["amount_exact"], False
    tolerance = max(AMOUNT_TOLERANCE_ABS, AMOUNT_TOLERANCE_PCT * max(txn_amount, inv_amount))
    if diff <= tolerance:
        return "tolerance", SCORE_WEIGHTS["amount_tolerance"], False
    return "mismatch", 0, False


def date_signal(txn_date, inv_date):
    d = days_between(txn_date, inv_date)
    if d <= DATE_VERY_CLOSE_DAYS:
        return "very_close", SCORE_WEIGHTS["date_very_close"], False
    if d <= DATE_FAR_DAYS:
        return "reasonably_close", SCORE_WEIGHTS["date_reasonably_close"], False
    return "far", 0, False


def other_identifier_signal(txn_row, inv_row):
    """legal_entity / tax_id agreement. A populated, DIFFERING tax_id on
    both sides is treated as a hard veto -- it's the strongest deterministic
    identity signal available and contradicting it should not be
    overridable by amount/date/reference alone."""
    t_tax, i_tax = (txn_row.get("tax_id") or "").strip(), (inv_row.get("tax_id") or "").strip()
    if t_tax and i_tax:
        if t_tax == i_tax:
            return "exact", SCORE_WEIGHTS["other_identifier_exact"], False
        return "contradiction", 0, True
    return "absent", 0, False


def score_pair(txn_row, inv_row, txn_date, inv_date):
    """Scores one (transaction, invoice) candidate pair. Returns a dict with
    the total score, whether any signal vetoed the pair, and the per-signal
    breakdown (kept for the exception schema / auditability)."""
    ref_label, ref_pts, ref_veto = reference_signal(txn_row.get("reference"), inv_row.get("reference"))
    amt_label, amt_pts, amt_veto = amount_signal(float(txn_row["amount"]), float(inv_row["amount"]))
    date_label, date_pts, date_veto = date_signal(txn_date, inv_date)
    oid_label, oid_pts, oid_veto = other_identifier_signal(txn_row, inv_row)

    veto = ref_veto or amt_veto or date_veto or oid_veto
    total = 0 if veto else (ref_pts + amt_pts + date_pts + oid_pts)

    return {
        "score": total,
        "veto": veto,
        "signals": {
            "reference": ref_label,
            "amount": amt_label,
            "date": date_label,
            "other_identifier": oid_label,
        },
    }
