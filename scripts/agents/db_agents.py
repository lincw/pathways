"""Parallel database agent nodes — Ch.3 Parallelization.

KEGG uses seed_genes (gene-based pathway lookup).
Reactome and WikiPathways use search_terms (text search).
"""

from scripts.state import PipelineState
from scripts.tools import kegg_tools, reactome_tools, wikipathways_tools


def kegg_agent_node(state: PipelineState) -> dict:
    seed_genes = state.get("seed_genes", [])
    pathways = kegg_tools.fetch_lps_pathways(seed_genes)
    return {"raw_pathways": pathways}


def reactome_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [Reactome] querying {len(search_terms)} terms...")
    pathways = reactome_tools.fetch_lps_pathways(search_terms)
    print(f"  [Reactome] found {len(pathways)} pathways")
    return {"raw_pathways": pathways}


def wikipathways_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [WikiPathways] querying {len(search_terms)} terms...")
    pathways = wikipathways_tools.fetch_lps_pathways(search_terms)
    print(f"  [WikiPathways] found {len(pathways)} pathways")
    return {"raw_pathways": pathways}
