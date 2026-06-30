"""Reactome ContentService REST API tools.

Docs: https://reactome.org/ContentService/
Returns plain Python data — no LLM calls here.
"""

import time
from typing import List, Dict

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import REACTOME_BASE
from scripts.state import PathwayEntry

SPECIES_ID = 9606  # Homo sapiens


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _reactome_get(endpoint: str, params: dict = None) -> dict | list:
    resp = requests.get(f"{REACTOME_BASE}{endpoint}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def search_reactome_pathways(query: str) -> List[Dict]:
    """Search Reactome for pathways. Returns list of {id, name, description}."""
    data = _reactome_get("/search/query", params={
        "query": query,
        "species": "Homo sapiens",
        "types": "Pathway",
        "cluster": "true",
    })
    results = []
    for entry in data.get("results", []):
        for item in entry.get("entries", [])[:5]:
            results.append({
                "id": item.get("stId", ""),
                "name": item.get("name", ""),
                "description": item.get("summation", ""),
            })
    return results[:10]  # cap at 10 per query


def get_reactome_pathway_genes(pathway_stable_id: str) -> List[str]:
    """Get all gene symbols participating in a Reactome pathway."""
    try:
        data = _reactome_get(f"/data/participants/{pathway_stable_id}/participatingPhysicalEntities")
    except Exception:
        return []

    genes = set()
    for entity in data:
        gene_name = entity.get("geneName") or entity.get("displayName", "")
        if gene_name and gene_name.isupper() and len(gene_name) < 20:
            genes.add(gene_name)
    return sorted(genes)


def get_reactome_pathway_description(pathway_stable_id: str) -> str:
    """Return display name + summation text for a Reactome pathway."""
    try:
        data = _reactome_get(f"/data/query/{pathway_stable_id}")
        name = data.get("displayName", "")
        summations = data.get("summation", [])
        text = summations[0].get("text", "") if summations else ""
        return f"{name}. {text[:300]}".strip()
    except Exception:
        return ""


def fetch_lps_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Main entry point: search + fetch for a list of terms. Returns PathwayEntry list."""
    seen_ids = set()
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
