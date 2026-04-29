"""
VendorWatch — Narrator Agent
============================

Five tools:
  1. compute_citizen_impact(total_millions)
  2. generate_alert_card(finding_id)
  3. generate_policy_brief(finding_ids)
  4. answer_judge_question(question)
  5. export_to_pdf(brief_markdown)

The Narrator turns structured findings into language a deputy minister
can sign on Monday morning. For CRITICAL findings, the agent calls
compute_citizen_impact ITSELF before drafting — humanisation is autonomous.
"""
from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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
    ANTHROPIC_API_KEY,
    BEDROCK_REGION,
    BOURSE_ETUDIANTE,
    COUT_HOPITAL_REGIONAL,
    COUT_KM_AUTOROUTE,
    IMPACT_RATIO_CONSERVATIF,
    MIN_VALUE_MAJOR,
    MIN_VALUE_SIGNIFICANT,
    NARRATOR_MODEL,
    SALAIRE_ENSEIGNANT,
)
from memory import findings_store

# Severity thresholds (M$) — derived from config's financial thresholds
_CRITICAL_M = MIN_VALUE_MAJOR / 1_000_000          # 100 M$
_HIGH_M     = MIN_VALUE_SIGNIFICANT / 1_000_000    # 10 M$

log = logging.getLogger("vendorwatch.narrator")
if not log.handlers:
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [narrator] %(message)s", "%H:%M:%S"))
    log.addHandler(h)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_finding_by_id(finding_id: str) -> dict[str, Any] | None:
    """Fetch one finding by its 'id' field (e.g. 'F0001'). Local helper —
    findings_store only exposes get_findings(severity, category)."""
    for f in findings_store.get_findings():
        if f.get("id") == finding_id:
            return f
    return None


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — compute_citizen_impact
# ─────────────────────────────────────────────────────────────────────────────
@tool
def compute_citizen_impact(total_millions: float) -> str:
    """Convert dollar exposure into citizen-comprehensible units.

    Auto-called by generate_alert_card for CRITICAL findings and by
    generate_policy_brief for aggregate exposure. Applies a deliberately
    conservative 10% ratio so the figure is defensible.

    Args:
        total_millions: Exposure in millions of CAD.

    Returns:
        JSON string: hopitaux, enseignants, km_routes, bourses, note.
    """
    log.info("compute_citizen_impact(%.1f M$)", total_millions)
    try:
        if total_millions <= 0:
            raise ValueError("total_millions must be positive")
        recoverable = total_millions * 1_000_000 * IMPACT_RATIO_CONSERVATIF
        return json.dumps({
            "exposure_total_millions": round(total_millions, 1),
            "recoverable_estimate_cad": int(recoverable),
            "ratio_applied_pct": int(IMPACT_RATIO_CONSERVATIF * 100),
            "hopitaux":    round(recoverable / COUT_HOPITAL_REGIONAL, 2),
            "enseignants": int(recoverable / SALAIRE_ENSEIGNANT),
            "km_routes":   int(recoverable / COUT_KM_AUTOROUTE),
            "bourses":     int(recoverable / BOURSE_ETUDIANTE),
            "note": (
                f"Estimation prudente à {int(IMPACT_RATIO_CONSERVATIF * 100)} % "
                "du surcoût potentiel — pas l'exposition totale."
            ),
        }, ensure_ascii=False)
    except (ValueError, ZeroDivisionError) as exc:
        log.warning("compute_citizen_impact failed: %s", exc)
        return json.dumps({"error": str(exc), "exposure_total_millions": total_millions})


def _severity_from_finding(finding: dict[str, Any]) -> str:
    if (sev := finding.get("severity")) in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return sev
    exposure = float(finding.get("exposure_millions", 0))
    if exposure >= _CRITICAL_M:
        return "CRITICAL"
    if exposure >= _HIGH_M:
        return "HIGH"
    return "MEDIUM"


