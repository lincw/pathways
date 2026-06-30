"""KEGG REST API tools — gene-based pathway lookup.

Instead of text search (which fails for multi-word scientific terms), we use
gene-based lookup: for each seed gene, find all human pathways containing it.
This is more reliable and biologically complete.

Flow:
  gene_symbol → KEGG gene ID (/find/hsa/{symbol})
              → pathway IDs  (/link/pathway/{kegg_id})
              → pathway data (/get/{pathway_id})
"""

import re
import time
from typing import Dict, List, Optional, Set

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from scripts.config import KEGG_BASE
from scripts.state import PathwayEntry


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _kegg_get(endpoint: str) -> str:
    resp = requests.get(f"{KEGG_BASE}{endpoint}", timeout=30)
    resp.raise_for_status()
    return resp.text


def _get_kegg_gene_id(gene_symbol: str) -> Optional[str]:
    """Resolve a gene symbol to a KEGG human gene ID (e.g. 'TLR4' → 'hsa:7100')."""
    try:
        raw = _kegg_get(f"/find/hsa/{gene_symbol}")
        for line in raw.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                kegg_id = parts[0].strip()        # "hsa:7100"
                description = parts[1].upper()    # "TLR4; TOLL-LIKE RECEPTOR 4..."
                if gene_symbol.upper() in description:
                    return kegg_id
        # fallback: return first result if any
        first = raw.strip().splitlines()
        if first:
            return first[0].split("\t")[0].strip()
    except Exception:
        pass
    return None


def _get_pathway_ids_for_gene(kegg_gene_id: str) -> List[str]:
    """Return human pathway IDs (e.g. 'hsa04620') containing the given gene."""
    try:
        raw = _kegg_get(f"/link/pathway/{kegg_gene_id}")
        ids = []
        for line in raw.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                pid = parts[1].strip()   # "path:hsa04620"
                if "hsa" in pid:
                    ids.append(pid.replace("path:", ""))
        return ids
    except Exception:
        return []


def _get_pathway_genes(pathway_id: str) -> List[str]:
    """Extract gene symbols from a KEGG pathway flat file."""
    try:
        raw = _kegg_get(f"/get/{pathway_id}")
    except Exception:
        return []

    genes = []
    in_gene_section = False
    for line in raw.splitlines():
        if line.startswith("GENE"):
            in_gene_section = True
        elif re.match(r"^[A-Z]", line) and not line.startswith("GENE"):
            in_gene_section = False
        if in_gene_section:
            # Format: "GENE        12345  SYMBOL; full name [KO:...]"
            match = re.search(r"\d+\s+([A-Za-z][A-Z0-9a-z]+);", line)
            if match:
                genes.append(match.group(1))
    return list(dict.fromkeys(genes))  # deduplicate, preserve order


def _get_pathway_name_and_desc(pathway_id: str, raw: str) -> tuple[str, str]:
    """Parse NAME and DESCRIPTION from a KEGG pathway flat file."""
    name, desc = pathway_id, ""
    for line in raw.splitlines():
        if line.startswith("NAME"):
            name = re.sub(r"\s+", " ", line[4:]).strip().rstrip(" - Homo sapiens (human)")
        if line.startswith("DESCRIPTION"):
            desc = re.sub(r"\s+", " ", line[11:]).strip()
    return name, desc


def fetch_lps_pathways(seed_genes: List[str]) -> List[PathwayEntry]:
    """Main entry: find all KEGG pathways containing any of the seed genes."""
    seen_pathway_ids: Set[str] = set()
    entries: List[PathwayEntry] = []

    for gene in seed_genes:
        kegg_id = _get_kegg_gene_id(gene)
        if not kegg_id:
            continue
        time.sleep(0.2)

        pathway_ids = _get_pathway_ids_for_gene(kegg_id)
        for pid in pathway_ids:
            if pid in seen_pathway_ids:
                continue
            seen_pathway_ids.add(pid)
            try:
                raw = _kegg_get(f"/get/{pid}")
                genes = _get_pathway_genes(pid)  # parse from already-fetched raw below
                name, desc = _get_pathway_name_and_desc(pid, raw)
                # re-parse genes from the raw we already have
                gene_list = []
                in_section = False
                for line in raw.splitlines():
                    if line.startswith("GENE"):
                        in_section = True
                    elif re.match(r"^[A-Z]", line) and not line.startswith("GENE"):
                        in_section = False
                    if in_section:
                        m = re.search(r"\d+\s+([A-Za-z][A-Z0-9a-z]+);", line)
                        if m:
                            gene_list.append(m.group(1))
                entries.append(PathwayEntry(
                    source="KEGG",
                    pathway_id=pid,
                    pathway_name=name,
                    genes=list(dict.fromkeys(gene_list)),
                    description=desc,
                ))
                time.sleep(0.3)
            except Exception:
                pass

    print(f"  [KEGG] {len(seed_genes)} seed genes → {len(entries)} pathways")
    return entries
