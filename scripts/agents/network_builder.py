"""Network builder — assembles the protein→protein signaling network.

The pipeline's real output edge list should be protein-protein interactions
(directed causal signaling), NOT gene↔pathway membership. This node fetches
directed edges for the pathways that SURVIVED filtering (so it only calls the
extra endpoints for ~tens of pathways, not the pre-filter hundreds) and keeps
only edges between proteins that are in the final collected gene set.

Edge sources (both directed):
  - SIGNOR : curated causal relations (A up/down-regulates B, + mechanism)
  - KEGG   : KGML PPrel/GErel relations (activation/inhibition, + mechanism)
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from scripts.state import PipelineState, working_pathways
from scripts.tools import kegg_tools, signor_tools


def network_node(state: PipelineState) -> dict:
    pathways = working_pathways(state)
    gene_set = {n["id"] for n in state.get("nodes", []) if n.get("type") == "gene"}

    edges: List[dict] = []
    for pw in pathways:
        src, pid = pw["source"], pw["pathway_id"]
        if src == "SIGNOR":
            edges.extend(signor_tools.signor_edges_for_pathway(pid))
        elif src == "KEGG":
            edges.extend(kegg_tools.kegg_edges_for_pathway(pid))

    # Keep only interactions between collected proteins, then deduplicate.
    edges = [e for e in edges if e["source"] in gene_set and e["target"] in gene_set]
    seen = set()
    unique: List[dict] = []
    for e in edges:
        key = (e["source"], e["target"], e["effect"], e["mechanism"], e["db"])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    by_db = dict(Counter(e["db"] for e in unique))
    nodes_in_net = len({e["source"] for e in unique} | {e["target"] for e in unique})
    print(f"  [Network] {len(unique)} protein→protein edges "
          f"({by_db}) over {nodes_in_net} proteins")

    stats: Dict = {"edges": len(unique), "proteins_in_network": nodes_in_net, "by_db": by_db}
    return {"edges": unique, "network_stats": stats}
