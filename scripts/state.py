"""LangGraph shared state for the signaling-pathway pipeline."""

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

    # Cumulative pool of seed genes across reflection iterations (fan-in reducer),
    # used as the query set for hypergeometric enrichment filtering.
    seed_gene_pool: Annotated[List[str], operator.add]

    # --- Parallel DB outputs (operator.add = fan-in reducer) ---
    raw_pathways: Annotated[List[PathwayEntry], operator.add]

    # --- Relevance filtering (enrichment + LLM gate) ---
    # Overwrite semantics (no reducer): the filtered, deduplicated view that all
    # downstream nodes consume instead of the ever-accumulating raw_pathways.
    filtered_pathways: List[PathwayEntry]
    filter_stats: Dict

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

    # --- Independent QC (monitor mode) ---
    validation: Dict

    # --- Final ---
    report: str
    output_files: List[str]


def dedup_pathways(pathways: List[PathwayEntry]) -> List[PathwayEntry]:
    """Deduplicate on (source, pathway_id), preserving first-seen order."""
    seen = set()
    unique = []
    for pw in pathways:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            unique.append(pw)
    return unique


def working_pathways(state: "PipelineState") -> List[PathwayEntry]:
    """The pathway set downstream nodes should use.

    Prefer the relevance-filtered view once the filter node has run; fall back to
    the deduplicated raw pathways so the pipeline is safe if filtering is disabled.
    """
    filtered = state.get("filtered_pathways")
    if filtered is not None:
        return filtered
    return dedup_pathways(state.get("raw_pathways", []))
