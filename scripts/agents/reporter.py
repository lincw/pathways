"""Reporter node — builds the systematic pathway narrative.

Does NOT compare databases. Uses the collected genes and pathways as
biological evidence to write a coherent, hierarchical signaling-pathway narrative.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from scripts.config import OUTPUT_DIR
from scripts.llm import active_cli_info, call_llm
from scripts.state import PipelineState, working_pathways


def _slugify(text: str, maxlen: int = 48) -> str:
    """Filesystem-safe slug of the query target for the output folder name."""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", text or "").strip("_")
    slug = slug[:maxlen].rstrip("_")
    return slug or "run"


def _format_validation_md(val: dict) -> str:
    """Render the independent-QC block for report.md."""
    if not val or val.get("status") in ("skipped", "error") or "coverage" not in val:
        reason = (val or {}).get("error") or (val or {}).get("reason") or "not run"
        return f"\n\n## Independent Validation (QC)\n\n_Validation unavailable: {reason}._\n"

    lines = [
        "\n\n## Independent Validation (QC)",
        "",
        "Read-only check: the final gene set scored with an external tool "
        "(g:Profiler) against a held-out reference (GO:BP) the pipeline does not query.",
        "",
        f"- **Verdict:** {val.get('verdict', 'n/a')}",
        f"- **Coverage (precision):** {val['coverage']:.1%} "
        f"— fraction of output genes inside the queried pathway (over-inclusion guard)",
        f"- **Recall:** {val['recall']:.1%} "
        f"— fraction of the reference term recovered (completeness)",
        f"- **Genes covered:** {val.get('genes_covered')} / {val.get('n_output_genes')}",
    ]
    pr = val.get("primary_reference")
    if pr:
        lines.append(f"- **Primary reference term:** {pr['native']} {pr['name']} "
                     f"({pr['hits']}/{pr['term_size']})")
    targets = val.get("target_terms", [])
    if targets:
        lines.append("- **Matched pathway terms:**")
        for t in targets[:12]:
            lines.append(f"    - {t['native']} {t['name']} (hits {t['hits']}/{t['term_size']})")
    return "\n".join(lines) + "\n"


def reporter_node(state: PipelineState) -> dict:
    db_coverage = state.get("db_coverage", {})
    nodes = state.get("nodes", [])
    edges = state.get("edges", [])
    assessment = state.get("coverage_assessment", "")
    id_mapping = state.get("id_mapping", {})
    query = state.get("query", "")
    required = state.get("required_components", [])

    # Relevance-filtered, deduplicated pathway set (same as synthesizer).
    pathways = working_pathways(state)

    all_genes = sorted({n["id"] for n in nodes if n.get("type") == "gene"})
    all_pathway_names = [pw["pathway_name"] for pw in pathways]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Folder name = request target + timestamp (not timestamp alone).
    out_dir = OUTPUT_DIR / f"{_slugify(query)}_{timestamp}"
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

    # --- 3. Protein→protein signaling network (directed causal edges) ---
    if edges:
        cols = ["source", "target", "effect", "mechanism", "db", "pathway_id"]
        df_edges = pd.DataFrame(edges)
        df_edges = df_edges[[c for c in cols if c in df_edges.columns]]
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

Derive the section structure from THIS pathway's biology (the genes and pathway
names in the evidence), not from a fixed template. A typical signaling hierarchy is:

## Overview
(2-3 sentences: what stimulus/ligand initiates the pathway and its major outputs)

## 1. Initiation & Receptor Recognition
(ligand(s), receptor(s), co-receptors, and membrane-proximal events)

## 2. Proximal Signaling & Adaptors
(adaptor proteins, scaffolds, and enzymes recruited to the activated receptor)

## 3. Core Signal Transduction
(the main kinase/enzyme cascades and second messengers that propagate the signal)

## 4. Transcriptional & Effector Outputs
(transcription factors activated and representative target genes / effector responses)

## 5. Regulation & Negative Feedback
(phosphatases, ubiquitin ligases, inhibitors, and feedback loops)

Rename, merge, split, or add sections to fit the ACTUAL pathway in the evidence,
using the specific molecules present. Use gene symbols in **bold** and show signal
flow with → arrows. Fill in only what the evidence supports; note any gaps honestly.
"""
    report_text = call_llm(prompt, desc="Reporter: synthesizing pathway narrative...")

    # Prepend an AI-model note so every report records which CLI/model produced it.
    cli = active_cli_info()
    model_label = f"{cli['name']} {cli['version']}".strip() or cli["name"]
    header = (
        f"> **Generated by:** {model_label}  \n"
        f"> **Query:** {query}  \n"
        f"> **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n"
        f"> **Evidence:** {', '.join(db_coverage.keys()) or 'KEGG, Reactome, SIGNOR'}"
        f" · Independent QC: g:Profiler (held-out GO:BP)\n\n"
    )
    report_text = header + report_text

    # Append the independent QC block (monitor mode).
    report_text = report_text + _format_validation_md(state.get("validation", {}))

    report_file = out_dir / "report.md"
    report_file.write_text(report_text, encoding="utf-8")
    output_files.append(str(report_file))

    # --- 5. Pipeline state snapshot ---
    state_file = out_dir / "pipeline_state.json"
    state_file.write_text(json.dumps({
        "query": query,
        "llm_model": model_label,
        "iterations": state.get("iteration", 0),
        "db_coverage": db_coverage,
        "gene_count": len(all_genes),
        "pathway_count": len(pathways),
        "required_components": required,
        "coverage_assessment": assessment,
        "filter_stats": state.get("filter_stats", {}),
        "network_stats": state.get("network_stats", {}),
        "validation": state.get("validation", {}),
    }, indent=2), encoding="utf-8")
    output_files.append(str(state_file))

    print(f"  [Reporter] {len(output_files)} files → {out_dir}")
    return {"report": report_text, "output_files": output_files}
