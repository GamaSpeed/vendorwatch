"""
Précache des requêtes lentes au démarrage.
Lance HHI-par-ministère dès le boot pour éviter les timeouts en démo.
"""
import pandas as pd
from db.connection import query
from memory.query_cache import set as cache_set

_precache_done = False
_hhi_by_ministry = None


def run_precache():
    """
    À appeler une fois au démarrage de app.py ou api.py.
    Pré-charge le HHI par ministère (~2s) pour lectures O(1) ensuite.
    """
    global _precache_done, _hhi_by_ministry
    if _precache_done:
        return
    print("⏳ Précache HHI-par-ministère en cours...")
    SQL = """
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
            (ARRAY_AGG(s.vendor ORDER BY s.valeur DESC))[1]   AS top_vendor,
            ROUND(MAX(s.valeur) / t.total * 100, 1)           AS top_share_pct,
            COUNT(DISTINCT s.vendor)                          AS nb_vendors,
            ROUND(t.total / 1e9, 2)                           AS total_G$
        FROM min_shares s
        JOIN min_totals t USING (ministere)
        GROUP BY s.ministere, t.total
        HAVING ROUND(t.total / 1e9, 2) > 0.1
        ORDER BY hhi DESC;
    """
    try:
        df = query(SQL)
        cache_set(SQL, df)
        _hhi_by_ministry = df
        _precache_done = True
        print(f"✅ Précache terminé — {len(df)} ministères chargés")
    except Exception as e:
        print(f"⚠️ Précache échoué (non bloquant) : {e}")


def get_hhi_by_ministry() -> pd.DataFrame | None:
    """Retourne le DataFrame précaché ou None si pas encore chargé."""
    return _hhi_by_ministry
