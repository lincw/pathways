"""Planner agent — Ch.6 Planning.

Generates search terms (for Reactome/WikiPathways text search) AND seed genes
(for KEGG gene-based pathway lookup). On re-runs uses gap info from critic.
"""

from __future__ import annotations

import json
from typing import List

from pydantic import BaseModel, Field

from scripts.llm import call_agy_structured
from scripts.state import PipelineState


class PlannerOutput(BaseModel):
    search_terms: List[str] = Field(
        description="6-10 search strings for Reactome and WikiPathways text search"
    )
    seed_genes: List[str] = Field(
        description="10-20 gene symbols (e.g. TLR4, MYD88, TRAF6) for KEGG gene-based lookup"
    )
    plan: str = Field(description="2-3 sentence retrieval strategy")


def planner_node(state: PipelineState) -> dict:
    iteration = state.get("iteration", 0)
    query = state.get("query", "LPS intracellular signaling")

    if iteration == 0:
        prompt = f"""You are a computational biologist planning a systematic pathway database query.

Goal: {query}

Generate:
1. search_terms: 6-10 text strings for searching Reactome and WikiPathways databases.
   Examples: "Toll-like receptor signaling", "MyD88 signaling", "NF-kB activation",
   "innate immune response", "TRIF signaling", "type I interferon"

2. seed_genes: 10-20 human gene symbols to use for KEGG gene-based pathway lookup.
   Include: receptors, adaptors, kinases, ubiquitin ligases, transcription factors,
   and negative regulators relevant to the goal.
   Examples: TLR4, MYD88, TICAM1, IRAK4, IRAK1, TRAF6, MAP3K7, CHUK, RELA, IRF3

3. plan: brief strategy description.
"""
    else:
        gaps = state.get("coverage_gaps", [])
        extra_genes = state.get("additional_seed_genes", [])
        extra_terms = state.get("additional_search_terms", [])
        prompt = f"""You are filling gaps in an LPS pathway analysis.

Coverage gaps identified:
{json.dumps(gaps, indent=2)}

Generate NEW (non-overlapping) terms and genes targeting these gaps:
- search_terms: 4-6 NEW text strings for Reactome/WikiPathways
- seed_genes: 5-10 NEW gene symbols for KEGG
- plan: how these address the gaps
"""

    label = "Planner: gap-fill query..." if iteration > 0 else "Planner: generating search strategy..."
    result = call_agy_structured(prompt, PlannerOutput, desc=label)

    return {
        "search_terms": result.search_terms,
        "seed_genes": result.seed_genes,
        # Accumulate seeds across iterations (operator.add reducer) so the
        # enrichment filter tests pathways against the full LPS query set.
        "seed_gene_pool": list(result.seed_genes),
        "plan": result.plan,
        "iteration": iteration + 1,
    }
