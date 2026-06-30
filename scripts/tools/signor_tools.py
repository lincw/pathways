"""SIGNOR REST API tools.

SIGNOR (SIGnaling Network Open Resource) provides curated causal signaling
relationships annotated with effect (up/down-regulates) and mechanism
(phosphorylation, binding, ubiquitination, etc.).

Real API (TSV/PHP):
  List pathways:     GET /getPathwayData.php?description
                     → TSV: sig_id | path_name | path_description | path_curator
  Pathway relations: GET /getPathwayData.php?pathway={id}&relations=only
                     → TSV: pathway_id | pathway_name | entitya | regulator_location
                            | typea | ida | databasea | entityb | target_location
                            | typeb | idb | databaseb | effect | mechanism
                            | residue | sequence | tax_id | ...

Column indices (0-based) for gene extraction:
  2  = entitya   (gene symbol / family name)
  4  = typea     ("protein", "proteinfamily", "complex", "chemical", ...)
  7  = entityb
  9  = typeb
  16 = tax_id    (9606 for human)

Pathway selection is done by the LLM (not keyword matching) so no biology
is hardcoded here — the same tool works for any signaling query.
"""

import csv
import io
import re
import time
from typing import List, Set

import requests
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import SIGNOR_BASE
from scripts.llm import call_agy_structured
from scripts.state import PathwayEntry

_GENE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
_PROTEIN_TYPES = {"protein", "proteinfamily"}


# ---------------------------------------------------------------------------
# SIGNOR HTTP helpers
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _fetch_tsv(endpoint: str) -> List[List[str]]:
    """Fetch a SIGNOR TSV endpoint and return rows (header stripped)."""
    resp = requests.get(f"{SIGNOR_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    # Some description fields contain bare \r — strip them before csv.reader
    # to avoid "new-line character seen in unquoted field" errors.
    clean = resp.text.replace("\r", " ")
    reader = csv.reader(io.StringIO(clean), delimiter="\t")
    rows = list(reader)
    return rows[1:] if rows else []


def _pathway_list() -> List[dict]:
    """Return all SIGNOR pathways as {id, name, desc} dicts."""
    rows = _fetch_tsv("/getPathwayData.php?description")
    return [
        {
            "id":   row[0].strip(),
            "name": row[1].strip(),
            "desc": row[2].strip() if len(row) > 2 else "",
        }
        for row in rows if len(row) >= 2
    ]


def _pathway_relations(pathway_id: str) -> List[List[str]]:
    """Return all relation rows for a SIGNOR pathway."""
    return _fetch_tsv(f"/getPathwayData.php?pathway={pathway_id}&relations=only")


def _genes_from_rows(rows: List[List[str]]) -> List[str]:
    """Extract unique human protein gene symbols from SIGNOR relation rows."""
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


# ---------------------------------------------------------------------------
# LLM-based pathway selection
# ---------------------------------------------------------------------------

class _PathwaySelection(BaseModel):
    pathway_ids: List[str] = Field(
        description="SIGNOR pathway IDs relevant to the query, "
                    "e.g. ['SIGNOR-TLR', 'SIGNOR-NFKBC', 'SIGNOR-IIS']"
    )


def _llm_select_pathways(
    query: str,
    search_terms: List[str],
    all_pathways: List[dict],
) -> List[str]:
    """Ask the LLM to select relevant SIGNOR pathways from the full catalogue."""
    catalogue = "\n".join(f"  {p['id']}: {p['name']}" for p in all_pathways)
    terms_text = "\n".join(f"  - {t}" for t in search_terms) if search_terms else "  (none)"

    prompt = f"""You are a molecular biologist selecting pathway databases for a literature search.

Biological query: {query}

Specific signaling components the search should cover:
{terms_text}

From the SIGNOR catalogue below, select pathway IDs whose core signaling machinery
is DIRECTLY relevant to the query — receptors, adaptors, kinases, ubiquitin ligases,
transcription factors, or downstream effectors of this pathway.
Include pathways that share key signaling components.
Exclude pathways that only loosely relate via shared terminology.
Aim for 5–12 pathways.

SIGNOR catalogue:
{catalogue}
"""
    result = call_agy_structured(
        prompt,
        _PathwaySelection,
        desc="SIGNOR: selecting relevant pathways...",
    )
    # Validate: keep only IDs that actually exist in the catalogue
    valid = {p["id"] for p in all_pathways}
    return [pid for pid in result.pathway_ids if pid in valid]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_signor_pathways(query: str, search_terms: List[str]) -> List[PathwayEntry]:
    """Fetch SIGNOR pathways relevant to *query*, selected by LLM judgement."""
    try:
        all_pathways = _pathway_list()
    except Exception as exc:
        print(f"  [SIGNOR] failed to list pathways: {exc}", flush=True)
        return []

    try:
        selected_ids = _llm_select_pathways(query, search_terms, all_pathways)
    except Exception as exc:
        print(f"  [SIGNOR] LLM selection failed: {exc}", flush=True)
        return []

    id_to_pw = {p["id"]: p for p in all_pathways}
    entries: List[PathwayEntry] = []
    for pid in selected_ids:
        pw = id_to_pw.get(pid)
        if not pw:
            continue
        print(f"  [SIGNOR] {pid}: {pw['name']}", flush=True)
        try:
            rows = _pathway_relations(pid)
            genes = _genes_from_rows(rows)
            entries.append(PathwayEntry(
                source="SIGNOR",
                pathway_id=pid,
                pathway_name=pw["name"],
                genes=genes,
                description=pw["desc"][:200],
            ))
            time.sleep(0.3)
        except Exception as exc:
            print(f"  [SIGNOR] skipping {pid}: {exc}", flush=True)

    return entries
