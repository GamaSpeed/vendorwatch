"""
Cache des résultats SQL.
Clé = SHA256 de la requête SQL. TTL = 24h.
Évite de re-interroger Render à chaque appel agent.
"""
import json
import hashlib
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from config import QUERY_CACHE_PATH

_mem_cache: dict = {}
TTL = timedelta(hours=24)


def _key(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode()).hexdigest()[:16]


def get(sql: str) -> pd.DataFrame | None:
    k = _key(sql)
    if k in _mem_cache:
        entry = _mem_cache[k]
        if datetime.now() - entry["cached_at"] < TTL:
            return pd.DataFrame(entry["data"])
    return None


def set(sql: str, df: pd.DataFrame):
    k = _key(sql)
    _mem_cache[k] = {
        "cached_at": datetime.now(),
        "data": df.to_dict(orient="records")
    }


def invalidate():
    """Vide tout le cache — appeler quand les données sont mises à jour."""
    _mem_cache.clear()
    print("✅ Query cache invalidated")
