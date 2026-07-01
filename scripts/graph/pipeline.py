"""LangGraph pipeline assembly for the signaling pathway project.

Design patterns from "Agentic Design Patterns":
  Ch.1  Prompt Chaining     — sequential nodes (planner → id_mapper → synthesizer → critic → reporter)
  Ch.3  Parallelization     — Send fan-out to kegg/reactome/signor agents simultaneously
  Ch.4  Reflection          — critic → conditional edge back to planner if coverage gaps remain
  Ch.5  Tool Use            — each tool module wraps a real API endpoint
  Ch.6  Planning            — planner_node generates structured search strategy
  Ch.7  Multi-Agent         — specialised nodes for each database and each processing step

Graph topology:
  START → planner
        → [Send] → kegg_agent ─────┐
                 → reactome_agent ──┤→ pathway_filter → id_mapper → synthesizer → critic
                 → signor_agent ────┘
  critic → (if gaps & budget) → planner        [reflection loop]
         → (else)             → validator → reporter → END

  pathway_filter (enrichment ORA + LLM relevance gate) removes hub-gene
  over-inclusion before ID mapping so downstream nodes see a focused set.
  validator (monitor) scores the final gene set with an independent tool
  (g:Profiler vs held-out GO:BP) — read-only QC, never alters collection.
"""

from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph

from scripts.agents.critic import critic_node, route_after_critic
from scripts.agents.db_agents import (
    kegg_agent_node,
    reactome_agent_node,
    signor_agent_node,
)
from scripts.agents.id_mapper import id_mapper_node
from scripts.agents.pathway_filter import pathway_filter_node
from scripts.agents.planner import planner_node
from scripts.agents.reporter import reporter_node
from scripts.agents.synthesizer import synthesizer_node
from scripts.agents.validator import validator_node
from scripts.state import PipelineState


def _dispatch_to_db_agents(state: PipelineState):
    """Fan-out: run all three DB agents in parallel (Ch.3 Parallelization)."""
    return [
        Send("kegg_agent", state),
        Send("reactome_agent", state),
        Send("signor_agent", state),
    ]


def build_pipeline() -> "CompiledGraph":
    builder = StateGraph(PipelineState)

    # --- Register nodes ---
    builder.add_node("planner", planner_node)
    builder.add_node("kegg_agent", kegg_agent_node)
    builder.add_node("reactome_agent", reactome_agent_node)
    builder.add_node("signor_agent", signor_agent_node)
    builder.add_node("pathway_filter", pathway_filter_node)
    builder.add_node("id_mapper", id_mapper_node)
    builder.add_node("synthesizer", synthesizer_node)
    builder.add_node("critic", critic_node)
    builder.add_node("validator", validator_node)
    builder.add_node("reporter", reporter_node)

    # --- Define edges ---

    # Entry
    builder.add_edge(START, "planner")

    # Fan-out from planner → 3 parallel DB agents (Ch.3)
    builder.add_conditional_edges("planner", _dispatch_to_db_agents)

    # Fan-in: all three parallel agents converge at the relevance filter.
    # LangGraph waits for ALL incoming branches before running the join node.
    builder.add_edge("kegg_agent", "pathway_filter")
    builder.add_edge("reactome_agent", "pathway_filter")
    builder.add_edge("signor_agent", "pathway_filter")

    # Sequential processing chain (Ch.1 Prompt Chaining)
    # Filter (enrichment + LLM gate) → ID mapping → synthesis
    builder.add_edge("pathway_filter", "id_mapper")
    builder.add_edge("id_mapper", "synthesizer")
    builder.add_edge("synthesizer", "critic")

    # Reflection loop conditional edge (Ch.4 Reflection)
    builder.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "planner": "planner",     # re-query with gap-filling search terms
            "validator": "validator", # sufficient coverage → independent QC → report
        },
    )

    # Independent QC (monitor only) then final report
    builder.add_edge("validator", "reporter")
    builder.add_edge("reporter", END)

    return builder.compile()


# Singleton — import and use directly
pipeline = build_pipeline()
