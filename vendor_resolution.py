"""
Stage A of the pipeline: vendor identity resolution.

Design constraints agreed earlier in this project:
  - Send ONLY the list of unique raw vendor strings to the LLM -- never the
    full transaction/invoice dataframes.
  - The LLM's job is identity resolution only. It never touches amounts,
    dates, or references, and it never performs the actual reconciliation.
  - Ambiguous names must come back as needs_review=True, not be silently
    merged or silently kept separate.
  - needs_review vendors are excluded from the automated matching pool
    entirely and routed straight to the exception ledger -- there is no
    "fall back to raw-name matching" path.

This module is written so the LLM call is a swappable function
(`llm_call_fn`). If none is provided, `offline_fallback_resolution` runs
instead -- a conservative, deterministic stand-in used so this pipeline can
be executed and validated without network/API access. It intentionally
tries to make the SAME judgment calls the LLM prompt asks for (merge only
when confident; needs_review when not), so it doubles as a sanity check on
the prompt's expected behavior, not a permanent substitute for it.
"""

import json
import itertools
from collections import defaultdict
from utils import normalize_name, string_similarity
from config import VENDOR_NAME_MIN_SIMILARITY_FOR_AUTO_MERGE


VENDOR_RESOLUTION_PROMPT_TEMPLATE = """You are resolving vendor identities for a finance reconciliation system.

You will be given a list of unique, raw vendor name strings pulled from a
company's transactions and invoices. Some of these strings refer to the same
real-world vendor written differently (e.g. "AWS" and "Amazon Web Services
Inc."). Others may look similar but could be genuinely different legal
entities -- you must not guess.

Rules:
1. Only group names together if you are confident they refer to the same
   vendor (abbreviations, legal-suffix differences, obvious short forms).
2. If two or more names are plausibly related but you cannot be confident
   they are the same vendor (e.g. "ABC Ltd" vs "ABC Trading Ltd" -- could be
   a parent/subsidiary, a rebrand, or two unrelated companies), do NOT merge
   them. Instead return them under "needs_review" with a short reason.
3. A name that doesn't resemble any other name is its own vendor group.
4. Never invent a relationship you are not confident about. A false merge is
   worse than an unresolved one.

Return ONLY valid JSON in exactly this shape, with no other text:

{{
  "vendors": [
    {{
      "canonical_id": "V001",
      "canonical_name": "Amazon Web Services",
      "source_names": ["Amazon Web Services", "AWS", "Amazon Web Services Inc."]
    }}
  ],
  "needs_review": [
    {{
      "names": ["ABC Ltd", "ABC Trading Ltd"],
      "reason": "Could be the same vendor or unrelated companies; no shared tax ID or legal entity provided."
    }}
  ]
}}

Unique vendor names to resolve:
{vendor_list_json}
"""


def build_prompt(unique_names):
    return VENDOR_RESOLUTION_PROMPT_TEMPLATE.format(
        vendor_list_json=json.dumps(sorted(unique_names), indent=2)
    )


def call_llm_vendor_resolution(unique_names, llm_call_fn):
    """llm_call_fn: Callable[[str], str] that sends a prompt to a model and
    returns its raw text response. Kept generic so any provider/SDK can be
    plugged in -- see make_groq_llm_call_fn() below for the default one
    used by this project.
    """
    prompt = build_prompt(unique_names)
    raw = llm_call_fn(prompt)
    cleaned = raw.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def make_groq_llm_call_fn(model="llama-3.3-70b-versatile", api_key=None):
    """Builds an llm_call_fn backed by Groq's free-tier hosted models.

    Groq's API is OpenAI-SDK compatible, so this uses the `openai` package
    pointed at Groq's base URL. Requires GROQ_API_KEY to be set in the
    environment (or pass api_key explicitly). Get a free key at
    https://console.groq.com/keys. Install with:
        pip install openai

    Model names on Groq's free tier change periodically as they add/retire
    models -- check https://console.groq.com/docs/models for the current
    list if "model" here starts returning a model_decommissioned error.

    Usage:
        from vendor_resolution import make_groq_llm_call_fn
        llm_call_fn = make_groq_llm_call_fn()
        result = run_pipeline(txn_path, inv_path, llm_call_fn=llm_call_fn)
    """
    import os
    from openai import OpenAI

    resolved_key = api_key or os.environ.get("GROQ_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "No Groq API key found. Set the GROQ_API_KEY environment variable "
            "or pass api_key= explicitly. Get a free key at "
            "https://console.groq.com/keys."
        )

    client = OpenAI(
        api_key=resolved_key,
        base_url="https://api.groq.com/openai/v1",
    )

    def llm_call_fn(prompt):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content

    return llm_call_fn


