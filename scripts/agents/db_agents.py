"""Parallel database agent nodes — Ch.3 (Parallelization) + Ch.5 (Tool Use).

Three independent nodes (kegg_agent, reactome_agent, wikipathways_agent) run
simultaneously via LangGraph's Send fan-out. Each writes to raw_pathways, which
accumulates via operator.add reducer.

No LLM calls here — pure deterministic API fetching.
"""

from scripts.state import PipelineState
from scripts.tools import kegg_tools, reactome_tools, wikipathways_tools


def kegg_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [KEGG] querying {len(search_terms)} terms...")
    pathways = kegg_tools.fetch_lps_pathways(search_terms)
    print(f"  [KEGG] found {len(pathways)} pathways")
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
