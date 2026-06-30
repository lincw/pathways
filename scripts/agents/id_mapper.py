"""ID harmonization node — Ch.5 (Tool Use).

Maps all gene symbols collected from parallel DB agents to unified IDs
(Entrez, UniProt, Ensembl) using MyGene.info. Deduplicates raw_pathways
on (source, pathway_id) to handle accumulated duplicates across reflection iterations.
"""

from scripts.state import PipelineState
from scripts.tools.id_mapping_tools import collect_all_genes, map_gene_symbols


def id_mapper_node(state: PipelineState) -> dict:
    raw = state.get("raw_pathways", [])

    # Deduplicate pathways that may have been collected in multiple iterations
    seen = set()
    unique_pathways = []
    for pw in raw:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            unique_pathways.append(pw)

    print(f"  [ID Mapper] {len(raw)} raw → {len(unique_pathways)} unique pathways")

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

    # Write back deduplicated pathways by overriding raw_pathways.
    # Since raw_pathways uses operator.add, we cannot "reset" it here —
    # instead we store the deduplicated view separately; synthesizer uses id_mapping.
    return {
        "id_mapping": mapping,
        # raw_pathways stays as-is; synthesizer will re-deduplicate
    }
