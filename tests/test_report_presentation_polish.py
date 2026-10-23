"""V1 release polish: presentation regressions found by rendering real reports (2026-10-23).

Every assertion is presentation-only: no claim value, status or ledger entry changes.

  header       reader-first facts (basis, classification, research / forecast / valuation)
  section 0    badge = research completeness, never 'Complete' over PARTIAL research
  summary      a point holding only a data limitation is not 'AVAILABLE'; a lender's forecast
               range uses its PAT forecast instead of claiming 'no Box 10 forecast'
  valuation    an unavailable valuation leads with its readable reason
  precision    statements and exact values carry at most six decimals (the value keeps all)
  tables       a sector-gated metric is 'Not applicable' in every year (not 'not reported');
               shared column tags and one-reason rows are stated once
  basis        a stale-consolidated fallback is explained (forensic finding F-03)
"""

import re
from decimal import Decimal

import pytest
from test_business_classification_integrity import HUDCO, run_layers
from test_reporting_basis_integrity import TFCI_PAGES, TFCI_STANDALONE, _detailed

from stock_research_ai.application.report_html import render_html
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_panel import build_panel
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.domain import report_rendering as rr
from stock_research_ai.domain.report_rendering import ReportManifest, display_exact


@pytest.fixture(scope="module")
def hudco():
    report = run_layers(HUDCO)["report"]
    return report, build_presentation(report)


def _panel(report):
    return build_panel(report, ReportManifest.model_construct(
        artifact_id=report.report_id, screener_page="HUDCO", status="COMPLETE", files=(),
        failures=()))


def test_header_leads_with_what_a_reader_needs(hudco):
    _report, doc = hudco
    keys = [k for k, _v in doc.header_facts]
    assert keys[:8] == ["As of", "Latest fiscal year end", "Analytical basis", "Control basis",
                        "Business classification", "Research completeness", "Forecast",
                        "Valuation"]
    facts = dict(doc.header_facts)
    assert facts["Valuation"] == ("Data unavailable — no valuation multiple was supplied (none "
                                  "is assumed) (MULTIPLE_MISSING)")
    assert facts["Research completeness"].startswith("Partial")
    assert keys.index("Box 14 SHA-256") > keys.index("Valuation")  # technical facts last


def test_section_zero_badge_is_the_research_conclusion(hudco):
    report, doc = hudco
    zero = doc.sections[0]
    assert (zero.status, zero.status_label) == ("PARTIAL", "Research Partial")
    assert report.sections[0].status.value == "COMPLETE"  # the dataset is unchanged
    panel_zero = _panel(report).sections[0]
    assert (panel_zero.status, panel_zero.status_label) == ("PARTIAL", "Research Partial")
    html = render_html(doc)
    assert '<span class="tag st-PARTIAL">Research Partial</span>' in html
    assert ".st-UNAVAILABLE{color:var(--lim)" in html  # never green for unavailable


def test_summary_points_say_what_they_hold(hudco):
    _report, doc = hudco
    md = render_markdown(doc)
    assert "### Management guidance — Data unavailable" in md  # only a data limitation
    assert "— AVAILABLE" not in md
    assert "no Box 10 forecast" not in md  # the lender HAS a Box 10 forecast
    assert re.search(r"Forecast range\n\n- \*\*Scenario · BEAR[^\n]*forecast pat", md)


def test_unavailable_valuation_leads_with_its_reason(hudco):
    _report, doc = hudco
    valuation = next(s for s in doc.sections if s.section_id == "VALUATION")
    lead = next(b for b in valuation.blocks if isinstance(b, rr.Paragraph)
                and b.label == "Valuation availability")
    assert lead.text.startswith("Valuation: Data unavailable. no valuation multiple was "
                                "supplied (none is assumed) (MULTIPLE_MISSING).")


def test_precision_is_bounded_for_readers_only(hudco):
    report, doc = hudco
    long = [c for c in report.claims if c.value is not None
            and -c.value.as_tuple().exponent > 6]  # type: ignore[operator]
    assert long  # Box 10 arithmetic carries more digits ...
    assert all(not re.search(r"\d\.\d{7,}", c.statement) for c in report.claims)  # ... text not
    assert display_exact(Decimal("292572.313182919813034539087232")) == "292,572.313182…"
    assert display_exact(Decimal("-0.938861")) == "-0.938861"
    assert not re.search(r"\d\.\d{7,}", render_markdown(doc))


def test_sector_gated_rows_are_not_applicable_never_not_reported(hudco):
    report, doc = hudco
    for code in ("ANNUAL_FINANCIALS", "ANNUAL_FINANCIALS_FULL"):
        table = report.table(code)
        keys = {r.key for r in table.rows}
        # a lender has no EBITDA / operating margin / capex line in its history
        assert not keys & {"EBITDA", "OPERATING_MARGIN", "CAPEX", "FCF", "OPERATING_PROFIT"}
    annual = next(b for s in doc.sections for b in s.blocks
                  if isinstance(b, rr.Table) and b.title.startswith("Annual financials"))
    assert "Columns: HISTORICAL" in annual.tags
    assert all(not c.tags for c in annual.columns[1:])  # stated once, not per column


def test_one_reason_rows_state_it_once(hudco):
    _report, doc = hudco
    tables = [b for s in doc.sections for b in s.blocks if isinstance(b, rr.Table)]
    for t in tables:
        for row in t.rows:
            gaps = [c for c in row.cells if c.kind in ("UNAVAILABLE", "NOT_APPLICABLE")]
            if " where shown (" in row.label:
                assert all(c.note is None for c in gaps)
                assert all(c.text in ("Data unavailable", "Not applicable") for c in gaps)


async def test_stale_consolidated_fallback_is_explained(tmp_path, hudco):
    report, panel = await _detailed(tmp_path, TFCI_STANDALONE[1], TFCI_PAGES)
    note = ("Standalone selected because the consolidated statements are older (consolidated "
            "FY ends 2022-03-31, standalone 2026-03-31).")
    assert dict(build_presentation(report).header_facts)["Basis selection"] == note
    assert _panel(report).basis_note == note
    hudco_report = hudco[0]  # the default consolidated choice gets no note
    assert "Basis selection" not in dict(build_presentation(hudco_report).header_facts)
    assert _panel(hudco_report).basis_note is None
