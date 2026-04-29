# agents/analyst_agent.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import logging
from typing import Any

import boto3
import pandas as pd
from strands import Agent, tool
from strands.models import BedrockModel

from config import ANALYST_MODEL, BEDROCK_REGION
from db.connection import query as db_query
from db.schema_cache import get_schema as get_schema_text
from memory.findings_store import add_finding, get_findings
from pipeline.hhi import calculate_hhi, humanize_impact
from pipeline.lockout import detect_all_lockins, find_tipping_point

logger = logging.getLogger(__name__)

ANALYST_SYSTEM_PROMPT = """
Tu es un agent analyste spécialisé en audit des marchés publics canadiens.
Tu identifies des patterns de concentration, verrouillage et compétition fictive.
Tu formules 3 hypothèses explicites avant chaque analyse.
Tu sauvegardes chaque finding validé avec save_finding().
Tu ne fais jamais d'affirmations sans données traçables.
"""

# ──────────────────────────────────────────────
# OUTILS
# ──────────────────────────────────────────────

@tool
def compute_hhi(ministry: str = None) -> str:
    """
    Calcule le HHI de concentration par ministère fédéral.
    Si ministry est fourni, filtre sur ce ministère uniquement.

    Args:
        ministry: Nom du ministère (optionnel). Si None, calcule tous les ministères.
    """
    try:
        where = "AND gc.owner_org_title ILIKE %(ministry)s" if ministry else ""
        params = {"ministry": f"%{ministry}%"} if ministry else {}

        sql = f"""
            WITH min_shares AS (
                SELECT
                    gc.owner_org_title AS ministere,
                    COALESCE(egr.canonical_name, gc.recipient_legal_name) AS vendor,
                    SUM(gc.agreement_value) AS valeur
                FROM fed.vw_grants_decoded gc
                LEFT JOIN general.entity_source_links esl
                    ON esl.source_schema = 'fed'
                    AND esl.source_table = 'grants_contributions'
                    AND esl.source_pk->>'_id' = gc._id::text
                LEFT JOIN general.entity_golden_records egr
                    ON egr.id = esl.entity_id
                WHERE gc.agreement_value > 0
                  AND gc.owner_org_title IS NOT NULL
                  AND gc.recipient_legal_name NOT ILIKE '%batch%'
                  {where}
                GROUP BY gc.owner_org_title,
                         COALESCE(egr.canonical_name, gc.recipient_legal_name)
            ),
            min_totals AS (
                SELECT ministere, SUM(valeur) AS total
                FROM min_shares GROUP BY ministere
            )
            SELECT
                s.ministere,
                ROUND(SUM(POWER(s.valeur * 100.0 / t.total, 2))) AS hhi,
                (ARRAY_AGG(s.vendor ORDER BY s.valeur DESC))[1] AS top_vendor,
                ROUND(MAX(s.valeur) / t.total * 100, 1) AS top_share_pct,
                COUNT(DISTINCT s.vendor) AS nb_vendors,
                ROUND(t.total / 1e9, 2) AS total_G$
            FROM min_shares s
            JOIN min_totals t USING (ministere)
            GROUP BY s.ministere, t.total
            HAVING ROUND(t.total / 1e9, 2) > 0.01
            ORDER BY hhi DESC
            LIMIT 20;
        """
        df = db_query(sql, params)
        return df.to_json(orient="records", force_ascii=False)
    except Exception as e:
        return f"ERROR compute_hhi: {str(e)}"


