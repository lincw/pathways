"""WikiPathways tools via SPARQL endpoint.

The WikiPathways v2 REST URL was unreliable. Switching to the stable SPARQL
endpoint (https://sparql.wikipathways.org/sparql) which is the canonical
machine-readable interface for WikiPathways data.

Pathway search: FILTER on pathway title matching any keyword
Gene extraction: wp:GeneProduct rdfs:label in the pathway graph
"""

import re
import time
from typing import List, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.state import PathwayEntry

SPARQL_URL = "https://sparql.wikipathways.org/sparql"
SPARQL_HEADERS = {"Accept": "application/sparql-results+json"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _sparql(query: str) -> dict:
    resp = requests.get(
        SPARQL_URL,
        params={"query": query},
        headers=SPARQL_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def search_wikipathways(search_terms: List[str]) -> List[dict]:
    """Search WikiPathways for human pathways whose title matches any term."""
    # Build FILTER with CONTAINS conditions (limit terms to avoid huge queries)
    terms = search_terms[:8]
    filters = " || ".join(
        f'CONTAINS(LCASE(STR(?title)), "{t.lower().split()[0]}")'
        for t in terms
    )
    query = f"""
PREFIX wp:      <http://vocabularies.wikipathways.org/wp#>
PREFIX dc:      <http://purl.org/dc/elements/1.1/>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX rdfs:    <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?pathway ?title WHERE {{
  ?pathway a wp:Pathway ;
           dc:title ?title ;
           dcterms:isPartOf ?version .
  ?version wp:organism ?organism .
  ?organism rdfs:label "Homo sapiens"@en .
  FILTER ({filters})
}}
LIMIT 25
"""
    data = _sparql(query)
    results = []
    for binding in data.get("results", {}).get("bindings", []):
        uri   = binding.get("pathway", {}).get("value", "")
        title = binding.get("title",   {}).get("value", "")
        if not uri or not title:
            continue
        # Extract WP identifier from URI, e.g. "https://identifiers.org/wikipathways/WP4258"
        match = re.search(r"(WP\d+)", uri)
        wp_id = match.group(1) if match else uri.split("/")[-1]
        results.append({"id": wp_id, "name": title})
    return results


def get_wikipathways_genes(wp_id: str) -> List[str]:
    """Get gene symbols for all GeneProduct nodes in a WikiPathways pathway."""
    query = f"""
PREFIX wp:      <http://vocabularies.wikipathways.org/wp#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX rdfs:    <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?label WHERE {{
  ?gene a wp:GeneProduct ;
        rdfs:label ?label ;
        dcterms:isPartOf <https://identifiers.org/wikipathways/{wp_id}> .
}}
"""
    try:
        data = _sparql(query)
        genes = []
        for binding in data.get("results", {}).get("bindings", []):
            label = binding.get("label", {}).get("value", "").strip()
            if label and len(label) < 30:
                genes.append(label)
        return genes
    except Exception:
        return []


def fetch_lps_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Search WikiPathways and fetch genes for each matched pathway."""
    seen_ids: Set[str] = set()
    entries: List[PathwayEntry] = []

    try:
        matches = search_wikipathways(search_terms)
    except Exception:
        return []

    for match in matches:
        wp_id = match["id"]
        if not wp_id or wp_id in seen_ids:
            continue
        seen_ids.add(wp_id)
        try:
            genes = get_wikipathways_genes(wp_id)
            entries.append(PathwayEntry(
                source="WikiPathways",
                pathway_id=wp_id,
                pathway_name=match["name"],
                genes=genes,
                description="",
            ))
            time.sleep(0.3)
        except Exception:
            pass

    return entries
