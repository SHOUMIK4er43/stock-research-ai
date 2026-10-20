"""Box 10 forecast models follow the company's economics (Box 4's ONE classification).

  GENERAL -> NON_FINANCIAL   industrial P&L route (unchanged)
  BANK    -> BANK            revenue only (unchanged)
  FINANCIAL class, not a bank -> NON_BANK_FINANCIAL: balance-sheet lender model
      total assets (asset growth) -> PAT (ROA on average assets) -> equity (equity change /
      PAT) -> non-equity funding (assets - equity) -> borrowings (share of funding) ->
      finance cost (cost-of-borrowings PROXY on average borrowings); total income on average
      assets. No revenue, EBITDA, working-capital, capex or gross-debt driver.
  otherwise -> UNKNOWN       revenue only, no sector-specific line

Box 11 refuses a NON_BANK_FINANCIAL forecast (no lender valuation yet) and any forecast whose
model is not the company's.
"""

import asyncio
import tempfile
from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest
from test_financial_analytics import B, M, V, company, fy, make, pit
from test_financial_cell_integrity import KEY, filing_observations, reconcile
from test_screener_dataset import page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import financial_reconciliation as fr
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.forecast_integrity import verify_forecast_integrity
from stock_research_ai.application.forecasting import (
    ForecastInputError,
    build_forecast,
    quantize,
)
from stock_research_ai.application.fundamental_valuation import (
    ValuationInputError,
    _model_problem,
    build_valuation,
)
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    FinancialLimits,
    Industry,
)
from stock_research_ai.domain.forecasting import (
    AssumptionInput,
    Driver,
    ForecastLine,
    ForecastModel,
    ForecastRequest,
    Scenario,
)

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
L, S = ForecastLine, Scenario
LENDER_LINES = {L.TOTAL_ASSETS, L.PAT, L.EQUITY, L.NON_EQUITY_FUNDING, L.TOTAL_BORROWINGS,
                L.FINANCE_COST, L.TOTAL_INCOME}
INDUSTRIAL = {L.EBITDA, L.OPERATING_PROFIT, L.CAPEX, L.TRADE_WORKING_CAPITAL,
              L.WORKING_CAPITAL_CHANGE, L.GROSS_DEBT, L.FCF_ESTIMATE, L.REVENUE}
BASIS = Path(__file__).parent / "fixtures" / "reporting_basis"
SCREENER = Path(__file__).parent / "fixtures" / "screener"


def lender_rows(**kw):
    rows = company(**kw)
    basis = kw.get("basis", B.CONSOLIDATED)
    return rows + [V(M.TOTAL_INCOME, fy(y), 1100 + 100 * (y - 2021), basis=basis)
                   for y in range(2021, 2027)]


def forecast(rows, industry=Industry.NBFC, cls=None, **request):
    data = make(rows, industry)
    if cls is not None:
        data = data.model_copy(update={"business_class": cls})
    return data, build_forecast(data, request=ForecastRequest.model_validate(request),
                                generated_at=NOW)


def value(f, line, scenario=S.BASE, year=2027):
    return next(v for v in f.values if v.line == line and v.scenario == scenario
                and v.period.fiscal_year == year)


def assumption(f, driver, scenario=S.BASE, year=2027):
    return next(a for a in f.assumptions if a.driver == driver and a.scenario == scenario
                and a.fiscal_year == year)


# --- A-D: model selection from Box 4's classification ---------------------------------------


@pytest.mark.parametrize("industry,cls", [(Industry.NBFC, None),
                                          (Industry.UNKNOWN, BusinessClass.FINANCIAL),
                                          (Industry.INSURANCE, None)])
def test_a_non_bank_financial_gets_the_lender_model(industry, cls):
    _data, f = forecast(lender_rows(), industry, cls)
    assert f.base.forecast_model == ForecastModel.NON_BANK_FINANCIAL
    assert {v.line for v in f.values} == LENDER_LINES
    assert all(c.status.value == "COMPLETE" for c in f.completeness)


