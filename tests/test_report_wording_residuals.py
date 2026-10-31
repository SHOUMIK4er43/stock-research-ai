"""V1 RC1 (live TECHM, F17 follow-up): the wording F17 missed.

Re-rendered TECHM output still printed 'March 31 2026 As at As at March 31 2026' for a
source disagreement (the filing's garbled header label), diligence questions 'in As at
2025-03-31? | 51.506173 % difference' and shareholding changes '-0.010000 percentage points
to 2026-09-30'. Each now goes through the shared period name / display policy; the
structured values keep every digit.
"""

import re
from datetime import date
from decimal import Decimal

from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.due_diligence import basis_question, shown


def test_a_balance_date_question_names_the_date_once():
    assert basis_question("GROSS_DEBT", "As at 2025-03-31", date(2025, 3, 31)) == (
        "Why does GROSS_DEBT differ between standalone and consolidated as at 31-Mar-2025?")
    assert basis_question("PAT", "FY2022", date(2022, 3, 31)) == (
        "Why does PAT differ between standalone and consolidated in FY2022?")


def test_a_percentage_in_reader_text_has_two_decimals():
    assert shown(Decimal("51.506173")) == "51.51"
    assert shown(None) == "unavailable"


def test_no_claim_repeats_a_period_word_or_prints_six_decimal_points():
    report = run_layers([(SCREENER, INFY)])["report"]
    for c in report.claims:
        assert not re.search(r"(?i)\bas at as at\b|\bin as at\b", c.statement), c.statement
        assert not re.search(r"\d\.\d{6} percentage points", c.statement), c.statement
        assert not re.search(r"INR (?:CRORES|MONEY)|observed -?\d+\.\d{6}", c.statement), (
            c.statement)


def test_an_exact_value_drops_only_trailing_zeros():
    from stock_research_ai.domain.report_rendering import display_exact, display_value

    assert display_value(Decimal("0.000000"), "PERCENT", exact=True) == "0%"
    assert display_exact(Decimal("-0.010000")) == "-0.01"
    assert display_exact(Decimal("20.481624")) == "20.481624"
    assert display_exact(Decimal("292572.313182919")) == "292,572.313182…"


def test_a_cell_shows_its_own_claims_unit():
    from stock_research_ai.domain.report_rendering import cell_number

    assert cell_number(Decimal("-0.01"), "PERCENTAGE_POINTS") == "-0.01 pp"
    assert cell_number(Decimal("-0.01"), "PERCENT") == "-0.01%"
