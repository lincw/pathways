"""Parallel database agent nodes

All three databases are symmetric entry points and each returns whole pathways
(full gene membership), selected by LLM from that database's catalogue (KEGG,
SIGNOR) or by text search (Reactome). Central relevance filtering over the pooled
candidates happens later in the pathway_filter node.
"""

from scripts.state import PipelineState
from scripts.tools import kegg_tools, reactome_tools, signor_tools


def kegg_agent_node(state: PipelineState) -> dict:
    query = state.get("query", "")
    search_terms = state.get("search_terms", [])
    print(f"  [KEGG] asking LLM to select relevant pathways...", flush=True)
    try:
        pathways = kegg_tools.fetch_pathways(query, search_terms)
    except Exception as exc:
        print(f"  [KEGG] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [KEGG] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}


def reactome_agent_node(state: PipelineState) -> dict:
    search_terms = state.get("search_terms", [])
    print(f"  [Reactome] querying {len(search_terms)} terms...", flush=True)
    try:
        pathways = reactome_tools.fetch_pathways(search_terms)
    except Exception as exc:
        print(f"  [Reactome] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [Reactome] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}


def signor_agent_node(state: PipelineState) -> dict:
    query = state.get("query", "")
    search_terms = state.get("search_terms", [])
    print(f"  [SIGNOR] asking LLM to select relevant pathways...", flush=True)
    try:
        pathways = signor_tools.fetch_signor_pathways(query, search_terms)
    except Exception as exc:
        print(f"  [SIGNOR] ERROR: {exc}", flush=True)
        pathways = []
    print(f"  [SIGNOR] found {len(pathways)} pathways", flush=True)
    return {"raw_pathways": pathways}
