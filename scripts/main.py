"""Entry point for the LPS signaling pathway agentic pipeline.

Usage:
    cd ~/gdrive/01_Going_Projects/LPS_signaling_pathway
    python -m scripts.main
    python -m scripts.main --query "LPS-induced NF-kB and MAPK signaling in macrophages"
    python -m scripts.main --visualise  # also draw the graph topology
"""

import argparse

from scripts.graph.pipeline import pipeline
from scripts.state import PipelineState


def parse_args():
    parser = argparse.ArgumentParser(
        description="LPS Signaling Pathway Agentic Pipeline (LangGraph + agy CLI)"
    )
    parser.add_argument(
        "--query",
        type=str,
        default="Systematic mapping of LPS intracellular signaling pathways in human macrophages",
        help="Natural language query describing the biological goal",
    )
    parser.add_argument(
        "--visualise",
        action="store_true",
        help="Print the LangGraph topology as a Mermaid diagram",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.visualise:
        print("\n=== LangGraph Pipeline Topology (Mermaid) ===")
        print(pipeline.get_graph().draw_mermaid())
        print()

    print("=" * 60)
    print("LPS Signaling Pathway Pipeline")
    print("=" * 60)
    print(f"Query: {args.query}")
    print()

    initial_state: PipelineState = {
        "query": args.query,
        "search_terms": [],   # planner fills these from the query
        "seed_genes": [],     # planner fills these from the query
        "plan": "",
        "iteration": 0,
        "seed_gene_pool": [],
        "raw_pathways": [],
        "filtered_pathways": None,
        "filter_stats": {},
        "id_mapping": {},
        "nodes": [],
        "edges": [],
        "db_coverage": {},
        "required_components": [],
        "coverage_assessment": "",
        "coverage_gaps": [],
        "additional_search_terms": [],
        "additional_seed_genes": [],
        "validation": {},
        "report": "",
        "output_files": [],
    }

    print("Running pipeline...\n")
    final_state = pipeline.invoke(initial_state)

    print("\n" + "=" * 60)
    print("Pipeline Complete")
    print("=" * 60)
    print(f"Iterations:    {final_state.get('iteration', 0)}")
    print(f"DB coverage:   {final_state.get('db_coverage', {})}")
    print(f"Genes found:   {sum(1 for n in final_state.get('nodes', []) if n.get('type') == 'gene')}")
    print(f"\nOutput files:")
    for f in final_state.get("output_files", []):
        print(f"  {f}")

    print("\n--- Coverage Assessment ---")
    print(final_state.get("coverage_assessment", "(none)"))

    val = final_state.get("validation", {})
    if val.get("status") == "ok":
        print("\n--- Independent QC (g:Profiler vs held-out GO:BP) ---")
        print(f"Coverage: {val['coverage']:.1%} | Recall: {val['recall']:.1%} "
              f"| Verdict: {val.get('verdict')}")
    elif val.get("status") in ("error", "skipped"):
        print(f"\n--- Independent QC: {val.get('status')} "
              f"({val.get('error') or val.get('reason')}) ---")


if __name__ == "__main__":
    main()
