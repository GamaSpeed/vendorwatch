# agents/sql_agent.py
# VENDORWATCH — Agent SQL
# Modèle : us.anthropic.claude-sonnet-4-6 (via Bedrock)
# Rôle   : Écrire, valider, exécuter et auto-corriger le SQL sur les 4 schémas

import json
import logging
from typing import Any

import boto3
import pandas as pd
from strands import Agent, tool
from strands.models import BedrockModel

from config import SQL_MODEL, BEDROCK_REGION
from db.connection import query as db_query
from db.schema_cache import get_schema as get_schema_text
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# PROMPT SYSTÈME — injecté une seule fois à l'init
# ──────────────────────────────────────────────
SQL_SYSTEM_PROMPT = """
You are an expert SQL agent on a Canadian government PostgreSQL database.
You must write SQL that is defensible before judges. Absolute precision required.

## AVAILABLE SCHEMAS
- fed.vw_grants_decoded      — federal grants (~1.28M rows) — ALWAYS use this view, NEVER fed.grants_contributions
- ab.ab_sole_source          — Alberta sole-source contracts (~2.61M rows)
- ab.ab_grants               — Alberta grants
- ab.ab_contracts            — Alberta open tenders
- cra.cra_directors          — T3010 directors (~2.87M rows)
- cra.cra_identification     — charity profiles
- general.entity_golden_records  — resolved entities (key = id, NOT golden_id)
- general.entity_source_links    — source links
- general.vw_entity_funding      — all-in-one view

## 9 CRITICAL RULES (violations = immediate error)

1. NEVER fed.grants_contributions — use ONLY fed.vw_grants_decoded
2. Mandatory cast for JSON joins:
   esl.source_pk->>'_id' = gc._id::text  (FED)
   esl.source_pk->>'id'  = ss.id::text   (AB)
3. Exclude BATCH REPORT: WHERE recipient_legal_name NOT ILIKE '%batch%'
4. Duration in years: (end_date::date - start_date::date)::numeric / 365
   NEVER EXTRACT(EPOCH FROM ...)
5. Golden records key: egr.id (NEVER egr.golden_id)
6. HHI: mandatory intermediate CTE — never SUM + OVER in same GROUP BY
7. Negative amounts: always filter WHERE amount > 0 or agreement_value > 0
8. ab_grants duplicates: filter WHERE aggregation_type = 'by_fiscal_year' if available
9. EXPLAIN before any heavy query (estimated >100K rows)

## RESPONSE FORMAT
Return ONLY valid SQL, no markdown, no explanation.
If self-correcting, start with -- CORRECTION: [short reason]
Always respond in English.
"""

# ──────────────────────────────────────────────
# OUTILS STRANDS
# ──────────────────────────────────────────────

@tool
def get_schema() -> str:
    """
    Retourne le cache du schéma PostgreSQL avec les colonnes exactes
    et les 9 règles critiques SQL. Appeler en premier avant d'écrire du SQL.
    """
    try:
        schema_text = get_schema_text()
        return schema_text
    except Exception as e:
        logger.error(f"get_schema error: {e}")
        return f"ERREUR schema_cache: {str(e)}"


@tool
def validate_query(sql: str) -> str:
    """
    Exécute EXPLAIN sur la requête pour valider la syntaxe et estimer le coût
    AVANT d'exécuter. Retourne le plan ou l'erreur.

    Args:
        sql: Requête SQL à valider (sans EXPLAIN — ajouté automatiquement)
    """
    try:
        explain_sql = f"EXPLAIN {sql}"
        rows = db_query(explain_sql)
        plan_lines = [row[0] if isinstance(row, (list, tuple)) else str(row) for row in rows]
        return "\n".join(plan_lines)
    except Exception as e:
        return f"VALIDATION_ERROR: {str(e)}"


@tool
def execute_sql(sql: str) -> str:
    """
    Exécute une requête SQL sur PostgreSQL et retourne le résultat en JSON.
    Limite à 500 lignes pour éviter les timeouts. Retourne aussi les colonnes.

    Args:
        sql: Requête SQL prête à exécuter (validée au préalable)
    """
    try:
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("SELECT") and "LIMIT" not in sql_upper:
            sql = sql.rstrip("; \n") + " LIMIT 500"

        df = db_query(sql)

        # FIX — ne jamais faire "if not df" sur un DataFrame
        if df is None or (hasattr(df, 'empty') and df.empty):
            return json.dumps({"rows": [], "count": 0, "note": "Aucun résultat"})

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        result = {
            "count": len(df),
            "columns": list(df.columns),
            "rows": df.head(500).to_dict(orient="records")
        }
        return json.dumps(result, default=str, ensure_ascii=False)

    except Exception as e:
        return f"EXECUTION_ERROR: {str(e)}"