class _UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _is_prefix_match(norm_a, norm_b):
    """One normalized name is a whole-word prefix of the other, e.g.
    'hcl tech' -> 'hcl technologies', 'google cloud' -> 'google cloud
    platform'. Requires the shorter side to have 2+ tokens so single short
    words (like 'abc') can't trivially prefix-match anything -- that's
    exactly the false-merge risk the ABC Ltd / ABC Trading Ltd case guards
    against."""
    short, long_ = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if len(short.split()) < 2:
        return False
    return long_.startswith(short)


def _is_initials_match(norm_a, norm_b):
    """True if the shorter, single-token side is an acronym of the longer
    side's words, e.g. 'aws' <-> 'amazon web services', 'tcs' <-> 'tata
    consultancy services'."""
    for short, long_ in ((norm_a, norm_b), (norm_b, norm_a)):
        words = long_.split()
        if len(words) < 2 or " " in short:
            continue
        initials = "".join(w[0] for w in words)
        if short == initials:
            return True
    return False


def offline_fallback_resolution(unique_names):
    """Deterministic stand-in for the LLM call, used only when no
    llm_call_fn is supplied (e.g. running this pipeline without API access).

    Two separate, conservative rule sets, matching what the prompt asks a
    real LLM to do:
      - CONFIRMED merge: identical normalized form, whole-word prefix match,
        or acronym/initials match. These are the low-risk, high-confidence
        patterns (legal-suffix differences, abbreviations, short forms).
      - NEEDS REVIEW: names that share a first word but were NOT confirmed
        merged (e.g. 'ABC Ltd' / 'ABC Trading Ltd', 'Sunrise Traders' /
        'Sunrise Trading Co') -- plausibly related, not confidently the same
        vendor, so they are flagged rather than guessed either way.
    """
    names = sorted(set(unique_names))
    norm = {n: normalize_name(n) for n in names}

    uf = _UnionFind(names)
    for a, b in itertools.combinations(names, 2):
        na, nb = norm[a], norm[b]
        if not na or not nb:
            continue
        if na == nb or _is_prefix_match(na, nb) or _is_initials_match(na, nb):
            uf.union(a, b)

    confirmed_groups = defaultdict(set)
    for n in names:
        confirmed_groups[uf.find(n)].add(n)

    singletons = [n for n in names if len(confirmed_groups[uf.find(n)]) == 1]
    review_uf = _UnionFind(singletons)
    for a, b in itertools.combinations(singletons, 2):
        first_a, first_b = norm[a].split()[0] if norm[a] else None, norm[b].split()[0] if norm[b] else None
        if first_a and first_a == first_b:
            review_uf.union(a, b)

    review_clusters = defaultdict(set)
    for n in singletons:
        review_clusters[review_uf.find(n)].add(n)

    vendors = []
    needs_review = []
    vid_counter = 1
    flagged_names = set()

    for cluster in review_clusters.values():
        if len(cluster) > 1:
            needs_review.append({
                "names": sorted(cluster),
                "reason": "Share a common first word but no confirmed legal-entity/tax-ID anchor "
                          "or recognizable abbreviation pattern -- could be the same vendor, a "
                          "subsidiary, or unrelated companies (offline heuristic).",
            })
            flagged_names.update(cluster)

    for root, members in confirmed_groups.items():
        if members & flagged_names:
            continue
        vendors.append({
            "canonical_id": f"V{vid_counter:03d}",
            "canonical_name": sorted(members)[0],
            "source_names": sorted(members),
        })
        vid_counter += 1

    return {"vendors": vendors, "needs_review": needs_review}


def resolve_vendors(transactions, invoices, llm_call_fn=None):
    """Returns:
      vendor_map: {raw_name: canonical_vendor_id}   (only for confidently
                  resolved vendors -- needs_review names are absent)
      needs_review_groups: [{"names": [...], "reason": "..."}]
    """
    unique_names = {t["raw_vendor_name"] for t in transactions} | {i["raw_vendor_name"] for i in invoices}

    if llm_call_fn is not None:
        result = call_llm_vendor_resolution(unique_names, llm_call_fn)
    else:
        result = offline_fallback_resolution(unique_names)

    vendor_map = {}
    for v in result["vendors"]:
        for name in v["source_names"]:
            vendor_map[name] = v["canonical_id"]

    return vendor_map, result.get("needs_review", [])