@pytest.mark.parametrize("industry,model,lines", [
    (Industry.GENERAL, ForecastModel.NON_FINANCIAL, None),  # B
    (Industry.BANK, ForecastModel.BANK, {L.REVENUE}),  # C
    (Industry.UNKNOWN, ForecastModel.UNKNOWN, {L.REVENUE}),  # D: no sector-specific line
])
def test_b_c_d_other_models_keep_their_lines(industry, model, lines):
    _data, f = forecast(lender_rows(), industry)
    assert f.base.forecast_model == model
    produced = {v.line for v in f.values}
    assert not produced & (LENDER_LINES - {L.PAT, L.FINANCE_COST})
    if lines is not None:
        assert produced == lines
    else:
        assert {L.EBITDA, L.PAT, L.CAPEX} <= produced  # the industrial route, unchanged


# --- E-G: no industrial driver for a lender -------------------------------------------------


def test_e_f_g_no_ebitda_working_capital_or_capex_for_a_lender():
    _data, f = forecast(lender_rows())
    drivers = {a.driver for a in f.assumptions}
    assert drivers == {Driver.ASSET_GROWTH, Driver.ROA, Driver.EQUITY_CHANGE_TO_PAT,
                       Driver.BORROWINGS_SHARE_OF_FUNDING, Driver.COST_OF_BORROWINGS,
                       Driver.TOTAL_INCOME_TO_AVERAGE_ASSETS}
    assert not {v.line for v in f.values} & INDUSTRIAL
    assert not {v.line for v in f.actuals} & INDUSTRIAL
    for driver in (Driver.EBITDA_MARGIN, Driver.WORKING_CAPITAL_TO_REVENUE,
                   Driver.CAPEX_TO_REVENUE):
        bad = AssumptionInput(driver=driver, scenario=S.BASE, fiscal_year=2027, value=D(10),
                              unit="PERCENT")
        with pytest.raises(ForecastInputError) as refused:
            forecast(lender_rows(), explicit_assumptions=[bad])
        assert refused.value.code == "UNUSED_EXPLICIT_ASSUMPTION"


# --- H-L: drivers come from valid history, lines from coherent relationships ---------------


def test_h_asset_growth_is_the_historical_cagr_of_total_assets():
    _data, f = forecast(lender_rows())
    growth = assumption(f, Driver.ASSET_GROWTH)
    # company(): total assets 2000 + 150 k; window FY2022..FY2026
    expected = quantize(((D(2750) / D(2150)) ** (D(1) / D(4)) - 1) * 100)
    assert growth.value == expected and growth.method.value == "HISTORICAL_CAGR"
    assert value(f, L.TOTAL_ASSETS).value == D(2750) * (1 + growth.value / 100)


@pytest.fixture
def box10_precision():
    """Box 10's working precision (34 digits), restored after the test."""
    with localcontext() as context:
        context.prec = 34
        yield


def test_i_j_k_balance_sheet_and_profit_relationships_are_coherent(box10_precision):
    _data, f = forecast(lender_rows())
    roa = assumption(f, Driver.ROA)
    for scenario in (S.BASE, S.BULL, S.BEAR):
        prior_assets = next(v.value for v in f.actuals if v.line == L.TOTAL_ASSETS)
        prior_equity = next(v.value for v in f.actuals if v.line == L.EQUITY)
        for year in (2027, 2028, 2029):
            assets, pat = value(f, L.TOTAL_ASSETS, scenario, year), value(f, L.PAT, scenario, year)
            equity = value(f, L.EQUITY, scenario, year)
            funding = value(f, L.NON_EQUITY_FUNDING, scenario, year)
            borrowings = value(f, L.TOTAL_BORROWINGS, scenario, year)
            a = {d: assumption(f, d, scenario, year) for d in (
                Driver.ROA, Driver.EQUITY_CHANGE_TO_PAT, Driver.BORROWINGS_SHARE_OF_FUNDING)}
            assert pat.value == a[Driver.ROA].value / 100 * (prior_assets + assets.value) / 2
            assert equity.value == prior_equity + pat.value * a[
                Driver.EQUITY_CHANGE_TO_PAT].value / 100  # J
            assert assets.value - equity.value == funding.value  # the funding identity
            assert borrowings.value == funding.value * a[
                Driver.BORROWINGS_SHARE_OF_FUNDING].value / 100  # I
            assert borrowings.value <= funding.value
            prior_assets, prior_equity = assets.value, equity.value
    assert roa.method.value == "HISTORICAL_MEDIAN"  # K: never an industrial margin
    assert value(f, L.PAT).formula_id == "PAT/1/ROA_ON_AVERAGE_ASSETS"


