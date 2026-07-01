"""ID harmonization node

Maps all gene symbols collected from parallel DB agents to unified IDs
(Entrez, UniProt, Ensembl) using MyGene.info. Deduplicates raw_pathways
on (source, pathway_id) to handle accumulated duplicates across reflection iterations.
"""

from scripts.state import PipelineState, working_pathways
from scripts.tools.id_mapping_tools import collect_all_genes, map_gene_symbols


def id_mapper_node(state: PipelineState) -> dict:
    # Use the relevance-filtered pathway set (already deduplicated) so we only
    # map genes that survive enrichment + the LLM gate.
    unique_pathways = working_pathways(state)

    print(f"  [ID Mapper] mapping genes for {len(unique_pathways)} filtered pathways")

    all_genes = collect_all_genes(unique_pathways)
    print(f"  [ID Mapper] mapping {len(all_genes)} unique gene symbols...")

    # Batch in chunks of 100 — MyGene.info POST body can grow large at 200
    mapping = {}
    chunk_size = 100
    for i in range(0, len(all_genes), chunk_size):
        chunk = all_genes[i : i + chunk_size]
        try:
            mapping.update(map_gene_symbols(chunk))
        except Exception as exc:
            print(f"  [ID Mapper] chunk {i//chunk_size + 1} failed: {exc}", flush=True)

    print(f"  [ID Mapper] successfully mapped {len(mapping)} genes")

    return {"id_mapping": mapping}
