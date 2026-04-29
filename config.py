import os
from dotenv import load_dotenv
load_dotenv()

# ── Base de données ──────────────────────────────────────
DB_URL = os.getenv("DB_CONNECTION_STRING")

# ── AWS Bedrock — IDs modèles exacts (pas de suffixe -v1:0) ──
ORCHESTRATOR_MODEL = "anthropic.claude-opus-4-5"
ANALYST_MODEL      = "anthropic.claude-opus-4-5"
SQL_MODEL          = "anthropic.claude-sonnet-4-5"
NARRATOR_MODEL     = "anthropic.claude-sonnet-4-5"
WATCHDOG_MODEL     = "anthropic.claude-haiku-4-5-20251001"
BEDROCK_REGION     = "us-east-1"

# ── Backup LLM direct ────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")

# ── Seuils HHI ───────────────────────────────────────────
HHI_MONOPOLE   = 5000
HHI_CRITIQUE   = 2500
HHI_MODERE     = 1500

# ── Seuils financiers ────────────────────────────────────
MIN_VALUE_SIGNIFICANT = 10_000_000    # 10 M$
MIN_VALUE_MAJOR       = 100_000_000   # 100 M$
MIN_VALUE_BIGOV       = 5_000_000     # 5 M$ (réseau bi-gouvernemental)

# ── Détection lock-in ────────────────────────────────────
CORR_THRESHOLD        = 0.7
MIN_YEARS_LOCKIN      = 3
LOCKIN_FS_RATIO       = 0.6   # 60% sole-source = lock-in
LOCKIN_MIN_YEARS      = 3     # 3 ans minimum

# ── Impact citoyen — utilisé par le Narrateur ────────────
COUT_HOPITAL_REGIONAL    = 45_000_000
SALAIRE_ENSEIGNANT       = 85_000
COUT_KM_AUTOROUTE        = 2_800_000
BOURSE_ETUDIANTE         = 10_000
IMPACT_RATIO_CONSERVATIF = 0.10   # 10% du surcoût potentiel

# ── Refresh données ──────────────────────────────────────
DATA_REFRESH_INTERVAL_HOURS = 6
WATCHDOG_DEMO_INTERVAL_SEC  = 60   # intervalle réduit pour la démo

# ── Chemins ──────────────────────────────────────────────
FINDINGS_PATH    = "memory/findings.json"
QUERY_CACHE_PATH = "memory/query_cache.json"
BACKUP_ALERTS    = "memory/backup_alerts.md"
