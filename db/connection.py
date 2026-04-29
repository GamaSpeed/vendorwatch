"""
Pool de connexions PostgreSQL.
Utilise ThreadedConnectionPool pour gérer plusieurs agents simultanément.
"""
import psycopg2
import psycopg2.pool
import pandas as pd
from config import DB_URL

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DB_URL
        )
    return _pool


def query(sql: str, params=None) -> pd.DataFrame:
    """
    Exécute une requête SQL et retourne un DataFrame pandas.
    Gestion automatique du pool — connexion rendue après usage.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        df = pd.read_sql(sql, conn, params=params)
        return df
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        pool.putconn(conn)


def execute(sql: str, params=None) -> None:
    """
    Exécute une requête sans retour (INSERT, CREATE TABLE, etc.).
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        pool.putconn(conn)


def test_connection() -> bool:
    """Test rapide de la connexion — à lancer au démarrage."""
    try:
        df = query("SELECT 1 AS ok")
        print("✅ PostgreSQL Render — connexion OK")
        return True
    except Exception as e:
        print(f"❌ PostgreSQL — connexion échouée : {e}")
        return False
