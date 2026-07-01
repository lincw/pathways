"""Query-agnostic validation of a pipeline run — does the AI really work?

The pipeline is generic (any signaling pathway), so the validator must be too:
it bakes in NO pathway-specific ground truth. Given only (query, results_dir) it
scores the output gene set with an INDEPENDENT enrichment service (g:Profiler)
against sources the pipeline does not use as primary input (GO:BP), and reports:

  coverage (precision)  = |output ∩ target-term genes| / |output|
                          -> fraction of fetched genes explained by the queried
                             pathway. LOW = bloated/over-inclusion (failure),
                             even if the target term ranks #1 on p-value.

  recall                = |output ∩ target-term genes| / |target-term genes|
                          -> fraction of the reference recovered. LOW = missed
                             real biology.

"Target terms" are chosen generally: the LLM picks, from g:Profiler's significant
terms, the ones that ARE the queried pathway (same no-hardcoding philosophy as the
pipeline). A run passes only if BOTH coverage and recall clear their thresholds.

Usage:
    python -m scripts.validate --results-dir results/20260701_025925 \
        --query "LPS intracellular signaling pathway in human cells"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

import requests
from pydantic import BaseModel, Field

from scripts.llm import call_agy_structured

GPROFILER_URL = "https://biit.cs.ut.ee/gprofiler/api/gost/profile/"
# Independent reference sources. GO:BP is a curated, held-out reference (not a
# primary pipeline source). KEGG/REAC are shown for context only, not scored,
# to avoid circularity with what the pipeline queried.
REFERENCE_SOURCES = ["GO:BP"]
CONTEXT_SOURCES = ["GO:BP", "KEGG", "REAC"]

# Pass thresholds (tunable). Coverage is the primary over-inclusion guard.
COVERAGE_PASS = 0.50
RECALL_PASS = 0.30

# Only consider reference terms whose size falls in an informative window:
#   > MAX  -> generic GO parents ("signaling", "response to stimulus") that
#            dominate a bloated set on p-value alone and drown the real pathway.
#   < MIN  -> hyper-specific leaf terms too small to be a meaningful reference.
# A size window (not a pathway name — no hardcoded biology); scoring still uses
# whatever the LLM picks from within it. Tunable via LPS_VALIDATE_*_TERM_SIZE.
import os as _os
MIN_TARGET_TERM_SIZE = int(_os.getenv("LPS_VALIDATE_MIN_TERM_SIZE", "5"))
MAX_TARGET_TERM_SIZE = int(_os.getenv("LPS_VALIDATE_MAX_TERM_SIZE", "1000"))
SHORTLIST_SIZE = int(_os.getenv("LPS_VALIDATE_SHORTLIST", "70"))
# Fallback recall reference must be at least this big, so a tiny leaf term doesn't
# make the denominator noisy when the LLM doesn't designate a primary.
RECALL_REF_MIN_SIZE = int(_os.getenv("LPS_VALIDATE_RECALL_MIN_SIZE", "20"))


# ---------------------------------------------------------------------------
# Load output
# ---------------------------------------------------------------------------

def load_output_genes(results_dir: Path) -> List[str]:
    genes_file = results_dir / "genes.tsv"
    if not genes_file.exists():
        raise FileNotFoundError(f"no genes.tsv in {results_dir}")
    genes: List[str] = []
    with genes_file.open() as f:
        header = f.readline().rstrip("\n").split("\t")
        col = header.index("gene_symbol") if "gene_symbol" in header else 0
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) > col and parts[col].strip():
                genes.append(parts[col].strip().upper())
    return sorted(set(genes))


# ---------------------------------------------------------------------------
# g:Profiler
# ---------------------------------------------------------------------------

def run_gprofiler(genes: List[str], sources: List[str]) -> Dict:
    """POST to g:Profiler; return enriched terms with per-gene membership."""
    payload = {
        "organism": "hsapiens",
        "query": genes,
        "sources": sources,
        "user_threshold": 0.05,
        "significance_threshold_method": "g_SCS",
        "no_evidences": False,        # include which query genes hit each term
        "no_iea": False,
        "ordered": False,
    }
    resp = requests.post(GPROFILER_URL, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()

    # Resolve the query gene order and ensg->symbol map so we can turn each
    # term's boolean-ish "intersections" vector back into gene symbols.
    meta = data.get("meta", {})
    qmeta = meta.get("genes_metadata", {}).get("query", {})
    first = next(iter(qmeta.values()), {}) if qmeta else {}
    ensg_order = first.get("ensgs", [])
    ensg_to_symbol: Dict[str, str] = {}
    for sym, eg in (first.get("mapping", {}) or {}).items():
        for e in (eg if isinstance(eg, list) else [eg]):
            ensg_to_symbol[e] = sym.upper()

    terms = []
    for t in data.get("result", []):
        members = set()
        inter = t.get("intersections") or []
        for i, ev in enumerate(inter):
            if ev and i < len(ensg_order):
                eg = ensg_order[i]
                members.add(ensg_to_symbol.get(eg, eg).upper())
        terms.append({
            "native": t.get("native", ""),
            "name": t.get("name", ""),
            "source": t.get("source", ""),
            "p_value": t.get("p_value"),
            "term_size": t.get("term_size"),
            "intersection_size": t.get("intersection_size"),
            "members": members,
        })
    terms.sort(key=lambda x: (x["p_value"] if x["p_value"] is not None else 1.0))
    return {"terms": terms}


# ---------------------------------------------------------------------------
# LLM target-term selection (query-driven, no hardcoding)
# ---------------------------------------------------------------------------

class TargetTerms(BaseModel):
    target_natives: List[str] = Field(
        description="native IDs (e.g. 'GO:0002224') of terms that ARE the queried "
        "pathway or its direct up/downstream signaling modules. Exclude off-topic "
        "terms that merely share hub genes. Used to measure COVERAGE."
    )
    primary_native: str = Field(
        default="",
        description="native ID of the SINGLE term that most specifically names the "
        "queried pathway itself — a focused, representative pathway term. NOT a broad "
        "parent (e.g. 'response to cytokine', 'signal transduction') and NOT a "
        "hyper-narrow leaf. Used as the RECALL reference. Must be one of target_natives.",
    )


def select_target_terms(query: str, terms: List[Dict], top: int = SHORTLIST_SIZE):
    """Return (target_natives, primary_native) chosen by the LLM from a size-windowed
    shortlist. primary_native anchors recall to the tightest query-specific term."""
    # Keep only terms inside the informative size window [MIN, MAX] so the
    # specific queried pathway can surface in a bloated gene set; then take the
    # most significant remaining terms.
    specific = [
        t for t in terms
        if MIN_TARGET_TERM_SIZE <= (t["term_size"] or 0) <= MAX_TARGET_TERM_SIZE
    ]
    shortlist = specific[:top]
    if not shortlist:
        return [], None
    listing = "\n".join(
        f"{t['native']} [{t['source']}] {t['name']} "
        f"(term_size={t['term_size']}, hits={t['intersection_size']})"
        for t in shortlist
    )
    prompt = f"""You are validating a pathway analysis.

