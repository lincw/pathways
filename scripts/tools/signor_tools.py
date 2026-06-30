"""SIGNOR REST API tools.

SIGNOR (SIGnaling Network Open Resource) provides curated causal signaling
relationships annotated with effect (up/down-regulates) and mechanism
(phosphorylation, binding, ubiquitination, etc.).

Real API (TSV/PHP — not JSON/REST):
  List pathways:    GET /getPathwayData.php?description
                    → TSV: sig_id | path_name | path_description | path_curator
  Pathway relations: GET /getPathwayData.php?pathway={id}&relations=only
                    → TSV: pathway_id | pathway_name | entitya | regulator_location
                           | typea | ida | databasea | entityb | target_location
                           | typeb | idb | databaseb | effect | mechanism
                           | residue | sequence | tax_id | ...

Column indices (0-based) used for gene extraction:
  2  = entitya   (gene symbol / family name)
  4  = typea     ("protein", "proteinfamily", "complex", "chemical", ...)
  7  = entityb
  9  = typeb
  16 = tax_id    (9606 for human)
"""

import csv
import io
import re
import time
from typing import List, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import SIGNOR_BASE
from scripts.state import PathwayEntry

_GENE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_PROTEIN_TYPES = {"protein", "proteinfamily"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_tsv(endpoint: str) -> List[List[str]]:
    """Fetch a SIGNOR TSV endpoint and return rows as lists of strings."""
    resp = requests.get(f"{SIGNOR_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    # SIGNOR descriptions occasionally contain bare \r — strip them so csv.reader
    # doesn't treat them as record separators inside unquoted fields.
    clean = resp.text.replace("\r", " ")
    reader = csv.reader(io.StringIO(clean), delimiter="\t")
    rows = list(reader)
    # Skip header row
    return rows[1:] if rows else []


def _pathway_list() -> List[dict]:
    """Return all SIGNOR pathways as list of {id, name, description} dicts."""
    rows = _fetch_tsv("/getPathwayData.php?description")
    pathways = []
    for row in rows:
        if len(row) < 2:
            continue
        pathways.append({
            "id":   row[0].strip(),
            "name": row[1].strip(),
            "desc": row[2].strip() if len(row) > 2 else "",
        })
    return pathways


def _pathway_relations(pathway_id: str) -> List[List[str]]:
    """Return all relation rows for a SIGNOR pathway."""
    return _fetch_tsv(f"/getPathwayData.php?pathway={pathway_id}&relations=only")


def _genes_from_rows(rows: List[List[str]]) -> List[str]:
    """Extract unique human protein gene symbols from SIGNOR relation rows.

    Columns: 2=entitya, 4=typea, 7=entityb, 9=typeb, 16=tax_id.
    Accepts type "protein" and "proteinfamily"; skips chemicals, complexes,
    stimuli, phenotypes. Applies gene-symbol regex to filter display names
    that are not gene symbols (e.g. "14-3-3 protein beta/alpha").
    """
    genes: Set[str] = set()
    for row in rows:
        if len(row) < 17:
            continue
        tax = row[16].strip()
        if tax and tax != "9606":
            continue
        for entity, etype in ((row[2], row[4]), (row[7], row[9])):
            if etype.strip().lower() not in _PROTEIN_TYPES:
                continue
            name = entity.strip()
            if name and _GENE_RE.match(name):
                genes.add(name)
    return sorted(genes)


def _build_keywords(search_terms: List[str]) -> List[str]:
    """Extract search keywords from planner-generated terms.

    Split on whitespace only (not hyphens) so that "NF-kB" stays together
    and cleans to "nfkb" (4 chars) rather than being dropped as "nf" + "kb"
    (2 chars each, below the minimum).
    """
    stopwords = {"and", "the", "in", "of", "a", "an", "to", "is", "for", "by", "with",
                 "signaling", "pathway", "response", "activation", "dependent"}
    keywords: list[str] = []
    seen: set[str] = set()
    for term in search_terms:
        for word in term.lower().split():           # split on whitespace only
            word = re.sub(r"[^a-z0-9]", "", word)  # strip hyphens, parens, etc.
            if len(word) >= 3 and word not in stopwords and word not in seen:
                seen.add(word)
                keywords.append(word)
    return keywords


def fetch_signor_pathways(search_terms: List[str]) -> List[PathwayEntry]:
    """Keyword-match SIGNOR pathway names/descriptions and fetch their relations."""
    try:
        all_pathways = _pathway_list()
    except Exception as exc:
        print(f"  [SIGNOR] failed to list pathways: {exc}", flush=True)
        return []

    keywords = _build_keywords(search_terms)
    if not keywords:
        return []

    # Score: 2 pts per keyword hit in name, 1 pt in description.
    # Normalize both sides by stripping non-alphanumeric so "NF-KB Canonical"
    # matches keyword "nfkb" (which came from search term "NF-kB ...").
    scored = []
    for pw in all_pathways:
        name_n = re.sub(r"[^a-z0-9 ]", "", pw["name"].lower())
        desc_n = re.sub(r"[^a-z0-9 ]", "", pw["desc"].lower())
        score = sum(2 * (kw in name_n) + (kw in desc_n) for kw in keywords)
        if score > 0:
            scored.append((score, pw))
    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [pw for _, pw in scored[:15]]  # cap at 15 pathways

    if not matched:
        print(f"  [SIGNOR] 0 pathways matched keywords: {keywords[:8]}", flush=True)
        return []

    entries: List[PathwayEntry] = []
    for pw in matched:
        print(f"  [SIGNOR] {pw['id']}: {pw['name']}", flush=True)
        try:
            rows = _pathway_relations(pw["id"])
            genes = _genes_from_rows(rows)
            entries.append(PathwayEntry(
                source="SIGNOR",
                pathway_id=pw["id"],
                pathway_name=pw["name"],
                genes=genes,
                description=pw["desc"][:200],
            ))
            time.sleep(0.3)
        except Exception as exc:
            print(f"  [SIGNOR] skipping {pw['id']}: {exc}", flush=True)

    return entries
