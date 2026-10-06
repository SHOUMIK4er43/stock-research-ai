"""Deterministic Markdown renderings of a ResearchReportSnapshot.

A rendered Markdown file is NOT the source of truth - the persisted
ResearchReportSnapshot is. Re-rendering the same snapshot in another
presentation format never requires re-running research.
"""

from pathlib import Path

from stock_research_ai.domain.report import ReportSection, ResearchReportSnapshot


def _render_table(section: ReportSection) -> list[str]:
    lines: list[str] = []
    for table in section.tables:
        lines.append(f"**{table.title}**")
        lines.append("")
        lines.append("| " + " | ".join(table.columns) + " |")
        lines.append("|" + "---|" * len(table.columns))
        for row in table.rows:
            metric = row[0]
            lines.append(
                f"| {metric.label} | {metric.display_value or 'n/a'} | {metric.unit or ''} | "
                f"{metric.period or ''} | {metric.reporting_scope or ''} | {metric.status} |"
            )
        lines.append("")
    return lines


def _render_section(section: ReportSection) -> list[str]:
    lines = [f"### {section.section.replace('_', ' ').title()}", ""]
    lines.append(f"Coverage: **{section.coverage.status}**")
    if section.coverage.reason_code:
        lines.append(f"Reason code: `{section.coverage.reason_code}`")
    lines.append("")
    lines.append(section.coverage.reason)
    lines.append("")
    lines.extend(_render_table(section))
    for statement in section.statements:
        prefix = ""
        if statement.challenge_outcome:
            prefix = f"[{statement.challenge_outcome}] "
        if statement.quote_attribution:
            prefix += f"[{statement.quote_attribution}] "
        lines.append(f"- {prefix}{statement.text}")
        if statement.limitations:
            lines.append(f"  - Limitations: {', '.join(statement.limitations)}")
    lines.append("")
    return lines


def render_institutional_report(snapshot: ResearchReportSnapshot) -> str:
    lines = [
        "# Company Institutional Research",
        "",
        f"Report ID: `{snapshot.id}`",
        f"Analysis snapshot: `{snapshot.analysis_snapshot_id}`",
        f"Challenge snapshot: `{snapshot.challenge_snapshot_id}`",
        f"Generated at: {snapshot.evaluated_at.isoformat()}",
        "",
        "## Executive Research Snapshot",
        "",
        f"Research completeness: **{snapshot.executive_snapshot.research_completeness}**",
        f"Valuation evidence state: **{snapshot.executive_snapshot.valuation_evidence_state}**",
        "",
    ]
    if snapshot.executive_snapshot.unresolved_questions:
        lines.append("Unresolved questions:")
        lines.extend(f"- {q}" for q in snapshot.executive_snapshot.unresolved_questions)
        lines.append("")
    for section in snapshot.sections:
        lines.extend(_render_section(section))
    lines.append("## Document Corpus (pinned report input only)")
    lines.append("")
    lines.append(f"corpus_scope: `{snapshot.document_corpus.corpus_scope}`")
    lines.append("")
    for doc_entry in snapshot.document_corpus.entries:
        period = doc_entry.fiscal_label or doc_entry.publication_date or "undated"
        lines.append(f"- {doc_entry.document_type} — {period} ({doc_entry.coverage_status})")
    lines.append("")
    lines.append("## Monitoring Dashboard (static)")
    lines.append("")
    for monitor_entry in snapshot.monitoring:
        value = monitor_entry.current_value or "n/a"
        lines.append(f"- {monitor_entry.metric}: {value} — {monitor_entry.monitoring_rationale}")
    return "\n".join(lines) + "\n"


def render_investor_brief(snapshot: ResearchReportSnapshot) -> str:
    exec_snap = snapshot.executive_snapshot
    lines = [
        "# Company Investor Brief",
        "",
        f"Derived from report `{snapshot.id}` — cannot contradict the institutional report; "
        "it is the same validated content, condensed.",
        "",
        "## What the company does",
        *[f"- {s.text}" for s in exec_snap.company_overview],
        "",
        "## What is improving",
        *[f"- {s.text}" for s in exec_snap.supporting_evidence],
        "",
        "## Financial trends",
        *[f"- {s.text}" for s in exec_snap.key_financial_trend],
        "",
        "## Sector KPIs",
        *[f"- {s.text}" for s in exec_snap.key_sector_trend],
        "",
        "## Management/document takeaways",
        *[f"- {s.text}" for s in exec_snap.document_highlights],
        "",
        "## Valuation",
        *[f"- {s.text}" for s in exec_snap.valuation_evidence],
        "",
        "## Market-implied expectations",
        *[f"- {s.text}" for s in exec_snap.market_implied_expectations],
        "",
        "## Catalysts",
        *[f"- {s.text}" for s in exec_snap.catalysts],
        "",
        "## Risks",
        *[f"- {s.text}" for s in exec_snap.risks],
        "",
        "## Challenger findings / thesis invalidation",
        *[f"- finding `{fid}`" for fid in exec_snap.material_challenger_finding_ids],
        "",
        "## Data gaps",
        *[f"- {g.message}" for g in exec_snap.major_data_gaps],
        "",
        "## What to monitor",
        *[f"- {m.metric}: {m.monitoring_rationale}" for m in snapshot.monitoring],
    ]
    return "\n".join(lines) + "\n"


def export_markdown(snapshot: ResearchReportSnapshot, output: Path) -> None:
    """Does not gate on a fresh/empty directory itself - callers writing multiple
    artifacts into the same output directory should call check_output once, before
    the first exporter runs."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "COMPANY_INSTITUTIONAL_RESEARCH.md").write_text(
        render_institutional_report(snapshot), encoding="utf-8"
    )
    (output / "COMPANY_INVESTOR_BRIEF.md").write_text(
        render_investor_brief(snapshot), encoding="utf-8"
    )


__all__ = ["export_markdown", "render_institutional_report", "render_investor_brief"]
