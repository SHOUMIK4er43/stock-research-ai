"""Equity-definition integrity: every metric declares which equity claim it uses.

  SHAREHOLDERS_EQUITY (Box 5)  ACCOUNTING equity attributable to owners: share capital +
                               reserves (a provider aggregate that already contains hybrids), or
                               capital + separately reported hybrid + other equity. Net worth,
                               leverage, capital and ROE use it.
  COMMON_EQUITY (Box 5)        ORDINARY shareholders' equity = SHAREHOLDERS_EQUITY - equity-
                               classified hybrid instruments (Box 4 HYBRID_EQUITY_INSTRUMENTS,
                               e.g. HUDCO's 'Instruments entirely equity in nature', perpetual
                               debt senior to the equity shares). Box 10's lender equity line
                               and Box 11 P/B use it.
Hybrid = 0 only on a filing nil at that date or when no source reports any hybrid for the
company; otherwise an ordinary-equity figure at a date without a hybrid amount is unavailable.
"""

from dataclasses import replace as dc_replace
from datetime import date
from decimal import Decimal as D

import pytest
from test_financial_analytics import B, M, V, company, fy, make, metric, pit
from test_financial_company_analytics import _company as company_layers
from test_financial_company_analytics import hudco as hudco_layers  # noqa: F401 (fixture)
from test_financial_forecasting import (
    hudco,  # noqa: F401 (fixture)
    lender_rows,
)
from test_summary_input import INFY, YESBANK

from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_formulas import FORMULAS
from stock_research_ai.application.forecast_integrity import verify_forecast_integrity
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.fundamental_valuation import build_valuation
from stock_research_ai.domain.financial_analytics import MetricCategory
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Industry,
    NumberStatus,
    ParsedNumber,
    SourceType,
)
from stock_research_ai.domain.forecasting import Driver, ForecastLine, Scenario
from stock_research_ai.domain.fundamental_valuation import (
    AssumptionInput,
    MultipleInput,
    ValuationMethod,
    ValuationRequest,
)

L, VM = ForecastLine, ValuationMethod
FY25, FY26 = date(2025, 3, 31), date(2026, 3, 31)
SHARES = D(100_000_000)
HYBRID = 200  # an equity-classified perpetual instrument in the synthetic company


def hybrid_rows(years=range(2021, 2027), amount=HYBRID, basis=B.CONSOLIDATED, **kw):
    return [V(M.HYBRID_EQUITY_INSTRUMENTS, pit(y), amount, basis, **kw) for y in years]


def with_nil(data, year, basis=B.CONSOLIDATED):
    """Append a filing '-' for the hybrid line at `year` (no canonical value)."""
    template = data.observations[0]
    nil = template.model_copy(update={
        "observation_id": template.observation_id.__class__(int=year),
        "metric": M.HYBRID_EQUITY_INSTRUMENTS, "raw_value": "-",
        "parsed": ParsedNumber(raw="-", status=NumberStatus.DASH), "normalized_value": None,
        "period": pit(year), "reporting_basis": basis})
    return data.model_copy(update={"observations": (*data.observations, nil)})


def screener(rows):
    return [dc_replace(r, source=SourceType.SCREENER) for r in rows]


def box5(data):
    from test_financial_analytics import NOW
    return build_financial_analytics(data, generated_at=NOW)


def bal(a, code, end=FY26, basis=B.CONSOLIDATED):
    from stock_research_ai.domain.financial_canonical import PeriodType
    return metric(a, code, end, basis, PeriodType.POINT_IN_TIME)


def forecast(data):
    from test_financial_valuation import NOW
    return build_forecast(data, generated_at=NOW)


def valuation(data, f, *multiples):
    from test_financial_valuation import NOW
    req = ValuationRequest(multiples=multiples, assumptions=(AssumptionInput(
        kind="SHARE_COUNT", value=SHARES, unit="COUNT", scale="ONES"),))
    return build_valuation(f, request=req, box4=data, generated_at=NOW)


def pb(scenario=Scenario.BASE, value="1.5", method=VM.PB):
    return MultipleInput(method=method, scenario=scenario, fiscal_year=2027,
                         multiple=D(value), reference="test request assumption")


def anchor(f, line=L.COMMON_EQUITY):
    return next(v for v in f.actuals if v.line == line)


def line(f, line, scenario=Scenario.BASE, year=2027):
    return next(v for v in f.values if v.line == line and v.scenario == scenario
                and v.period.fiscal_year == year)


def result(v, method, scenario=Scenario.BASE):
    return next(r for r in v.results if r.method == method and r.scenario == scenario)


