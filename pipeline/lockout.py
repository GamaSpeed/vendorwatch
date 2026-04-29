"""
Détection du verrouillage progressif (lock-in) et point de basculement.
"""
import pandas as pd
import numpy as np
from config import HHI_CRITIQUE, CORR_THRESHOLD, MIN_YEARS_LOCKIN


def find_tipping_point(series: pd.DataFrame) -> dict:
    """
    Identifie l'année exacte où HHI a dépassé HHI_CRITIQUE
    et n'est jamais redescendu. Notre angle différenciateur principal.
    """
    s = series.sort_values("annee").reset_index(drop=True)
    tipping_year = None
    for _, row in s.iterrows():
        if row["hhi"] > HHI_CRITIQUE:
            future = s[s["annee"] > row["annee"]]
            if len(future) == 0 or (future["hhi"] > HHI_CRITIQUE).all():
                tipping_year = int(row["annee"])
                break
    hhi_at_tip = None
    if tipping_year:
        vals = s[s["annee"] == tipping_year]["hhi"].values
        hhi_at_tip = float(vals[0]) if len(vals) > 0 else None
    return {
        "tipping_year":   tipping_year,
        "years_locked":   (2025 - tipping_year) if tipping_year else 0,
        "hhi_at_tipping": hhi_at_tip,
    }


def detect_all_lockins(by_cat_year: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque catégorie : corrélation HHI/année.
    Si corrélation > CORR_THRESHOLD sur 3+ ans → verrouillage confirmé.
    """
    results = []
    for cat, group in by_cat_year.groupby("agreement_type"):
        g = group.sort_values("annee")
        if len(g) < MIN_YEARS_LOCKIN:
            continue
        corr = float(np.corrcoef(g["annee"], g["hhi"])[0, 1])
        if corr > CORR_THRESHOLD:
            tp = find_tipping_point(g)
            results.append({
                "categorie":      cat,
                "hhi_debut":      round(float(g["hhi"].iloc[0]), 0),
                "hhi_fin":        round(float(g["hhi"].iloc[-1]), 0),
                "croissance_hhi": round(float(g["hhi"].iloc[-1]) - float(g["hhi"].iloc[0]), 0),
                "correlation":    round(corr, 2),
                "top_vendor":     g["top_vendor"].iloc[-1],
                "top_share_pct":  float(g["top_vendor_share_pct"].iloc[-1]),
                "tipping_year":   tp["tipping_year"],
                "years_locked":   tp["years_locked"],
                "hhi_at_tipping": tp["hhi_at_tipping"],
            })
    return (
        pd.DataFrame(results)
        .sort_values("croissance_hhi", ascending=False)
        .reset_index(drop=True)
    )


def estimate_monopoly_premium(
    df_fed: pd.DataFrame,
    by_cat_year: pd.DataFrame,
    category: str
) -> dict:
    """
    Estime le surcoût payé depuis le point de basculement.
    Présenté comme corrélation, pas causalité prouvée.
    """
    cat_data   = df_fed[df_fed["agreement_type"] == category].copy()
    cat_series = by_cat_year[by_cat_year["agreement_type"] == category]
    tp = find_tipping_point(cat_series)
    if not tp["tipping_year"]:
        return {"error": "Pas de point de basculement détecté"}
    cat_data["count"] = 1
    by_yr = cat_data.groupby("annee").agg(
        total=("agreement_value", "sum"),
        count=("count", "count")
    )
    by_yr["prix_unitaire"] = by_yr["total"] / by_yr["count"]
    before = by_yr[by_yr.index < tp["tipping_year"]]
    after  = by_yr[by_yr.index >= tp["tipping_year"]]
    if before.empty:
        return {"error": "Pas assez de données pré-basculement"}
    prix_avant = float(before["prix_unitaire"].mean())
    prix_apres = float(after["prix_unitaire"].mean())
    premium_pct = (prix_apres - prix_avant) / prix_avant * 100
    surcout = (prix_apres - prix_avant) * int(after["count"].sum())
    return {
        "tipping_year":   tp["tipping_year"],
        "years_locked":   tp["years_locked"],
        "prix_avant":     round(prix_avant, 0),
        "prix_apres":     round(prix_apres, 0),
        "premium_pct":    round(premium_pct, 1),
        "surcout_estime": round(surcout, 0),
        "note":           "Corrélation temporelle — pas causalité prouvée",
    }
