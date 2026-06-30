"""Critic / Reflection agent — Ch.4 (Reflection).

Evaluates the current pathway coverage and identifies biological gaps.
Uses agy CLI via call_agy_structured (Pydantic-validated, auto-retry).
"""

from __future__ import annotations

import json
from typing import List

from pydantic import BaseModel, Field

from scripts.config import MAX_REFLECTION_ITERATIONS
from scripts.llm import call_agy_structured
from scripts.state import PipelineState

REQUIRED_LPS_COMPONENTS = [
    "TLR4/MD-2 receptor complex",
    "MyD88-dependent pathway",
    "TRIF/TICAM1-dependent pathway",
    "IRAK1/IRAK4 kinase cascade",
    "TRAF6 ubiquitin ligase",
    "TAK1 (MAP3K7) activation",
    "NF-κB canonical pathway",
    "MAPK cascade (ERK, JNK, p38)",
    "IRF3 / type I interferon",
    "PI3K/Akt pathway",
    "negative regulators (IRAK-M, TOLLIP, SOCS1)",
    "LPS endosomal signaling",
]


class CriticOutput(BaseModel):
    coverage_assessment: str = Field(
        description="2-3 sentence expert assessment of the analysis quality"
    )
    missing_components: List[str] = Field(
        description="Required components NOT covered by current pathways"
    )
    additional_search_terms: List[str] = Field(
        description="2-5 specific search terms to find the missing components"
    )
    is_sufficient: bool = Field(
        description="True only if all critical MyD88/TRIF/NF-kB components are present"
    )


def critic_node(state: PipelineState) -> dict:
    iteration = state.get("iteration", 1)
    hub_genes = state.get("hub_genes", [])
    db_coverage = state.get("db_coverage", {})
    nodes = state.get("nodes", [])

    pathway_names = [n["name"] for n in nodes if n.get("type") == "pathway"]
    gene_count = sum(1 for n in nodes if n.get("type") == "gene")

    prompt = f"""You are an expert immunologist reviewing a computational LPS signalling pathway analysis.

Current analysis summary:
- Total pathways found: {len(pathway_names)}
- Total genes/proteins: {gene_count}
- Database coverage: {json.dumps(db_coverage)}
- Top hub genes (cross-database): {hub_genes[:20]}
- Pathway names found: {json.dumps(pathway_names[:25])}

Required LPS signalling components to cover:
{json.dumps(REQUIRED_LPS_COMPONENTS, indent=2)}

Evaluate whether each required component is represented in the pathway names or hub genes.
Be strict — mark is_sufficient as false if any critical MyD88/TRIF/NF-kB/IRF3 component is missing.
"""

    result = call_agy_structured(prompt, CriticOutput)

    print(
        f"  [Critic] iteration={iteration}, "
        f"gaps={len(result.missing_components)}, "
        f"sufficient={result.is_sufficient}"
    )

    return {
        "coverage_assessment": result.coverage_assessment,
        "coverage_gaps": result.missing_components,
        "additional_search_terms": result.additional_search_terms,
    }


def route_after_critic(state: PipelineState) -> str:
    """Reflection loop routing — re-plan if gaps remain and budget allows (Ch.4)."""
    iteration = state.get("iteration", 1)
    gaps = state.get("coverage_gaps", [])

    if gaps and iteration <= MAX_REFLECTION_ITERATIONS:
        print(f"  [Router] reflection {iteration} — re-querying for {len(gaps)} gap(s)")
        return "planner"
    return "reporter"
