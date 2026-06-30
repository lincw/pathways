"""Parallel database agent nodes — Ch.3 Parallelization.

KEGG uses seed_genes (gene-based pathway lookup).
Reactome and WikiPathways use search_terms (text search).
"""

from scripts.state import PipelineState
from scripts.tools import kegg_tools, reactome_tools, wikipathways_tools


def kegg_agent_node(state: PipelineState) -> dict:
    seed_genes = state.get("seed_genes", [])
    print(f"  [KEGG] starting with {len(seed_genes)} seed genes: {seed_genes[:5]}", flush=True)
    try:
        pathways = kegg_tools.fetch_lps_pathways(seed_genes)
    except Exception as exc:
        print(f"  [KEGG] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [KEGG] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}


def reactome_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [Reactome] querying {len(search_terms)} terms...", flush=True)
    try:
        pathways = reactome_tools.fetch_lps_pathways(search_terms)
    except Exception as exc:
        print(f"  [Reactome] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [Reactome] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}


def wikipathways_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [WikiPathways] querying {len(search_terms)} terms...", flush=True)
    try:
        pathways = wikipathways_tools.fetch_lps_pathways(search_terms)
    except Exception as exc:
        print(f"  [WikiPathways] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [WikiPathways] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}
