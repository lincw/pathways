"""Pathway relevance filter — removes hub-gene over-inclusion.

Gene-based lookup (esp. KEGG) pulls in every pathway that shares a promiscuous
hub gene (NFKB1, MAPK1, AKT1 ...), so a query for "LPS signaling" drags in
"Pathways in cancer", "Alzheimer disease", "Thermogenesis", etc. This node cuts
that noise with two data-driven stages and NO hardcoded pathway names or ID
patterns:

  Stage 1 — Statistical enrichment (hypergeometric ORA):
      Keep a pathway only if its overlap with the seed-gene set is statistically
      over-represented (BH-adjusted p < FDR). A pathway sharing one incidental
      hub gene is not enriched and is dropped.

  Stage 2 — LLM relevance gate:
      The query drives an LLM judgement on each survivor's name/description —
      "is this part of, or directly downstream of, the queried pathway?" — so
      large hub-rich but off-topic maps (e.g. cancer/neurodegeneration) are
      removed even when they pass the statistical test.

Both stages are pure functions of the seed set and the user query; nothing about
LPS biology is baked into the code.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from pydantic import BaseModel, Field

from scripts.config import (
    ENRICHMENT_BACKGROUND_SIZE,
    ENRICHMENT_ENABLED,
    ENRICHMENT_FDR,
    ENRICHMENT_MIN_OVERLAP,
    LLM_GATE_CHUNK,
    LLM_RELEVANCE_GATE,
)
from scripts.llm import call_agy_structured
from scripts.state import PathwayEntry, PipelineState, dedup_pathways


# ---------------------------------------------------------------------------
# Stage 1 — hypergeometric over-representation (no scipy dependency)
# ---------------------------------------------------------------------------

def _log_choose(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def hypergeom_sf(k: int, N: int, K: int, n: int) -> float:
    """P(X >= k) for X ~ Hypergeometric(population N, successes K, draws n)."""
    if k <= 0:
        return 1.0
    upper = min(K, n)
    if k > upper:
        return 0.0
    log_denom = _log_choose(N, n)
    total = 0.0
    for i in range(k, upper + 1):
        total += math.exp(_log_choose(K, i) + _log_choose(N - K, n - i) - log_denom)
    return min(1.0, total)


def benjamini_hochberg(pvals: List[float]) -> List[float]:
    """Benjamini-Hochberg FDR adjustment, preserving input order."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        idx = order[rank]
        val = pvals[idx] * m / (rank + 1)
        prev = min(prev, val)
        adj[idx] = min(1.0, prev)
    return adj


def enrichment_filter(
    pathways: List[PathwayEntry], seed_genes: List[str]
) -> Tuple[List[PathwayEntry], Dict]:
    """Keep pathways statistically over-represented for the seed-gene set."""
    seeds = {g.upper().strip() for g in seed_genes if g and g.strip()}
    if not seeds or not pathways:
        return pathways, {"skipped": True, "reason": "no seed set" if not seeds else "no pathways"}

    N = ENRICHMENT_BACKGROUND_SIZE
    n = len(seeds)

    stats = []
    for pw in pathways:
        genes = {g.upper().strip() for g in pw.get("genes", []) if g and g.strip()}
        K = len(genes)
        k = len(genes & seeds)
        # Guard the background model: a pathway can't have more genes than the
        # universe, and the seed set is drawn from that same universe.
        p = hypergeom_sf(k, N, min(K, N), n) if K else 1.0
        stats.append((pw, k, K, p))

    padj = benjamini_hochberg([s[3] for s in stats])

    kept_entries = [
        pw for (pw, k, K, p), q in zip(stats, padj)
        if k >= ENRICHMENT_MIN_OVERLAP and q < ENRICHMENT_FDR
    ]
    info = {
        "seed_set_size": n,
        "input": len(pathways),
        "kept": len(kept_entries),
        "dropped": len(pathways) - len(kept_entries),
        "fdr": ENRICHMENT_FDR,
        "min_overlap": ENRICHMENT_MIN_OVERLAP,
    }
    return kept_entries, info


