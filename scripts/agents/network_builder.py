"""Network builder — assembles the protein→protein signaling network.

The pipeline's real output edge list should be protein-protein interactions
(directed causal signaling). This node fetches
directed edges for the pathways that SURVIVED filtering (so it only calls the
extra endpoints for ~tens of pathways, not the pre-filter hundreds) and keeps
only edges between proteins that are in the final collected gene set.

Edge sources (all directed):
  - SIGNOR   : curated causal relations (A up/down-regulates B, + mechanism)
  - KEGG     : KGML PPrel/GErel relations (activation/inhibition, + mechanism)
  - Reactome : reaction graph projected to catalyst/regulator→output edges
               (regulation gives the sign; catalysis gives direction only)

Cross-database consensus: each directed (source→target) interaction is annotated
with the number of independent databases that assert it (``support``). Edges
confirmed by two or more sources form the *robust* conserved sub-network — the
same interaction independently curated by different resources.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List

from scripts.config import REACTOME_EDGES, ROBUST_MIN_SUPPORT
from scripts.state import PipelineState, working_pathways
from scripts.tools import kegg_tools, reactome_tools, signor_tools


def _consensus(unique: List[dict]) -> List[dict]:
    """Collapse edges to one row per directed pair, annotated with DB support.

    Returns the *robust* rows (support >= ROBUST_MIN_SUPPORT): distinct DBs,
    support count, and the distinct effects/mechanisms each DB reported. Effects
    are listed verbatim (including 'unknown') — divergent signs are shown, not
    silently merged, so the user sees where databases disagree.
    """
    dbs: Dict = defaultdict(set)
    effects: Dict = defaultdict(set)
    mechs: Dict = defaultdict(set)
    for e in unique:
        key = (e["source"], e["target"])
        dbs[key].add(e["db"])
        if e["effect"]:
            effects[key].add(e["effect"])
        if e["mechanism"]:
            mechs[key].add(e["mechanism"])

    robust: List[dict] = []
    for key, db_set in dbs.items():
        if len(db_set) < ROBUST_MIN_SUPPORT:
            continue
        src, tgt = key
        robust.append({
            "source": src,
            "target": tgt,
            "support": len(db_set),
            "db_support": "|".join(sorted(db_set)),
            "effects": "|".join(sorted(effects[key])) or "unknown",
            "mechanisms": "|".join(sorted(mechs[key])),
        })
    robust.sort(key=lambda r: (-r["support"], r["source"], r["target"]))
    return robust


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
        elif src == "Reactome" and REACTOME_EDGES:
            edges.extend(reactome_tools.reactome_edges_for_pathway(pid))

    # Keep only interactions between collected proteins, then deduplicate.
    edges = [e for e in edges if e["source"] in gene_set and e["target"] in gene_set]
    seen = set()
    unique: List[dict] = []
    for e in edges:
        key = (e["source"], e["target"], e["effect"], e["mechanism"], e["db"])
        if key not in seen:
            seen.add(key)
            unique.append(e)

    # Cross-DB consensus: how many distinct databases assert each directed pair.
    pair_dbs: Dict = defaultdict(set)
    for e in unique:
        pair_dbs[(e["source"], e["target"])].add(e["db"])
    for e in unique:
        support_dbs = pair_dbs[(e["source"], e["target"])]
        e["support"] = len(support_dbs)
        e["db_support"] = "|".join(sorted(support_dbs))

    robust = _consensus(unique)

    by_db = dict(Counter(e["db"] for e in unique))
    by_support = dict(Counter(len(v) for v in pair_dbs.values()))
    nodes_in_net = len({e["source"] for e in unique} | {e["target"] for e in unique})
    robust_nodes = len({r["source"] for r in robust} | {r["target"] for r in robust})
    print(f"  [Network] {len(unique)} protein→protein edges "
          f"({by_db}) over {nodes_in_net} proteins")
    print(f"  [Network] robust (≥{ROBUST_MIN_SUPPORT} DBs): {len(robust)} "
          f"conserved edges over {robust_nodes} proteins; support {by_support}")

    stats: Dict = {
        "edges": len(unique),
        "proteins_in_network": nodes_in_net,
        "by_db": by_db,
        "unique_directed_pairs": len(pair_dbs),
        "by_support": by_support,
        "robust_min_support": ROBUST_MIN_SUPPORT,
        "robust_edges": len(robust),
        "robust_proteins": robust_nodes,
    }
    return {"edges": unique, "robust_edges": robust, "network_stats": stats}
