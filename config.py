"""
All tunable knobs live here. Every value below is an initial guess to be
validated against ground_truth.csv, not a fact about how finance works —
per the design conversation, nothing here should be hard-coded inside the
matching logic itself.
"""

# --- Final decision thresholds (used by decide()) ---------------------------
MATCH_THRESHOLD = 70      # best candidate must score at least this to be RECONCILED
MIN_SCORE_GAP = 15        # best candidate must beat the runner-up by at least this

# --- Scoring weights (additive; a hard veto overrides all of this) ----------
SCORE_WEIGHTS = {
    "reference_exact": 50,
    "reference_partial": 30,   # fuzzy/token match above FUZZY_REFERENCE_THRESHOLD
    "reference_absent": 0,     # missing on one/both sides -> neutral, not penalized
    "amount_exact": 30,
    "amount_tolerance": 20,    # within AMOUNT_TOLERANCE_* -> still positive evidence
    "date_very_close": 20,     # within DATE_VERY_CLOSE_DAYS -- same-day/near-same-day is
                                # strong corroborating evidence, on par with amount-tolerance
    "date_reasonably_close": 10,  # within DATE_FAR_DAYS
    "other_identifier_exact": 20,  # legal_entity / tax_id agree
}

# --- Fuzzy / tolerance bands --------------------------------------------------
FUZZY_REFERENCE_THRESHOLD = 0.80   # difflib ratio >= this -> "partial" reference match
AMOUNT_TOLERANCE_ABS = 50          # absolute rupee tolerance (e.g. bank fees)
AMOUNT_TOLERANCE_PCT = 0.02        # 2% relative tolerance, whichever is larger
DATE_VERY_CLOSE_DAYS = 3
DATE_FAR_DAYS = 10                 # beyond this, date contributes nothing

# --- Allocation search bounds -------------------------------------------------
MAX_ALLOCATION_GROUP_SIZE = 4  # max invoices/txns considered in a subset-sum combo
                                 # (keeps brute-force combination search cheap;
                                 # real deployments would need a smarter solver
                                 # for larger vendor batches)

# --- Vendor resolution -------------------------------------------------------
# Below this, two vendor name strings are never auto-merged, even if the LLM
# is confident on paper -- kept as a sanity backstop, not the primary decision
# mechanism (the primary mechanism is the LLM's own needs_review flag).
VENDOR_NAME_MIN_SIMILARITY_FOR_AUTO_MERGE = 0.55
