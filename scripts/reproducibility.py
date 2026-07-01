"""Reproducibility harness — pairwise Jaccard agreement across repeat runs.

Given N result folders produced by the pipeline for the SAME query, this
quantifies how stable the output is run-to-run. It reports, for three
entity types (genes, pathways, directed edges):

- Pairwise Jaccard for every run pair, plus mean / min / max.
- A stability spectrum: how many runs each entity appears in, so you can
  separate the reproducible CORE (present in all N runs) from the fuzzy
  PERIPHERY (present in only some). This is the honest picture — a single
  Jaccard number hides that the canonical core is stable while only the
  borderline-adjacent members churn.

No pipeline internals are imported; it reads the TSV outputs directly, so it
works on any set of run folders regardless of the query.

Usage:
    # explicit folders
    python -m scripts.reproducibility results/run_a results/run_b results/run_c

    # or glob a pattern (quote it so the shell doesn't expand)
    python -m scripts.reproducibility --glob "results/LPS_*"

    # write a machine-readable summary alongside the console report
    python -m scripts.reproducibility --glob "results/LPS_*" --out results/reproducibility
"""

import argparse
import csv
import glob
import itertools
import json
import os
from pathlib import Path


# --- entity extractors: each returns a set of canonical keys for one run ----

def _read_tsv(path):
    """Yield rows of a TSV as dicts; empty list if the file is missing."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def genes_of(run_dir):
    """Gene set = the gene_symbol column, upper-cased and stripped."""
    rows = _read_tsv(os.path.join(run_dir, "genes.tsv"))
    return {r["gene_symbol"].strip().upper() for r in rows if r.get("gene_symbol", "").strip()}


def pathways_of(run_dir):
    """Pathway set keyed by (source, pathway_id) — identity independent of name text."""
    rows = _read_tsv(os.path.join(run_dir, "pathways.tsv"))
    return {(r.get("source", "").strip(), r.get("pathway_id", "").strip())
            for r in rows if r.get("pathway_id", "").strip()}


def edges_of(run_dir):
    """Directed signaling edge set keyed by (source, target, effect).

    Mechanism and db are deliberately excluded from the key so the same
    biological relation asserted by different databases counts once — this
    measures agreement on the *signaling logic*, not on provenance.
    """
    rows = _read_tsv(os.path.join(run_dir, "network_edges.tsv"))
    keys = set()
    for r in rows:
        s = r.get("source", "").strip().upper()
        t = r.get("target", "").strip().upper()
        if s and t:
            keys.add((s, t, r.get("effect", "").strip().lower()))
    return keys


ENTITIES = {
    "genes": genes_of,
    "pathways": pathways_of,
    "edges": edges_of,
}


# --- metrics ----------------------------------------------------------------

def jaccard(a, b):
    """|A ∩ B| / |A ∪ B|; 1.0 if both empty (vacuously identical)."""
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def pairwise_stats(sets_by_run):
    """Pairwise Jaccard for every run pair; returns (matrix, mean, min, max)."""
    names = list(sets_by_run)
    matrix = {}
    vals = []
    for i, j in itertools.combinations(names, 2):
        val = jaccard(sets_by_run[i], sets_by_run[j])
        matrix[(i, j)] = val
        vals.append(val)
    mean = sum(vals) / len(vals) if vals else 1.0
    return matrix, mean, (min(vals) if vals else 1.0), (max(vals) if vals else 1.0)


def stability_spectrum(sets_by_run):
    """Occupancy of each entity: how many of the N runs contain it.

    Returns (counts_by_occupancy, union_size, core_size, mean_occupancy_frac).
    core = present in ALL N runs.
    """
    n = len(sets_by_run)
    occ = {}
    for s in sets_by_run.values():
        for e in s:
            occ[e] = occ.get(e, 0) + 1
    counts = {k: 0 for k in range(1, n + 1)}
    for c in occ.values():
        counts[c] += 1
    union_size = len(occ)
    core_size = counts.get(n, 0)
    mean_frac = (sum(occ.values()) / (union_size * n)) if union_size else 1.0
    return counts, union_size, core_size, mean_frac


# --- reporting --------------------------------------------------------------

def _bar(frac, width=24):
    filled = int(round(frac * width))
    return "#" * filled + "-" * (width - filled)


def analyse(run_dirs):
    labels = [Path(d).name for d in run_dirs]
    n = len(run_dirs)
    result = {"runs": labels, "n_runs": n, "entities": {}}

    for ent_name, extractor in ENTITIES.items():
        sets_by_run = {Path(d).name: extractor(d) for d in run_dirs}
        matrix, mean, mn, mx = pairwise_stats(sets_by_run)
        counts, union_size, core_size, mean_frac = stability_spectrum(sets_by_run)
        sizes = {k: len(v) for k, v in sets_by_run.items()}
        result["entities"][ent_name] = {
            "per_run_size": sizes,
            "pairwise_jaccard": {f"{a} vs {b}": round(v, 4) for (a, b), v in matrix.items()},
            "mean_jaccard": round(mean, 4),
            "min_jaccard": round(mn, 4),
            "max_jaccard": round(mx, 4),
            "union_size": union_size,
            "core_size": core_size,
            "core_fraction_of_union": round(core_size / union_size, 4) if union_size else 1.0,
            "mean_occupancy_fraction": round(mean_frac, 4),
            "occupancy_histogram": {str(k): counts[k] for k in sorted(counts)},
        }
    return result


def print_report(result):
    n = result["n_runs"]
    print("=" * 70)
    print(f"REPRODUCIBILITY — {n} runs")
    print("=" * 70)
    for i, r in enumerate(result["runs"], 1):
        print(f"  run {i}: {r}")
    print()

    for ent_name, e in result["entities"].items():
        print("-" * 70)
        print(f"[{ent_name.upper()}]")
        sizes = e["per_run_size"]
        print(f"  per-run sizes : {list(sizes.values())}  "
              f"(union={e['union_size']}, core={e['core_size']})")
        print(f"  Jaccard       : mean={e['mean_jaccard']:.3f}  "
              f"min={e['min_jaccard']:.3f}  max={e['max_jaccard']:.3f}")
        print(f"  core stability: {e['core_size']}/{e['union_size']} of the union appear in ALL "
              f"{n} runs ({e['core_fraction_of_union']:.1%})")
        print(f"  mean occupancy: {e['mean_occupancy_fraction']:.1%} "
              f"(avg fraction of runs an entity appears in)")
        print("  pairwise:")
        for pair, v in e["pairwise_jaccard"].items():
            print(f"      {v:.3f}  {pair}")
        print("  occupancy spectrum (present in k of N runs):")
        hist = e["occupancy_histogram"]
        total = e["union_size"] or 1
        for k in sorted(hist, key=int):
            cnt = hist[k]
            print(f"      in {k}/{n} runs: {cnt:5d}  {_bar(cnt / total)}")
        print()

    # one-line verdict per entity
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for ent_name, e in result["entities"].items():
        print(f"  {ent_name:9s}  mean Jaccard {e['mean_jaccard']:.3f}  |  "
              f"core {e['core_fraction_of_union']:.1%} of union stable across all {n} runs")
    print()
    print("Interpretation: mean Jaccard = overall run-to-run overlap; core fraction =")
    print("the reproducible backbone. A modest Jaccard with a high core fraction means")
    print("the canonical pathway is stable and only the fuzzy periphery moves.")


def main():
    ap = argparse.ArgumentParser(description="Pairwise Jaccard reproducibility across repeat runs.")
    ap.add_argument("run_dirs", nargs="*", help="Result folders to compare (2+).")
    ap.add_argument("--glob", dest="glob_pat", default=None,
                    help='Glob for run folders, e.g. "results/LPS_*" (quote it).')
    ap.add_argument("--out", default=None,
                    help="Optional directory to write reproducibility.json + .tsv summary.")
    args = ap.parse_args()

    run_dirs = list(args.run_dirs)
    if args.glob_pat:
        run_dirs.extend(sorted(glob.glob(args.glob_pat)))
    run_dirs = [d for d in run_dirs if os.path.isdir(d)]

    if len(run_dirs) < 2:
        ap.error(f"need at least 2 run folders, got {len(run_dirs)}: {run_dirs}")

    result = analyse(run_dirs)
    print_report(result)

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reproducibility.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        with open(out_dir / "reproducibility_summary.tsv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["entity", "n_runs", "mean_jaccard", "min_jaccard", "max_jaccard",
                        "union_size", "core_size", "core_fraction_of_union",
                        "mean_occupancy_fraction"])
            for ent_name, e in result["entities"].items():
                w.writerow([ent_name, result["n_runs"], e["mean_jaccard"], e["min_jaccard"],
                            e["max_jaccard"], e["union_size"], e["core_size"],
                            e["core_fraction_of_union"], e["mean_occupancy_fraction"]])
        print(f"\nWrote: {out_dir/'reproducibility.json'}")
        print(f"Wrote: {out_dir/'reproducibility_summary.tsv'}")


if __name__ == "__main__":
    main()