def test_l_finance_cost_uses_the_labelled_proxy_on_average_borrowings(box10_precision):
    _data, f = forecast(lender_rows())
    cost = assumption(f, Driver.COST_OF_BORROWINGS)
    assert "COST_OF_BORROWINGS_PROXY" in cost.warnings
    finance = value(f, L.FINANCE_COST)
    assert finance.formula_id == "FINANCE_COST/1/COST_OF_BORROWINGS_PROXY"
    assert "PROXY" in (finance.formula_text or "")
    opening = next(v.value for v in f.actuals if v.line == L.TOTAL_BORROWINGS)
    closing = value(f, L.TOTAL_BORROWINGS).value
    assert finance.value == cost.value / 100 * (opening + closing) / 2
    _d, bank = forecast(lender_rows(), Industry.BANK)
    assert not [v for v in bank.values if v.line == L.FINANCE_COST]


# --- M-O: fail closed --------------------------------------------------------------------------


def test_m_without_opening_balances_the_drivers_are_unavailable():
    latest_only = [r for r in lender_rows() if r.metric not in (M.TOTAL_BORROWINGS,)
                   ] + [V(M.TOTAL_BORROWINGS, pit(2026), 300)]
    _d, f = forecast(latest_only)
    cost = [a for a in f.assumptions if a.driver == Driver.COST_OF_BORROWINGS]
    assert not cost  # no year has both the opening and the closing borrowings
    finance = value(f, L.FINANCE_COST)
    assert finance.status == "UNAVAILABLE" and finance.value is None
    assert finance.reason == "ASSUMPTION_MISSING"


def test_n_cross_basis_history_is_never_used():
    rows = [r for r in lender_rows() if r.metric != M.TOTAL_BORROWINGS]
    rows += [V(M.TOTAL_BORROWINGS, pit(y), 300, basis=B.STANDALONE) for y in range(2021, 2027)]
    data, f = forecast(rows)
    assert f.base.reporting_basis == B.CONSOLIDATED
    anchor = next(v for v in f.actuals if v.line == L.TOTAL_BORROWINGS)
    assert anchor.status == "UNAVAILABLE"
    assert value(f, L.TOTAL_BORROWINGS).status == "UNAVAILABLE"
    standalone = {v.canonical_value_id for v in data.canonical_values
                  if v.reporting_basis == B.STANDALONE}
    for a in f.assumptions:
        assert not set(a.canonical_value_ids) & standalone


def test_o_a_rejected_canonical_input_makes_the_forecast_unavailable():
    data = make(lender_rows(), Industry.NBFC)
    target = next(v for v in data.canonical_values
                  if v.metric == M.TOTAL_ASSETS and v.period == pit(2026))
    bad = data.model_copy(update={"observations": tuple(
        o.model_copy(update={"warnings": ("SPLIT_NUMBER_SUSPECT",)})
        if o.observation_id == target.selected_observation_id else o
        for o in data.observations)})
    f = build_forecast(bad, generated_at=NOW)
    assert f.status.value == "UNAVAILABLE" and not f.values
    assert any(i.code == "ASSET_BASE_MISSING" for i in f.issues)


# --- P / Q: units and scenario ordering ---------------------------------------------------------


