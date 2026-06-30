"""LangGraph shared state for the LPS signaling pipeline.

Design pattern: Agentic Design Patterns Ch.3 (Parallelization) — raw_pathways
uses operator.add reducer so all three parallel DB agents append to the same list.
"""

import operator
from typing import Annotated, Dict, List, TypedDict


class PathwayEntry(TypedDict):
    source: str          # "KEGG" | "Reactome" | "WikiPathways"
    pathway_id: str
    pathway_name: str
    genes: List[str]     # gene symbols
    description: str


class PipelineState(TypedDict):
    # --- Input ---
    query: str

    # --- Planner output (Ch.6: Planning) ---
    search_terms: List[str]
    plan: str
    iteration: int

    # --- Parallel DB outputs — accumulates across iterations (Ch.3: Parallelization) ---
    raw_pathways: Annotated[List[PathwayEntry], operator.add]

    # --- ID mapping output (Ch.5: Tool Use) ---
    id_mapping: Dict[str, Dict]   # gene_symbol -> {entrez, uniprot, ensembl}

    # --- Synthesis output ---
    nodes: List[Dict]
    edges: List[Dict]
    hub_genes: List[str]
    db_coverage: Dict[str, int]   # {"KEGG": 5, "Reactome": 8, ...}

    # --- Critic output (Ch.4: Reflection) ---
    coverage_assessment: str
    coverage_gaps: List[str]
    additional_search_terms: List[str]

    # --- Final ---
    report: str
    output_files: List[str]
