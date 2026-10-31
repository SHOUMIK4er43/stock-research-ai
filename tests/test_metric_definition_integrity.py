"""V1 RC1 (Tech Mahindra): every margin names its definition; guidance is never compared
with a differently defined measure.

TECHM guides to an EBIT margin of 15 % (Q2 FY27 reported 14.4 %). The report's 'Operating
margin' (Screener operating profit / revenue: before D&A and other income) read 17.8 %; Box 5
'EBIT' was PBT + finance cost (with other income and exceptional items); and PBT-based EBITDA
fell below Screener operating profit with no explanation, because Screener's other income nets
an exceptional loss. Finance cost (lease interest included) was also divisible by filed
borrowings that exclude leases.
"""

from decimal import Decimal

from test_financial_analytics import FY26, V, analyse, fy, metric, pit
from test_forecasting import GROWING, NOW, company

from stock_research_ai.application.financial_formulas import FORMULAS
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    Industry,
    Outcome,
    SourceType,
)
from stock_research_ai.domain.forecasting import Driver

M = CanonicalMetric


def rows(other_income: str = "-24", source: SourceType = SourceType.FILING) -> list[V]:
    """One fiscal year: operating profit 903 on revenue 5,681; D&A 188; finance cost 30."""
    p = fy(2026)
    values = {M.REVENUE_FROM_OPERATIONS: "5681", M.OPERATING_PROFIT: "903",
              M.OTHER_INCOME: other_income, M.DEPRECIATION_AMORTIZATION: "188",
              M.FINANCE_COST: "30"}
    out = [V(m, p, v, source=source) for m, v in values.items()]
    pbt = Decimal("903") + Decimal(other_income) - 188 - 30
    out.append(V(M.PBT, p, pbt, source=source))
    out.append(V(M.PAT, p, pbt * Decimal("0.75"), source=source))
    return out


def test_each_margin_names_its_definition():
    assert "before D&A and other income" in FORMULAS["OPERATING_MARGIN"].name
    assert "other income and exceptional items" in FORMULAS["EBIT_MARGIN"].name
    assert "other income" in FORMULAS["EBITDA_MARGIN"].name
    assert "excludes other income" in FORMULAS["OPERATING_EBIT_MARGIN"].name


def test_operating_ebit_margin_excludes_other_income():
    a = analyse(rows())
    got = metric(a, "OPERATING_EBIT_MARGIN", FY26).value
    assert got is not None
    assert abs(got - (Decimal(903 - 188) * 100 / 5681)) < Decimal("0.000001")
    # the PBT-based EBIT includes the (negative) other income: a different number
    assert metric(a, "EBIT_MARGIN", FY26).value != got


def test_pbt_based_ebitda_below_operating_profit_is_flagged():
    below = metric(analyse(rows("-24")), "EBITDA", FY26)
    assert below.value is not None and below.value < 903
    assert "EBITDA_BELOW_OPERATING_PROFIT" in below.warnings
    above = metric(analyse(rows("50")), "EBITDA", FY26)
    assert "EBITDA_BELOW_OPERATING_PROFIT" not in above.warnings


def _with_leases(source: SourceType) -> list[V]:
    out = [v if v.metric != M.TOTAL_BORROWINGS else V(
        v.metric, v.period, v.value, source=source, outcome=Outcome.SINGLE_SOURCE)
        for v in company(GROWING)]
    return out + [V(M.LEASE_LIABILITIES, pit(y), "400", source=source)
                  for y in range(2021, 2027)]


def test_a_rate_on_debt_never_divides_lease_interest_by_lease_free_debt():
    from test_financial_analytics import make
    filed = build_forecast(make(_with_leases(SourceType.FILING), Industry.GENERAL, 3),
                           generated_at=NOW)
    drivers = {a.driver for a in filed.assumptions}
    assert Driver.INTEREST_RATE_ON_DEBT not in drivers
    assert Driver.FINANCE_COST_TO_REVENUE in drivers
    # without reported leases the rate on debt is used as before
    plain = build_forecast(make(company(GROWING), Industry.GENERAL, 3), generated_at=NOW)
    assert Driver.INTEREST_RATE_ON_DEBT in {a.driver for a in plain.assumptions}


def test_the_report_shows_the_operating_ebit_margin_and_explains_low_ebitda():
    from test_business_classification_integrity import SCREENER, run_layers
    from test_summary_input import INFY

    report = run_layers([(SCREENER, INFY)])["report"]
    hist = report.table("ANNUAL_FINANCIALS")
    labels = [r.label for r in hist.rows]
    assert any(label.startswith("Operating EBIT margin") for label in labels)
    assert any("before D&A and other income" in label for label in labels)
    for c in report.claims:
        if "below operating profit" in c.statement:
            assert c.topic == "EBITDA"
