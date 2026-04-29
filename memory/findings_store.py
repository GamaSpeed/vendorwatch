"""
Stockage persistant des findings entre les runs d'agents.
Format JSON. Append-on-save. Dédupliqué par (vendor_id, finding_type).
"""
import json
import os, tempfile
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
    path = Path(FINDINGS_PATH)
    path.parent.mkdir(exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".findings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, default=str, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def add_finding(finding: dict) -> str:
    store = load()

    # Déduplication par type + première entité
    ftype = finding.get("type", "")
    first_entity = finding.get("entities", [""])[0].lower()[:20]
    for f in store["findings"]:
        if (f.get("type") == ftype and
                f.get("entities", [""])[0].lower()[:20] == first_entity):
            return f["id"]  # déjà là

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


def get_finding(finding_id: str) -> dict | None:
    """Fetch one finding by its 'id' field. Used by Narrator + Orchestrator."""
    for f in load()["findings"]:
        if f.get("id") == finding_id:
            return f
    return None


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
