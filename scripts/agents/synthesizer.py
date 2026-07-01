"""Synthesis node — builds the unified signaling gene-pathway graph.

Removed hub_genes (not needed). Focuses on building the bipartite
gene ↔ pathway graph with provenance (which DB contributed each edge).
"""

from collections import Counter

import networkx as nx

from scripts.state import PipelineState, working_pathways


def synthesizer_node(state: PipelineState) -> dict:
    id_mapping = state.get("id_mapping", {})
    # Symbols MyGene.info could not resolve to any gene ID — not real genes
    # (reaction cofactors, complex names, viral proteins). Excluded here rather
    # than just left unannotated, so they don't ride along in genes.tsv/the
    # network as if they were verified.
    unmapped = set(state.get("unmapped_gene_symbols", []))

    # Relevance-filtered, deduplicated pathway set.
    pathways = working_pathways(state)

    print(f"  [Synthesizer] building graph from {len(pathways)} filtered pathways...")

    G = nx.Graph()

    for pw in pathways:
        G.add_node(pw["pathway_id"], type="pathway",
                   name=pw["pathway_name"], source=pw["source"],
                   description=pw.get("description", ""))

    dropped = set()
    for pw in pathways:
        for gene in pw.get("genes", []):
            g = gene.upper().strip()
            if not g:
                continue
            if g in unmapped:
                dropped.add(g)
                continue
            if g not in G:
                meta = id_mapping.get(g, {})
                G.add_node(g, type="gene",
                           entrez=meta.get("entrez", ""),
                           uniprot=meta.get("uniprot", ""),
                           ensembl=meta.get("ensembl", ""))
            G.add_edge(g, pw["pathway_id"], db=pw["source"])

    if dropped:
        print(f"  [Synthesizer] excluded {len(dropped)} non-gene symbols: "
              f"{sorted(dropped)[:15]}{' ...' if len(dropped) > 15 else ''}")

    db_coverage = dict(Counter(pw["source"] for pw in pathways))

    nodes = [{"id": n, **{k: str(v) for k, v in data.items()}}
             for n, data in G.nodes(data=True)]

    gene_count = sum(1 for n in nodes if n.get("type") == "gene")
    print(f"  [Synthesizer] {len(pathways)} pathways, {gene_count} genes")

    # The signaling network edges (protein→protein) are built later by the
    # network_builder node; the synthesizer only assembles the gene/pathway nodes.
    return {"nodes": nodes, "db_coverage": db_coverage}
