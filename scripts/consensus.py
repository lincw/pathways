"""Cross-run consensus orchestrator.

A single pipeline run is one stochastic sample: the planner LLM proposes a
different seed-gene set each time, and the search amplifies that into a very
different gene universe (run-to-run Jaccard as low as ~0.36 for the same model).
Rather than trusting one draw, this runs the pipeline N times and reports each
gene / directed edge with its **support** = how many runs recovered it. The
high-support core is the reproducible finding; low-support items are noise from
sampling.

Ollama runs are pinned (temperature=0 + seed) so their variance is small;
agentic CLIs (agy) cannot be pinned, so consensus is the main lever there.

Usage:
    python -m scripts.consensus --query "LPS signaling in human cells" \
        --model "ornith:9b" --cli ollama --runs 3
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pandas as pd

from scripts import llm
from scripts.config import OUTPUT_DIR
from scripts.graph.pipeline import pipeline
from scripts.main import build_initial_state


def _run_dir(final_state: dict) -> Path | None:
    """The timestamped output directory the reporter wrote this run to."""
    for f in final_state.get("output_files", []):
        return Path(f).parent
    return None


def _read_genes(run_dir: Path) -> set[str]:
    f = run_dir / "genes.tsv"
    if not f.exists():
        return set()
    df = pd.read_csv(f, sep="\t", dtype=str).fillna("")
    return {g.strip() for g in df.get("gene_symbol", []) if g.strip()}


def _read_edges(run_dir: Path) -> set[tuple[str, str]]:
    """Directed (source, target) pairs from the robust cross-DB sub-network."""
    f = run_dir / "robust_edges.tsv"
    if not f.exists():
        return set()
    df = pd.read_csv(f, sep="\t", dtype=str).fillna("")
    return {
        (r["source"].strip(), r["target"].strip())
        for _, r in df.iterrows()
        if r.get("source", "").strip() and r.get("target", "").strip()
    }


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _support_table(per_run: list[set], key_cols: list[str], n: int) -> pd.DataFrame:
    """Count how many runs each item appears in; sort by support desc."""
    counts = Counter()
    for s in per_run:
        counts.update(s)
    rows = []
    for item, sup in counts.items():
        values = item if isinstance(item, tuple) else (item,)
        rows.append({**dict(zip(key_cols, values)),
                     "support": sup, "frac": round(sup / n, 3)})
    df = pd.DataFrame(rows)
    return df.sort_values(["support", *key_cols], ascending=[False, *([True] * len(key_cols))])


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
    print(f"Consensus: {args.runs} runs")
    print("=" * 60)
    print(f"Query: {args.query}")
    print(f"LLM CLI: {cli['name']} {cli['version']}".rstrip())
    print(f"Model:   {cli['model']} (user-declared)\n")

    gene_sets: list[set[str]] = []
    edge_sets: list[set[tuple[str, str]]] = []
    run_dirs: list[str] = []

    for i in range(1, args.runs + 1):
        print(f"--- Run {i}/{args.runs} ---")
        final_state = pipeline.invoke(build_initial_state(args.query))
        rd = _run_dir(final_state)
        if rd is None:
            print(f"  [warn] run {i} produced no output files; skipping.")
            continue
        genes, edges = _read_genes(rd), _read_edges(rd)
        gene_sets.append(genes)
        edge_sets.append(edges)
        run_dirs.append(str(rd))
        print(f"  {len(genes)} genes, {len(edges)} robust edges → {rd.name}\n")

    n = len(gene_sets)
    if n < 2:
        raise SystemExit("Fewer than 2 runs succeeded; cannot form a consensus.")

    from scripts.agents.reporter import _slugify  # reuse folder-name slug
    out_dir = OUTPUT_DIR / f"{_slugify(args.query)}_consensus_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    gene_df = _support_table(gene_sets, ["gene_symbol"], n)
    edge_df = _support_table(edge_sets, ["source", "target"], n)
    gene_df.to_csv(out_dir / "consensus_genes.tsv", sep="\t", index=False)
    edge_df.to_csv(out_dir / "consensus_edges.tsv", sep="\t", index=False)

    # Pairwise Jaccard between runs (how reproducible the draws were).
    pairwise = [_jaccard(a, b) for a, b in combinations(gene_sets, 2)]
    mean_j = sum(pairwise) / len(pairwise) if pairwise else 1.0

    def _tier(df: pd.DataFrame) -> dict:
        vc = df["support"].value_counts().to_dict()
        return {int(k): int(vc.get(k, 0)) for k in range(n, 0, -1)}

    lines = [
        f"# Consensus report — {args.query}",
        "",
        f"- **Runs:** {n} | **Model:** {cli['model']} | **CLI:** {cli['name']} {cli['version']}".rstrip(),
        f"- **Mean pairwise gene Jaccard:** {mean_j:.2f} "
        f"({'reproducible' if mean_j >= 0.7 else 'high run-to-run variance'})",
        f"- **Genes in all {n} runs (core):** {int((gene_df['support'] == n).sum())} "
        f"/ {len(gene_df)} total observed",
        f"- **Robust edges in all {n} runs:** {int((edge_df['support'] == n).sum())} "
        f"/ {len(edge_df)} total observed",
        "",
        "## Gene support distribution (genes seen in exactly k runs)",
        *[f"- seen in {k}/{n} runs: {v}" for k, v in _tier(gene_df).items()],
        "",
        "## Runs aggregated",
        *[f"- {d}" for d in run_dirs],
        "",
        "_Threshold `consensus_genes.tsv` / `consensus_edges.tsv` by the `support` "
        "column: support == N is the intersection (highest confidence); "
        f"support >= {n // 2 + 1} is majority._",
    ]
    (out_dir / "consensus_report.md").write_text("\n".join(lines) + "\n")

    print("=" * 60)
    print("Consensus complete")
    print("=" * 60)
    print(f"Mean pairwise gene Jaccard: {mean_j:.2f}")
    print(f"Core genes (all {n} runs):  {int((gene_df['support'] == n).sum())}")
    print(f"Output: {out_dir}")


if __name__ == "__main__":
    main()