def _format_finding_for_prompt(finding: dict[str, Any]) -> str:
    bn = finding.get("vendor_name", "Fournisseur inconnu")
    ftype = finding.get("finding_type", "concentration")
    exposure = finding.get("exposure_millions")
    fs_ratio = finding.get("sole_source_ratio")
    years = finding.get("contract_years")
    quote = finding.get("contract_quote")
    cra = finding.get("cra_revocation")
    evidence = finding.get("evidence", [])

    lines = [f"- VENDOR: {bn}", f"  TYPE: {ftype}"]
    if exposure is not None:
        lines.append(f"  EXPOSURE: {exposure} M$ CAD")
    if fs_ratio is not None:
        lines.append(f"  SOLE-SOURCE RATIO: {fs_ratio:.1%}")
    if years is not None:
        lines.append(f"  CONTRACT YEARS: {years}")
    if quote:
        lines.append(f'  CONTRACT QUOTE (verbatim): "{quote}"')
    if cra:
        lines.append(f"  CRA SIGNAL: {cra}")
    if evidence:
        lines.append(f"  EVIDENCE LINES: {len(evidence)} contract rows traceable")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — generate_alert_card
# ─────────────────────────────────────────────────────────────────────────────
_ALERT_CARD_SYSTEM = """\
You are the Narrator agent for VendorWatch. You write 100-word plain-language
alert cards for Canadian procurement officers. Your voice is Bloomberg-meets-
Auditor-General: factual, named, dollar-specific, never alarmist.

ABSOLUTE RULES
1. Never invent a fact not present in the finding payload.
2. Always name the vendor and the dollar exposure in the first sentence.
3. If a verbatim contract quote is supplied, USE IT in quotation marks.
4. End with one concrete action a deputy minister could authorise Monday.
5. ~100 words. No headings. No emojis. Plain prose.
6. If severity is CRITICAL, include the citizen-impact translation that was
   computed for you (one sentence, e.g. "≈ 228 teacher salaries per year").
"""


def _build_alert_card_prompt(finding: dict[str, Any], citizen_impact: dict[str, Any] | None) -> str:
    parts = [
        "Draft a 100-word alert card for the following finding.\n",
        "FINDING:\n" + _format_finding_for_prompt(finding),
        f"\nSEVERITY: {_severity_from_finding(finding)}",
    ]
    if citizen_impact:
        parts.append(
            "\nCITIZEN IMPACT (already computed — weave one line into the card):\n"
            + json.dumps(citizen_impact, ensure_ascii=False, indent=2)
        )
    parts.append("\nWrite the card now. ~100 words.")
    return "\n".join(parts)


