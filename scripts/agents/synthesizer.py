"""Synthesis node — builds the unified LPS signaling network graph.

Uses NetworkX to build a bipartite gene-pathway graph and identifies hub genes
(genes shared across multiple pathways/databases). No LLM call — pure graph logic.
"""

from collections import Counter
from typing import Dict, List

import networkx as nx

from scripts.state import PipelineState


def synthesizer_node(state: PipelineState) -> dict:
    raw = state.get("raw_pathways", [])
    id_mapping = state.get("id_mapping", {})

    # Deduplicate pathways
    seen = set()
    pathways = []
    for pw in raw:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            pathways.append(pw)

    print(f"  [Synthesizer] building graph from {len(pathways)} unique pathways...")

    G = nx.Graph()

    # Node: pathway
    for pw in pathways:
        G.add_node(
            pw["pathway_id"],
            type="pathway",
            name=pw["pathway_name"],
            source=pw["source"],
            description=pw.get("description", ""),
        )

    # Node: gene; Edge: gene — pathway
    gene_pathway_count: Dict[str, int] = Counter()
    gene_db_sources: Dict[str, set] = {}

    for pw in pathways:
        for gene in pw.get("genes", []):
            gene_u = gene.upper()
            if not gene_u:
                continue
            if gene_u not in G.nodes:
                meta = id_mapping.get(gene_u, {})
                G.add_node(
                    gene_u,
                    type="gene",
                    entrez=meta.get("entrez", ""),
                    uniprot=meta.get("uniprot", ""),
                    ensembl=meta.get("ensembl", ""),
                )
            G.add_edge(gene_u, pw["pathway_id"], source=pw["source"])
            gene_pathway_count[gene_u] += 1
            gene_db_sources.setdefault(gene_u, set()).add(pw["source"])

    # Hub genes: appear in pathways from ≥2 different databases
    hub_genes = sorted(
        [g for g, sources in gene_db_sources.items() if len(sources) >= 2],
        key=lambda g: -gene_pathway_count[g],
    )

    # DB coverage summary
    db_coverage = Counter(pw["source"] for pw in pathways)

    # Serialise graph for state (LangGraph state must be JSON-serialisable)
    nodes = [
        {"id": n, **{k: str(v) for k, v in data.items()}}
        for n, data in G.nodes(data=True)
    ]
    edges = [
        {"source": u, "target": v, "db": data.get("source", "")}
        for u, v, data in G.edges(data=True)
    ]

    print(f"  [Synthesizer] {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(f"  [Synthesizer] {len(hub_genes)} hub genes across ≥2 databases")

    return {
        "nodes": nodes,
        "edges": edges,
        "hub_genes": hub_genes[:50],  # top 50
        "db_coverage": dict(db_coverage),
    }
