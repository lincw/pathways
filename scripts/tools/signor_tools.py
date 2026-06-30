"""SIGNOR REST API tools.

SIGNOR (SIGnaling Network Open Resource) provides curated causal signaling
relationships with annotated effect (up/down-regulates) and mechanism
(phosphorylation, binding, ubiquitination, etc.).

Unlike KEGG/Reactome which list genes per pathway, SIGNOR records HOW
proteins regulate each other — the directed edges are what makes it
valuable for signaling research.

Flow:
  search_terms → keyword filter on SIGNOR pathway name/description
               → fetch causal relations per matched pathway (/api/pathway/{id}/relations/)
               → extract human protein gene symbols from entitya / entityb
"""

import re
import time
from typing import List, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import SIGNOR_BASE
from scripts.state import PathwayEntry

_GENE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _get(endpoint: str) -> list | dict:
    resp = requests.get(f"{SIGNOR_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def _unwrap_list(data: list | dict, fallback_keys=("results", "pathways", "relations", "data")) -> List[dict]:
    """Return a list from a JSON response whether it's a bare list or wrapped in a key."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in fallback_keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _pathway_list() -> List[dict]:
    return _unwrap_list(_get("/api/pathway/"))


def _pathway_relations(pathway_id: str) -> List[dict]:
    return _unwrap_list(_get(f"/api/pathway/{pathway_id}/relations/"))


def _genes_from_relations(relations: List[dict]) -> List[str]:
    """Extract unique human protein gene symbols from SIGNOR relation records.

    Handles both nested format ({'entitya': {'name': 'TLR4', 'type': 'protein'}})
    and flat format ({'entitya_name': 'TLR4', 'entitya_type': 'protein'}).
    """
    genes: Set[str] = set()
    for rel in relations:
        tax = str(rel.get("tax_id") or rel.get("taxid") or "9606")
        if tax not in ("9606", ""):
            continue
        for prefix in ("entitya", "entityb"):
            nested = rel.get(prefix)
            if isinstance(nested, dict):
                etype = (nested.get("type") or "protein").lower()
                name = (nested.get("name") or nested.get("gene_name") or "").strip()
            else:
                etype = (rel.get(f"{prefix}_type") or "protein").lower()
                name = (rel.get(f"{prefix}_name") or "").strip()
            # Accept protein and proteinfamily; skip stimuli, phenotype, chemical
            if etype not in ("protein", "proteinfamily", ""):
                continue
            if name and _GENE_RE.match(name):
                genes.add(name)
    return sorted(genes)


def _build_keywords(search_terms: List[str]) -> List[str]:
    stopwords = {"and", "the", "in", "of", "a", "an", "to", "is", "for", "by", "with"}
    keywords: list[str] = []
    seen: set[str] = set()
    for term in search_terms:
        for word in re.split(r"[\s\-/]+", term.lower()):
            word = re.sub(r"[^a-z0-9κβα]", "", word)
            if len(word) >= 3 and word not in stopwords and word not in seen:
                seen.add(word)
                keywords.append(word)
    return keywords


def fetch_signor_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Search SIGNOR pathway list by keyword and return PathwayEntry list."""
    try:
        all_pathways = _pathway_list()
    except Exception as exc:
        print(f"  [SIGNOR] failed to list pathways: {exc}", flush=True)
        return []

    keywords = _build_keywords(search_terms)
    if not keywords:
        return []

    matched = []
    for pw in all_pathways:
        pw_id = (pw.get("id") or pw.get("signor_id") or pw.get("pathway_id") or "").strip()
        pw_name = (pw.get("name") or pw.get("pathway_name") or pw.get("label") or pw_id).strip()
        text = (pw_name + " " + (pw.get("description") or pw.get("abstract") or "")).lower()
        if any(kw in text for kw in keywords):
            matched.append({"id": pw_id, "name": pw_name})

    if not matched:
        print(f"  [SIGNOR] 0 pathways matched keywords: {keywords[:8]}", flush=True)
        return []

    entries: List[PathwayEntry] = []
    for pw in matched:
        pw_id, pw_name = pw["id"], pw["name"]
        print(f"  [SIGNOR] {pw_id}: {pw_name[:70]}", flush=True)
        try:
            relations = _pathway_relations(pw_id)
            genes = _genes_from_relations(relations)
            entries.append(PathwayEntry(
                source="SIGNOR",
                pathway_id=pw_id,
                pathway_name=pw_name,
                genes=genes,
                description="",
            ))
            time.sleep(0.3)
        except Exception as exc:
            print(f"  [SIGNOR] skipping {pw_id}: {exc}", flush=True)

    return entries
