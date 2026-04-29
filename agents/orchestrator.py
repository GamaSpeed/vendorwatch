"""
VendorWatch — Orchestrator Agent
=================================
The Orchestrator is the brain. It coordinates all other agents, routes
user queries, and wakes up autonomously when the Watchdog signals a
data change. It never computes or writes SQL directly — it delegates
everything and synthesises the results.

Five tools (per brief §5 / F25–F27):

    1. check_for_updates()
       Delegates to Watchdog.run_watchdog_cycle(). Returns the cycle
       report and whether a refresh was signalled.

    2. run_full_analysis()
       Delegates to Analyst.run_analysis(). Triggers the full
       HHI + lock-in + fictional competition + bi-gov + CRA pipeline.

    3. generate_narratives(finding_ids)
       Delegates to Narrator.generate_alert_cards_for_all() and
       generate_policy_brief(). Returns all cards + the brief.

    4. answer_question(question)
       Routes to Narrator.answer_judge_question(). Used for live
       judge Q&A during the demo.

    5. get_summary()
       Returns a fast summary from the findings store — no re-analysis.
       The Orchestrator reads existing findings and returns the top
       stats. Zero LLM calls unless the store is empty.

Plus the main entry points used externally:

    run_vendorwatch(force_refresh)  — full pipeline, one call
    wake_on_watchdog_signal()       — called when signal file appears
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

try:
    from strands import Agent, tool
    from strands.models import BedrockModel
    _STRANDS_AVAILABLE = True
except ImportError:
    _STRANDS_AVAILABLE = False
    def tool(fn):
        fn.is_tool = True
        return fn

from config import (
    BEDROCK_REGION,
    ORCHESTRATOR_MODEL,
    WATCHDOG_DEMO_INTERVAL_SEC,
    MIN_VALUE_MAJOR,
)
from memory import findings_store
from agents import watchdog_agent
from agents import narrator_agent

# Analyst is Person B's file — lazy import so we can test without it
def _get_analyst():
    try:
        from agents import analyst_agent
        return analyst_agent
    except ImportError:
        return None

log = logging.getLogger("vendorwatch.orchestrator")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [orchestrator] %(message)s", "%H:%M:%S"))
    log.addHandler(h)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(event: str, payload: dict[str, Any] | None = None) -> None:
    """Emit an activity event — consumed by api.py /agent/activity SSE stream.
    Logs to the named logger; api.py hooks this handler at startup."""
    log.info("[activity] %s %s", event, json.dumps(payload or {}, ensure_ascii=False))


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — check_for_updates
# ─────────────────────────────────────────────────────────────────────────────
@tool
def check_for_updates() -> str:
    """Delegate to Watchdog.run_watchdog_cycle() and return the result.

    The Watchdog checks row-count deltas and new critical vendors. If it
    finds anything material, it writes the signal file. This tool reads
    the cycle report — the Orchestrator decides what to do with it.

    Returns:
        JSON string: cycle report from the Watchdog.
    """
    _emit("check_for_updates.start")
    try:
        cycle = watchdog_agent.run_watchdog_cycle()
        _emit("check_for_updates.done", {
            "has_changes": cycle.get("freshness", {}).get("has_changes"),
            "signalled": cycle.get("signalled"),
        })
        return json.dumps(cycle, ensure_ascii=False, default=str)
    except Exception as exc:
        log.error("check_for_updates failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc), "generated_at": _now_iso()})


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — run_full_analysis
# ─────────────────────────────────────────────────────────────────────────────
@tool
def run_full_analysis() -> str:
    """Delegate to Analyst agent to run the full detection pipeline.

    Triggers: compute_hhi, detect_lockin, find_fictional_competition,
    find_bigov_network, find_cra_revocations. Each finding is saved to
    findings_store by the Analyst.

    Returns:
        JSON string: {ok, findings_count, finding_ids, generated_at}
    """
    _emit("run_full_analysis.start")
    analyst = _get_analyst()
    if analyst is None:
        msg = "analyst_agent not available — A's branch not merged yet"
        log.warning(msg)
        return json.dumps({"ok": False, "error": msg, "generated_at": _now_iso()})

    try:
        # Analyst exposes run_analysis() — returns list of finding ids or dict
        result = analyst.run_analysis()

        # Normalise: run_analysis() may return list[str] or dict
        if isinstance(result, list):
            finding_ids = result
        elif isinstance(result, dict):
            finding_ids = result.get("finding_ids", result.get("ids", []))
        else:
            finding_ids = []

        findings_count = len(findings_store.get_findings())
        _emit("run_full_analysis.done", {
            "findings_count": findings_count,
            "new_ids": finding_ids,
        })
        return json.dumps({
            "ok": True,
            "findings_count": findings_count,
            "finding_ids": finding_ids,
            "generated_at": _now_iso(),
        }, ensure_ascii=False)
    except Exception as exc:
        log.error("run_full_analysis failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc), "generated_at": _now_iso()})


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — generate_narratives
# ─────────────────────────────────────────────────────────────────────────────
@tool
def generate_narratives(finding_ids: list[str] | None = None) -> str:
    """Delegate to Narrator to produce alert cards and a policy brief.

    If finding_ids is None or empty, generates cards for ALL findings
    in the store. Always produces a policy brief for CRITICAL findings.

    Returns:
        JSON string: {ok, cards_count, brief, generated_at}
    """
    _emit("generate_narratives.start", {"finding_ids": finding_ids})
    try:
        # Alert cards
        if finding_ids:
            cards = [
                json.loads(narrator_agent.generate_alert_card(fid))
                for fid in finding_ids
            ]
        else:
            cards = narrator_agent.generate_alert_cards_for_all()

        # Policy brief — CRITICAL findings only
        critical_ids = [
            c["id"] for c in cards
            if c.get("severity") == "CRITICAL" and not c.get("error")
        ]
        brief: dict[str, Any] | None = None
        if critical_ids:
            brief = json.loads(narrator_agent.generate_policy_brief(critical_ids))

        _emit("generate_narratives.done", {
            "cards_count": len(cards),
            "brief_generated": brief is not None,
        })
        return json.dumps({
            "ok": True,
            "cards_count": len(cards),
            "cards": cards,
            "brief": brief,
            "generated_at": _now_iso(),
        }, ensure_ascii=False)
    except Exception as exc:
        log.error("generate_narratives failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc), "generated_at": _now_iso()})


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4 — answer_question
# ─────────────────────────────────────────────────────────────────────────────
@tool
def answer_question(question: str) -> str:
    """Route a free-form question to Narrator.answer_judge_question().

    Used for live judge Q&A in the demo. Mirrors language automatically
    (EN/FR). Cites findings from the store.

    Args:
        question: judge's question, English or French.

    Returns:
        JSON string from Narrator.answer_judge_question().
    """
    _emit("answer_question", {"question": question[:80]})
    try:
        return narrator_agent.answer_judge_question(question)
    except Exception as exc:
        log.error("answer_question failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc), "question": question})


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 5 — get_summary
# ─────────────────────────────────────────────────────────────────────────────
@tool
def get_summary() -> str:
    """Return a fast KPI summary from the findings store. No LLM calls.

    Per brief: Orchestrator.get_summary() must NOT re-trigger analysis.
    Reads the store and returns top-level stats the UI can render
    immediately — total exposure, vendor count, severity breakdown,
    last run timestamp.

    Returns:
        JSON string: {ok, kpis, top_vendors, last_full_run, generated_at}
    """
    _emit("get_summary")
    try:
        store_raw = findings_store.load()
        all_findings = store_raw.get("findings", [])
        last_run = store_raw.get("last_full_run")

        total_exposure = sum(
            float(f.get("exposure_millions", 0)) for f in all_findings
        )
        severity_counts: dict[str, int] = {}
        for f in all_findings:
            sev = f.get("severity", "UNKNOWN")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        top_vendors = sorted(
            [
                {
                    "id": f.get("id"),
                    "vendor_name": f.get("vendor_name"),
                    "exposure_millions": f.get("exposure_millions"),
                    "severity": f.get("severity"),
                    "finding_type": f.get("finding_type"),
                    "cra_revocation": f.get("cra_revocation"),
                }
                for f in all_findings
                if f.get("exposure_millions")
            ],
            key=lambda x: float(x["exposure_millions"] or 0),
            reverse=True,
        )[:10]

        critical_exposure = sum(
            float(f.get("exposure_millions", 0))
            for f in all_findings
            if f.get("severity") == "CRITICAL"
        )

        return json.dumps({
            "ok": True,
            "kpis": {
                "total_findings": len(all_findings),
                "total_exposure_millions": round(total_exposure, 1),
                "critical_exposure_millions": round(critical_exposure, 1),
                "severity_breakdown": severity_counts,
                "vendors_tracked": len({f.get("vendor_id") for f in all_findings if f.get("vendor_id")}),
            },
            "top_vendors": top_vendors,
            "last_full_run": last_run,
            "signal_pending": watchdog_agent.SIGNAL_PATH.exists(),
            "generated_at": _now_iso(),
        }, ensure_ascii=False)
    except Exception as exc:
        log.error("get_summary failed: %s", exc)
        return json.dumps({"ok": False, "error": str(exc), "generated_at": _now_iso()})


# ─────────────────────────────────────────────────────────────────────────────
# Strands Agent singleton
# ─────────────────────────────────────────────────────────────────────────────
_agent_singleton: Any = None


def _get_agent() -> Any:
    global _agent_singleton
    if _agent_singleton is not None:
        return _agent_singleton
    if not _STRANDS_AVAILABLE:
        return None
    model = BedrockModel(model_id=ORCHESTRATOR_MODEL, region_name=BEDROCK_REGION)
    _agent_singleton = Agent(
        model=model,
        tools=[
            check_for_updates,
            run_full_analysis,
            generate_narratives,
            answer_question,
            get_summary,
        ],
        system_prompt=(
            "You are the Orchestrator for VendorWatch. You coordinate five "
            "specialist agents. You never compute directly — you delegate. "
            "When the Watchdog signals a change, run_full_analysis then "
            "generate_narratives. Never hallucinate findings."
        ),
    )
    return _agent_singleton


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────
def wake_on_watchdog_signal() -> dict[str, Any] | None:
    """Poll the signal file. If present, consume it and run a full cycle.

    Called by the Orchestrator's heartbeat loop. Returns the cycle result
    or None if no signal was pending.
    """
    signal = watchdog_agent.consume_signal()
    if signal is None:
        return None

    log.info("Watchdog signal consumed: %s — starting full cycle", signal.get("reason"))
    _emit("wake_on_watchdog_signal", {"reason": signal.get("reason")})

    analysis = json.loads(run_full_analysis())
    finding_ids = analysis.get("finding_ids") or []
    narratives = json.loads(generate_narratives(finding_ids if finding_ids else None))
    findings_store.mark_run_complete()

    result = {
        "triggered_by": signal,
        "analysis": analysis,
        "narratives_summary": {
            "cards_count": narratives.get("cards_count"),
            "brief_generated": narratives.get("brief") is not None,
        },
        "completed_at": _now_iso(),
    }
    _emit("wake_on_watchdog_signal.complete", result["narratives_summary"])
    return result


def run_vendorwatch(force_refresh: bool = False) -> dict[str, Any]:
    """Main entry point. One call runs the full pipeline.

    Args:
        force_refresh: skip Watchdog check and run analysis regardless.

    Returns:
        Full pipeline result dict.
    """
    log.info("═══ run_vendorwatch(force_refresh=%s) ═══", force_refresh)
    _emit("run_vendorwatch.start", {"force_refresh": force_refresh})
    start = time.monotonic()

    # Step 1: check for updates unless forced
    should_run = force_refresh
    watchdog_report: dict[str, Any] | None = None
    if not force_refresh:
        watchdog_report = json.loads(check_for_updates())
        should_run = watchdog_report.get("signalled", False) or watchdog_report.get("is_first_run", False)
        if not should_run:
            log.info("No data change detected — returning cached summary")
            summary = json.loads(get_summary())
            summary["source"] = "cache"
            summary["watchdog"] = watchdog_report
            return summary

    # Step 2: full analysis
    analysis = json.loads(run_full_analysis())

    # Step 3: narratives
    finding_ids = analysis.get("finding_ids") or []
    narratives = json.loads(generate_narratives(finding_ids if finding_ids else None))

    # Step 4: mark complete
    findings_store.mark_run_complete()

    elapsed = round(time.monotonic() - start, 1)
    result = {
        "ok": True,
        "source": "fresh_analysis",
        "elapsed_sec": elapsed,
        "watchdog": watchdog_report,
        "analysis": analysis,
        "findings_count": analysis.get("findings_count", 0),
        "cards_count": narratives.get("cards_count", 0),
        "brief": narratives.get("brief"),
        "cards": narratives.get("cards", []),
        "generated_at": _now_iso(),
    }
    _emit("run_vendorwatch.done", {
        "elapsed_sec": elapsed,
        "findings_count": result["findings_count"],
        "cards_count": result["cards_count"],
    })
    log.info("═══ run_vendorwatch complete in %.1fs ═══", elapsed)
    return result


__all__ = [
    "check_for_updates",
    "run_full_analysis",
    "generate_narratives",
    "answer_question",
    "get_summary",
    "run_vendorwatch",
    "wake_on_watchdog_signal",
]