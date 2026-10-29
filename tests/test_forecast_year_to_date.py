"""V1 RC1 (Tech Mahindra): a forecast respects the months already reported.

TECHM's FY27 forecast was grown from FY26 although H1 FY27 actuals were already reported. When
Box 4 holds quarters of the first forecast year (contiguous from its first quarter), that year
is now the reported year-to-date + a forecast of the remaining months only, with the BASE
remaining-month drivers taken from the year-to-date trajectory. BULL / BEAR historical extremes
are no longer held flat for the whole horizon.
"""

from datetime import date
from decimal import Decimal, localcontext

import pytest
from test_financial_analytics import V, make, quarter
from test_forecasting import GROWING, NOW, company, dec

from stock_research_ai.application.forecast_integrity import (
    ForecastIntegrityError,
    verify_forecast_integrity,
)
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.domain.financial_canonical import CanonicalMetric, Industry
from stock_research_ai.domain.forecasting import (
    DerivationMethod,
    Driver,
    ForecastLine,
    ForecastRequest,
    Scenario,
)

M, L, D, S = CanonicalMetric, ForecastLine, Driver, Scenario
P = 34
#: FY2026 (= GROWING[-1] = 1610.51) by quarter, and FY2027's reported quarters
PRIOR_Q = {date(2025, 6, 30): dec("380"), date(2025, 9, 30): dec("390"),
           date(2025, 12, 31): dec("410"), date(2026, 3, 31): dec("430.51")}
CURRENT_Q = {date(2026, 6, 30): dec("440"), date(2026, 9, 30): dec("462"),
             date(2026, 12, 31): dec("480")}
MARGIN_Q = dec(24)  # the reported months run at a higher margin than the 20 % history
HISTORY_MARGIN = dec(20)


def quarter_rows(revenues: dict[date, Decimal], margin: Decimal = HISTORY_MARGIN,
                 skip: frozenset[tuple[CanonicalMetric, date]] = frozenset()) -> list[V]:
    rows: list[V] = []
    for end, rev in revenues.items():
        op, oi, da, fc = rev * margin / 100, rev * 2 / 100, rev * 3 / 100, dec(20)
        pbt = op + oi - da - fc
        pat = pbt * 75 / 100  # no quarterly tax line: tax is implied as PBT - PAT
        for metric, value in ((M.REVENUE_FROM_OPERATIONS, rev), (M.OPERATING_PROFIT, op),
                              (M.OTHER_INCOME, oi), (M.DEPRECIATION_AMORTIZATION, da),
                              (M.FINANCE_COST, fc), (M.PBT, pbt), (M.PAT, pat)):
            if (metric, end) not in skip:
                rows.append(V(metric, quarter(end), value))
    return rows


