"""Reporter node — builds the systematic pathway narrative.

Does NOT compare databases. Uses the collected genes and pathways as
biological evidence to write a coherent, hierarchical LPS signaling narrative.
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from scripts.config import OUTPUT_DIR
from scripts.llm import call_agy
from scripts.state import PipelineState


def reporter_node(state: PipelineState) -> dict:
    raw = state.get("raw_pathways", [])
    db_coverage = state.get("db_coverage", {})
    nodes = state.get("nodes", [])
    edges = state.get("edges", [])
    assessment = state.get("coverage_assessment", "")
    id_mapping = state.get("id_mapping", {})
    query = state.get("query", "")
    required = state.get("required_components", [])

    # Deduplicate pathways
    seen = set()
    pathways = []
    for pw in raw:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            pathways.append(pw)

    all_genes = sorted({n["id"] for n in nodes if n.get("type") == "gene"})
    all_pathway_names = [pw["pathway_name"] for pw in pathways]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_DIR / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    output_files = []

    # --- 1. Pathway table TSV ---
    if pathways:
        df_pw = pd.DataFrame([{
            "source": pw["source"],
            "pathway_id": pw["pathway_id"],
            "pathway_name": pw["pathway_name"],
            "gene_count": len(pw.get("genes", [])),
            "genes": "|".join(pw.get("genes", [])),
        } for pw in pathways])
        pw_file = out_dir / "pathways.tsv"
        df_pw.to_csv(pw_file, sep="\t", index=False)
        output_files.append(str(pw_file))

    # --- 2. Gene table TSV with unified IDs ---
    if all_genes:
        df_genes = pd.DataFrame([{
            "gene_symbol": g,
            "entrez": id_mapping.get(g, {}).get("entrez", ""),
            "uniprot": id_mapping.get(g, {}).get("uniprot", ""),
            "ensembl": id_mapping.get(g, {}).get("ensembl", ""),
        } for g in all_genes])
        gene_file = out_dir / "genes.tsv"
        df_genes.to_csv(gene_file, sep="\t", index=False)
        output_files.append(str(gene_file))

    # --- 3. Edge list ---
    if edges:
        df_edges = pd.DataFrame(edges)
        edge_file = out_dir / "network_edges.tsv"
        df_edges.to_csv(edge_file, sep="\t", index=False)
        output_files.append(str(edge_file))

    # --- 4. Systematic pathway narrative (LLM) ---
    prompt = f"""You are a molecular biologist writing a comprehensive review chapter.

Goal: {query}

Evidence collected from {list(db_coverage.keys())} databases:
- {sum(db_coverage.values())} pathways identified
- {len(all_genes)} unique genes/proteins: {all_genes[:60]}
- Pathway names identified: {json.dumps(all_pathway_names[:30])}
- Required components checklist: {json.dumps(required)}
- Coverage assessment: {assessment}

Write a SYSTEMATIC, INTEGRATED narrative of the signaling pathway as a review-style
description. Structure it as a biological hierarchy — do NOT mention or compare databases.

Use this structure:
## Overview
(2-3 sentences: what triggers the pathway and what are the major outputs)

## 1. LPS Recognition and Receptor Activation
(TLR4/MD-2 complex, CD14, LBP — upstream)

## 2. MyD88-Dependent Branch
(MyD88, TIRAP, IRAK4, IRAK1, TRAF6, TAK1 → NF-κB and MAPK cascades)

## 3. TRIF-Dependent Branch
(TRIF/TICAM1, TRAM, TRAF3, TBK1, IRF3 → type I IFN)

## 4. Downstream Effector Pathways
(NF-κB nuclear translocation, MAPK: ERK/JNK/p38, PI3K/Akt)

## 5. Transcriptional Outputs
(target genes: TNF, IL6, IL1B, IFNB1, CXCL8 etc.)

## 6. Negative Regulators
(IRAK-M/IRAK3, TOLLIP, SOCS1, A20/TNFAIP3 etc.)

Use gene symbols in **bold**. Show signal flow with → arrows.
Fill in only what the evidence supports; note any gaps honestly.
"""
    report_text = call_agy(prompt, desc="Reporter: synthesizing pathway narrative...")

    report_file = out_dir / "report.md"
    report_file.write_text(report_text, encoding="utf-8")
    output_files.append(str(report_file))

    # --- 5. Pipeline state snapshot ---
    state_file = out_dir / "pipeline_state.json"
    state_file.write_text(json.dumps({
        "query": query,
        "iterations": state.get("iteration", 0),
        "db_coverage": db_coverage,
        "gene_count": len(all_genes),
        "pathway_count": len(pathways),
        "required_components": required,
        "coverage_assessment": assessment,
    }, indent=2), encoding="utf-8")
    output_files.append(str(state_file))

    print(f"  [Reporter] {len(output_files)} files → {out_dir}")
    return {"report": report_text, "output_files": output_files}
