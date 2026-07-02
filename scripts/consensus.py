"""Cross-run consensus orchestrator — anchored on the selected pathways.

Collection is pathway-anchored: each database returns whole pathways chosen by
the LLM (KEGG/SIGNOR catalogue selection) or text search (Reactome), and a
pathway's gene membership and directed edges are then a DETERMINISTIC function of
its ID. So the only thing that varies run-to-run is *which pathway IDs the model
selected*. Consensus is therefore measured over the selected pathway set — not
over the post-fetch gene/edge tables, which just re-expand the same choice and
add noise (a big pathway swings the gene Jaccard far more than the one selection
bit it represents).

Each run contributes its set of selected pathways; every pathway is reported with
its **support** = how many runs selected it. The pathways selected by ALL runs
(the *gold* set) are the reproducible core; genes and the directed network are
derived deterministically from that gold set.

Ollama runs are pinned (temperature=0 + seed) so their variance is small;
agentic CLIs (agy) cannot be pinned, so consensus is the main lever there.

Usage:
    python -m scripts.consensus --query "LPS signaling in human cells" \
        --model "ornith:9b" --cli ollama --runs 3
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pandas as pd

from scripts import llm
from scripts.agents.network_builder import network_node
from scripts.config import OUTPUT_DIR
from scripts.graph.pipeline import pipeline
from scripts.main import build_initial_state
from scripts.state import working_pathways


def _run_dir(final_state: dict) -> Path | None:
    """The timestamped output directory the reporter wrote this run to."""
    for f in final_state.get("output_files", []):
        return Path(f).parent
    return None


def _selected_pathways(final_state: dict) -> dict[tuple[str, str], dict]:
    """The pathways this run actually collected, keyed by (source, pathway_id).

    This is the model-driven decision the consensus is anchored on. Uses the
    relevance-filtered survivor set (falls back to the deduplicated raw set if
    filtering was disabled).
    """
    out: dict[tuple[str, str], dict] = {}
    for pw in working_pathways(final_state):
        key = (pw.get("source", ""), pw.get("pathway_id", ""))
        if key[1] and key not in out:
            out[key] = pw
    return out


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def parse_args():
    p = argparse.ArgumentParser(description="Cross-run consensus for the pathway pipeline")
    p.add_argument("--query", required=True, help="Pathway query (same as scripts.main)")
    p.add_argument("--model", required=True, help="Model label (recorded per run)")
    p.add_argument("--cli", default=None,
                   choices=["agy", "claude", "gemini", "codex", "ollama"],
                   help="LLM CLI backend (default: agy / PW_LLM_CLI)")
    p.add_argument("--runs", type=int, default=3, help="Number of pipeline runs (default 3)")
    return p.parse_args()


def main():
    args = parse_args()
    if args.runs < 2:
        raise SystemExit("--runs must be >= 2 for a consensus.")

    llm.set_llm_cli(args.cli)
    llm.set_llm_model(args.model)
    cli = llm.active_cli_info()

    print("=" * 60)
    print(f"Consensus: {args.runs} runs (pathway-anchored)")
    print("=" * 60)
    print(f"Query: {args.query}")
    print(f"LLM CLI: {cli['name']} {cli['version']}".rstrip())
    print(f"Model:   {cli['model']} (user-declared)\n")

    # Per-run set of selected pathway keys, plus a deterministic registry mapping
    # each key to its PathwayEntry (name + full membership are ID-determined, so
    # any run that selected it is authoritative).
    per_run_keys: list[set[tuple[str, str]]] = []
    registry: dict[tuple[str, str], dict] = {}
    run_dirs: list[str] = []

    for i in range(1, args.runs + 1):
        print(f"--- Run {i}/{args.runs} ---")
        final_state = pipeline.invoke(build_initial_state(args.query))
        selected = _selected_pathways(final_state)
        per_run_keys.append(set(selected))
        for key, pw in selected.items():
            registry.setdefault(key, pw)
        rd = _run_dir(final_state)
        if rd:
            run_dirs.append(str(rd))
        print(f"  {len(selected)} pathways selected"
              f"{' → ' + rd.name if rd else ''}\n")

    n = len(per_run_keys)
    if n < 2:
        raise SystemExit("Fewer than 2 runs succeeded; cannot form a consensus.")

    from scripts.agents.reporter import _slugify  # reuse folder-name slug
    out_dir = OUTPUT_DIR / f"{_slugify(args.query)}_consensus_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Pathway consensus (the anchor) ------------------------------------
    support = Counter()
    for keys in per_run_keys:
        support.update(keys)
    if not support:
        raise SystemExit("No pathways were selected in any run; nothing to reconcile.")

    pw_rows = []
    for key, sup in support.items():
        pw = registry[key]
        pw_rows.append({
            "source": key[0],
            "pathway_id": key[1],
            "pathway_name": pw.get("pathway_name", ""),
            "n_genes": len({g.upper().strip() for g in pw.get("genes", []) if g.strip()}),
            "support": sup,
            "frac": round(sup / n, 3),
        })
    pw_df = pd.DataFrame(pw_rows).sort_values(
        ["support", "source", "pathway_id"], ascending=[False, True, True])
    pw_df.to_csv(out_dir / "consensus_pathways.tsv", sep="\t", index=False)

    gold_keys = {key for key, sup in support.items() if sup == n}

    # Reproducibility = agreement on the pathway selection itself.
    pairwise = [_jaccard(a, b) for a, b in combinations(per_run_keys, 2)]
    mean_j = sum(pairwise) / len(pairwise) if pairwise else 1.0

    # --- Genes derived from pathway support (deterministic membership) ------
    # A gene is as reliable as the most-reproducibly-selected pathway containing
    # it: support(gene) = max run-support over the selected pathways it belongs to.
    gene_support: dict[str, int] = defaultdict(int)
    gene_pathways: dict[str, set] = defaultdict(set)
    for key, sup in support.items():
        for g in registry[key].get("genes", []):
            g = g.upper().strip()
            if not g:
                continue
            gene_support[g] = max(gene_support[g], sup)
            gene_pathways[g].add(key[1])
    gene_df = pd.DataFrame([
        {"gene_symbol": g, "support": s, "frac": round(s / n, 3),
         "n_pathways": len(gene_pathways[g]),
         "pathways": "|".join(sorted(gene_pathways[g]))}
        for g, s in gene_support.items()
    ]).sort_values(["support", "gene_symbol"], ascending=[False, True])
    gene_df.to_csv(out_dir / "consensus_genes.tsv", sep="\t", index=False)

    # --- Directed network of the GOLD pathways (deterministic) -------------
    # Rebuild edges only from pathways every run agreed on, so the network is the
    # reproducible core rather than one run's draw.
    gold_pathways = [registry[k] for k in gold_keys]
    gold_genes = sorted({g.upper().strip()
                         for k in gold_keys for g in registry[k].get("genes", [])
                         if g.strip()})
    net_state = {
        "filtered_pathways": gold_pathways,
        "nodes": [{"id": g, "type": "gene"} for g in gold_genes],
    }
    net = network_node(net_state) if gold_pathways else {"robust_edges": []}
    edge_df = pd.DataFrame(net.get("robust_edges", []))
    edge_df.to_csv(out_dir / "consensus_edges.tsv", sep="\t", index=False)

    # --- Report ------------------------------------------------------------
    def _tier(counter_support: Counter) -> dict:
        vc = Counter(counter_support.values())
        return {k: int(vc.get(k, 0)) for k in range(n, 0, -1)}

    lines = [
        f"# Consensus report (pathway-anchored) — {args.query}",
        "",
        f"- **Runs:** {n} | **Model:** {cli['model']} | **CLI:** {cli['name']} {cli['version']}".rstrip(),
        f"- **Mean pairwise pathway Jaccard:** {mean_j:.2f} "
        f"({'reproducible' if mean_j >= 0.7 else 'high run-to-run variance'})",
        f"- **Pathways selected by all {n} runs (gold):** {len(gold_keys)} "
        f"/ {len(support)} distinct pathways observed",
        f"- **Genes in the gold pathway set:** {len(gold_genes)}",
        f"- **Directed edges in the gold network:** {len(edge_df)}",
        "",
        "## Pathway support distribution (pathways selected in exactly k runs)",
        *[f"- selected in {k}/{n} runs: {v}" for k, v in _tier(support).items()],
        "",
        "## Runs aggregated",
        *[f"- {d}" for d in run_dirs],
        "",
        "_`consensus_pathways.tsv` is the anchor: threshold by `support` "
        f"(support == {n} is the gold intersection; >= {n // 2 + 1} is majority). "
        "`consensus_genes.tsv` gives each gene the support of the best pathway "
        "containing it; `consensus_edges.tsv` is the directed network rebuilt from "
        "the gold pathways only._",
    ]
    (out_dir / "consensus_report.md").write_text("\n".join(lines) + "\n")

    print("=" * 60)
    print("Consensus complete (pathway-anchored)")
    print("=" * 60)
    print(f"Mean pairwise pathway Jaccard: {mean_j:.2f}")
    print(f"Gold pathways (all {n} runs):  {len(gold_keys)} / {len(support)} observed")
    print(f"Gold-network genes / edges:    {len(gold_genes)} / {len(edge_df)}")
    print(f"Output: {out_dir}")


if __name__ == "__main__":
    main()