def forecast(months: int, **kw: object):
    current = dict(list(CURRENT_Q.items())[: months // 3])
    rows = company(GROWING) + quarter_rows(PRIOR_Q) + quarter_rows(
        current, MARGIN_Q, kw.pop("skip", frozenset()))  # type: ignore[arg-type]
    data = make(rows, Industry.GENERAL, 3)
    return data, build_forecast(data, request=ForecastRequest.model_validate(kw),
                                generated_at=NOW)


def value(f, line, scenario=S.BASE, year=2027):
    v = f.value(line, scenario, year)
    assert v.value is not None, (line, scenario, year, v.reason, v.missing_inputs)
    return v.value


def test_no_reported_quarter_forecasts_the_full_year():
    _data, f = forecast(0)
    assert f.base.year_to_date is None and f.year_to_date_actuals == ()
    assert f.value(L.REVENUE, S.BASE, 2027).formula_id == "REVENUE/1"


@pytest.mark.parametrize("months", [3, 6, 9])
def test_the_first_year_is_reported_months_plus_the_remaining_months(months):
    data, f = forecast(months)
    ytd = f.base.year_to_date
    assert ytd is not None and (ytd.months, ytd.fiscal_year) == (months, 2027)
    assert ytd.period.label.startswith({3: "Q1", 6: "H1", 9: "9M"}[months])
    reported = list(CURRENT_Q.values())[: months // 3]
    same_months = list(PRIOR_Q.values())[: months // 3]
    ytd_revenue, prior = sum(reported), sum(same_months)
    growth = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    assert growth.method == DerivationMethod.YEAR_TO_DATE_TRAJECTORY
    with localcontext() as ctx:
        ctx.prec = P
        assert growth.value == round((ytd_revenue / prior - 1) * 100, 6)
        remainder = dec(GROWING[-1]) - prior
        full = ytd_revenue + remainder * (1 + growth.value / 100)
    assert value(f, L.REVENUE) == full
    # the reported months are never re-forecast: every P&L line reconciles to YTD + remainder
    by_line = {v.line: v for v in f.year_to_date_actuals if v.period == ytd.period}
    assert by_line[L.REVENUE].value == ytd_revenue
    with localcontext() as ctx:
        ctx.prec = P
        rem_revenue = value(f, L.REVENUE) - ytd_revenue
        assert rem_revenue > 0
        margin = f.assumption(D.OPERATING_MARGIN, S.BASE, 2027).value
        assert margin == round(MARGIN_Q, 6)  # the current trajectory, not the 20 % median
        assert value(f, L.OPERATING_PROFIT) == by_line[L.OPERATING_PROFIT].value + \
            rem_revenue * margin / 100
        rem_pbt = value(f, L.PBT) - by_line[L.PBT].value
        rem_tax = value(f, L.TAX) - by_line[L.TAX].value
        assert value(f, L.PAT) == by_line[L.PAT].value + rem_pbt - rem_tax
    # later years grow from that full year; the integrity verifier reproduces every number
    assert f.value(L.REVENUE, S.BASE, 2028).formula_id == "REVENUE/1"
    assert f.value(L.REVENUE, S.BASE, 2028).inputs[0].value == value(f, L.REVENUE)
    verify_forecast_integrity(f, data)
    verify_forecast_integrity(f)


def test_scenarios_keep_their_order_around_the_trajectory():
    _data, f = forecast(6)
    for driver in (D.REVENUE_GROWTH, D.OPERATING_MARGIN):
        bear, base, bull = (f.assumption(driver, s, 2027).value
                            for s in (S.BEAR, S.BASE, S.BULL))
        assert bear <= base <= bull, driver


def test_incomplete_reported_months_are_flagged_not_half_used():
    _data, f = forecast(6, skip=frozenset({(M.OTHER_INCOME, date(2026, 9, 30))}))
    assert f.base.year_to_date is None
    assert any(i.code == "YTD_ACTUALS_INCOMPLETE" for i in f.issues)


def test_a_quarter_without_its_predecessors_is_not_a_year_to_date():
    rows = company(GROWING) + quarter_rows(PRIOR_Q) + quarter_rows(
        {date(2026, 9, 30): CURRENT_Q[date(2026, 9, 30)]})
    f = build_forecast(make(rows, Industry.GENERAL, 3), generated_at=NOW)
    assert f.base.year_to_date is None


def test_a_historical_extreme_fades_toward_base():
    varied = [1000, 1050, 1260, 1323, "1455.3", "1610.51"]  # +5 / +20 / +5 / +10 / ~+10.7 %
    f = build_forecast(make(company(varied), Industry.GENERAL, 3), generated_at=NOW)
    bull = [f.assumption(D.REVENUE_GROWTH, S.BULL, y) for y in (2027, 2028, 2029)]
    base = f.assumption(D.REVENUE_GROWTH, S.BASE, 2028).value
    assert bull[0].method == DerivationMethod.HISTORICAL_MAX_YOY
    assert bull[1].method == bull[2].method == DerivationMethod.HISTORICAL_FADE_TO_BASE
    assert bull[0].value > bull[1].value > bull[2].value > base
    with localcontext() as ctx:
        ctx.prec = P
        assert bull[1].value == round(bull[0].value + (base - bull[0].value) / 3, 6)


def test_a_forged_year_to_date_anchor_is_refused():
    data, f = forecast(6)
    forged = tuple(v.model_copy(update={"value": v.value + 1}) if v.line == L.PAT else v
                   for v in f.year_to_date_actuals)
    with pytest.raises(ForecastIntegrityError):
        verify_forecast_integrity(f.model_copy(update={"year_to_date_actuals": forged}), data)


def test_the_trajectory_fades_toward_history_instead_of_snapping_back():
    _data, f = forecast(6)
    margins = [f.assumption(D.OPERATING_MARGIN, S.BASE, y) for y in (2027, 2028, 2029)]
    assert margins[0].method == DerivationMethod.YEAR_TO_DATE_TRAJECTORY
    assert margins[0].value == round(MARGIN_Q, 6)
    historical = dec(20)  # the window's median margin
    assert margins[1].method == DerivationMethod.HISTORICAL_FADE_TO_BASE
    assert "FROM_YEAR_TO_DATE" in margins[1].warnings
    with localcontext() as ctx:
        ctx.prec = P
        step = (historical - MARGIN_Q) / 3
        assert margins[1].value == round(MARGIN_Q + step, 6)
        assert margins[2].value == round(MARGIN_Q + 2 * step, 6)
    # the finance-cost ratio is only assumed when the projection uses it
    assert not any(a.driver == D.FINANCE_COST_TO_REVENUE for a in f.assumptions)
