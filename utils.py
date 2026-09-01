"""Shared low-level helpers used across the pipeline."""

import re
from datetime import datetime
from difflib import SequenceMatcher


def normalize_name(name: str) -> str:
    """Lowercase, strip legal suffixes/punctuation, collapse whitespace.
    Used only as a cheap pre-check before/alongside LLM vendor resolution —
    never as the sole basis for merging two vendors."""
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"[.,]", "", s)
    for suffix in [" inc", " ltd", " limited", " corp", " corporation", " pvt", " private", " co"]:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalize_reference(ref: str) -> str:
    """Strip formatting characters so 'INV/2026_0045/DEP' and
    'INV-2026-0045' are comparable. Does NOT decide the match -- that's the
    scorer's job -- this just removes formatting noise before comparing."""
    if not ref:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", ref).upper()


def string_similarity(a: str, b: str) -> float:
    """0..1 similarity ratio. Used for fuzzy reference matching and as a
    backstop sanity check on vendor name merges."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def parse_date(date_str: str):
    if not date_str:
        return None
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()


def days_between(d1, d2) -> int:
    if d1 is None or d2 is None:
        return 10**6  # effectively "infinitely far" if a date is missing
    return abs((d1 - d2).days)
