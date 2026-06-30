# Signaling Pathway Agentic Pipeline

An agentic AI pipeline that systematically maps intracellular signaling pathways from multiple pathway databases. Takes any natural-language query — LPS/TLR4, TNF-alpha, IL-6 JAK-STAT, EGFR, etc. — and returns an integrated, narrative-form pathway report.

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
# Provide any natural-language query
python -m scripts.main --query "LPS intracellular signaling pathway in human macrophages"
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

| Database | Strength | API approach |
|----------|----------|-------------|
| **KEGG** | Broad pathway coverage, stable IDs | Gene-based: seed gene → gene ID → pathway IDs → flat file parse |
| **Reactome** | Deep human signaling, curated hierarchy | ContentService REST: text search → `/data/participants/{id}` |
| **SIGNOR** | Causal signaling edges (who activates/inhibits whom + mechanism) | REST: keyword filter on pathway list → `/api/pathway/{id}/relations/` |

## Reflection loop

After the three DB agents run in parallel, the **Critic** agent checks coverage against a query-specific checklist it generates from scratch for each query (no hardcoded biology). If gaps remain and the iteration budget allows (`MAX_REFLECTION_ITERATIONS` in `config.py`), the **Planner** reruns with gap-targeted terms and genes.

## Configuration

Edit `scripts/config.py` to tune:

```python
AGY_TIMEOUT = 120               # seconds per agy CLI call
MAX_REFLECTION_ITERATIONS = 2   # reflection loop budget
OUTPUT_DIR = Path("outputs")    # where results are written
```