@tool
def detect_lockin(vendor_name: str, source: str = "ab") -> str:
    """
    Détecte le verrouillage progressif d'un fournisseur spécifique.
    Calcule ratio sole-source, durée totale, et point de basculement.

    Args:
        vendor_name: Nom du fournisseur (ex: "IBM", "Microsoft", "TELUS")
        source: "ab" pour Alberta sole-source, "fed" pour fédéral
    """
    try:
        if source == "ab":
            sql = """
                SELECT
                    vendor,
                    ministry,
                    amount,
                    start_date::date AS start_date,
                    end_date::date AS end_date,
                    permitted_situations,
                    (end_date::date - start_date::date)::numeric / 365 AS duree_annees,
                    contract_services
                FROM ab.ab_sole_source
                WHERE LOWER(vendor) LIKE LOWER(%(vendor)s)
                  AND amount > 0
                  AND end_date IS NOT NULL
                ORDER BY amount DESC
                LIMIT 50
            """
        else:
            sql = """
                SELECT
                    COALESCE(egr.canonical_name, gc.recipient_legal_name) AS vendor,
                    gc.owner_org_title AS ministry,
                    gc.agreement_value AS amount,
                    gc.agreement_start_date::date AS start_date,
                    gc.agreement_end_date::date AS end_date,
                    gc.agreement_type AS permitted_situations
                FROM fed.vw_grants_decoded gc
                LEFT JOIN general.entity_source_links esl
                    ON esl.source_schema = 'fed'
                    AND esl.source_table = 'grants_contributions'
                    AND esl.source_pk->>'_id' = gc._id::text
                LEFT JOIN general.entity_golden_records egr
                    ON egr.id = esl.entity_id
                WHERE LOWER(COALESCE(egr.canonical_name, gc.recipient_legal_name))
                      LIKE LOWER(%(vendor)s)
                  AND gc.agreement_value > 0
                ORDER BY gc.agreement_value DESC
                LIMIT 50
            """

        df = db_query(sql, {"vendor": f"%{vendor_name}%"})

        if df.empty:
            return json.dumps({"error": f"Aucun contrat trouvé pour {vendor_name}"})

        total = float(df["amount"].sum())
        duree_max = float(df["duree_annees"].max()) if "duree_annees" in df.columns else 0

        # Ratio situation g (sole-source AB)
        ratio_g = 0.0
        if "permitted_situations" in df.columns:
            total_g = float(df[df["permitted_situations"] == "g"]["amount"].sum())
            ratio_g = round(total_g / total * 100, 1) if total > 0 else 0.0

        result = {
            "vendor": vendor_name,
            "source": source,
            "nb_contrats": len(df),
            "total_M$": round(total / 1e6, 1),
            "duree_max_annees": round(duree_max, 1),
            "ratio_sole_source_pct": ratio_g,
            "ministeres": df["ministry"].unique().tolist(),
            "contrats": df.head(5).to_dict(orient="records"),
            "verdict": "VERROUILLAGE CONFIRME" if (ratio_g > 80 and duree_max > 3) else "A SURVEILLER"
        }
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as e:
        return f"ERROR detect_lockin: {str(e)}"


@tool
def find_fictional_competition() -> str:
    """
    Identifie les paires d'organisations qui partagent des administrateurs
    communs (T3010 ARC) et soumissionnent dans la même catégorie FED.
    Finding clé : CAMH (9 admins / 452M$), Boréal (29 admins / 383.7M$).
    """
    try:
        sql = """
            WITH fed_vendors AS (
                SELECT
                    egr.id AS entity_id,
                    egr.canonical_name,
                    egr.bn_root,
                    gc.agreement_type,
                    SUM(gc.agreement_value) AS valeur_categorie,
                    COUNT(*) AS nb_contrats
                FROM fed.vw_grants_decoded gc
                JOIN general.entity_source_links esl
                    ON esl.source_schema = 'fed'
                    AND esl.source_table = 'grants_contributions'
                    AND esl.source_pk->>'_id' = gc._id::text
                JOIN general.entity_golden_records egr ON egr.id = esl.entity_id
                WHERE gc.agreement_value > 0
                  AND gc.recipient_legal_name NOT ILIKE '%batch%'
                GROUP BY egr.id, egr.canonical_name, egr.bn_root, gc.agreement_type
                HAVING SUM(gc.agreement_value) > 10000000 AND COUNT(*) >= 5
            ),
            vendor_directors AS (
                SELECT
                    fv.entity_id, fv.canonical_name,
                    fv.agreement_type, fv.valeur_categorie,
                    LOWER(TRIM(REGEXP_REPLACE(
                        d.last_name || ' ' || COALESCE(d.first_name,''),
                        '[^a-zA-Z ]', '', 'g'
                    ))) AS director_norm
                FROM fed_vendors fv
                JOIN cra.cra_directors d ON LEFT(d.bn, 9) = fv.bn_root
                WHERE d.last_name IS NOT NULL
                  AND LENGTH(TRIM(d.last_name)) > 1
            )
            SELECT
                a.agreement_type AS categorie,
                a.canonical_name AS fournisseur_a,
                b.canonical_name AS fournisseur_b,
                COUNT(DISTINCT a.director_norm) AS admins_communs,
                STRING_AGG(DISTINCT a.director_norm, ' | '
                    ORDER BY a.director_norm) AS noms_admins,
                ROUND((a.valeur_categorie + b.valeur_categorie) / 1e6, 1) AS total_M$
            FROM vendor_directors a
            JOIN vendor_directors b
                ON a.agreement_type = b.agreement_type
                AND a.entity_id < b.entity_id
                AND a.director_norm = b.director_norm
            GROUP BY a.agreement_type, a.canonical_name, b.canonical_name,
                     a.valeur_categorie, b.valeur_categorie
            HAVING COUNT(DISTINCT a.director_norm) >= 2
            ORDER BY total_M$ DESC
            LIMIT 20;
        """
        df = db_query(sql)
        if df.empty:
            return json.dumps({"error": "Aucune compétition fictive détectée"})
        return df.to_json(orient="records", force_ascii=False)
    except Exception as e:
        return f"ERROR find_fictional_competition: {str(e)}"


