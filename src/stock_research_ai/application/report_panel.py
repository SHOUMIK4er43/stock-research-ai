"""DETAILED_RESEARCH_V1 Box 15: the side-panel view of one stored final report (pure).

The extension shows two views of the SAME Box 14 FinalResearchReportDataset:

  * Detailed Research - the executive summary points and every section's status, as Box 14
    states them (the full document is the PDF / HTML / Markdown / JSON download);
  * Negatives / Risks - Box 14's risk presentation: RED and YELLOW company risks, positive
    controls, and - kept apart, never styled as company risk - input conflicts, data
    limitations and open diligence items.

Only Box 14 text and codes are copied (claim statements are Box 14's advice-checked
templates); nothing is computed, re-graded or summarised here.
"""

from collections.abc import Iterable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.report_presentation import basis_note
from stock_research_ai.application.research_completeness import summary_lines
from stock_research_ai.domain.final_research_report import (
    FinalResearchReportDataset,
    ReportSection,
    ResearchCompleteness,
    SectionId,
)
from stock_research_ai.domain.report_rendering import (
    DISCLAIMER,
    STATUS_LABELS,
    ReportManifest,
)

MAX_PANEL_ITEMS = 300  # per list; the downloads always hold everything


class PanelItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str  # e.g. a risk code, a summary kind, a limitation kind
    severity: str | None = None  # RED / YELLOW / GREEN, copied from Box 14
    statement: str  # the Box 14 claim statement, verbatim
    detail: str | None = None  # category / reason / needed source, copied


class PanelSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    order: int
    title: str
    status: str
    status_label: str  # "Data unavailable" vs "Not applicable" stay distinct
    reason: str


class PanelList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[PanelItem, ...]
    total: int  # how many Box 14 holds (items may be capped for the panel)


class FinalReportPanel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: UUID
    report_id: UUID
    company_name: str
    nse_symbol: str | None
    isin: str | None
    screener_page: str | None
    as_of: str
    status: str
    formats: tuple[str, ...]
    failed_formats: tuple[str, ...]
    disclaimer: str
    summary: PanelList
    sections: tuple[PanelSection, ...]
    red: PanelList
    yellow: PanelList
    positive_controls: PanelList
    input_conflicts: PanelList
    data_limitations: PanelList
    open_diligence: PanelList
    recommendation: str = "NONE"
    #: the one reporting basis of every comparable figure above (the Summary acquires the
    #: same basis); the control basis appears only as the report's labelled control layer
    analytical_basis: str | None = None
    control_basis: str | None = None
    #: the report's ONE business classification (Box 4): coarse class + Industry subtype
    business_class: str | None = None
    industry: str | None = None
    #: THE research completeness conclusion (Box 14's material-requirement ledger): the
    #: rendering status above is execution, this is analysis; None on older reports
    research_status: str | None = None
    research_completeness: tuple[tuple[str, str], ...] = ()
    #: a readable note when the basis is not the default consolidated choice (e.g. stale)
    basis_note: str | None = None
    #: THE risk coverage (Box 14): the headline, its counts, the rules that ran clean and
    #: the rules NOT EVALUATED (by name, with their missing evidence); None on older reports
    risk_coverage: str | None = None
    risk_counts: tuple[tuple[str, int], ...] = ()
    evaluated_clear: PanelList = PanelList(items=(), total=0)
    not_evaluated: PanelList = PanelList(items=(), total=0)


def _list(items: Iterable[PanelItem]) -> PanelList:
    all_items = list(items)
    return PanelList(items=tuple(all_items[:MAX_PANEL_ITEMS]), total=len(all_items))


def _label(code: str) -> str:
    return code.replace("_", " ").strip().capitalize()


def _panel_section(s: ReportSection, rc: ResearchCompleteness | None) -> PanelSection:
    """Section 0 shows the research conclusion: its own status only says its inputs exist."""
    if s.section_id == SectionId.DUE_DILIGENCE_STATUS and rc is not None:
        return PanelSection(order=s.order, title=s.title, status=rc.overall.value,
                            status_label=f"Research {STATUS_LABELS[rc.overall.value]}",
                            reason="research completeness (status overview inputs: "
                                   f"{STATUS_LABELS[s.status.value]})")
    return PanelSection(order=s.order, title=s.title, status=s.status.value,
                        status_label=STATUS_LABELS[s.status.value], reason=s.status_reason)


