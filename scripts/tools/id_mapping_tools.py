"""Gene ID harmonization using MyGene.info API.

Maps gene symbols to Entrez, UniProt, and Ensembl IDs.
Docs: https://docs.mygene.info/en/latest/
"""

from typing import Dict, List

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import MYGENE_BASE


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def map_gene_symbols(symbols: List[str]) -> Dict[str, Dict]:
    """
    Batch-query MyGene.info for a list of gene symbols.
    Returns: {symbol: {entrez, uniprot, ensembl}}
    """
    if not symbols:
        return {}

    resp = requests.post(
        f"{MYGENE_BASE}/gene/query",
        data={
            "q": ",".join(symbols),
            "scopes": "symbol",
            "fields": "symbol,entrezgene,uniprot,ensembl.gene",
            "species": "human",
            "returnall": "false",
        },
        timeout=30,
    )
    resp.raise_for_status()
    hits = resp.json()

    mapping: Dict[str, Dict] = {}
    for hit in hits:
        if hit.get("notfound"):
            continue
        sym = hit.get("symbol", "").upper()
        if not sym:
            continue
        uniprot_raw = hit.get("uniprot", {})
        uniprot_id = (
            uniprot_raw.get("Swiss-Prot", "")
            if isinstance(uniprot_raw, dict)
            else (uniprot_raw[0] if isinstance(uniprot_raw, list) else "")
        )
        ensembl_raw = hit.get("ensembl", {})
        ensembl_id = (
            ensembl_raw.get("gene", "")
            if isinstance(ensembl_raw, dict)
            else (ensembl_raw[0].get("gene", "") if isinstance(ensembl_raw, list) else "")
        )
        mapping[sym] = {
            "entrez": str(hit.get("entrezgene", "")),
            "uniprot": uniprot_id,
            "ensembl": ensembl_id,
        }

    return mapping


def collect_all_genes(raw_pathways: List[Dict]) -> List[str]:
    """Extract unique gene symbols from all collected pathways."""
    genes = set()
    for pw in raw_pathways:
        for g in pw.get("genes", []):
            if g and isinstance(g, str):
                genes.add(g.upper())
    return sorted(genes)