# --- A / B: the two claims ------------------------------------------------------------------


def test_a_ordinary_equity_only_total_equals_common():
    a = box5(make(company()))
    for end in (FY25, FY26):
        common, total = bal(a, "COMMON_EQUITY", end), bal(a, "SHAREHOLDERS_EQUITY", end)
        assert common.status.value == "CALCULATED" and common.value == total.value
        # the filing balance sheet presents equity with no hybrid line: declared, not silent
        assert "NO_HYBRID_LINE_REPORTED" in common.warnings
    provider = box5(make(screener(company())))  # a provider's reserves cannot show hybrids
    common = bal(provider, "COMMON_EQUITY")
    assert common.value is None and any("provider" in n for n in common.missing_inputs)
    assert bal(provider, "SHAREHOLDERS_EQUITY").value == 1500


def test_b_ordinary_plus_hybrid_stay_separate():
    # a provider 'Reserves' line contains the hybrid; the filing reports it on its own line
    a = box5(make(company() + hybrid_rows()))
    total, common = bal(a, "SHAREHOLDERS_EQUITY"), bal(a, "COMMON_EQUITY")
    assert total.value == 100 + 1400  # capital + reserves (accounting total, hybrid included)
    assert common.value == total.value - HYBRID
    assert {i.metric for i in common.provenance} >= {M.HYBRID_EQUITY_INSTRUMENTS}
    # filing components: capital + hybrid + other equity, other equity never contains it
    rows = [r if r.metric != M.RESERVES else V(M.OTHER_EQUITY, r.period, r.value - HYBRID)
            for r in company()] + hybrid_rows()
    a = box5(make(rows))
    total, common = bal(a, "SHAREHOLDERS_EQUITY"), bal(a, "COMMON_EQUITY")
    assert total.formula_id.endswith("/CAPITAL_PLUS_HYBRID_PLUS_OTHER_EQUITY")
    assert (total.value, common.value) == (1500, 1300)


# --- C / G / L: P/B and book value per share price the ordinary claim ------------------------


def test_c_g_l_pb_prices_box10_ordinary_equity_never_the_hybrid():
    data = make(lender_rows() + hybrid_rows(), Industry.NBFC)
    f = forecast(data)
    a = anchor(f)
    assert a.formula_id == "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES_LESS_HYBRID"
    assert a.value == 100 + 1400 - HYBRID
    v = valuation(data, f, pb())
    r = result(v, VM.PB)
    common = line(f, L.COMMON_EQUITY)
    assert r.status.value == "COMPLETE" and r.multiple.metric == "COMMON_EQUITY"
    assert r.multiple.metric_value_id == common.value_id  # L: Box 10's own value
    assert r.equity_value == common.value * D("1.5")
    # G: per share = P/B x ordinary book value per share (crore -> ones over the share count)
    assert r.fair_value_per_share == common.value * D("1.5") * 10**7 / SHARES
    with_hybrid = make(lender_rows(), Industry.NBFC)  # same company, no hybrid at all
    assert result(valuation(with_hybrid, forecast(with_hybrid), pb()), VM.PB).equity_value \
        > r.equity_value  # the senior claim is never priced as ordinary equity


# --- D / J / R: ROE pairs the numerator with the equity claim -------------------------------


def test_d_r_roe_keeps_the_accounting_claim_and_is_unchanged_by_the_split():
    plain, split = box5(make(company())), box5(make(company() + hybrid_rows()))
    assert metric(split, "ROE").value == metric(plain, "ROE").value  # PAT / avg total equity
    assert "PAT_ATTRIBUTABLE_TO_OWNERS" in FORMULAS["ROE"].optional_inputs


def test_d_j_consolidated_nci_never_pairs_total_pat_with_owners_equity():
    nci = company() + [V(M.MINORITY_INTEREST, fy(y), 15) for y in range(2021, 2027)]
    roe = metric(box5(make(nci)), "ROE")
    assert roe.status.value == "INPUT_MISSING" and roe.value is None
    assert any("non-controlling" in n for n in roe.missing_inputs)
    owners = nci + [V(M.PAT_ATTRIBUTABLE_TO_OWNERS, fy(y), 100) for y in range(2021, 2027)]
    roe = metric(box5(make(owners)), "ROE")
    assert roe.status.value == "CALCULATED"
    assert {i.metric for i in roe.provenance if i.role == "pat"} == {
        M.PAT_ATTRIBUTABLE_TO_OWNERS}
    # total equity as reported (may carry NCI) pairs with total PAT, and is never ordinary
    total = [r for r in nci if r.metric not in (M.EQUITY_SHARE_CAPITAL, M.RESERVES)] + [
        V(M.TOTAL_EQUITY, pit(y), 1100 + 100 * (y - 2021)) for y in range(2021, 2027)]
    a = box5(make(total))
    roe = metric(a, "ROE")
    assert roe.status.value == "CALCULATED"
    assert {i.metric for i in roe.provenance if i.role == "pat"} == {M.PAT}
    assert bal(a, "COMMON_EQUITY").value is None


