"""Entry point for the LPS signaling pathway agentic pipeline.

Usage:
    cd ~/gdrive/01_Going_Projects/LPS_signaling_pathway
    python -m scripts.main
    python -m scripts.main --query "LPS-induced NF-kB and MAPK signaling in macrophages"
    python -m scripts.main --visualise  # also draw the graph topology
"""

import argparse
import sys
from pprint import pprint

from scripts.config import DEFAULT_SEARCH_TERMS
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
        "search_terms": DEFAULT_SEARCH_TERMS,
        "seed_genes": DEFAULT_SEED_GENES,
        "plan": "",
        "iteration": 0,
        "raw_pathways": [],
        "id_mapping": {},
        "nodes": [],
        "edges": [],
        "db_coverage": {},
        "required_components": [],
        "coverage_assessment": "",
        "coverage_gaps": [],
        "additional_search_terms": [],
        "additional_seed_genes": [],
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


if __name__ == "__main__":
    main()