# ---------------------------------------------------------------------------
# Stage 2 — LLM relevance gate
# ---------------------------------------------------------------------------

class RelevanceOutput(BaseModel):
    relevant_tags: List[str] = Field(
        description="Tags (e.g. 'P3') of pathways that ARE part of, or directly "
        "downstream/upstream of, the queried signaling pathway. Omit off-topic ones."
    )


def _gate_chunk(query: str, chunk: List[PathwayEntry]) -> set[str]:
    lines = []
    for i, pw in enumerate(chunk):
        desc = (pw.get("description") or "").strip().replace("\n", " ")[:200]
        lines.append(f"[P{i}] {pw['pathway_name']} — {desc}")
    listing = "\n".join(lines)

    prompt = f"""You are a molecular biologist curating a focused pathway set.

Analysis goal: "{query}"

Below are candidate pathways (tag — name — short description). Decide which ones
genuinely belong to this signaling pathway: the canonical cascade PLUS modules
directly upstream or immediately downstream of it. EXCLUDE pathways that merely
share some hub proteins but are really about a different biological process or
disease (e.g. cancer, neurodegeneration, unrelated infections, metabolism,
development) unless the goal is explicitly about them.

Return the tags of the pathways to KEEP.

Candidates:
{listing}
"""
    try:
        result = call_agy_structured(
            prompt, RelevanceOutput,
            desc=f"Relevance gate: judging {len(chunk)} pathways...",
        )
        return {t.strip().upper() for t in result.relevant_tags}
    except Exception as exc:  # be conservative: keep the chunk on failure
        print(f"  [Filter] LLM gate chunk failed, keeping all: {exc}", flush=True)
        return {f"P{i}" for i in range(len(chunk))}


def llm_relevance_gate(query: str, pathways: List[PathwayEntry]) -> Tuple[List[PathwayEntry], Dict]:
    if not pathways:
        return pathways, {"skipped": True, "reason": "no pathways"}

    kept: List[PathwayEntry] = []
    for start in range(0, len(pathways), LLM_GATE_CHUNK):
        chunk = pathways[start : start + LLM_GATE_CHUNK]
        keep_tags = _gate_chunk(query, chunk)
        for i, pw in enumerate(chunk):
            if f"P{i}" in keep_tags:
                kept.append(pw)

    # Safety net: never let the gate empty the set (likely a parsing failure).
    if not kept:
        print("  [Filter] LLM gate returned nothing — reverting to enrichment survivors.", flush=True)
        return pathways, {"input": len(pathways), "kept": len(pathways), "reverted": True}

    return kept, {"input": len(pathways), "kept": len(kept), "dropped": len(pathways) - len(kept)}


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

def pathway_filter_node(state: PipelineState) -> dict:
    pathways = dedup_pathways(state.get("raw_pathways", []))
    seed_genes = state.get("seed_gene_pool", []) or state.get("seed_genes", [])

    print(f"  [Filter] {len(pathways)} unique pathways; seed set = {len(set(seed_genes))} genes")

    stats: Dict = {"raw_unique": len(pathways)}

    survivors = pathways
    if ENRICHMENT_ENABLED:
        survivors, enr = enrichment_filter(survivors, seed_genes)
        stats["enrichment"] = enr
        print(f"  [Filter] enrichment: {enr.get('input', len(pathways))} → {len(survivors)}")

    if LLM_RELEVANCE_GATE:
        survivors, gate = llm_relevance_gate(state.get("query", ""), survivors)
        stats["llm_gate"] = gate
        print(f"  [Filter] LLM gate: {gate.get('input')} → {len(survivors)}")

    stats["final"] = len(survivors)
    print(f"  [Filter] final: {len(survivors)} pathways retained")

    return {"filtered_pathways": survivors, "filter_stats": stats}
