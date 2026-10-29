"""V1 RC1 (Tech Mahindra): risk coverage is explicit.

The Negatives view headlined '0 RED' while only 11 of 25 applicable company-risk rules ran;
14 were never evaluated (auditor report, pledge, related parties, board ... unavailable).
'No adverse evidence found' and 'not evaluated' are different conclusions. One canonical
coverage, computed once from the Box 13 register, now heads every surface.
"""

import pytest
from test_business_classification_integrity import SCREENER, _panel, run_layers
from test_summary_input import INFY

from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.domain.final_research_report import RiskKind, SummaryKind


@pytest.fixture(scope="module")
def report():
    return run_layers([(SCREENER, INFY)])["report"]


def test_coverage_counts_every_applicable_rule_once(report):
    cov = report.risks.coverage
    assert cov is not None
    company = [r for r in report.risks.risk_register if r.kind == RiskKind.COMPANY_RISK]
    assert len(cov.rules) == len(company)
    assert cov.applicable == cov.evaluated + cov.not_evaluated
    assert cov.applicable + cov.not_applicable == len(company)
    assert cov.evaluated == cov.green + cov.yellow + cov.red
    assert cov.not_evaluated > 0  # the fixture has no auditor report, pledge data ...
    for rule in cov.rules:
        if rule.outcome == "NOT_EVALUATED":
            assert rule.reason and rule.reason_text and rule.title


def test_no_clean_bill_of_health_from_unevaluated_rules(report):
    cov = report.risks.coverage
    assert cov.red == cov.yellow == 0
    assert f"{cov.evaluated} of {cov.applicable}" in cov.headline
    assert "not evaluated" in cov.headline and "not a finding of no risk" in cov.headline
    point = next(p for p in report.executive_summary.points if p.kind == SummaryKind.MAJOR_RISKS)
    assert point.reason == cov.headline


def test_every_surface_shows_the_same_coverage(report):
    cov = report.risks.coverage
    md = render_markdown(build_presentation(report))
    assert f"Risk rules evaluated: {cov.evaluated} / {cov.applicable}" in md
    panel = _panel(report)
    assert panel.risk_coverage == cov.headline
    assert dict(panel.risk_counts) == {"EVALUATED": cov.evaluated, "APPLICABLE": cov.applicable,
                                       "GREEN": cov.green, "YELLOW": cov.yellow, "RED": cov.red,
                                       "NOT_EVALUATED": cov.not_evaluated,
                                       "NOT_APPLICABLE": cov.not_applicable}
    assert panel.not_evaluated.total == cov.not_evaluated
    assert panel.evaluated_clear.total == cov.green


def test_coverage_heads_section_17_and_names_missing_evidence_in_words(report):
    md = render_markdown(build_presentation(report))
    section = md[md.index("Risk coverage — Risk rules evaluated"):]
    assert section.index("Rules NOT EVALUATED") < section.index("RED company risks")
    for rule in report.risks.coverage.rules:
        if rule.outcome == "NOT_EVALUATED":
            # a reason that was only a code is spelled out, never shown as the code alone
            assert rule.reason_text != rule.reason
            assert "_" not in rule.reason_text or " " in rule.reason_text