def test_p_q_units_and_deterministic_scenario_ordering():
    _data, f = forecast(lender_rows())
    assert {a.unit.value for a in f.assumptions} == {"PERCENT"}
    assert {(v.unit, v.scale.value) for v in f.values} == {("MONEY", "CRORES")}
    for year in (2027, 2028, 2029):
        for line in (L.TOTAL_ASSETS, L.PAT):
            bull, base, bear = (value(f, line, s, year).value for s in (S.BULL, S.BASE, S.BEAR))
            assert bull >= base >= bear, (line, year)
    again = build_forecast(make(lender_rows(), Industry.NBFC), generated_at=NOW)
    assert [v.value for v in again.values] == [v.value for v in f.values]


# --- R / S / T: HUDCO, YESBANK, INFY ------------------------------------------------------------


@pytest.fixture(scope="module")
def hudco():
    observations = list(filing_observations())
    for case in (("hudco_consolidated.html",
                  "https://www.screener.in/company/HUDCO/consolidated/"),
                 ("hudco_standalone.html", "https://www.screener.in/company/HUDCO/")):
        with tempfile.TemporaryDirectory() as t:
            ds = build_screener_dataset(*asyncio.run(page(Path(t), BASIS, case)), None)
        c = fr._Collector(KEY, FinancialLimits())
        fr.screener_observations(ds.model_copy(update={"company_key": KEY}), c,
                                 Industry.UNKNOWN, 3)
        observations += c.observations
    data = reconcile(observations).model_copy(update={"business_class": BusinessClass.FINANCIAL})
    return data, build_forecast(data, generated_at=NOW)


def test_r_hudco_exact_lender_forecast(hudco):
    data, f = hudco
    assert (f.base.forecast_model, f.base.reporting_basis) == (
        ForecastModel.NON_BANK_FINANCIAL, B.CONSOLIDATED)
    anchors = {v.line: v.value for v in f.actuals}
    assert anchors == {L.TOTAL_ASSETS: D(166838), L.TOTAL_BORROWINGS: D(141677),
                       L.EQUITY: D("21975.90"), L.PAT: D(4034),
                       L.FINANCE_COST: D("8930.69"), L.TOTAL_INCOME: D("13327.13")}
    expected = {Driver.ASSET_GROWTH: "20.590404", Driver.ROA: "2.427878",
                Driver.EQUITY_CHANGE_TO_PAT: "57.436802",
                Driver.BORROWINGS_SHARE_OF_FUNDING: "96.337669",
                Driver.COST_OF_BORROWINGS: "7.308845",
                Driver.TOTAL_INCOME_TO_AVERAGE_ASSETS: "9.175717"}
    for driver, v in expected.items():
        assert assumption(f, driver).value == D(v), driver
    assert assumption(f, Driver.ASSET_GROWTH, S.BULL).value == D("37.543619")
    assert assumption(f, Driver.ASSET_GROWTH, S.BEAR).value == D("2.630111")
    assert assumption(f, Driver.ROA, S.BULL).value == D("2.731822")  # FY2026 ROA (max)
    assert assumption(f, Driver.ROA, S.BEAR).value == D("2.128072")
    # only two filing years of finance cost / total income: SPARSE, never a guess
    for driver in (Driver.COST_OF_BORROWINGS, Driver.TOTAL_INCOME_TO_AVERAGE_ASSETS):
        assert assumption(f, driver).completeness.value == "SPARSE"
    base = {line: quantize(value(f, line).value) for line in LENDER_LINES}
    assert base == {L.TOTAL_ASSETS: D("201190.618226"), L.PAT: D("4467.642928"),
                    L.EQUITY: D("24541.971223"), L.NON_EQUITY_FUNDING: D("176648.647003"),
                    L.TOTAL_BORROWINGS: D("170179.188843"), L.FINANCE_COST: D("11396.542733"),
                    L.TOTAL_INCOME: D("16884.632244")}
    verify_forecast_integrity(f, data)  # Box 10's own arithmetic, rebuilt from Box 4
    valuation = build_valuation(f, box4=data, generated_at=NOW)  # U
    # the lender valuation model: P/B and P/E only, unavailable without a supplied multiple
    assert {r.method.value for r in valuation.results} == {"PB", "PE"}
    assert {r.reason for r in valuation.results} == {"MULTIPLE_MISSING"}
    assert all(r.status.value == "UNAVAILABLE" for r in valuation.results)