# --- E / F: leverage keeps total accounting equity ------------------------------------------


def test_e_f_leverage_uses_total_accounting_equity_only():
    data = make(lender_rows() + hybrid_rows(), Industry.NBFC)
    a = box5(data)
    total = bal(a, "SHAREHOLDERS_EQUITY")
    lev = metric(a, "BORROWINGS_TO_EQUITY")
    assert lev.value == (D(300) / total.value).quantize(D("0.000001"))
    for code, f in FORMULAS.items():  # F: no leverage / capital ratio uses ordinary equity
        if f.category in (MetricCategory.LEVERAGE, MetricCategory.RETURNS):
            assert not any("COMMON_EQUITY" in i for i in f.required_inputs), code


# --- H / K / M / N: fail closed; no unsupported hybrid growth -------------------------------


def test_h_missing_hybrid_decomposition_fails_closed():
    # provider history; the filing's hybrid line only at FY2026: earlier dates are unknown
    rows = screener(company()) + hybrid_rows(years=[2026])
    a = box5(make(rows))
    assert bal(a, "COMMON_EQUITY", FY26).value == 1500 - HYBRID
    early = bal(a, "COMMON_EQUITY", FY25)
    assert early.value is None and early.status.value == "INPUT_MISSING"
    assert any("hybrid" in n for n in early.missing_inputs)
    data = make(screener(lender_rows()) + hybrid_rows(years=[2026]), Industry.NBFC)
    f = forecast(data)
    assert line(f, L.COMMON_EQUITY).value is None  # no ordinary-equity history: no forecast
    assert line(f, L.TOTAL_BORROWINGS).value is None  # nor the funding chain built on it
    r = result(valuation(data, f, pb()), VM.PB)
    assert r.equity_value is None and r.status.value == "UNAVAILABLE"


def test_k_box10_grows_ordinary_equity_only_and_a_filing_nil_is_evidence():
    # a 200 hybrid issued in FY2026 (filing nil at FY2025): the provider's FY2026 reserves rise
    # by it, accounting equity jumps, ordinary equity does not
    issued = [V(r.metric, r.period, r.value + HYBRID) if r.metric == M.RESERVES
              and r.period.period_end == FY26 else r for r in lender_rows()]
    data = with_nil(make(issued + hybrid_rows(years=[2026]), Industry.NBFC), 2025)
    plain = make(lender_rows(), Industry.NBFC)  # the same company with no hybrid at all
    naive = make(issued, Industry.NBFC)  # the hybrid hidden inside reserves as if ordinary
    f, f_plain, f_naive = forecast(data), forecast(plain), forecast(naive)
    a = box5(data)
    assert "HYBRID_NIL_REPORTED" in bal(a, "COMMON_EQUITY", FY25).warnings
    total_change = bal(a, "SHAREHOLDERS_EQUITY", FY26).value - bal(
        a, "SHAREHOLDERS_EQUITY", FY25).value
    common_change = bal(a, "COMMON_EQUITY", FY26).value - bal(a, "COMMON_EQUITY", FY25).value
    assert total_change - common_change == HYBRID  # the issuance is never "retained earnings"

    def driver(x):
        return next(d for d in x.assumptions if d.driver == Driver.EQUITY_CHANGE_TO_PAT
                    and d.scenario == Scenario.BASE).value
    assert driver(f) == driver(f_plain) != driver(f_naive)
    assert anchor(f).value == anchor(f_plain).value
    assert line(f, L.COMMON_EQUITY).value == line(f_plain, L.COMMON_EQUITY).value
    base = line(f, L.COMMON_EQUITY)
    assert base.value == anchor(f).value + line(f, L.PAT).value * driver(f) / 100
    verify_forecast_integrity(f, data)


def test_m_cross_basis_hybrid_is_never_subtracted_from_another_basis():
    rows = company() + screener(company(basis=B.STANDALONE)) + hybrid_rows()  # consolidated
    a = box5(make(rows))
    assert bal(a, "COMMON_EQUITY").value == 1300
    standalone = bal(a, "COMMON_EQUITY", basis=B.STANDALONE)
    assert standalone.value is None  # no primary evidence on this basis; never borrowed
    assert all(i.reporting_basis == B.CONSOLIDATED for i in bal(a, "COMMON_EQUITY").provenance)


