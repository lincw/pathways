"""Reactome ContentService REST API tools.

Key fixes vs original:
- Endpoint: /data/participants/{id}  (not /participatingPhysicalEntities)
- geneName is a LIST in the Reactome response — handle accordingly
- Fallback: parse gene symbol from displayName "GENE [compartment]"
"""

import re
import time
from typing import List, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import REACTOME_BASE
from scripts.state import PathwayEntry


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _reactome_get(endpoint: str, params: dict = None):
    resp = requests.get(f"{REACTOME_BASE}{endpoint}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def search_reactome_pathways(query: str) -> List[dict]:
    """Search Reactome for human pathways. Returns [{id, name, description}]."""
    data = _reactome_get("/search/query", params={
        "query": query,
        "species": "Homo sapiens",
        "types": "Pathway",
        "cluster": "true",
    })
    results = []
    for entry in data.get("results", []):
        for item in entry.get("entries", [])[:5]:
            stid = item.get("stId", "")
            if stid:
                results.append({
                    "id": stid,
                    "name": item.get("name", ""),
                    "description": item.get("summation", "")[:300],
                })
    return results[:10]


_GENE_PATTERN = re.compile(r'^[A-Z][A-Z0-9]{1,14}$')


def _extract_genes_from_entity(entity: dict) -> List[str]:
    """Extract gene symbol(s) from a Reactome PhysicalEntity dict."""
    genes = []

    # geneName is a list in Reactome API responses
    gene_names = entity.get("geneName", [])
    if isinstance(gene_names, list):
        for g in gene_names:
            if g and _GENE_PATTERN.match(str(g)):
                genes.append(str(g))
    elif isinstance(gene_names, str) and _GENE_PATTERN.match(gene_names):
        genes.append(gene_names)

    # Fallback: parse "SYMBOL [compartment]" from displayName
    if not genes:
        display = entity.get("displayName", "")
        name = display.split("[")[0].split(":")[0].strip()
        if name and _GENE_PATTERN.match(name):
            genes.append(name)

    return genes


def get_reactome_pathway_genes(pathway_stable_id: str) -> List[str]:
    """Get gene symbols for all participants in a Reactome pathway."""
    try:
        # Correct endpoint: /data/participants/{id}
        data = _reactome_get(f"/data/participants/{pathway_stable_id}")
    except Exception:
        return []

    genes: Set[str] = set()
    for entity in data:
        genes.update(_extract_genes_from_entity(entity))
    return sorted(genes)


def get_reactome_pathway_description(pathway_stable_id: str) -> str:
    try:
        data = _reactome_get(f"/data/query/{pathway_stable_id}")
        name = data.get("displayName", "")
        summations = data.get("summation", [])
        text = summations[0].get("text", "") if summations else ""
        return f"{name}. {text[:300]}".strip()
    except Exception:
        return ""


def fetch_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Search Reactome with text terms and fetch genes for each pathway."""
    seen_ids: Set[str] = set()
    entries: List[PathwayEntry] = []

    for term in search_terms:
        try:
            matches = search_reactome_pathways(term)
            for match in matches[:5]:
                pid = match["id"]
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                try:
                    genes = get_reactome_pathway_genes(pid)
                    desc = get_reactome_pathway_description(pid) or match.get("description", "")
                    entries.append(PathwayEntry(
                        source="Reactome",
                        pathway_id=pid,
                        pathway_name=match["name"],
                        genes=genes,
                        description=desc[:500],
                    ))
                    time.sleep(0.2)
                except Exception:
                    pass
        except Exception:
            pass

    return entries
