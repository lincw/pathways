"""Independent QC node — runs the query-agnostic validator on the final gene set.

Monitor mode: this is a *read-only* quality check. It scores the collected genes
with an external service (g:Profiler) against a held-out reference (GO:BP, which
the pipeline never fetches from) and records coverage/recall/verdict. It does NOT
change what the pipeline collects — keeping the check honest (the pipeline is not
optimized to pass its own grader). Runs once, right before the reporter.

Never fatal: if g:Profiler is unreachable the run still completes; the QC block
just records the error.
"""

from __future__ import annotations

from scripts.state import PipelineState
from scripts.validate import score, verdict


def validator_node(state: PipelineState) -> dict:
    query = state.get("query", "")
    nodes = state.get("nodes", [])
    genes = sorted({n["id"] for n in nodes if n.get("type") == "gene"})

    print(f"  [Validator] independent QC on {len(genes)} genes via g:Profiler (GO:BP held-out)...")
    if not genes:
        return {"validation": {"status": "skipped", "reason": "no genes"}}

    try:
        result = score(query, genes)
        result["verdict"] = verdict(result)
        result["status"] = "ok"
        print(f"  [Validator] coverage={result['coverage']:.1%} "
              f"recall={result['recall']:.1%} → {result['verdict']}")
        return {"validation": result}
    except Exception as exc:
        print(f"  [Validator] QC failed (non-fatal): {exc}", flush=True)
        return {"validation": {"status": "error", "error": str(exc)}}
