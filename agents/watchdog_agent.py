"""
VendorWatch — Watchdog Agent
============================

Five responsibilities:
  1. signal_refresh_needed(reason, context)  — write a cross-agent refresh signal
  2. consume_signal()                         — read and delete the signal (Orchestrator side)
  3. check_data_freshness()                   — detect new rows in monitored tables
  4. check_new_critical_vendors()             — detect vendors newly above the bi-gov threshold
  5. run_watchdog_cycle()                     — one full pass: freshness + vendors + signal

Uses amazon.nova-lite-v1:0 (sub-second) for any LLM calls.
Degrades gracefully if db.connection is unavailable.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    BEDROCK_REGION,
    MIN_VALUE_BIGOV,
    WATCHDOG_MODEL,
)
from memory import findings_store

# ── Signal file ─────────────────────────────────────────────────────────────
SIGNAL_PATH = Path("memory/watchdog_signal.json")

# ── Tables monitored for row-count deltas ───────────────────────────────────
MONITORED_TABLES = [
    "fed.grants_contributions",
    "ab.ab_sole_source",
    "general.entity_golden_records",
]

log = logging.getLogger("vendorwatch.watchdog")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [watchdog] %(message)s", "%H:%M:%S"))
    log.addHandler(h)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MECHANISM
# ─────────────────────────────────────────────────────────────────────────────
def signal_refresh_needed(reason: str, context: dict[str, Any] | None = None) -> str:
    """Write a JSON refresh signal to SIGNAL_PATH.

    The Orchestrator polls consume_signal() and triggers a full re-analysis
    when a signal is present.

    Args:
        reason:  short machine-readable label (e.g. 'new_data', 'new_vendor').
        context: optional dict with extra detail.

    Returns:
        JSON string: ok, signal_path, reason, triggered_at.
    """
    log.info("signal_refresh_needed(reason=%r)", reason)
    payload = {
        "reason": reason,
        "context": context or {},
        "triggered_at": _now_iso(),
    }
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return json.dumps({
        "ok": True,
        "signal_path": str(SIGNAL_PATH),
        "reason": reason,
        "triggered_at": payload["triggered_at"],
    }, ensure_ascii=False)


def consume_signal() -> dict[str, Any] | None:
    """Read and delete the refresh signal (Orchestrator side).

    Returns:
        The signal dict if a signal is pending, None otherwise.
    """
    if not SIGNAL_PATH.exists():
        return None
    try:
        payload = json.loads(SIGNAL_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("consume_signal: corrupt signal file — %s", exc)
        SIGNAL_PATH.unlink(missing_ok=True)
        return None
    SIGNAL_PATH.unlink()
    log.info("consume_signal: consumed signal (reason=%r)", payload.get("reason"))
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# DATA FRESHNESS CHECK
# ─────────────────────────────────────────────────────────────────────────────
def check_data_freshness() -> str:
    """Compare current row counts in MONITORED_TABLES to the stored snapshot.

    On the first call, establishes a baseline (is_first_run=True).
    Subsequent calls set has_changes=True only when counts differ.

    Returns:
        JSON string: ok, tables_checked, is_first_run, has_changes, delta, checked_at.
    """
    log.info("check_data_freshness")
    try:
        from db.connection import query as db_query
    except ImportError:
        return json.dumps({
            "ok": False,
            "error": "db.connection not available",
            "tables_checked": MONITORED_TABLES,
            "checked_at": _now_iso(),
        })

    def _count_one(tbl: str) -> tuple[str, int]:
        schema, name = tbl.split(".", 1)
        df = db_query(f"SELECT COUNT(*) AS cnt FROM {schema}.{name}")
        return tbl, int(df["cnt"].iloc[0])

    try:
        current_counts: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=len(MONITORED_TABLES)) as pool:
            futures = {pool.submit(_count_one, t): t for t in MONITORED_TABLES}
            for fut in as_completed(futures):
                tbl, cnt = fut.result()
                current_counts[tbl] = cnt
    except Exception as exc:
        log.warning("check_data_freshness DB error: %s", exc)
        return json.dumps({
            "ok": False,
            "error": str(exc),
            "tables_checked": MONITORED_TABLES,
            "checked_at": _now_iso(),
        })

    snapshot = findings_store.get_snapshot()
    prev_counts: dict[str, int] = snapshot.get("table_counts", {})

    is_first_run = not bool(prev_counts)
    has_changes = False
    delta: dict[str, dict] = {}

    for tbl in MONITORED_TABLES:
        cur = current_counts.get(tbl, 0)
        prev = prev_counts.get(tbl, 0)
        diff = cur - prev
        if diff != 0:
            has_changes = True
        delta[tbl] = {"previous": prev, "current": cur, "diff": diff}

    snapshot["table_counts"] = current_counts
    findings_store.update_snapshot(snapshot)

    return json.dumps({
        "ok": True,
        "tables_checked": MONITORED_TABLES,
        "is_first_run": is_first_run,
        "has_changes": has_changes,
        "delta": delta,
        "checked_at": _now_iso(),
    }, ensure_ascii=False, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# NEW CRITICAL VENDORS CHECK
# ─────────────────────────────────────────────────────────────────────────────
def check_new_critical_vendors(has_changes: bool = True) -> str:
    """Scan for vendors newly above the bi-governmental threshold (MIN_VALUE_BIGOV).

    Skips the expensive DB scan when has_changes=False and a prior scan exists,
    because no new vendors can appear if no rows were added.

    Args:
        has_changes: pass False to skip the full scan when freshness shows no delta.

    Returns:
        JSON string: ok, threshold_cad, new_vendors_count, new_vendors, scanned_at.
    """
    log.info("check_new_critical_vendors (threshold=%d CAD)", MIN_VALUE_BIGOV)

    snapshot = findings_store.get_snapshot()
    known: set[str] = set(snapshot.get("known_vendor_names", []))

    # Skip the expensive scan when data is unchanged and we have a baseline.
    if not has_changes and known:
        log.info("check_new_critical_vendors: no data changes — skipping full scan")
        return json.dumps({
            "ok": True,
            "threshold_cad": MIN_VALUE_BIGOV,
            "new_vendors_count": 0,
            "new_vendors": [],
            "scanned_at": _now_iso(),
            "skipped": True,
        }, ensure_ascii=False)

    try:
        from db.connection import query as db_query
    except ImportError:
        return json.dumps({
            "ok": False,
            "error": "db.connection not available",
            "threshold_cad": MIN_VALUE_BIGOV,
            "new_vendors_count": 0,
            "scanned_at": _now_iso(),
        })

    try:
        # %% is a literal % in psycopg2 parameterised queries.
        df = db_query(
            """
            SELECT recipient_legal_name,
                   SUM(agreement_value)::numeric AS total_cad
            FROM   fed.vw_grants_decoded
            WHERE  agreement_value > 0
              AND  recipient_legal_name NOT ILIKE '%%batch%%'
            GROUP  BY recipient_legal_name
            HAVING SUM(agreement_value) >= %s
            """,
            params=(MIN_VALUE_BIGOV,),
        )
    except Exception as exc:
        log.warning("check_new_critical_vendors DB error: %s", exc)
        return json.dumps({
            "ok": False,
            "error": str(exc),
            "threshold_cad": MIN_VALUE_BIGOV,
            "new_vendors_count": 0,
            "scanned_at": _now_iso(),
        })

    all_names: list[str] = df["recipient_legal_name"].dropna().tolist()
    new_vendors = [n for n in all_names if n not in known]

    snapshot["known_vendor_names"] = list(known | set(all_names))
    findings_store.update_snapshot(snapshot)

    return json.dumps({
        "ok": True,
        "threshold_cad": MIN_VALUE_BIGOV,
        "new_vendors_count": len(new_vendors),
        "new_vendors": new_vendors[:50],
        "scanned_at": _now_iso(),
    }, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# FULL WATCHDOG CYCLE
# ─────────────────────────────────────────────────────────────────────────────
def run_watchdog_cycle() -> dict[str, Any]:
    """Run one full watchdog pass: freshness + new vendors + conditional signal.

    Fires signal_refresh_needed() when new rows or new critical vendors are found.

    Returns:
        dict: freshness, new_vendors, signalled, run_at.
    """
    log.info("run_watchdog_cycle — starting")

    freshness = json.loads(check_data_freshness())
    new_vendors = json.loads(check_new_critical_vendors(
        has_changes=freshness.get("has_changes", True)
    ))

    needs_signal = (
        freshness.get("has_changes", False)
        or new_vendors.get("new_vendors_count", 0) > 0
    )
    signalled = False
    if needs_signal:
        reason = "new_vendor" if not freshness.get("has_changes") else "new_data"
        signal_refresh_needed(
            reason=reason,
            context={
                "has_changes": freshness.get("has_changes"),
                "new_vendors_count": new_vendors.get("new_vendors_count", 0),
            },
        )
        signalled = True
        log.info("run_watchdog_cycle — signal fired (%s)", reason)
    else:
        log.info("run_watchdog_cycle — no changes detected, no signal")

    return {
        "freshness": freshness,
        "new_vendors": new_vendors,
        "signalled": signalled,
        "run_at": _now_iso(),
    }


__all__ = [
    "SIGNAL_PATH",
    "MONITORED_TABLES",
    "MIN_VALUE_BIGOV",
    "signal_refresh_needed",
    "consume_signal",
    "check_data_freshness",
    "check_new_critical_vendors",
    "run_watchdog_cycle",
]
