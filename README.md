# LPS Signaling Pathway Agentic Pipeline

An agentic AI pipeline that systematically maps intracellular signaling pathways from multiple pathway databases. Designed for LPS/TLR4 but works for any signaling query (TNF-alpha, IL-6, etc.).

## Architecture

Built with **LangGraph** using the Agentic Design Patterns:

| Pattern | Where used |
|---------|-----------|
| Prompt Chaining (Ch.1) | planner → DB agents → id_mapper → synthesizer → critic → reporter |
| Parallelization (Ch.3) | KEGG + Reactome + WikiPathways agents run concurrently via `Send` fan-out |
| Reflection (Ch.4) | Critic evaluates coverage; loops back to planner if gaps remain |
| Tool Use (Ch.5) | REST API tools for KEGG, Reactome, WikiPathways; MyGene.info for ID mapping |
| Multi-agent (Ch.7) | Eight specialized agent nodes, each with a single responsibility |

**LLM backend:** `agy -p "prompt"` (antigravity CLI — no API key required)

## Prerequisites

- Python 3.11+
- [antigravity CLI](https://github.com/antigravity-dev/antigravity) (`agy`) installed and on `PATH`

## Setup

```bash
cd ~/gdrive/01_Going_Projects/LPS_signaling_pathway

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Default query (LPS in human macrophages)
python -m scripts.main

# Custom query — works for any signaling pathway
python -m scripts.main --query "TNF-alpha intracellular signaling in human endothelial cells"
python -m scripts.main --query "IL-6 JAK-STAT signaling in hepatocytes"

# Also print the LangGraph topology as a Mermaid diagram
python -m scripts.main --visualise
```

## Output

Each run creates a timestamped directory under `outputs/`:

| File | Contents |
|------|----------|
| `report.md` | Systematic biological narrative (not a database comparison) |
| `pathways.tsv` | All pathways found (source, ID, name, gene count, gene list) |
| `genes.tsv` | Unique genes with Entrez / UniProt / Ensembl IDs |
| `network_edges.tsv` | Bipartite gene ↔ pathway edges with provenance |
| `pipeline_state.json` | Full pipeline metadata snapshot |

## Progress display

The pipeline prints per-step progress while running:

- **Spinners** on all LLM calls (planner, critic, reporter) — animated, shows which agent is thinking
- **Per-gene counters** in KEGG (`[KEGG] 5/15 TLR4...`) — so you can see API calls progressing
- **Per-pathway lines** in WikiPathways and Reactome
- **Per-node summaries** at completion of each agent

## Database coverage

| Database | API approach |
|----------|-------------|
| KEGG | Gene-based lookup: seed gene → KEGG gene ID → pathway IDs → flat file parse |
| Reactome | ContentService REST: text search → `/data/participants/{id}` for genes |
| WikiPathways | SPARQL endpoint: keyword FILTER on pathway title → `wp:GeneProduct` labels |

## Reflection loop

After the three DB agents run in parallel, the **Critic** agent checks coverage against a query-specific checklist it generates itself (no hardcoded biology). If gaps remain and the iteration budget allows (`MAX_REFLECTION_ITERATIONS` in `config.py`), the **Planner** reruns with gap-targeted terms and genes.

## Configuration

Edit `scripts/config.py` to tune:

```python
AGY_TIMEOUT = 120               # seconds per agy CLI call
MAX_REFLECTION_ITERATIONS = 2   # reflection loop budget
OUTPUT_DIR = Path("outputs")    # where results are written
```
