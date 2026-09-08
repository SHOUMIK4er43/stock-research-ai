from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_research_ai.adapters.screener.values import parse_period, parse_value
from stock_research_ai.domain.financial import FinancialPeriod, PeriodFrequency


@pytest.mark.parametrize(
    "text,value,status",
    [
        ("0", "0", "available"),
        ("  \n ", None, "blank"),
        ("--", None, "not_reported"),
        ("not applicable", None, "not_applicable"),
        ("N/A", None, "parse_error"),
        ("unavailable", None, "unavailable"),
        ("oops", None, "parse_error"),
        ("−1,234.50", "-1234.50", "available"),
        ("(123)", "-123", "available"),
        ("48,88,441", "4888441", "available"),
        ("1,234,567", "1234567", "available"),
        ("1,2,34", None, "parse_error"),
        ("NaN", None, "parse_error"),
        ("Infinity", None, "parse_error"),
        ("1.25 Cr.", "1.25", "available"),
    ],
)
def test_independent_numeric_expectations(text, value, status):
    parsed = parse_value(text)
    assert parsed.value == (Decimal(value) if value is not None else None)
    assert parsed.status == status
    assert parsed.original == text


def test_units_and_scales_are_only_explicit():
    money = parse_value("₹ 1.25 Cr.")
    assert money.value == Decimal("1.25")
    assert money.unit.currency == "INR" and money.unit.scale == Decimal("10000000")
    assert parse_value("23.39%").unit.kind == "percent"
    assert parse_value("123").unit.kind == "unknown"
    assert parse_value("1.25 Cr.").unit.currency is None


@pytest.mark.parametrize(
    "frequency,instant,label,key,expected_resolution",
    [
        ("annual", False, "Mar 2026", "2026-03-31", "partial"),
        ("quarterly", False, "Jun 2026", "2026-06-30", "partial"),
        ("point_in_time", True, "Mar 2026", "2026-03-31", "exact"),
        ("point_in_time", True, "Jun 2026", None, "partial"),
        ("annual", False, "TTM", None, "unresolved"),
        ("annual", False, "Something new", "not-a-date", "unresolved"),
    ],
)
def test_period_certainty(frequency, instant, label, key, expected_resolution):
    period = parse_period(label, key, PeriodFrequency(frequency), instant)
    assert period.date_resolution == expected_resolution
    assert period.start_date is None
    assert period.fiscal_year is None and period.fiscal_quarter is None
    assert (
        period.end_date is None
        if not key or key == "not-a-date"
        else period.end_date.isoformat() == key
    )
    assert period.kind == ("instant" if instant else "duration")
    if label == "TTM":
        assert period.frequency == "ttm"


def test_derived_dates_require_rule_and_inputs():
    with pytest.raises(ValidationError, match="rule and inputs"):
        FinancialPeriod(
            kind="instant",
            fiscal_label="Mar 2026",
            date_resolution="derived",
            end_date="2026-03-31",
        )
