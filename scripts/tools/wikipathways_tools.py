"""WikiPathways REST API v2 tools.

Docs: https://www.wikipathways.org/api.html
Returns plain Python data — no LLM calls here.
"""

import time
from typing import List, Dict

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import WIKIPATHWAYS_BASE
from scripts.state import PathwayEntry


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _wp_get(endpoint: str, params: dict = None) -> dict | list:
    resp = requests.get(
        f"{WIKIPATHWAYS_BASE}{endpoint}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def search_wikipathways(query: str, species: str = "Homo sapiens") -> List[Dict]:
    """Search WikiPathways. Returns list of {id, name, description}."""
    data = _wp_get("/pathways", params={"query": query, "species": species})
    results = []
    for pw in data if isinstance(data, list) else data.get("pathways", []):
        results.append({
            "id": pw.get("id", ""),
            "name": pw.get("name", ""),
            "description": pw.get("description", ""),
        })
    return results[:10]


def get_wikipathways_genes(pathway_id: str) -> List[str]:
    """Get gene symbols in a WikiPathways pathway."""
    try:
        data = _wp_get(f"/pathways/{pathway_id}")
        genes = []
        for item in data.get("datanodes", []):
            if item.get("type") in ("GeneProduct", "Protein", "RNA"):
                label = item.get("label", "").strip()
                if label and len(label) < 20:
                    genes.append(label)
        return list(dict.fromkeys(genes))
    except Exception:
        return []


def fetch_lps_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Main entry point: search + fetch for a list of terms. Returns PathwayEntry list."""
    seen_ids = set()
    entries: List[PathwayEntry] = []

    for term in search_terms:
        try:
            matches = search_wikipathways(term)
            for match in matches[:5]:
                pid = match["id"]
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                try:
                    genes = get_wikipathways_genes(pid)
                    entries.append(PathwayEntry(
                        source="WikiPathways",
                        pathway_id=pid,
                        pathway_name=match["name"],
                        genes=genes,
                        description=match.get("description", "")[:500],
                    ))
                    time.sleep(0.2)
                except Exception:
                    pass
        except Exception:
            pass

    return entries