def test_n_rejected_hybrid_observation_is_never_assumed_nil():
    rows = company() + hybrid_rows(confidence=Confidence.LOW)
    a = box5(make(rows))
    common = bal(a, "COMMON_EQUITY")
    assert common.value is None and common.status.value == "LOW_CONFIDENCE_INPUT"
    assert bal(a, "SHAREHOLDERS_EQUITY").value == 1500  # the accounting claim is unaffected


# --- I: standalone P/E needs the ordinary shareholders' profit ------------------------------


def test_i_standalone_pe_refuses_pat_before_hybrid_distributions():
    rows = lender_rows(basis=B.STANDALONE) + hybrid_rows(basis=B.STANDALONE)
    data = make(rows, Industry.NBFC)
    f = forecast(data)
    v = valuation(data, f, pb(method=VM.PE, value="10"), pb())
    pe = result(v, VM.PE)
    assert (pe.status.value, pe.reason) == ("UNAVAILABLE", "ATTRIBUTION_UNKNOWN")
    assert "hybrid instruments outstanding" in pe.missing_inputs[0]
    assert result(v, VM.PB).status.value == "COMPLETE"  # P/B on ordinary equity still prices


# --- O / P / Q: HUDCO, INFY, YESBANK ---------------------------------------------------------


def test_o_hudco_exact_equity_semantics(hudco_layers):  # noqa: F811
    data, a, _box13, report = hudco_layers
    hybrid = [c for c in data.canonical_values if c.metric == M.HYBRID_EQUITY_INSTRUMENTS]
    assert [(c.value, c.period.period_end) for c in hybrid] == [(D("1442.00"), FY26)]
    obs = next(o for o in data.observations
               if o.observation_id == hybrid[0].selected_observation_id)
    assert (obs.raw_label, obs.source.page_number, obs.source.row_index) == (
        "(b) Instruments entirely equity in nature", 363, 21)
    # accounting total unchanged; ordinary = total - 1,442 (FY2025: the filing's '-')
    assert bal(a, "SHAREHOLDERS_EQUITY").value == D("21975.90")
    assert bal(a, "COMMON_EQUITY").value == D("20533.90")
    assert bal(a, "COMMON_EQUITY", FY25).value == D("17967.90")
    assert "HYBRID_NIL_REPORTED" in bal(a, "COMMON_EQUITY", FY25).warnings
    # earlier dates have no hybrid evidence, the standalone basis has no hybrid line at all
    assert bal(a, "COMMON_EQUITY", date(2024, 3, 31)).value is None
    assert bal(a, "COMMON_EQUITY", basis=B.STANDALONE).value is None
    # ROE and leverage keep the accounting claim (annual report RoNW 20.20%)
    assert metric(a, "ROE").value == D("20.198379")
    assert metric(a, "BORROWINGS_TO_EQUITY").value == D("6.446926")
    table = next(t for t in report.tables if any(r.key == "COMMON_EQUITY" for r in t.rows))
    assert {r.key for r in table.rows} >= {"EQUITY", "COMMON_EQUITY"}


def test_o_hudco_box10_box11_use_the_ordinary_claim(hudco):  # noqa: F811
    data, f = hudco
    a = anchor(f)
    assert a.value == D("20533.90")
    assert {(c.metric, c.value) for c in a.actual_components} == {
        (M.EQUITY_SHARE_CAPITAL, D("2001.90")), (M.RESERVES, D(19974)),
        (M.HYBRID_EQUITY_INSTRUMENTS, D("1442.00"))}


@pytest.mark.parametrize("case,basis,roe", [(INFY, B.STANDALONE, "34.732411"),  # P
                                            (YESBANK, B.CONSOLIDATED, "7.095595")])  # Q
def test_p_q_no_hybrid_companies_are_unchanged(case, basis, roe):
    data, a, _box13, _report = company_layers(case)
    assert not any(o.metric == M.HYBRID_EQUITY_INSTRUMENTS for o in data.observations)
    # provider-only data cannot evidence the hybrid amount: ordinary equity is unavailable,
    # every existing metric (net worth, ROE, leverage) is unchanged; Box 10 / 11 were
    # compared value-by-value with the previous commit (identical)
    for m in a.building_blocks:
        if m.code == "COMMON_EQUITY" and m.reporting_basis == basis:
            assert m.value is None and m.status.value == "INPUT_MISSING"
            assert bal(a, "SHAREHOLDERS_EQUITY", m.period.period_end, basis).value is not None
    assert metric(a, "ROE", basis=basis).value == D(roe)
