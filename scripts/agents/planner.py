"""Planner agent

Generates search terms that drive collection from all three databases (Reactome
text search; KEGG and SIGNOR LLM catalogue selection use them as hints) plus a
small set of representative seed genes used as context. On re-runs uses gap info
from critic.
"""

from __future__ import annotations

import json
from typing import List

from pydantic import BaseModel, Field

from scripts.llm import call_llm_structured
from scripts.state import PipelineState


class PlannerOutput(BaseModel):
    search_terms: List[str] = Field(
        description="6-10 search strings for Reactome and WikiPathways text search"
    )
    seed_genes: List[str] = Field(
        description="10-20 representative core gene symbols (e.g. TLR4, MYD88, "
                    "TRAF6) — context/hints for pathway selection, not a lookup key"
    )
    plan: str = Field(description="2-3 sentence retrieval strategy")


def planner_node(state: PipelineState) -> dict:
    iteration = state.get("iteration", 0)
    query = state.get("query", "intracellular signaling pathway")

    if iteration == 0:
        prompt = f"""You are a immunologist planning a systematic pathway database query.

Goal: {query}

Generate, specifically for the pathway named in the Goal (do not default to any
other pathway):
1. search_terms: 6-10 text strings that name this signaling pathway and its parts.
   These drive collection from all three databases (Reactome text search; KEGG and
   SIGNOR pathway selection). Cover the canonical cascade name, its key branches,
   and its major outputs.

2. seed_genes: 10-20 human gene symbols representative of the pathway's core
   machinery — receptors, adaptors, kinases/enzymes, ubiquitin ligases,
   transcription factors, and negative regulators. Used as context, not a lookup key.

3. plan: brief strategy description.
"""
    else:
        gaps = state.get("coverage_gaps", [])
        extra_genes = state.get("additional_seed_genes", [])
        extra_terms = state.get("additional_search_terms", [])
        prompt = f"""You are filling gaps in a pathway analysis for: {query}.

Coverage gaps identified:
{json.dumps(gaps, indent=2)}

Generate NEW (non-overlapping) terms and genes targeting these gaps:
- search_terms: 4-6 NEW text strings to re-drive collection from all three databases
- seed_genes: 5-10 NEW representative gene symbols for the missing components
- plan: how these address the gaps
"""

    label = "Planner: gap-fill query..." if iteration > 0 else "Planner: generating search strategy..."
    result = call_llm_structured(prompt, PlannerOutput, desc=label)

    return {
        "search_terms": result.search_terms,
        "seed_genes": result.seed_genes,
        # Accumulate seeds across iterations (operator.add reducer) so the
        # enrichment filter tests pathways against the full query seed set.
        "seed_gene_pool": list(result.seed_genes),
        "plan": result.plan,
        "iteration": iteration + 1,
    }
