"""LangGraph shared state for the LPS signaling pipeline."""

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

    # --- Planner output ---
    search_terms: List[str]     # text terms for Reactome / WikiPathways
    seed_genes: List[str]       # gene symbols for KEGG gene-based lookup
    plan: str
    iteration: int

    # --- Parallel DB outputs (operator.add = fan-in reducer) ---
    raw_pathways: Annotated[List[PathwayEntry], operator.add]

    # --- ID mapping ---
    id_mapping: Dict[str, Dict]  # symbol -> {entrez, uniprot, ensembl}

    # --- Synthesis ---
    nodes: List[Dict]
    edges: List[Dict]
    db_coverage: Dict[str, int]

    # --- Critic ---
    required_components: List[str]  # LLM-generated once from query, reused
    coverage_assessment: str
    coverage_gaps: List[str]
    additional_search_terms: List[str]
    additional_seed_genes: List[str]

    # --- Final ---
    report: str
    output_files: List[str]
