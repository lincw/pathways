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
    Returns: {queried_symbol: {entrez, uniprot, ensembl}}

    Keyed by the ORIGINAL queried token (MyGene's "query" field), not the
    resolved official symbol ("symbol") — an alias-scope match resolves e.g.
    "RIP1" to official symbol "RIPK1", but network nodes are keyed by whatever
    literal string the source pathway database (KEGG/Reactome/SIGNOR) used, so
    the lookup must be retrievable under that original token. Ambiguous aliases
    (MyGene can return several genes for one alias) keep the first/highest-
    scored hit and drop the rest.
    """
    if not symbols:
        return {}

    resp = requests.post(
        f"{MYGENE_BASE}/query",   # correct batch endpoint (not /gene/query)
        data={
            "q": ",".join(symbols),
            # "alias" alongside "symbol" so genes referenced by an older/alternate
            # name (RIP1->RIPK1, IKKA->CHUK, FASL->FASLG) still resolve, instead
            # of looking like unmapped tokens.
            "scopes": "symbol,alias",
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
        query_token = hit.get("query", "").upper()
        if not query_token or query_token in mapping:
            continue  # keep the first (best-scored) hit for an ambiguous alias
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
        mapping[query_token] = {
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
