"""
Calculs HHI (Herfindahl-Hirschman Index) et humanisation d'impact.
Utilisé par l'Agent Analyste via ses outils.
"""
import pandas as pd
import numpy as np
from config import (
    HHI_MONOPOLE, HHI_CRITIQUE, HHI_MODERE,
    COUT_HOPITAL_REGIONAL, SALAIRE_ENSEIGNANT,
    COUT_KM_AUTOROUTE, BOURSE_ETUDIANTE,
    IMPACT_RATIO_CONSERVATIF
)


def classify_hhi(hhi: float) -> str:
    if hhi >= HHI_MONOPOLE:  return "MONOPOLE"
    if hhi >= HHI_CRITIQUE:  return "CRITIQUE"
    if hhi >= HHI_MODERE:    return "MODERE"
    return "COMPETITIF"


def hhi_color(hhi: float) -> str:
    if hhi >= HHI_MONOPOLE:  return "#e05252"
    if hhi >= HHI_CRITIQUE:  return "#f59e0b"
    if hhi >= HHI_MODERE:    return "#a78bfa"
    return "#22c55e"


def calculate_hhi(
    df: pd.DataFrame,
    group_cols: list,
    vendor_col: str,
    value_col: str
) -> pd.DataFrame:
    """
    Calcule HHI = somme des carrés des parts de marché (en %).
    RÈGLE : passer par cette fonction, jamais recalculer inline.
    """
    results = []
    for keys, group in df.groupby(group_cols):
        total = group[value_col].sum()
        if total <= 0:
            continue
        shares = (
            group.groupby(vendor_col)[value_col].sum()
            / total * 100
        )
        hhi = round((shares ** 2).sum(), 1)
        keys_list = [keys] if len(group_cols) == 1 else list(keys)
        row = dict(zip(group_cols, keys_list))
        row.update({
            "hhi":           hhi,
            "top_vendor":    shares.idxmax(),
            "top_share_pct": round(float(shares.max()), 1),
            "vendor_count":  len(shares),
            "total_value":   round(float(total), 0),
            "niveau":        classify_hhi(hhi),
            "hhi_color":     hhi_color(hhi),
        })
        results.append(row)
    return (
        pd.DataFrame(results)
        .sort_values("hhi", ascending=False)
        .reset_index(drop=True)
    )


def humanize_impact(surplus_dollars: float) -> dict:
    """
    Convertit un surcoût estimé en termes citoyens concrets.
    Utilisé comme utilitaire — le Narrateur l'appelle via son outil
    compute_citizen_impact pour l'intégrer dans les briefs.
    """
    estimate = surplus_dollars * IMPACT_RATIO_CONSERVATIF
    return {
        "hopitaux":    int(estimate / COUT_HOPITAL_REGIONAL),
        "enseignants": int(estimate / SALAIRE_ENSEIGNANT),
        "km_routes":   int(estimate / COUT_KM_AUTOROUTE),
        "bourses":     int(estimate / BOURSE_ETUDIANTE),
        "note":        f"Estimation prudente à {IMPACT_RATIO_CONSERVATIF*100:.0f}% du surcoût potentiel"
    }
