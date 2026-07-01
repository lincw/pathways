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

from scripts.config import REACTOME_BASE, REACTOME_EDGE_MAX_REACTIONS
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


# ---------------------------------------------------------------------------
# Reactome reaction graph → directed protein→protein signaling edges
# ---------------------------------------------------------------------------
#
# Reactome is reaction-centric, not edge-centric. We project each
# ReactionLikeEvent to directed protein→protein edges using the same causal
# roles Reactome curates (verified against live ContentService responses):
#
#   catalyst  → output   : the enzyme acts on the reaction's product.
#                          Direction is real; sign is NOT asserted by catalysis
#                          alone, so effect = "unknown", mechanism = "catalysis".
#   regulator → output   : sign taken from the Regulation subtype —
#                          PositiveRegulation → activation, NegativeRegulation
#                          → inhibition (this mirrors SIGNOR up/down-regulates).
#
# Complexes/sets are flattened to their member proteins via /data/participants,
# and only ReferenceGeneProduct (protein) participants are kept, which drops
# small molecules (ATP, LPS, …) automatically.

# ReactionLikeEvent schema classes that carry input/output/catalyst/regulation.
_REACTION_CLASSES = {"Reaction", "BlackBoxEvent", "Polymerisation",
                     "Depolymerisation", "FailedReaction"}

# Memo caches (module-level: reactions are shared across related pathways).
_gene_map_cache: dict = {}       # reaction stId -> {peDbId: set(genes)}
_control_cache: dict = {}        # catalyst/regulation dbId -> controller peDbId | None


def _gene_from_ref_display(display: str) -> str:
    """Gene symbol from a ReferenceGeneProduct displayName, e.g. 'UniProt:P18428 LBP'."""
    token = display.strip().split()[-1] if display.strip() else ""
    return token if _GENE_PATTERN.match(token) else ""


def _reaction_gene_map(reaction_stid: str) -> dict:
    """Map each participant peDbId of a reaction to its member protein genes.

    Complexes/sets are already flattened by /data/participants, so a Complex
    peDbId resolves to the genes of all its protein subunits.
    """
    if reaction_stid in _gene_map_cache:
        return _gene_map_cache[reaction_stid]
    out: dict = {}
    try:
        groups = _reactome_get(f"/data/participants/{reaction_stid}")
    except Exception:
        _gene_map_cache[reaction_stid] = out
        return out
    for grp in groups or []:
        pedbid = grp.get("peDbId")
        genes = set()
        for ref in grp.get("refEntities") or []:
            if ref.get("schemaClass") != "ReferenceGeneProduct":
                continue  # proteins only — skips ChEBI molecules, RNA, etc.
            g = _gene_from_ref_display(ref.get("displayName", ""))
            if g:
                genes.add(g)
        if pedbid is not None and genes:
            out[pedbid] = genes
    _gene_map_cache[reaction_stid] = out
    return out


def _control_pedbid(control_dbid):
    """Resolve a CatalystActivity/Regulation dbId to its controller PE dbId.

    Reactome does not populate the nested physicalEntity/regulator inline in the
    reaction view, so the control object must be fetched to recover the enzyme
    (CatalystActivity.physicalEntity) or the regulator (Regulation.regulator).
    """
    if control_dbid in _control_cache:
        return _control_cache[control_dbid]
    pedbid = None
    try:
        obj = _reactome_get(f"/data/query/{control_dbid}")
        controller = obj.get("physicalEntity") or obj.get("regulator") or {}
        pedbid = _as_dbid(controller)
    except Exception:
        pedbid = None
    _control_cache[control_dbid] = pedbid
    return pedbid


def _as_dbid(x):
    """A physical-entity reference may be a full dict or a bare int dbId."""
    if isinstance(x, dict):
        return x.get("dbId")
    if isinstance(x, int):
        return x
    return None


def _pe_dbids(role_value) -> List[object]:
    """dbIds of the physical entities in an input/output role list."""
    ids = [_as_dbid(x) for x in (role_value or [])]
    return [i for i in ids if i is not None]


def reactome_edges_for_pathway(pathway_id: str) -> List[dict]:
    """Directed protein→protein signaling edges projected from a Reactome pathway."""
    try:
        events = _reactome_get(f"/data/pathway/{pathway_id}/containedEvents")
    except Exception as exc:
        print(f"  [Reactome] containedEvents failed for {pathway_id}: {exc}", flush=True)
        return []

    reactions = [e for e in (events or [])
                 if isinstance(e, dict) and e.get("schemaClass") in _REACTION_CLASSES]
    if REACTOME_EDGE_MAX_REACTIONS > 0:
        reactions = reactions[:REACTOME_EDGE_MAX_REACTIONS]

    edges: List[dict] = []
    for ev in reactions:
        rid = ev.get("stId")
        if not rid:
            continue
        try:
            rxn = _reactome_get(f"/data/query/{rid}")
        except Exception:
            continue

        gene_map = _reaction_gene_map(rid)
        if not gene_map:
            continue

        out_dbids = _pe_dbids(rxn.get("output"))
        target_genes = {g for d in out_dbids for g in gene_map.get(d, ())}
        if not target_genes:
            continue

        def _emit(source_genes, effect, mechanism):
            for s in source_genes:
                for t in target_genes:
                    if s and t and s != t:
                        edges.append({
                            "source": s, "target": t, "effect": effect,
                            "mechanism": mechanism, "db": "Reactome",
                            "pathway_id": pathway_id,
                        })

        # catalyst → output (directed; sign not asserted by catalysis alone)
        for ca in rxn.get("catalystActivity") or []:
            if not isinstance(ca, dict):
                continue
            cat_pe = _as_dbid(ca.get("physicalEntity"))
            if cat_pe is None:
                cat_pe = _control_pedbid(ca.get("dbId"))
            _emit(gene_map.get(cat_pe, set()), "unknown", "catalysis")

        # regulator → output (signed by the regulation subtype)
        for rb in rxn.get("regulatedBy") or []:
            if not isinstance(rb, dict):
                continue
            cls = rb.get("schemaClass", "")
            if "PositiveRegulation" in cls:
                effect = "activation"
            elif "NegativeRegulation" in cls:
                effect = "inhibition"
            else:
                continue
            reg_pe = _as_dbid(rb.get("regulator"))
            if reg_pe is None:
                reg_pe = _control_pedbid(rb.get("dbId"))
            _emit(gene_map.get(reg_pe, set()), effect, "regulation")

        time.sleep(0.05)

    return edges
