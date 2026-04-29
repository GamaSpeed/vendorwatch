"""
Stockage persistant des findings entre les runs d'agents.
Format JSON. Append-on-save. Dédupliqué par (vendor_id, finding_type).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from config import FINDINGS_PATH


def load() -> dict:
    path = Path(FINDINGS_PATH)
    if path.exists() and path.stat().st_size > 0:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "findings": [],
        "last_full_run": None,
        "data_snapshot": {},
        "known_patterns": {}
    }


def save(store: dict):
    Path(FINDINGS_PATH).parent.mkdir(exist_ok=True)
    with open(FINDINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, default=str, ensure_ascii=False)


def add_finding(finding: dict) -> str:
    """
    Ajoute un finding. Retourne son ID.
    Déduplique par (vendor_id, finding_type) si les deux sont présents.
    """
    store = load()
    # Déduplication
    vendor_id = finding.get("vendor_id", "")
    ftype     = finding.get("finding_type", "")
    if vendor_id and ftype:
        existing = [
            f for f in store["findings"]
            if f.get("vendor_id") == vendor_id
            and f.get("finding_type") == ftype
        ]
        if existing:
            return existing[0]["id"]

    finding["discovered_at"] = datetime.now().isoformat()
    finding["id"] = f"F{len(store['findings']) + 1:04d}"
    store["findings"].append(finding)
    save(store)
    return finding["id"]


def get_findings(severity: str = None, category: str = None) -> list:
    store = load()
    findings = store["findings"]
    if severity:
        findings = [f for f in findings if f.get("severity") == severity]
    if category:
        findings = [f for f in findings if f.get("category") == category]
    return findings


def mark_run_complete():
    store = load()
    store["last_full_run"] = datetime.now().isoformat()
    save(store)


def update_snapshot(snapshot: dict):
    store = load()
    store["data_snapshot"] = snapshot
    save(store)


def get_snapshot() -> dict:
    return load().get("data_snapshot", {})


def clear_findings():
    """Réinitialise les findings — utile pour tester."""
    store = load()
    store["findings"] = []
    save(store)
