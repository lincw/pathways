"""Reporter node — final output generation.

Generates:
1. Markdown summary report (via agy LLM)
2. TSV file of all pathways with gene lists
3. TSV file of hub genes with cross-DB provenance
4. Edge list for network visualisation
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
    hub_genes = state.get("hub_genes", [])
    db_coverage = state.get("db_coverage", {})
    nodes = state.get("nodes", [])
    edges = state.get("edges", [])
    assessment = state.get("coverage_assessment", "")
    id_mapping = state.get("id_mapping", {})
    plan = state.get("plan", "")

    # --- Deduplicate pathways ---
    seen = set()
    pathways = []
    for pw in raw:
        key = (pw["source"], pw["pathway_id"])
        if key not in seen:
            seen.add(key)
            pathways.append(pw)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_DIR / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    output_files = []

    # --- 1. Pathway table TSV ---
    pathway_rows = []
    for pw in pathways:
        pathway_rows.append({
            "source": pw["source"],
            "pathway_id": pw["pathway_id"],
            "pathway_name": pw["pathway_name"],
            "gene_count": len(pw.get("genes", [])),
            "genes": "|".join(pw.get("genes", [])),
            "description": pw.get("description", "")[:200],
        })
    if pathway_rows:
        df_pw = pd.DataFrame(pathway_rows)
        pw_file = out_dir / "pathways.tsv"
        df_pw.to_csv(pw_file, sep="\t", index=False)
        output_files.append(str(pw_file))

    # --- 2. Hub gene table TSV ---
    gene_rows = []
    for gene in hub_genes:
        meta = id_mapping.get(gene, {})
        gene_rows.append({
            "gene_symbol": gene,
            "entrez": meta.get("entrez", ""),
            "uniprot": meta.get("uniprot", ""),
            "ensembl": meta.get("ensembl", ""),
            "pathway_count": sum(1 for e in edges if e["source"] == gene or e["target"] == gene),
        })
    if gene_rows:
        df_genes = pd.DataFrame(gene_rows)
        gene_file = out_dir / "hub_genes.tsv"
        df_genes.to_csv(gene_file, sep="\t", index=False)
        output_files.append(str(gene_file))

    # --- 3. Edge list TSV ---
    if edges:
        df_edges = pd.DataFrame(edges)
        edge_file = out_dir / "network_edges.tsv"
        df_edges.to_csv(edge_file, sep="\t", index=False)
        output_files.append(str(edge_file))

    # --- 4. LLM-generated narrative report ---
    prompt = f"""You are a computational biologist writing a results summary report.

Analysis: Systematic LPS intracellular signaling pathway mapping
Strategy: {plan}
Databases queried: {list(db_coverage.keys())}
Coverage: {json.dumps(db_coverage)}
Total pathways: {len(pathways)}
Total genes: {sum(1 for n in nodes if n.get("type") == "gene")}
Top hub genes (cross-database): {hub_genes[:30]}
Expert coverage assessment: {assessment}

Write a structured Markdown report with these sections:
1. ## Summary (3-4 sentences)
2. ## Database Coverage (brief table or list)
3. ## Key Signaling Components Identified
4. ## Hub Genes (top cross-database genes and their roles)
5. ## Biological Interpretation (1-2 paragraphs on TLR4/LPS downstream signaling)
6. ## Coverage Gaps and Limitations
7. ## Next Steps

Be precise and biologically accurate. Use gene symbols in bold where appropriate.
"""
    report_text = call_agy(prompt)

    report_file = out_dir / "report.md"
    report_file.write_text(report_text, encoding="utf-8")
    output_files.append(str(report_file))

    # --- 5. Raw state snapshot for reproducibility ---
    state_file = out_dir / "pipeline_state.json"
    state_file.write_text(
        json.dumps({
            "query": state.get("query", ""),
            "iterations": state.get("iteration", 0),
            "db_coverage": db_coverage,
            "hub_genes": hub_genes,
            "coverage_assessment": assessment,
            "pathway_count": len(pathways),
        }, indent=2),
        encoding="utf-8",
    )
    output_files.append(str(state_file))

    print(f"  [Reporter] wrote {len(output_files)} files to {out_dir}")
    print(f"  [Reporter] report: {report_file}")

    return {"report": report_text, "output_files": output_files}
