"""Planner agent node — Ch.6 (Planning) + Ch.17 (Reasoning).

Iteration 0: generates initial search strategy from the user query.
Iteration >0: generates targeted search terms to fill coverage gaps.
Uses agy CLI via call_agy_structured (Pydantic-validated, auto-retry).
"""

from __future__ import annotations

import json
from typing import List

from pydantic import BaseModel, Field

from scripts.config import DEFAULT_SEARCH_TERMS
from scripts.llm import call_agy_structured
from scripts.state import PipelineState


class PlannerOutput(BaseModel):
    search_terms: List[str] = Field(
        description="6-10 specific search strings for KEGG, Reactome, WikiPathways"
    )
    plan: str = Field(description="2-3 sentence description of the retrieval strategy")


def planner_node(state: PipelineState) -> dict:
    iteration = state.get("iteration", 0)
    query = state.get("query", "LPS intracellular signaling")

    if iteration == 0:
        prompt = f"""You are a computational biologist planning a systematic pathway database query.

Goal: {query}

Generate search terms for KEGG, Reactome, and WikiPathways databases.
Include: LPS/endotoxin receptor names, signalling cascade names, adaptor proteins,
kinases, transcription factors, and disease contexts (sepsis, macrophage activation).

Examples of good search terms:
"TLR4 signaling", "MyD88 NF-kB", "TRIF IRF3", "LPS macrophage",
"innate immunity", "IRAK TRAF6", "NF-kB canonical", "type I interferon"
"""
    else:
        gaps = state.get("coverage_gaps", [])
        prompt = f"""You are a computational biologist filling gaps in an LPS pathway analysis.

Previously identified coverage gaps:
{json.dumps(gaps, indent=2)}

Generate NEW search terms that specifically target these missing components.
Do NOT repeat previously used terms. Focus on the gaps above.
"""

    result = call_agy_structured(prompt, PlannerOutput)

    return {
        "search_terms": result.search_terms or DEFAULT_SEARCH_TERMS,
        "plan": result.plan,
        "iteration": iteration + 1,
    }
