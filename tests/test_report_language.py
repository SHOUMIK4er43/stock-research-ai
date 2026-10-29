"""V1 RC1 (Tech Mahindra): readable finance notation and honest rule wording.

TECHM's report printed '8792.1 inr money', '15.898858 percent', 'AUDITOR_SIGNAL = 1.000000
code', '(INR MONEY)' row labels and 'TOTAL_EQUITY ... for As at 2026-03-31'; a clean rule read
'Weak interest cover: 20.481624 TIMES - GREEN' as if the weakness existed, and 'Negative net
worth' sat next to a positive ₹29,616 Cr.
"""

import re
from decimal import Decimal

import pytest
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.report_claims import amount
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.domain.report_rendering import unit_label


@pytest.fixture(scope="module")
def report():
    return run_layers([(SCREENER, INFY)])["report"]


def test_amounts_read_as_finance_notation():
    assert amount(Decimal("29615.8"), "INR MONEY") == "₹29,615.80 Cr"
    assert amount(Decimal("16.87"), "PERCENT") == "16.87%"
    assert amount(Decimal("20.481624"), "TIMES") == "20.48x"
    assert amount(Decimal("84"), "DAYS") == "84.00 days"
    assert amount(Decimal("1"), "CODE") == "1"
    assert amount(Decimal("1750"), "INR") == "₹1,750.00"
    assert [unit_label(u) for u in ("PERCENT", "INR MONEY", "TIMES")] == ["%", "₹ Cr", "x"]


def test_no_machine_units_or_six_decimal_noise_in_statements(report):
    md = render_markdown(build_presentation(report))
    for bad in ("inr money", "INR MONEY", "(PERCENT)", "as at As at", "for As at"):
        assert bad not in md, bad
    for c in report.claims:
        if c.claim_type.value in ("CALCULATION", "FACT", "SCENARIO"):
            assert not re.search(r"\d\.\d{6} (?:percent|times|inr|money|code)\b",
                                 c.statement, re.I), c.statement


def test_a_green_rule_names_what_was_measured_not_the_risk(report):
    for r in report.risks.coverage.rules:
        if r.outcome == "GREEN":
            assert r.measured and "=" in r.measured
            assert not r.measured.lower().startswith(r.title.lower())
    for c in report.claims:
        if "GREEN: no adverse evidence" in c.statement:
            measure = c.statement.split(" = ")[0]
            assert "Risk tested: " in c.statement
            # the rule's threshold is stated as a threshold, never folded into the measure
            assert not re.search(r"(?:<=|>=|<|>)\s*-?[\d.]+\s*$", measure), measure