Query pathway: "{query}"

Below are enriched terms returned by an independent enrichment tool for the gene
set under test.

1. target_natives: select all terms that genuinely REPRESENT the queried pathway —
   the canonical cascade plus modules directly upstream/downstream. Exclude terms
   about other processes/diseases that merely share hub proteins.
2. primary_native: from your selection, name the ONE term that most specifically
   IS the queried pathway (a focused, representative pathway term — not a broad
   parent, not a hyper-narrow sub-branch). This is the recall reference.

Terms:
{listing}
"""
    valid = {t["native"] for t in shortlist}
    try:
        res = call_agy_structured(prompt, TargetTerms, desc="Validator: matching target terms...")
        picked = {n.strip() for n in res.target_natives}
        targets = [t["native"] for t in shortlist if t["native"] in picked]
        primary = res.primary_native.strip() if res.primary_native else ""
        if primary not in valid:
            primary = None
        return targets, primary
    except Exception as exc:
        print(f"  [validate] LLM term selection failed ({exc}); "
              "falling back to best-p GO:BP term.")
        for t in shortlist:
            if t["source"] == "GO:BP":
                return [t["native"]], t["native"]
        return [shortlist[0]["native"]], shortlist[0]["native"]


def _pick_recall_reference(target_terms: List[Dict], primary_native) -> Dict | None:
    """Choose the term recall is measured against: the LLM-designated specific term
    if valid, else the tightest target term above a size floor (avoids a tiny, noisy
    denominator), else the broadest target term."""
    if primary_native:
        match = next((t for t in target_terms if t["native"] == primary_native), None)
        if match:
            return match
    if not target_terms:
        return None
    floored = [t for t in target_terms if (t["term_size"] or 0) >= RECALL_REF_MIN_SIZE]
    if floored:
        return min(floored, key=lambda t: t["term_size"] or 0)
    return max(target_terms, key=lambda t: t["term_size"] or 0)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(query: str, genes: List[str]) -> Dict:
    n = len(genes)
    gp = run_gprofiler(genes, CONTEXT_SOURCES)
    terms = gp["terms"]
    # Score only against held-out reference sources (avoid grading with a source
    # the pipeline copied from).
    ref_terms = [t for t in terms if t["source"] in REFERENCE_SOURCES]
    targets, primary_native = select_target_terms(query, ref_terms)
    targetset = set(targets)
    target_terms = [t for t in ref_terms if t["native"] in targetset]

    # Coverage: fraction of OUTPUT genes explained by the queried pathway.
    covered = set().union(*[t["members"] for t in target_terms]) if target_terms else set()
    coverage = len(covered) / n if n else 0.0

    # Recall: fraction of the reference recovered. Anchor to the tightest
    # query-specific term (LLM-designated) rather than the broadest match, so the
    # denominator is the actual pathway asked for — not a sprawling parent term.
    primary = _pick_recall_reference(target_terms, primary_native)
    recall = (primary["intersection_size"] / primary["term_size"]
              if primary and primary["term_size"] else 0.0)

    return {
        "n_output_genes": n,
        "n_significant_terms": len(terms),
        "primary_reference": (
            {"native": primary["native"], "name": primary["name"],
             "term_size": primary["term_size"], "hits": primary["intersection_size"]}
            if primary else None
        ),
        "target_terms": [
            {"native": t["native"], "name": t["name"], "p_value": t["p_value"],
             "term_size": t["term_size"], "hits": t["intersection_size"]}
            for t in target_terms
        ],
        "genes_covered": len(covered),
        "coverage": round(coverage, 4),
        "recall": round(recall, 4),
        "top_terms_context": [
            {"native": t["native"], "source": t["source"], "name": t["name"],
             "p_value": t["p_value"], "hits": t["intersection_size"], "term_size": t["term_size"]}
            for t in terms[:10]
        ],
    }


def verdict(s: Dict) -> str:
    cov_ok = s["coverage"] >= COVERAGE_PASS
    rec_ok = s["recall"] >= RECALL_PASS
    if cov_ok and rec_ok:
        return "PASS"
    if not cov_ok and not rec_ok:
        return "FAIL (bloated AND incomplete)"
    if not cov_ok:
        return "FAIL (over-inclusion: most output genes are off-pathway)"
    return "FAIL (gaps: missed much of the reference)"


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate a pipeline run with g:Profiler.")
    ap.add_argument("--results-dir", required=True, help="results/<timestamp> directory")
    ap.add_argument("--query", required=True, help="the pathway query used for the run")
    args = ap.parse_args()

    genes = load_output_genes(Path(args.results_dir))
    print(f"Loaded {len(genes)} output genes from {args.results_dir}\n")

    s = score(args.query, genes)

    print("=== Independent validation (g:Profiler, GO:BP held-out reference) ===")
    print(f"Output genes:        {s['n_output_genes']}")
    print(f"Target terms:        {len(s['target_terms'])}")
    for t in s["target_terms"]:
        print(f"  - {t['native']} {t['name']} (p={t['p_value']:.1e}, hits={t['hits']}/{t['term_size']})")
    pr = s.get("primary_reference")
    if pr:
        print(f"Primary reference:   {pr['native']} {pr['name']} ({pr['hits']}/{pr['term_size']})")
    print(f"Genes covered:       {s['genes_covered']} / {s['n_output_genes']}")
    print(f"Coverage (precision):{s['coverage']:.1%}   [pass >= {COVERAGE_PASS:.0%}]  <- over-inclusion guard")
    print(f"Recall:              {s['recall']:.1%}   [pass >= {RECALL_PASS:.0%}]  <- completeness")
    print(f"\nVERDICT: {verdict(s)}\n")

    print("Top enriched terms (context, all sources):")
    for t in s["top_terms_context"]:
        print(f"  [{t['source']:6s}] {t['native']:12s} {t['name'][:45]:45s} "
              f"p={t['p_value']:.1e} hits={t['hits']}")


if __name__ == "__main__":
    main()
