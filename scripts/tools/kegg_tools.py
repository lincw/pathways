"""KEGG REST API tools — pathway-anchored catalogue selection.

The unit of collection is the pathway, not the gene. We fetch KEGG's human
pathway catalogue (/list/pathway/hsa) and let the LLM pick the pathway IDs that
belong to the queried signaling cascade, then take each selected pathway's FULL
member-gene list wholesale. This mirrors the SIGNOR tool and replaces the old
gene-seed lookup, which pulled in every pathway sharing a promiscuous hub gene
(the hub-gene blowup) and made the collected set depend on which seed genes the
planner LLM happened to invent.

Flow:
  query + search_terms → LLM selects pathway IDs from /list/pathway/hsa
                       → pathway data (/get/{pathway_id}) → full gene membership
"""

import re
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import KEGG_BASE
from scripts.llm import call_llm_structured
from scripts.state import PathwayEntry

# KGML relation subtypes → normalised effect / mechanism.
_KGML_EFFECT = {
    "activation": "activation", "expression": "activation",
    "inhibition": "inhibition", "repression": "inhibition",
}
_KGML_MECHANISM = {
    "phosphorylation", "dephosphorylation", "ubiquitination",
    "methylation", "glycosylation", "binding/association", "dissociation",
}
_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{1,14}$")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _kegg_get(endpoint: str) -> str:
    resp = requests.get(f"{KEGG_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    return resp.text


def _pathway_catalogue() -> List[dict]:
    """Return all human KEGG pathways as {id, name} dicts (/list/pathway/hsa).

    IDs come back bare (e.g. 'hsa04620'); the trailing ' - Homo sapiens (human)'
    suffix on names is stripped so the LLM sees clean pathway titles.
    """
    raw = _kegg_get("/list/pathway/hsa")
    out: List[dict] = []
    for line in raw.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            pid = parts[0].strip()
            name = parts[1].split(" - Homo sapiens")[0].strip()
            out.append({"id": pid, "name": name})
    return out


def _parse_pathway_flatfile(pathway_id: str, raw: str) -> tuple[str, str, List[str]]:
    """Parse NAME, DESCRIPTION, and the full GENE membership from a KEGG flat file."""
    name, desc = pathway_id, ""
    genes: List[str] = []
    in_gene_section = False
    for line in raw.splitlines():
        if line.startswith("NAME"):
            name = re.sub(r"\s+", " ", line[4:]).strip()
            name = name.split(" - Homo sapiens")[0].strip()
        elif line.startswith("DESCRIPTION"):
            desc = re.sub(r"\s+", " ", line[11:]).strip()

        if line.startswith("GENE"):
            in_gene_section = True
        elif re.match(r"^[A-Z]", line) and not line.startswith("GENE"):
            in_gene_section = False
        if in_gene_section:
            # Format: "GENE        12345  SYMBOL; full name [KO:...]"
            m = re.search(r"\d+\s+([A-Za-z][A-Z0-9a-z]+);", line)
            if m:
                genes.append(m.group(1))
    return name, desc, list(dict.fromkeys(genes))


def _pathway_entry(pathway_id: str) -> Optional[PathwayEntry]:
    """Fetch one KEGG pathway and build a PathwayEntry with full gene membership."""
    try:
        raw = _kegg_get(f"/get/{pathway_id}")
    except Exception as exc:
        print(f"  [KEGG] skipping {pathway_id}: {exc}", flush=True)
        return None
    name, desc, genes = _parse_pathway_flatfile(pathway_id, raw)
    return PathwayEntry(
        source="KEGG",
        pathway_id=pathway_id,
        pathway_name=name,
        genes=genes,
        description=desc,
    )


class _KeggPathwaySelection(BaseModel):
    pathway_ids: List[str] = Field(
        description="KEGG human pathway IDs (e.g. ['hsa04620', 'hsa04064']) whose "
                    "core machinery is part of the queried signaling pathway"
    )


def _llm_select_pathways(
    query: str,
    search_terms: List[str],
    catalogue: List[dict],
) -> List[str]:
    """Ask the LLM which KEGG pathways to collect, from the full human catalogue.

    This is the KEGG *entry* mechanism — proposing candidate pathways — mirroring
    the SIGNOR tool. Final relevance filtering over the pooled candidates from all
    three databases happens centrally in the pathway_filter node's LLM gate.
    """
    listing = "\n".join(f"  {p['id']}: {p['name']}" for p in catalogue)
    terms_text = "\n".join(f"  - {t}" for t in search_terms) if search_terms else "  (none)"

    prompt = f"""You are a molecular biologist selecting pathway maps for an analysis.

Biological query: {query}

Specific signaling components the search should cover:
{terms_text}

From the KEGG human pathway catalogue below, select the pathway IDs whose core
signaling machinery is DIRECTLY part of, or immediately upstream/downstream of,
the queried pathway — its receptors, adaptors, kinases, ubiquitin ligases,
transcription factors, and downstream effectors. Include pathways that share key
signaling components. EXCLUDE broad disease maps or unrelated processes (cancer,
neurodegeneration, metabolism, other infections) that merely share a hub gene.
Aim for 5-15 pathways.

KEGG catalogue:
{listing}
"""
    result = call_llm_structured(
        prompt,
        _KeggPathwaySelection,
        desc="KEGG: selecting relevant pathways...",
    )
    valid = {p["id"] for p in catalogue}
    return [pid for pid in result.pathway_ids if pid in valid]


def fetch_pathways(query: str, search_terms: List[str]) -> List[PathwayEntry]:
    """Collect KEGG pathways for *query* by LLM selection from the catalogue.

    Pathway-anchored: the LLM picks pathway IDs and we take each one's FULL gene
    membership. No gene-seed expansion (which pulled in every pathway sharing a
    hub gene and made the result depend on the planner's invented seed list).
    """
    try:
        catalogue = _pathway_catalogue()
    except Exception as exc:
        print(f"  [KEGG] failed to list pathways: {exc}", flush=True)
        return []

    try:
        selected_ids = _llm_select_pathways(query, search_terms, catalogue)
    except Exception as exc:
        print(f"  [KEGG] LLM selection failed: {exc}", flush=True)
        return []

    entries: List[PathwayEntry] = []
    seen: set = set()
    for pid in selected_ids:
        if pid in seen:
            continue
        seen.add(pid)
        print(f"  [KEGG] {pid}...", flush=True)
        entry = _pathway_entry(pid)
        if entry:
            entries.append(entry)
        time.sleep(0.3)

    print(f"  [KEGG] selected {len(entries)} pathways from catalogue of {len(catalogue)}")
    return entries


# ---------------------------------------------------------------------------
# KGML relations → directed signaling edges
# ---------------------------------------------------------------------------

def _kgml_entry_genes(root: ET.Element) -> Dict[str, List[str]]:
    """Map each KGML entry id to its gene symbol(s); expand group entries."""
    genes: Dict[str, List[str]] = {}
    groups: Dict[str, List[str]] = {}
    for entry in root.findall("entry"):
        eid = entry.get("id", "")
        etype = entry.get("type")
        if etype == "gene":
            syms: List[str] = []
            graphics = entry.find("graphics")
            if graphics is not None and graphics.get("name"):
                # graphics name is like "TLR4, CD284, TOLL..." — take valid symbols
                for part in graphics.get("name").split(","):
                    s = part.strip().rstrip(".").strip()
                    if _SYMBOL_RE.match(s):
                        syms.append(s)
                        break  # first symbol represents the entry
            genes[eid] = syms
        elif etype == "group":
            groups[eid] = [c.get("id", "") for c in entry.findall("component")]

    for gid, comps in groups.items():
        merged: List[str] = []
        for c in comps:
            merged.extend(genes.get(c, []))
        genes[gid] = merged
    return genes


def kegg_edges_for_pathway(pathway_id: str) -> List[dict]:
    """Directed signaling edges parsed from a KEGG pathway's KGML."""
    try:
        raw = _kegg_get(f"/get/{pathway_id}/kgml")
        root = ET.fromstring(raw)
    except Exception as exc:
        print(f"  [KEGG] KGML failed for {pathway_id}: {exc}", flush=True)
        return []

    entry_genes = _kgml_entry_genes(root)
    edges: List[dict] = []
    for rel in root.findall("relation"):
        # PPrel = protein-protein, GErel = gene-expression regulation
        if rel.get("type") not in ("PPrel", "GErel"):
            continue
        subs = [s.get("name", "") for s in rel.findall("subtype")]
        effect = "unknown"
        mechanism = ""
        for name in subs:
            if name in _KGML_EFFECT:
                effect = _KGML_EFFECT[name]
            if name in _KGML_MECHANISM:
                mechanism = name
        for ga in entry_genes.get(rel.get("entry1", ""), []):
            for gb in entry_genes.get(rel.get("entry2", ""), []):
                if ga and gb and ga != gb:
                    edges.append({
                        "source": ga,
                        "target": gb,
                        "effect": effect,
                        "mechanism": mechanism,
                        "db": "KEGG",
                        "pathway_id": pathway_id,
                    })
    return edges