@tool
def find_bigov_network() -> str:
    """
    Identifie les organisations qui reçoivent simultanément du financement
    fédéral (Immigration Canada) ET des contrats sole-source Alberta.
    Finding clé : Catholic Social Services 1351.9M$, Bow Valley 1551M$.
    """
    try:
        sql = """
            WITH fed_immigration AS (
                SELECT
                    egr.id AS entity_id,
                    egr.canonical_name,
                    ROUND(SUM(gc.agreement_value) / 1e6, 1) AS fed_M$,
                    COUNT(*) AS nb_contrats_fed
                FROM fed.vw_grants_decoded gc
                JOIN general.entity_source_links esl
                    ON esl.source_schema = 'fed'
                    AND esl.source_table = 'grants_contributions'
                    AND esl.source_pk->>'_id' = gc._id::text
                JOIN general.entity_golden_records egr ON egr.id = esl.entity_id
                WHERE gc.owner_org_title ILIKE '%Immigration%'
                  AND gc.agreement_value > 1000000
                  AND gc.recipient_legal_name NOT ILIKE '%batch%'
                GROUP BY egr.id, egr.canonical_name
                HAVING SUM(gc.agreement_value) > 5000000
            ),
            ab_social AS (
                SELECT
                    egr.id AS entity_id,
                    egr.canonical_name,
                    ROUND(SUM(ss.amount) / 1e6, 1) AS ab_M$,
                    COUNT(*) AS nb_sole_ab,
                    COUNT(DISTINCT ss.ministry) AS nb_min_ab
                FROM ab.ab_sole_source ss
                JOIN general.entity_source_links esl
                    ON esl.source_schema = 'ab'
                    AND esl.source_table = 'ab_sole_source'
                    AND esl.source_pk->>'id' = ss.id::text
                JOIN general.entity_golden_records egr ON egr.id = esl.entity_id
                WHERE ss.amount > 0
                GROUP BY egr.id, egr.canonical_name
                HAVING SUM(ss.amount) > 5000000
            )
            SELECT
                f.canonical_name,
                f.fed_M$,
                f.nb_contrats_fed,
                a.ab_M$,
                a.nb_sole_ab,
                a.nb_min_ab,
                ROUND(f.fed_M$ + a.ab_M$, 1) AS total_M$
            FROM fed_immigration f
            JOIN ab_social a USING (entity_id)
            ORDER BY total_M$ DESC
            LIMIT 15;
        """
        df = db_query(sql)
        if df.empty:
            return json.dumps({"error": "Aucun réseau bi-gouvernemental détecté"})
        return df.to_json(orient="records", force_ascii=False)
    except Exception as e:
        return f"ERROR find_bigov_network: {str(e)}"


