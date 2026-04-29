"""
Cache du schéma de la base de données.
Injecté dans le prompt de l'Agent SQL pour qu'il connaisse
les colonnes exactes et les règles critiques.
Refresh automatique toutes les 6h.
"""
from datetime import datetime, timedelta
from db.connection import query
from config import DATA_REFRESH_INTERVAL_HOURS

_cache = {"schema": None, "last_updated": None}


def get_schema() -> str:
    """
    Retourne le schéma complet sous forme de texte structuré.
    Met à jour le cache si expiré (TTL = DATA_REFRESH_INTERVAL_HOURS).
    """
    now = datetime.now()
    ttl = timedelta(hours=DATA_REFRESH_INTERVAL_HOURS)
    if (
        _cache["schema"] is None
        or _cache["last_updated"] is None
        or now - _cache["last_updated"] > ttl
    ):
        _refresh()
    return _cache["schema"]


def force_refresh() -> str:
    """Force une mise à jour immédiate du cache."""
    _refresh()
    return _cache["schema"]


def _refresh():
    df = query("""
        SELECT
            t.table_schema,
            t.table_name,
            t.table_type,
            c.column_name,
            c.data_type,
            c.is_nullable
        FROM information_schema.tables t
        JOIN information_schema.columns c
            ON c.table_schema = t.table_schema
            AND c.table_name  = t.table_name
        WHERE t.table_schema IN ('cra','fed','ab','general')
        ORDER BY t.table_schema, t.table_name, c.ordinal_position;
    """)

    lines = [
        "# SCHÉMA BASE DE DONNÉES AGENCY 2026",
        "# Généré automatiquement — source: information_schema",
        "",
    ]
    current_table = None
    for _, row in df.iterrows():
        key = f"{row['table_schema']}.{row['table_name']}"
        if key != current_table:
            if current_table:
                lines.append("")
            current_table = key
            ttype = "VUE" if row["table_type"] == "VIEW" else "TABLE"
            lines.append(f"## {ttype}: {key}")
        nullable = "" if row["is_nullable"] == "YES" else " NOT NULL"
        lines.append(f"  - {row['column_name']}: {row['data_type']}{nullable}")

    lines += [
        "",
        "# RÈGLES SQL ABSOLUES — VIOLATIONS = REQUÊTE INCORRECTE",
        "1. TOUJOURS utiliser fed.vw_grants_decoded — JAMAIS fed.grants_contributions",
        "2. TOUJOURS caster : esl.source_pk->>'_id' = gc._id::text",
        "3. TOUJOURS joindre : entity_source_links → entity_golden_records via entity_id",
        "4. TOUJOURS exclure : WHERE recipient_legal_name NOT ILIKE '%batch%'",
        "5. TOUJOURS filtrer : WHERE agreement_value > 0",
        "6. Durée = (end_date::date - start_date::date)::numeric / 365",
        "7. HHI requiert CTE intermédiaire — pas SUM + OVER dans même GROUP BY",
        "8. Clé entity_golden_records = 'id' (pas 'golden_id')",
        "9. Jointure AB : esl.source_pk->>'id' = ss.id::text",
        "",
        "# RÉSULTATS ATTENDUS (validation)",
        "- fed.vw_grants_decoded : ~1 275 521 lignes",
        "- ab.ab_sole_source : ~15 533 lignes",
        "- general.entity_golden_records : ~851 300 lignes",
        "- HHI Canadian Space Agency : ~5369 (ESA 73%)",
        "- HHI Health Canada : ~1583 (Ontario 30% sur 102G$)",
    ]

    _cache["schema"] = "\n".join(lines)
    _cache["last_updated"] = datetime.now()
    print(f"✅ Schema cache refreshed at {_cache['last_updated']:%H:%M:%S}")
