"""V1 RC1 (live HINDUNILVR): profit that includes discontinued operations says so.

Hindustan Unilever's FY2026 profit (₹15,059 Cr) includes a ₹4,407 Cr profit from discontinued
operations (the ice-cream demerger; continuing operations ₹10,652 Cr, flat). The report
presented a 41 % PAT increase and PAT CAGRs as earnings growth with no word of it. A fiscal
year for which a filing reports a non-zero discontinued-operations line now flags every
profit / EPS figure, and every calculation on one, for a period overlapping that year.
"""

from datetime import date
from uuid import uuid4

from test_financial_analytics import FY25, FY26, NOW, B, company, make, metric

from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_metrics import covers_discontinued
from stock_research_ai.domain.financial_canonical import (
    FinancialPeriod,
    NumberStatus,
    ParsedNumber,
    PeriodType,
)

FLAG = "DISCONTINUED_OPERATIONS_INCLUDED"
LABEL = "Profit/(Loss) for the year from Discontinued Operations"


def with_line(year_end: date = FY26, raw: str = "4,407", basis=B.CONSOLIDATED):
    data = make(company())
    template = next(o for o in data.observations if o.period.period_end == year_end
                    and o.period.period_type == PeriodType.FY)
    number = ParsedNumber(raw=raw, status=NumberStatus.NUMBER, value=4407) if raw != "-" else \
        ParsedNumber(raw=raw, status=NumberStatus.DASH, value=None)
    line = template.model_copy(update={
        "observation_id": uuid4(),
        "metric": None, "raw_label": LABEL, "raw_value": raw, "parsed": number,
        "normalized_value": None, "reporting_basis": basis})
    return build_financial_analytics(
        data.model_copy(update={"observations": (*data.observations, line)}), generated_at=NOW)


def test_profit_based_calculations_over_that_year_are_flagged():
    a = with_line()
    for code in ("PAT_GROWTH_YOY", "PAT_CAGR_3Y", "PAT_MARGIN"):
        assert FLAG in metric(a, code).warnings, code
    assert FLAG not in metric(a, "PAT_GROWTH_YOY", FY25).warnings  # FY2024 -> FY2025 only
    assert FLAG not in metric(a, "REVENUE_GROWTH_YOY").warnings  # not a profit figure


def test_a_nil_line_or_another_basis_is_not_discontinued_operations():
    for a in (with_line(raw="-"), with_line(basis=B.STANDALONE)):
        assert FLAG not in metric(a, "PAT_GROWTH_YOY").warnings


def test_overlap_with_the_flagged_fiscal_year():
    years = {FY26}

    def period(ptype: PeriodType, end: date, months: int) -> FinancialPeriod:
        return FinancialPeriod(period_type=ptype, label="x", period_end=end,
                               duration_months=months)

    assert covers_discontinued(period(PeriodType.TTM, date(2026, 9, 30), 12), years)
    assert covers_discontinued(period(PeriodType.QUARTER, date(2025, 12, 31), 3), years)
    assert not covers_discontinued(period(PeriodType.QUARTER, date(2026, 6, 30), 3), years)
    assert not covers_discontinued(period(PeriodType.FY, FY25, 12), years)


def test_an_undated_line_is_dated_only_by_its_own_document():
    """HUL's standalone P&L columns were not dated (ambiguous header): its discontinued line
    takes the fiscal years its own annual report's dated rows report, never a guess."""
    from stock_research_ai.application.financial_metrics import discontinued_years

    data = make(company())
    template = next(o for o in data.observations if o.period.period_type == PeriodType.FY)
    undated = FinancialPeriod(period_type=PeriodType.UNKNOWN, label="(column not dated)")
    line = template.model_copy(update={
        "observation_id": uuid4(), "metric": None, "raw_label": LABEL, "raw_value": "4,407",
        "parsed": ParsedNumber(raw="4,407", status=NumberStatus.NUMBER, value=4407),
        "normalized_value": None, "period": undated, "reporting_basis": B.STANDALONE})
    dated = {o.period.period_end for o in data.observations
             if o.period.period_type == PeriodType.FY}
    with_doc = data.model_copy(update={"observations": (*data.observations, line)})
    assert discontinued_years(with_doc, B.STANDALONE) == dated
    other_doc = line.model_copy(update={"source": line.source.model_copy(
        update={"document_id": uuid4()})})
    alone = data.model_copy(update={"observations": (*data.observations, other_doc)})
    assert discontinued_years(alone, B.STANDALONE) == set()  # nothing dates it: no flag