@tool
def find_cra_revocations(entity_name: str) -> str:
    """
    Cherche les signaux ARC (T3010) pour une entité.

    Args:
        entity_name: Nom de l'organisation à vérifier
    """
    try:
        sql = """
            SELECT
                ci.bn,
                ci.legal_name,
                ci.city,
                ci.category,
                COUNT(ti.bn) AS nb_violations_arithmetiques
            FROM cra.cra_identification ci
            LEFT JOIN cra.t3010_impossibilities ti ON LEFT(ti.bn, 9) = LEFT(ci.bn, 9)
            WHERE ci.legal_name ILIKE %(name)s
            GROUP BY ci.bn, ci.legal_name, ci.city, ci.category
            LIMIT 10;
        """
        df = db_query(sql, {"name": f"%{entity_name}%"})
        if df.empty:
            return json.dumps({"found": False, "entity": entity_name})

        return json.dumps({
            "found": True,
            "entity": entity_name,
            "records": df.to_dict(orient="records")
        }, default=str, ensure_ascii=False)
    except Exception as e:
        return f"ERROR find_cra_revocations: {str(e)}"


@tool
def save_finding(
    title: str,
    severity: str,
    total_M: float,
    entities: str,
    evidence: str,
    finding_type: str
) -> str:
    """
    Sauvegarde un finding validé dans findings.json.

    Args:
        title: Titre court du finding (ex: "Verrouillage IBM Alberta")
        severity: "CRITICAL", "HIGH", ou "MEDIUM"
        total_M: Montant total en millions de dollars
        entities: Entités impliquées séparées par virgule
        evidence: Preuve principale traçable dans les données
        finding_type: "lockin", "bigov_network", "fictional_competition", "hhi"
    """
    try:
        finding = {
            "title": title,
            "severity": severity,
            "total_M$": total_M,
            "entities": [e.strip() for e in entities.split(",")],
            "evidence": evidence,
            "type": finding_type,
        }
        add_finding(finding)
        return json.dumps({
            "saved": True,
            "title": title,
            "severity": severity,
            "total_M$": total_M
        })
    except Exception as e:
        return f"ERROR save_finding: {str(e)}"


# ──────────────────────────────────────────────
# CONSTRUCTION DE L'AGENT
# ──────────────────────────────────────────────

def _build_analyst_agent() -> Agent:
    import os
    model = BedrockModel(
        model_id=ANALYST_MODEL,
        region_name=BEDROCK_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN"),
    )
    return Agent(
        model=model,
        system_prompt=ANALYST_SYSTEM_PROMPT,
        tools=[
            compute_hhi, detect_lockin, find_fictional_competition,
            find_bigov_network, find_cra_revocations, save_finding
        ],
    )


# ──────────────────────────────────────────────
# POINT D'ENTRÉE PUBLIC
# ──────────────────────────────────────────────

def run_analysis() -> dict[str, Any]:
    """
    Lance l'analyse complète sur les 3 findings critiques.
    IBM + Catholic Social Services + CAMH → sauvegardés dans findings.json
    """
    agent = _build_analyst_agent()

    prompt = """
Analyse les données gouvernementales canadiennes et identifie les findings critiques.

Étapes obligatoires :
1. Formule 3 hypothèses explicites avant de commencer
2. detect_lockin("IBM", "ab") → vérifie verrouillage IT Alberta
3. detect_lockin("Microsoft", "ab") → vérifie verrouillage cloud
4. find_bigov_network() → identifie réseau bi-gouvernemental
5. find_fictional_competition() → identifie compétition fictive
6. find_cra_revocations("Catholic Social Services") → signal ARC
7. Pour chaque finding validé → save_finding() avec severity CRITICAL/HIGH/MEDIUM
8. Retourne un résumé JSON avec la liste des findings sauvegardés
"""

    try:
        response = agent(prompt)
        findings = get_findings()
        return {
            "success": True,
            "findings_count": len(findings),
            "findings": findings
        }
    except Exception as e:
        logger.error(f"run_analysis failed: {e}")
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────
# TEST DIRECT DES OUTILS (sans LLM)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== TEST 1 — detect_lockin IBM Alberta ===")
    result = detect_lockin("IBM", "ab")
    print(result)

    print("\n=== TEST 2 — find_cra_revocations CSS ===")
    result2 = find_cra_revocations("Catholic Social Services")
    print(result2)

    print("\n=== TEST 3 — save_finding test ===")
    result3 = save_finding(
        title="TEST Verrouillage IBM Alberta",
        severity="CRITICAL",
        total_M=194.6,
        entities="IBM CANADA LIMITED, Service Alberta, Technology and Innovation",
        evidence="93.6% contrats situation g, 8 ans IMAGIS PeopleSoft",
        finding_type="lockin"
    )
    print(result3)