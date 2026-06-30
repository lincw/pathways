"""KEGG REST API tools.

Docs: https://www.kegg.jp/kegg/rest/keggapi.html
All functions return plain Python dicts/lists — no LLM calls here.
"""

import re
import time
from typing import List, Dict

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import KEGG_BASE
from scripts.state import PathwayEntry


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _kegg_get(endpoint: str) -> str:
    resp = requests.get(f"{KEGG_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    return resp.text


def search_kegg_pathways(query: str, organism: str = "hsa") -> List[Dict]:
    """Search KEGG for human pathways matching query. Returns list of {id, name}."""
    raw = _kegg_get(f"/find/pathway/{query}")
    results = []
    for line in raw.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            pid, name = parts
            # Filter to human pathways (hsa prefix)
            if organism in pid:
                results.append({"id": pid.strip(), "name": name.strip()})
    return results


def get_kegg_pathway_genes(pathway_id: str) -> List[str]:
    """Fetch all gene symbols in a KEGG pathway."""
    raw = _kegg_get(f"/get/{pathway_id}")
    genes = []
    in_gene_section = False
    for line in raw.splitlines():
        if line.startswith("GENE"):
            in_gene_section = True
        elif line.startswith("COMPOUND") or line.startswith("REFERENCE") or line.startswith("///"):
            in_gene_section = False
        if in_gene_section:
            # Gene lines: "  12345  SYMBOL; full name"
            match = re.search(r"\d+\s+([A-Z0-9]+);", line)
            if match:
                genes.append(match.group(1))
    return list(dict.fromkeys(genes))  # deduplicate, preserve order


def get_kegg_pathway_description(pathway_id: str) -> str:
    """Return the NAME and DESCRIPTION fields from a KEGG pathway."""
    raw = _kegg_get(f"/get/{pathway_id}")
    name, description = "", ""
    for line in raw.splitlines():
        if line.startswith("NAME"):
            name = line.split(None, 1)[1].strip() if "\t" in line or " " in line else ""
        if line.startswith("DESCRIPTION"):
            description = line.split(None, 1)[1].strip() if len(line) > 12 else ""
    return f"{name}. {description}".strip()


def fetch_lps_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Main entry point: search + fetch for a list of terms. Returns PathwayEntry list."""
    seen_ids = set()
    entries: List[PathwayEntry] = []

    for term in search_terms:
        try:
            matches = search_kegg_pathways(term)
            for match in matches[:5]:  # cap per term to avoid overload
                pid = match["id"]
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                try:
                    genes = get_kegg_pathway_genes(pid)
                    desc = get_kegg_pathway_description(pid)
                    entries.append(PathwayEntry(
                        source="KEGG",
                        pathway_id=pid,
                        pathway_name=match["name"],
                        genes=genes,
                        description=desc,
                    ))
                    time.sleep(0.3)  # KEGG rate limit
                except Exception:
                    pass  # skip individual pathway failures
        except Exception:
            pass  # skip failed search terms

    return entries
