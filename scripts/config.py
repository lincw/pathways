"""Pipeline configuration — reads from environment or uses defaults."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.getenv("LPS_OUTPUT_DIR", str(BASE_DIR / "results")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

AGY_CMD = os.getenv("AGY_CMD", "agy")          # CLI binary name
AGY_TIMEOUT = int(os.getenv("AGY_TIMEOUT", "120"))  # seconds per call

MAX_REFLECTION_ITERATIONS = int(os.getenv("LPS_MAX_REFLECTIONS", "2"))

# Databases queried (extend here to add more)
ENABLED_DATABASES = ["KEGG", "Reactome", "WikiPathways"]

# Default LPS-relevant seed genes and search terms
DEFAULT_SEED_GENES = ["TLR4", "TLR2", "MD2", "LBP", "CD14", "MyD88", "TRIF", "TRAF6",
                      "IRAK1", "IRAK4", "TAK1", "NF-kB", "IRF3", "MAPK", "PI3K"]

DEFAULT_SEARCH_TERMS = ["LPS signaling", "TLR4 signaling", "lipopolysaccharide", "innate immunity"]

# API endpoints
KEGG_BASE = "https://rest.kegg.jp"
REACTOME_BASE = "https://reactome.org/ContentService"
WIKIPATHWAYS_BASE = "https://www.wikipathways.org/api/v2"
MYGENE_BASE = "https://mygene.info/v3"
