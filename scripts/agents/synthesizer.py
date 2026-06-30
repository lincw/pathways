"""Synthesis node — builds the unified LPS signaling gene-pathway graph.

Removed hub_genes (not needed). Focuses on building the bipartite
gene ↔ pathway graph with provenance (which DB contributed each edge).
"""

from collections import Counter

import networkx as nx

from scripts.state import PipelineState


def synthesizer_node(state: PipelineState) -> dict:
    raw = state.get("raw_pathways", [])
    id_mapping = state.get("id_mapping", {})

    # Deduplicate on (source, pathway_id)
    seen = set()
    pathways = []
    for pw in raw:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            pathways.append(pw)

    print(f"  [Synthesizer] building graph from {len(pathways)} unique pathways...")

    G = nx.Graph()

    for pw in pathways:
        G.add_node(pw["pathway_id"], type="pathway",
                   name=pw["pathway_name"], source=pw["source"],
                   description=pw.get("description", ""))

    for pw in pathways:
        for gene in pw.get("genes", []):
            g = gene.upper().strip()
            if not g:
                continue
            if g not in G:
                meta = id_mapping.get(g, {})
                G.add_node(g, type="gene",
                           entrez=meta.get("entrez", ""),
                           uniprot=meta.get("uniprot", ""),
                           ensembl=meta.get("ensembl", ""))
            G.add_edge(g, pw["pathway_id"], db=pw["source"])

    db_coverage = dict(Counter(pw["source"] for pw in pathways))

    nodes = [{"id": n, **{k: str(v) for k, v in data.items()}}
             for n, data in G.nodes(data=True)]
    edges = [{"source": u, "target": v, "db": d.get("db", "")}
             for u, v, d in G.edges(data=True)]

    gene_count = sum(1 for n in nodes if n.get("type") == "gene")
    print(f"  [Synthesizer] {len(pathways)} pathways, {gene_count} genes, {len(edges)} edges")

    return {"nodes": nodes, "edges": edges, "db_coverage": db_coverage}