def _company(case):
    from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
    with tempfile.TemporaryDirectory() as t:
        ds = build_screener_dataset(*asyncio.run(page(Path(t), SCREENER, case)), None)
    data = build_canonical_dataset(company_key=ds.company_key, company_name=ds.profile.name,
                                   screeners=(ds,), bundle=None, catalogue=None,
                                   generated_at=NOW)
    return data, build_forecast(data, generated_at=NOW)


@pytest.mark.parametrize("case,model", [(YESBANK, ForecastModel.BANK),  # S
                                        (INFY, ForecastModel.NON_FINANCIAL)])  # T
def test_s_t_bank_and_industrial_keep_their_models(case, model):
    data, f = _company(case)
    assert f.base.forecast_model == model
    assert not {v.line for v in f.values} & (LENDER_LINES - {L.PAT, L.FINANCE_COST})
    valuation = build_valuation(f, box4=data, generated_at=NOW)
    assert not {r.reason for r in valuation.results} & {"FORECAST_MODEL_NOT_VALUED",
                                                       "FORECAST_MODEL_MISMATCH"}
    if model == ForecastModel.BANK:
        assert {v.line for v in f.values} == {L.REVENUE}
    else:
        assert {L.EBITDA, L.PAT, L.FCF_ESTIMATE} <= {v.line for v in f.values}


# --- U / V: Box 11 interface and the report ----------------------------------------------------


def test_u_box11_never_consumes_a_wrong_model_forecast(hudco):
    data, f = hudco
    wrong = f.model_copy(update={"base": f.base.model_copy(
        update={"forecast_model": ForecastModel.NON_FINANCIAL})})
    assert _model_problem(wrong, data)[0] == "FORECAST_MODEL_MISMATCH"
    assert _model_problem(wrong, None)[0] == "FORECAST_MODEL_MISMATCH"  # UNKNOWN industry
    with pytest.raises(ValuationInputError):  # a tampered model never passes integrity
        build_valuation(wrong, box4=data, generated_at=NOW)
    _gdata, general = forecast(lender_rows(), Industry.GENERAL)
    lender_on_general = general.model_copy(update={"base": general.base.model_copy(
        update={"forecast_model": ForecastModel.NON_BANK_FINANCIAL})})
    assert _model_problem(lender_on_general, None)[0] == "FORECAST_MODEL_MISMATCH"


def test_v_report_states_the_model_and_its_assumptions():
    data = make(lender_rows(), Industry.NBFC)
    box5 = build_financial_analytics(data, generated_at=NOW)
    box10 = build_forecast(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box10=box10)
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box4=data,
        box5=box5, box13=box13, box10=box10)
    table = next(t for t in report.tables if t.code == "FORECAST_BASE")
    assert "NON_BANK_FINANCIAL" in table.title
    assert any("Forecast model: NON_BANK_FINANCIAL" in n and "PROXY" in n
               for n in table.footnotes)
    rows = {r.key for r in table.rows}
    assert rows == {line.value for line in LENDER_LINES}
    assumptions = next(t for t in report.tables if t.code == "ASSUMPTIONS_BASE")
    assert {r.key for r in assumptions.rows} == {
        "ASSET_GROWTH", "ROA", "EQUITY_CHANGE_TO_PAT", "BORROWINGS_SHARE_OF_FUNDING",
        "COST_OF_BORROWINGS", "TOTAL_INCOME_TO_AVERAGE_ASSETS"}
    claims = {c.claim_id: c for c in report.claims}
    for row in table.rows:  # an unavailable line is never shown as zero
        for cell in row.cells:
            if cell.claim_id is None:
                assert cell.status.value != "AVAILABLE"
            else:
                assert claims[cell.claim_id].value is not None