@tool
def generate_alert_card(finding_id: str) -> str:
    """Generate a 100-word plain-language alert card for a finding.

    For CRITICAL severity, first calls compute_citizen_impact and weaves
    the translation into the card.

    Args:
        finding_id: 'id' field from findings_store (e.g. 'F0001').

    Returns:
        JSON string: id, severity, card_markdown, citizen_impact, generated_at
    """
    log.info("generate_alert_card(%s)", finding_id)
    finding = _get_finding_by_id(finding_id)
    if finding is None:
        log.warning("finding %s not found", finding_id)
        return json.dumps({"error": "finding_not_found", "id": finding_id})

    severity = _severity_from_finding(finding)
    citizen_impact: dict[str, Any] | None = None
    if severity == "CRITICAL" and finding.get("exposure_millions"):
        try:
            citizen_impact = json.loads(
                compute_citizen_impact(float(finding["exposure_millions"]))
            )
        except (ValueError, json.JSONDecodeError):
            citizen_impact = None

    prompt = _build_alert_card_prompt(finding, citizen_impact)
    card_md = _invoke_model(system=_ALERT_CARD_SYSTEM, user=prompt, max_tokens=500)

    return json.dumps({
        "id": finding_id,
        "vendor_name": finding.get("vendor_name"),
        "severity": severity,
        "exposure_millions": finding.get("exposure_millions"),
        "citizen_impact": citizen_impact,
        "card_markdown": card_md,
        "generated_at": _now_iso(),
    }, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — generate_policy_brief
# ─────────────────────────────────────────────────────────────────────────────
_POLICY_BRIEF_SYSTEM = """\
You are the Narrator agent for VendorWatch. You draft policy briefs for the
Deputy Minister of Public Services and Procurement Canada. The reader has
seven minutes between meetings.

STRUCTURE (use these exact section headings):

  ## Issue
  One paragraph. What is the structural risk, in dollars, naming vendors.

  ## Findings
  Bullet list. One bullet per finding. Each bullet: vendor, dollar exposure,
  the single most damning data point (verbatim contract quote if available),
  and CRA signal if present.

  ## Recommended Action
  Numbered list. Each action: who acts, what they do, by when, citing the
  procurement instrument. No vague verbs ("review", "consider"). Use:
  "issue an open RFP", "audit the EA", "freeze sole-source extensions".

  ## Defensibility
  One paragraph. State that every figure traces to an open-data contract row
  or T3010 filing. Acknowledge limits: correlations, not legal conclusions.

CONSTRAINTS
- Plain English. No jargon beyond procurement terms a DM already knows.
- Every dollar figure must come from the findings payload, never invented.
- Every verbatim quote must be in quotation marks and attributed.
- No emojis. No headers beyond the four above. No closing pleasantries.
- The Defensibility section MUST end with a complete sentence — do not run long.
"""


@tool
def generate_policy_brief(finding_ids: list[str]) -> str:
    """Generate a deputy-minister policy brief from one or more findings.

    Args:
        finding_ids: list of 'id' values from findings_store.

    Returns:
        JSON string with full brief + aggregate citizen impact.
    """
    log.info("generate_policy_brief(%d findings)", len(finding_ids))
    findings = [f for f in (_get_finding_by_id(fid) for fid in finding_ids) if f]
    if not findings:
        return json.dumps({"error": "no_findings_resolved", "ids": finding_ids})

    total_exposure = sum(float(f.get("exposure_millions", 0)) for f in findings)
    citizen_impact: dict[str, Any] | None = None
    if total_exposure > 0:
        try:
            citizen_impact = json.loads(compute_citizen_impact(total_exposure))
        except (ValueError, json.JSONDecodeError):
            citizen_impact = None

    findings_block = "\n\n".join(_format_finding_for_prompt(f) for f in findings)
    impact_block = (
        "\n\nAGGREGATE CITIZEN IMPACT (use in Defensibility section):\n"
        + json.dumps(citizen_impact, ensure_ascii=False, indent=2)
        if citizen_impact else ""
    )
    user_prompt = (
        f"Draft a policy brief covering {len(findings)} finding(s). "
        f"Aggregate exposure: {total_exposure:.1f} M$ CAD.\n\n"
        f"FINDINGS:\n{findings_block}{impact_block}\n\n"
        "Write the brief now."
    )

    brief_md = _invoke_model(system=_POLICY_BRIEF_SYSTEM, user=user_prompt, max_tokens=2500)

    return json.dumps({
        "ids": finding_ids,
        "vendors": [f.get("vendor_name") for f in findings],
        "total_exposure_millions": round(total_exposure, 1),
        "citizen_impact": citizen_impact,
        "brief_markdown": brief_md,
        "generated_at": _now_iso(),
    }, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 4 — answer_judge_question
# ─────────────────────────────────────────────────────────────────────────────
_JUDGE_QA_SYSTEM = """\
You are the Narrator agent for VendorWatch, answering live questions from a
hackathon jury. You have access to the full findings store as context.

RULES
- Answer in 2–4 sentences. The jury is timing themselves.
- Cite at least one specific dollar figure or vendor name from the findings.
- If the question asks about something NOT in the findings, say so plainly:
  "The findings store doesn't contain that — VendorWatch only claims what
  the data supports." Do NOT speculate.
- Mirror the language of the question (English ↔ French) automatically.
- Never reproduce a contract quote longer than ~12 words.
"""


@tool
def answer_judge_question(question: str) -> str:
    """Answer a free-form question by retrieving from the findings store.

    Args:
        question: the judge's question, English or French.

    Returns:
        JSON string: question, answer, findings_referenced, generated_at
    """
    log.info("answer_judge_question(%r)", question[:80])
    all_findings = findings_store.get_findings()
    if not all_findings:
        return json.dumps({
            "question": question,
            "answer": (
                "The findings store is empty — no analysis has run yet. "
                "Trigger the Watchdog or run_full_cycle() first."
            ),
            "findings_referenced": [],
            "generated_at": _now_iso(),
        }, ensure_ascii=False)

    context_block = "\n\n".join(_format_finding_for_prompt(f) for f in all_findings)
    user_prompt = (
        f"JUDGE QUESTION:\n{question}\n\n"
        f"FINDINGS STORE ({len(all_findings)} entries):\n{context_block}\n\n"
        "Answer now in 2–4 sentences."
    )
    answer = _invoke_model(system=_JUDGE_QA_SYSTEM, user=user_prompt, max_tokens=600)

    return json.dumps({
        "question": question,
        "answer": answer,
        "findings_referenced": [f.get("id") for f in all_findings],
        "generated_at": _now_iso(),
    }, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 5 — export_to_pdf  (triple fallback, cross-platform temp dir)
# ─────────────────────────────────────────────────────────────────────────────
_PDF_CSS = """
@page { size: Letter; margin: 2cm; }
body  { font-family: 'Helvetica', sans-serif; font-size: 11pt;
        line-height: 1.55; color: #1a1a1a; }
h1    { font-size: 18pt; font-weight: 600; margin: 0 0 4pt 0; }
h2    { font-size: 13pt; font-weight: 600; margin: 18pt 0 6pt 0;
        border-bottom: 0.5pt solid #999; padding-bottom: 2pt; }
p, li { margin: 4pt 0; }
ul, ol{ margin: 6pt 0 6pt 18pt; }
.meta { color: #666; font-size: 9pt; margin-bottom: 16pt; }
strong{ font-weight: 600; }
"""


def _markdown_to_html(md: str) -> str:
    out: list[str] = []
    in_ul = False
    in_ol = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            _close_lists()
            continue
        if line.startswith("## "):
            _close_lists()
            out.append(f"<h2>{line[3:].strip()}</h2>")
        elif line.startswith("# "):
            _close_lists()
            out.append(f"<h1>{line[2:].strip()}</h1>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_ul:
                _close_lists()
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{line.lstrip()[2:]}</li>")
        elif line.lstrip()[:2].rstrip(".").isdigit() and ". " in line:
            if not in_ol:
                _close_lists()
                out.append("<ol>"); in_ol = True
            out.append(f"<li>{line.split('. ', 1)[1]}</li>")
        else:
            _close_lists()
            line = line.replace("**", "§§")
            parts = line.split("§§")
            line = "".join(f"<strong>{p}</strong>" if i % 2 else p for i, p in enumerate(parts))
            out.append(f"<p>{line}</p>")
    _close_lists()
    return "\n".join(out)


def _wrap_html(body_html: str, title: str = "VendorWatch — Note d'information") -> str:
    return (
        f"<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{_PDF_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        f"<div class='meta'>Généré le {_now_iso()} · VendorWatch · "
        f"Confidentiel — usage interne</div>{body_html}</body></html>"
    )


@tool
def export_to_pdf(brief_markdown: str, output_path: str | None = None) -> str:
    """Render a brief (markdown) to PDF using triple-fallback strategy.

    Strategy: weasyprint → reportlab → HTML print fallback.
    Uses tempfile.gettempdir() so paths are correct on Windows + Linux + macOS.

    Args:
        brief_markdown: the brief returned by generate_policy_brief.
        output_path: optional override; default <tempdir>/vendorwatch_brief_<ts>.pdf

    Returns:
        JSON string: ok, output_path, strategy_used
    """
    log.info("export_to_pdf (%d chars)", len(brief_markdown))

    if output_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = str(Path(tempfile.gettempdir()) / f"vendorwatch_brief_{ts}.pdf")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    body_html = _markdown_to_html(brief_markdown)
    full_html = _wrap_html(body_html)

    # Strategy 1: weasyprint
    try:
        import weasyprint
        weasyprint.HTML(string=full_html).write_pdf(str(out))
        log.info("export_to_pdf OK via weasyprint → %s", out)
        return json.dumps({"ok": True, "output_path": str(out), "strategy_used": "weasyprint"})
    except Exception as exc:
        log.warning("weasyprint failed: %s", exc)

    # Strategy 2: reportlab
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (
            ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer,
        )

        doc = SimpleDocTemplate(str(out), pagesize=LETTER,
                                leftMargin=54, rightMargin=54,
                                topMargin=54, bottomMargin=54)
        styles = getSampleStyleSheet()
        story: list[Any] = [
            Paragraph("VendorWatch — Note d'information", styles["Title"]),
            Paragraph(f"Généré le {_now_iso()} · Confidentiel — usage interne", styles["Italic"]),
            Spacer(1, 12),
        ]
        ul_buf: list[str] = []
        ol_buf: list[str] = []

        def _flush_ul() -> None:
            if ul_buf:
                story.append(ListFlowable(
                    [ListItem(Paragraph(t, styles["BodyText"])) for t in ul_buf],
                    bulletType="bullet",
                ))
                ul_buf.clear()

        def _flush_ol() -> None:
            if ol_buf:
                story.append(ListFlowable(
                    [ListItem(Paragraph(t, styles["BodyText"])) for t in ol_buf],
                    bulletType="1",
                ))
                ol_buf.clear()

        for raw in brief_markdown.splitlines():
            line = raw.rstrip()
            if not line.strip():
                _flush_ul(); _flush_ol()
                story.append(Spacer(1, 6))
                continue
            if line.startswith("## "):
                _flush_ul(); _flush_ol()
                story.append(Paragraph(line[3:].strip(), styles["Heading2"]))
            elif line.startswith("# "):
                _flush_ul(); _flush_ol()
                story.append(Paragraph(line[2:].strip(), styles["Heading1"]))
            elif line.lstrip().startswith(("- ", "* ")):
                _flush_ol()
                ul_buf.append(line.lstrip()[2:])
            elif line.lstrip()[:2].rstrip(".").isdigit() and ". " in line:
                _flush_ul()
                ol_buf.append(line.split(". ", 1)[1])
            else:
                _flush_ul(); _flush_ol()
                story.append(Paragraph(
                    line.replace("**", "<b>", 1).replace("**", "</b>", 1),
                    styles["BodyText"],
                ))
        _flush_ul(); _flush_ol()
        doc.build(story)
        log.info("export_to_pdf OK via reportlab → %s", out)
        return json.dumps({"ok": True, "output_path": str(out), "strategy_used": "reportlab"})
    except Exception as exc:
        log.warning("reportlab failed: %s", exc)

    # Strategy 3: HTML fallback
    html_path = out.with_suffix(".html")
    try:
        html_path.write_text(full_html, encoding="utf-8")
        log.info("export_to_pdf fallback HTML → %s", html_path)
        return json.dumps({
            "ok": True,
            "output_path": str(html_path),
            "strategy_used": "html_print_fallback",
            "note": "Open in browser and use Print → Save as PDF.",
        })
    except OSError as exc:
        log.error("export_to_pdf TOTAL FAILURE: %s", exc)
        return json.dumps({"ok": False, "error": str(exc)})


# ─────────────────────────────────────────────────────────────────────────────
# Bedrock invocation (with Anthropic API fallback per brief §6)
# ─────────────────────────────────────────────────────────────────────────────
_agent_singleton: Any = None


def _get_agent() -> Any:
    """Build the Strands Agent on first use (singleton)."""
    global _agent_singleton
    if _agent_singleton is not None:
        return _agent_singleton
    if not _STRANDS_AVAILABLE:
        return None
    model = BedrockModel(model_id=NARRATOR_MODEL, region_name=BEDROCK_REGION)
    _agent_singleton = Agent(
        model=model,
        tools=[
            compute_citizen_impact, generate_alert_card,
            generate_policy_brief, answer_judge_question, export_to_pdf,
        ],
        system_prompt=(
            "You are the Narrator agent for VendorWatch. Use your tools to "
            "produce alert cards, policy briefs, and judge answers. Always "
            "compute citizen impact before drafting CRITICAL content."
        ),
    )
    return _agent_singleton


def _invoke_model(system: str, user: str, max_tokens: int = 800) -> str:
    """Direct Sonnet invocation. Bedrock first, Anthropic API fallback."""
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        resp = client.converse(
            modelId=NARRATOR_MODEL,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3},
        )
        return resp["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:
        log.warning("Bedrock failed: %s — trying Anthropic API fallback", exc)

    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=max_tokens,
                temperature=0.3,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        except Exception as exc:
            log.error("Anthropic API fallback also failed: %s", exc)

    return "[LLM ERROR — Narrator could not generate prose. Both Bedrock and Anthropic API failed.]"


def generate_alert_cards_for_all() -> list[dict[str, Any]]:
    """Produce one card per finding currently in the store."""
    cards: list[dict[str, Any]] = []
    for f in findings_store.get_findings():
        fid = f.get("id")
        if not fid:
            continue
        cards.append(json.loads(generate_alert_card(fid)))
    return cards


__all__ = [
    "compute_citizen_impact", "generate_alert_card", "generate_policy_brief",
    "answer_judge_question", "export_to_pdf", "generate_alert_cards_for_all",
]