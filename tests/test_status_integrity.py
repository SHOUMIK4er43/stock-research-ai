"""V1 RC1 (Tech Mahindra): one canonical status model, no contradictions.

The TECHM report showed: cover 'Forecast: Complete' over Section 0 'Box 10 PARTIAL' and
Section 14 'Partial'; 'Complete - every required input family available' beside '16 of 19
families'; Management & Governance, Events, Cash Flow and Forensics 'Complete' while their
material checks could not run. COMPLETE now means every material required input exists AND
every material check of that section ran; each gap is a named code on the section.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_business_classification_integrity import HUDCO, SCREENER, _panel, run_layers
from test_summary_input import INFY

from stock_research_ai.application.final_report_composer import _material_gaps
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.domain.final_research_report import (
    REQUIREMENT_SECTIONS,
    SectionId,
    SectionStatus,
)
from stock_research_ai.domain.report_rendering import STATUS_LABELS

S, ST = SectionId, SectionStatus


@pytest.fixture(scope="module", params=["INFY", "HUDCO"])
def report(request):
    pages = [(SCREENER, INFY)] if request.param == "INFY" else HUDCO
    return run_layers(pages)["report"]


def test_complete_means_no_missing_input_and_no_material_gap(report):
    for s in report.sections:
        assert "every required input family available" not in s.status_reason
        if s.status == ST.COMPLETE:
            assert not s.completeness.missing and not s.completeness.material_gaps
        if s.completeness.material_gaps:
            assert s.status != ST.COMPLETE


def test_sections_never_complete_over_their_requirement(report):
    by = {s.section_id: s for s in report.sections}
    for block in report.research_completeness.blocks:
        section = REQUIREMENT_SECTIONS.get(block.block)
        if section is not None and block.status in (ST.PARTIAL, ST.UNAVAILABLE):
            assert by[section].status != ST.COMPLETE, block.block


def test_unevaluated_rules_keep_their_sections_partial(report):
    cov = report.risks.coverage
    by = {s.section_id: s for s in report.sections}
    for rule in cov.rules:
        if rule.outcome == "NOT_EVALUATED" and rule.category == "GOVERNANCE":
            gov = by[S.MANAGEMENT_GOVERNANCE]
            assert gov.status != ST.COMPLETE
            assert f"RULE_NOT_EVALUATED_{rule.code}" in gov.completeness.material_gaps


def test_forecast_status_is_one_status_everywhere(report):
    """Cover, Section 0 ledger, Section 14 and the side panel agree on the forecast."""
    rc = report.research_completeness
    fc = next(b for b in rc.blocks if b.block == "FORECAST")
    sec14 = next(s for s in report.sections if s.section_id == S.FORECASTS)
    assert sec14.status == fc.status or (fc.status == ST.UNAVAILABLE
                                         and sec14.status == ST.PARTIAL)
    if report.due_diligence_status.forecast_status.endswith("PARTIAL"):
        assert fc.status != ST.COMPLETE and sec14.status != ST.COMPLETE
    facts = dict(build_presentation(report).header_facts)
    assert facts["Forecast"].startswith(STATUS_LABELS[fc.status.value])
    panel = _panel(report)
    assert any(STATUS_LABELS[fc.status.value].lower() in text.lower() or fc.title in text
               for _label, text in panel.research_completeness)


def test_section_zero_header_states_the_research_status(report):
    md = render_markdown(build_presentation(report))
    rc = report.research_completeness
    zero = md[md.index("## 0. Due-Diligence Status"):md.index("## 1. ")]
    assert f"**Research completeness:** {STATUS_LABELS[rc.overall.value]}" in zero
    assert "every required input family available" not in md


def test_events_without_full_announcement_coverage_are_never_complete():
    """A blocked / partial announcement source keeps CORPORATE_EVENTS from COMPLETE."""
    def item(**kw):
        base = dict(category=SimpleNamespace(value="MATERIAL_ANNOUNCEMENTS"),
                    status=SimpleNamespace(value="AVAILABLE"), required_sources_complete=True,
                    processing_succeeded=True, coverage_window_complete=True)
        return SimpleNamespace(**(base | kw))

    def gaps(*items):
        ctx = SimpleNamespace(box12=SimpleNamespace(coverage=SimpleNamespace(items=items)))
        return _material_gaps(ctx, SimpleNamespace(coverage=None), ()).get(
            S.CORPORATE_EVENTS, ())

    assert gaps(item()) == ()
    assert gaps(item(status=SimpleNamespace(value="SOURCE_UNAVAILABLE"))) == (
        "MATERIAL_ANNOUNCEMENTS_COVERAGE_INCOMPLETE",)
    assert gaps(item(coverage_window_complete=False)) == (
        "MATERIAL_ANNOUNCEMENTS_COVERAGE_INCOMPLETE",)
    assert gaps() == ("MATERIAL_ANNOUNCEMENTS_COVERAGE_INCOMPLETE",)  # never reported


def test_a_forged_complete_section_is_refused(report):
    gov = next(b for b in report.research_completeness.blocks if b.block == "GOVERNANCE")
    assert gov.status != ST.COMPLETE
    forged = tuple(s.model_copy(update={"status": ST.COMPLETE})
                   if s.section_id == S.MANAGEMENT_GOVERNANCE else s for s in report.sections)
    with pytest.raises(ValidationError, match="COMPLETE"):
        type(report).model_validate({**dict(report), "sections": forged})