def build_panel(report: FinalResearchReportDataset, manifest: ReportManifest) -> FinalReportPanel:
    claims = {c.claim_id: c for c in report.claims}
    risks = {e.risk_id: e for e in report.risks.risk_register}

    def statement(claim_id: UUID) -> str:
        claim = claims.get(claim_id)
        return claim.statement if claim is not None else "Statement not available"

    def risk_items(ids: Iterable[UUID]) -> list[PanelItem]:
        out = []
        for risk_id in ids:
            entry = risks[risk_id]
            out.append(PanelItem(label=entry.code, severity=entry.severity,
                                 statement=statement(entry.claim_id),
                                 detail=f"{_label(entry.category)} · {entry.status}"))
        return out

    summary: list[PanelItem] = []
    for point in report.executive_summary.points:
        if point.status != "AVAILABLE":
            text = (STATUS_LABELS["NOT_APPLICABLE"] if point.status == "NOT_APPLICABLE"
                    else STATUS_LABELS["UNAVAILABLE"])
            summary.append(PanelItem(label=_label(point.kind.value), statement=text,
                                     detail=point.reason))
            continue
        for claim_id in point.claim_ids:
            claim = claims.get(claim_id)
            summary.append(PanelItem(
                label=_label(point.kind.value), statement=statement(claim_id),
                detail=claim.claim_type.value if claim is not None else None))

    conflicts = [PanelItem(label=e.code, statement=statement(e.claim_id),
                           detail="Input conflict - a data limitation, not a company risk")
                 for e in report.risks.risk_register if e.reason == "INPUT_CONFLICT"]
    conflicts += [PanelItem(label=f"SOURCE_DISAGREEMENT · {d.topic}", statement=statement(
        d.claim_id), detail=d.status) for d in report.source_disagreements
        if d.status == "OPEN"]
    limitations = [PanelItem(label=entry.kind, statement=statement(entry.claim_id),
                             detail=entry.layer) for entry in report.risks.data_limitations]
    diligence = [PanelItem(label=entry.importance, statement=statement(entry.claim_id),
                           detail=entry.needed_source) for entry in report.risks.open_diligence]
    cov = report.risks.coverage
    counts: tuple[tuple[str, int], ...] = ()
    clear: list[PanelItem] = []
    unevaluated: list[PanelItem] = []
    if cov is not None:
        counts = (("EVALUATED", cov.evaluated), ("APPLICABLE", cov.applicable),
                  ("GREEN", cov.green), ("YELLOW", cov.yellow), ("RED", cov.red),
                  ("NOT_EVALUATED", cov.not_evaluated), ("NOT_APPLICABLE", cov.not_applicable))
        clear = [PanelItem(label=r.measured or r.title, severity="GREEN",
                           statement="Evaluated - no adverse evidence found; risk tested: "
                           + r.title.lower(), detail=f"{_label(r.category)} · {r.code}")
                 for r in cov.rules if r.outcome == "GREEN"]
        unevaluated = [PanelItem(label=r.title, statement="Not evaluated - "
                                 + (r.reason_text or _label(r.reason or "")),
                                 detail=f"{_label(r.category)} · {r.reason}")
                       for r in cov.rules if r.outcome == "NOT_EVALUATED"]
    m = report.metadata
    return FinalReportPanel(
        artifact_id=manifest.artifact_id, report_id=report.report_id,
        company_name=m.company_name, nse_symbol=m.nse_symbol, isin=m.isin,
        screener_page=manifest.screener_page, as_of=str(m.as_of), status=manifest.status,
        formats=tuple(f.format.value for f in manifest.files),
        failed_formats=tuple(f.format.value for f in manifest.failures),
        disclaimer=DISCLAIMER, summary=_list(summary),
        sections=tuple(_panel_section(s, report.research_completeness)
                       for s in report.sections),
        red=_list(risk_items(report.risks.red)), yellow=_list(risk_items(report.risks.yellow)),
        positive_controls=_list(risk_items(report.risks.positive_controls)),
        input_conflicts=_list(conflicts), data_limitations=_list(limitations),
        open_diligence=_list(diligence), analytical_basis=m.analytical_basis.value,
        control_basis=m.control_basis.value if m.control_basis else None,
        business_class=m.business_class, industry=m.industry.value,
        research_status=rc.overall.value if (rc := report.research_completeness) else None,
        research_completeness=tuple(summary_lines(rc)) if rc else (),
        basis_note=basis_note(m.basis_selection),
        risk_coverage=cov.headline if cov else None, risk_counts=counts,
        evaluated_clear=_list(clear), not_evaluated=_list(unevaluated))


__all__ = ["FinalReportPanel", "PanelItem", "PanelList", "PanelSection", "build_panel"]
