# Signaling Pathway Agentic Pipeline

An agentic AI pipeline that systematically maps intracellular signaling pathways from multiple pathway databases. Takes any natural-language query — LPS/TLR4, TNF-alpha, IL-6 JAK-STAT, EGFR, etc. — and returns an integrated, narrative-form pathway report.

## Architecture

Built with **LangGraph** using the Agentic Design Patterns:

| Pattern | Where used |
|---------|-----------|
| Prompt Chaining | planner → DB agents → pathway_filter → id_mapper → synthesizer → critic → validator → reporter |
| Parallelization| KEGG + Reactome + WikiPathways agents run concurrently via `Send` fan-out |
| Reflection | Critic evaluates coverage; loops back to planner if gaps remain |
| Tool Use | REST API tools for KEGG, Reactome, WikiPathways; MyGene.info for ID mapping |
| Multi-agent | Eight specialized agent nodes, each with a single responsibility |

**LLM backend:** pluggable CLI — `agy` (default), `claude`, `gemini`, `codex`, or `ollama`.
Pick one with `--cli <name>` or `PW_LLM_CLI`. Every `report.md` records the **CLI**
(name + `--version`) and the **model**. CLIs don't reliably report their own model, so
**`--model` is required** and is a *user-declared label* (e.g. `claude-opus-4-8`) recorded
verbatim in the note — you own its accuracy. Where the CLI supports model selection
(`agy`, `ollama`) the label is also passed through to the CLI. Default `agy` needs no API key.

## Prerequisites

- Python 3.11+

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
# Provide any natural-language query. --model is REQUIRED (user-declared label).
python -m scripts.main --query "LPS intracellular signaling pathway in human macrophages" --model "claude-opus-4-8"
python -m scripts.main --query "TNF-alpha intracellular signaling in human endothelial cells" --model "gemini-2.5-pro"
python -m scripts.main --query "IL-6 JAK-STAT signaling in hepatocytes" --cli ollama --model "llama3.1"

# Also print the LangGraph topology as a Mermaid diagram
python -m scripts.main --query "Wnt signaling" --model "claude-opus-4-8" --visualise
```

## Output

Each run creates a `results/<query-slug>_<timestamp>/` directory:

| File | Contents |
|------|----------|
| `report.md` | Systematic biological narrative (+ AI-model note and QC block) |
| `pathways.tsv` | All pathways found (source, ID, name, gene count, gene list) |
| `genes.tsv` | Unique genes with Entrez / UniProt / Ensembl IDs |
| `network_edges.tsv` | Directed protein→protein signaling edges (`source, target, effect, mechanism, db, support, db_support`) from SIGNOR causal relations, KEGG KGML, and Reactome reactions |
| `robust_edges.tsv` | Cross-database **consensus** sub-network: directed pairs asserted by ≥2 sources (`support, db_support, effects, mechanisms`) |
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

### Directed edges (`network_edges.tsv`)

The signaling network is built from **directed protein→protein edges**, extracted
from each database's native causal model (verified against live API responses):

- **SIGNOR** — `entitya → entityb`, `up-/down-regulates` → activation/inhibition.
- **KEGG** — KGML `PPrel`/`GErel` relations `entry1 → entry2`, subtype gives sign.
- **Reactome** — reaction-centric, so edges are *projected* per reaction:
  `catalyst → output` (real direction; catalysis asserts no sign → `effect=unknown`)
  and `regulator → output` (signed: `PositiveRegulation`→activation,
  `NegativeRegulation`→inhibition). Complexes/sets are flattened to member
  proteins; non-protein participants (small molecules) are dropped. Bounded by
  `PW_REACTOME_EDGE_MAX_RXN` and toggled by `PW_REACTOME_EDGES`.

### Robust conserved network (cross-database consensus)

Each directed `source → target` interaction is annotated with **`support`** — the
number of independent databases (SIGNOR, KEGG, Reactome) that assert it. Edges
confirmed by **≥ `PW_ROBUST_MIN_SUPPORT`** sources (default 2) form the *robust*
conserved sub-network, written to `robust_edges.tsv` and summarised in `report.md`.
This is the database-consensus analogue of the run-to-run reproducibility check:
an interaction curated independently by multiple resources is the trustworthy
backbone, so raising the threshold to 3 keeps only edges seen in **all three**
sources. Where databases report different signs the `effects` column lists them
verbatim (e.g. `activation|unknown`) rather than silently merging them.

## Relevance filter

Gene-based lookup pulls in every pathway that shares a promiscuous hub gene
(NFKB1, MAPK1, AKT1 …), which otherwise drags "Pathways in cancer",
"Alzheimer disease", "Thermogenesis", etc. into an LPS query. The
**pathway_filter** node removes this over-inclusion in two data-driven stages,
with **no hardcoded pathway names or ID patterns**:

1. **Statistical enrichment (hypergeometric ORA):** a pathway is kept only if its
   overlap with the seed-gene set is over-represented (BH-adjusted *p* < `ENRICHMENT_FDR`).
   A pathway sharing one incidental hub gene is not enriched and is dropped.
2. **LLM relevance gate:** the query drives a per-pathway judgement — "is this part
   of, or directly up/downstream of, the queried pathway?" — removing hub-rich but
   off-topic maps (cancer, neurodegeneration, unrelated infections) that survive
   the statistical test.

Both stages are pure functions of the seed set and the user query. Tune or disable
them via `ENRICHMENT_*` and `LLM_*` settings in `config.py` (or the matching
`PW_*` environment variables).

## Independent validation (built-in QC)

Every run ends with a **monitor-mode** quality check (`validator` node). The final
gene set is scored by an external tool (**g:Profiler**) against a **held-out**
reference (**GO:BP**) that the pipeline never queries, so the check stays honest —
the pipeline is *not* tuned to pass its own grader. It reports two numbers:

- **Coverage (precision):** fraction of output genes that fall inside the queried
  pathway's terms — the over-inclusion guard. Low = bloated.
- **Recall:** fraction of the reference term recovered — completeness. Low = gaps.

A `PASS`/`FAIL` verdict plus the matched terms are written into `report.md` and
`pipeline_state.json`. To score any existing run manually:

```bash
python -m scripts.validate --results-dir results/<run_folder> --query "<the query>"
```

## Reflection loop

After the three DB agents run in parallel and the relevance filter prunes the set, the **Critic** agent checks coverage against a query-specific checklist it generates from scratch for each query (no hardcoded biology). If gaps remain and the iteration budget allows (`MAX_REFLECTION_ITERATIONS` in `config.py`), the **Planner** reruns with gap-targeted terms and genes.

## Configuration

Edit `scripts/config.py` to tune:

```python
LLM_CLI = "agy"                 # backend: agy|claude|gemini|codex|ollama (PW_LLM_CLI / --cli)
LLM_MODEL = ""                  # model id recorded in report note (PW_LLM_MODEL / --model)
LLM_TIMEOUT = 120               # seconds per LLM CLI call (PW_LLM_TIMEOUT)
MAX_REFLECTION_ITERATIONS = 2   # reflection loop budget
OUTPUT_DIR = Path("outputs")    # where results are written
```
