"""Critic / Reflection agent — Ch.4 Reflection.

No hardcoded biology. On iteration 1, the LLM generates the required
components from the query (stored in state). Subsequent iterations reuse them.
This makes the pipeline work for any pathway query, not just LPS.
"""

from __future__ import annotations

import json
from typing import List

from pydantic import BaseModel, Field

from scripts.config import MAX_REFLECTION_ITERATIONS
from scripts.llm import call_agy_structured
from scripts.state import PipelineState


class ComponentsOutput(BaseModel):
    required_components: List[str] = Field(
        description="8-12 specific molecular components a complete analysis must cover"
    )


class CriticOutput(BaseModel):
    coverage_assessment: str = Field(
        description="2-3 sentence expert assessment of analysis completeness"
    )
    missing_components: List[str] = Field(
        description="Required components NOT yet found in current pathways/genes"
    )
    additional_search_terms: List[str] = Field(
        description="2-5 text search terms for Reactome/WikiPathways to fill gaps"
    )
    additional_seed_genes: List[str] = Field(
        description="3-8 gene symbols for KEGG gene-based lookup to fill gaps"
    )
    is_sufficient: bool = Field(
        description="True only if all critical components are represented"
    )


def _generate_required_components(query: str) -> List[str]:
    """Ask the LLM what a complete analysis of this pathway should include."""
    prompt = f"""You are a molecular biologist. Given this analysis goal:
"{query}"

List 8-12 specific molecular components (proteins, complexes, or signaling modules)
that a COMPLETE database analysis MUST cover to be considered comprehensive.
Be specific: include receptor names, adaptor proteins, kinase cascades,
transcription factors, and regulatory mechanisms.
"""
    result = call_agy_structured(prompt, ComponentsOutput, desc="Critic: defining evaluation checklist...")
    return result.required_components


def critic_node(state: PipelineState) -> dict:
    iteration = state.get("iteration", 1)
    query = state.get("query", "")
    nodes = state.get("nodes", [])
    db_coverage = state.get("db_coverage", {})

    # Generate required components once on first critique; reuse afterwards
    required = state.get("required_components", [])
    if not required:
        print("  [Critic] generating evaluation criteria from query...")
        required = _generate_required_components(query)

    pathway_names = [n["name"] for n in nodes if n.get("type") == "pathway"]
    all_genes = [n["id"] for n in nodes if n.get("type") == "gene"]
    gene_count = len(all_genes)

    prompt = f"""You are an expert molecular biologist reviewing a pathway analysis.

Analysis goal: {query}

Current results:
- Pathways found: {len(pathway_names)}
- Genes/proteins found: {gene_count}
- Database coverage: {json.dumps(db_coverage)}
- Sample pathway names: {json.dumps(pathway_names[:20])}
- Sample genes found: {json.dumps(sorted(all_genes)[:40])}

Required components for a complete analysis:
{json.dumps(required, indent=2)}

For each required component, check whether it is represented in the pathway names
or gene list above. Mark is_sufficient=false if any critical signaling component is absent.
"""

    result = call_agy_structured(prompt, CriticOutput, desc=f"Critic: evaluating coverage (iteration {iteration})...")

    print(f"  [Critic] iteration={iteration}, gaps={len(result.missing_components)}, "
          f"sufficient={result.is_sufficient}")

    return {
        "required_components": required,
        "coverage_assessment": result.coverage_assessment,
        "coverage_gaps": result.missing_components,
        "additional_search_terms": result.additional_search_terms,
        "additional_seed_genes": result.additional_seed_genes,
    }


def route_after_critic(state: PipelineState) -> str:
    """Re-plan if gaps remain and reflection budget allows (Ch.4)."""
    iteration = state.get("iteration", 1)
    gaps = state.get("coverage_gaps", [])
    if gaps and iteration <= MAX_REFLECTION_ITERATIONS:
        print(f"  [Router] reflection {iteration} — filling {len(gaps)} gap(s)")
        return "planner"
    return "reporter"
