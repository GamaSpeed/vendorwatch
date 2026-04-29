import json
import hashlib
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from config import QUERY_CACHE_PATH

TTL = timedelta(hours=24)

def _key(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode()).hexdigest()[:16]

def _load() -> dict:
    try:
        p = Path(QUERY_CACHE_PATH)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save(cache: dict):
    try:
        Path(QUERY_CACHE_PATH).write_text(
            json.dumps(cache, default=str, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"⚠️ Cache save failed: {e}")

def get(sql: str) -> pd.DataFrame | None:
    k = _key(sql)
    cache = _load()
    if k in cache:
        entry = cache[k]
        cached_at = datetime.fromisoformat(entry["cached_at"])
        if datetime.now() - cached_at < TTL:
            return pd.DataFrame(entry["data"])
    return None

def set(sql: str, df: pd.DataFrame):
    k = _key(sql)
    cache = _load()
    cache[k] = {
        "cached_at": datetime.now().isoformat(),
        "data": df.to_dict(orient="records")
    }
    _save(cache)

def invalidate():
    Path(QUERY_CACHE_PATH).write_text(
        json.dumps({}, ensure_ascii=False),
        encoding="utf-8"
    )
    print("✅ Query cache invalidated")