@tool
def correct_query(sql: str, error_message: str) -> str:
    """
    Réécrit une requête SQL qui a échoué en appliquant les règles de correction.
    Retourne le SQL corrigé. Appelé automatiquement sur EXECUTION_ERROR.

    Args:
        sql: Requête SQL originale qui a échoué
        error_message: Message d'erreur PostgreSQL exact
    """
    corrections = []
    corrected = sql

    # Règle 1 — Table brute FED
    if "grants_contributions" in corrected and "vw_grants_decoded" not in corrected:
        corrected = corrected.replace(
            "fed.grants_contributions",
            "fed.vw_grants_decoded"
        )
        corrections.append("R1: grants_contributions → vw_grants_decoded")

    # Règle 2 — Cast JSON manquant
    if "operator does not exist: text = integer" in error_message or \
       "operator does not exist: integer = text" in error_message:
        # Cast _id FED
        corrected = corrected.replace(
            "source_pk->>'_id' = gc._id",
            "source_pk->>'_id' = gc._id::text"
        )
        # Cast id AB
        corrected = corrected.replace(
            "source_pk->>'id' = ss.id",
            "source_pk->>'id' = ss.id::text"
        )
        corrections.append("R2: cast ::text ajouté sur jointure JSON")

    # Règle 4 — EXTRACT EPOCH
    if "EXTRACT" in corrected.upper() and "EPOCH" in corrected.upper():
        import re
        corrected = re.sub(
            r"EXTRACT\s*\(\s*EPOCH\s+FROM\s*\(([^)]+)\)\s*\)\s*/\s*86400\s*/\s*365",
            r"(\1)::numeric / 365",
            corrected,
            flags=re.IGNORECASE
        )
        corrections.append("R4: EXTRACT EPOCH → cast ::numeric / 365")

    # Règle 5 — golden_id → id
    if "golden_id" in corrected:
        corrected = corrected.replace("egr.golden_id", "egr.id")
        corrections.append("R5: egr.golden_id → egr.id")

    # Règle 7 — Montants négatifs
    if "amount > 0" not in corrected and "agreement_value > 0" not in corrected:
        if "WHERE" in corrected.upper():
            corrected = corrected.replace("WHERE ", "WHERE amount > 0 AND ", 1)
        corrections.append("R7: filtre montants > 0 ajouté")

    if corrections:
        prefix = "-- CORRECTION: " + " | ".join(corrections) + "\n"
        return prefix + corrected
    else:
        # Aucune règle applicable — retourner avec note
        return f"-- CORRECTION: erreur non reconnue ({error_message[:80]})\n{corrected}"


# ──────────────────────────────────────────────
# CONSTRUCTION DE L'AGENT
# ──────────────────────────────────────────────

def _build_sql_agent() -> Agent:
    """Instancie l'agent SQL avec le modèle Bedrock configuré."""
    import os
    
    model = BedrockModel(
        model_id=SQL_MODEL,
        region_name=BEDROCK_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),  # requis Workshop Studio
    )

    agent = Agent(
        model=model,
        system_prompt=SQL_SYSTEM_PROMPT,
        tools=[get_schema, validate_query, execute_sql, correct_query],
    )
    return agent


# ──────────────────────────────────────────────
# POINT D'ENTRÉE PUBLIC
# ──────────────────────────────────────────────

def run_sql_query(natural_language_request: str, max_retries: int = 3) -> dict[str, Any]:
    """
    Exécute une requête en langage naturel via l'agent SQL.
    Auto-correction jusqu'à max_retries tentatives sur EXECUTION_ERROR.

    Args:
        natural_language_request: Question en français ou anglais
        max_retries: Nombre max de tentatives auto-correction (défaut 3)

    Returns:
        dict avec keys: 'success', 'data', 'sql_used', 'error', 'attempts'
    """
    agent = _build_sql_agent()

    prompt = f"""
Requête utilisateur : {natural_language_request}

Étapes obligatoires :
1. Appelle get_schema() pour connaître les colonnes exactes
2. Écris le SQL en respectant les 9 règles critiques
3. Appelle validate_query(sql) — si VALIDATION_ERROR, corrige
4. Appelle execute_sql(sql)
5. Si EXECUTION_ERROR → appelle correct_query(sql, error) → réessaie execute_sql
6. Répète étape 5 maximum {max_retries} fois
7. Retourne le résultat final en JSON structuré avec les clés :
   success, sql_used, row_count, data (liste des lignes), note
"""

    attempts = 0
    last_error = None

    try:
        response = agent(prompt)
        # Extraire le texte de la réponse Strands
        if hasattr(response, 'message'):
            raw = response.message.get('content', [])
            text_parts = [b['text'] for b in raw if b.get('type') == 'text']
            text = "\n".join(text_parts)
        else:
            text = str(response)

        # Tenter de parser le JSON dans la réponse
        try:
            # Chercher un bloc JSON dans la réponse texte
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                result['attempts'] = attempts + 1
                return result
        except (json.JSONDecodeError, AttributeError):
            pass

        # Si pas de JSON structuré, encapsuler la réponse brute
        return {
            "success": True,
            "data": text,
            "sql_used": None,
            "error": None,
            "attempts": 1,
            "raw_response": text
        }

    except Exception as e:
        logger.error(f"run_sql_query failed: {e}")
        return {
            "success": False,
            "data": None,
            "sql_used": None,
            "error": str(e),
            "attempts": 1
        }


# ──────────────────────────────────────────────
# TEST RAPIDE (python3 agents/sql_agent.py)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== TEST 1 — execute_sql direct (sans LLM) ===")
    result = execute_sql(
        "SELECT vendor, amount, ministry, start_date, end_date "
        "FROM ab.ab_sole_source "
        "WHERE LOWER(vendor) LIKE '%ibm%' AND amount > 0 "
        "ORDER BY amount DESC LIMIT 5"
    )
    print(result)

    print("\n=== TEST 2 — get_schema ===")
    schema = get_schema()
    print(schema[:500])

    print("\n=== TEST 3 — validate_query ===")
    plan = validate_query(
        "SELECT vendor, SUM(amount) FROM ab.ab_sole_source "
        "WHERE amount > 0 GROUP BY vendor ORDER BY 2 DESC LIMIT 5"
    )
    print(plan)