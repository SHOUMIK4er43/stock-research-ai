"""Box 11 lender valuation: the method follows the company's economic model.

NON_BANK_FINANCIAL forecasts are valued by EQUITY multiples on owners' figures only:
  P/B  equity value = Box 10 EQUITY (owners' book equity) x P/B
  P/E  equity value = Box 10 PAT (owners' PAT)            x P/E
Multiples are request assumptions (explicit, or a cited peer reference) - never defaults and
never source facts. Each multiple prices its own metric, in its own scenario and forecast year.
FCFF DCF, EV/EBITDA, EV/EBIT and the enterprise-to-equity bridge are refused for a lender;
per-share value needs the validated share count. Residual income is NOT implemented (no
validated cost-of-equity / terminal contract for lenders yet).
"""

import asyncio
import tempfile
from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest
from test_financial_analytics import B, M, make, pit
from test_financial_cell_integrity import KEY, filing_observations, reconcile
from test_financial_forecasting import lender_rows
from test_screener_dataset import page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import financial_reconciliation as fr
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.forecasting import build_forecast, quantize
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
from stock_research_ai.domain.forecasting import ForecastLine, ForecastModel, Scenario
from stock_research_ai.domain.fundamental_valuation import (
    DEFAULT_METHODS,
    AssumptionInput,
    MultipleInput,
    ValuationMethod,
    ValuationRequest,
)

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
VM, S, L = ValuationMethod, Scenario, ForecastLine
INDUSTRIAL = {VM.DCF_FCFF_GORDON, VM.DCF_FCFF_EXIT_MULTIPLE, VM.EV_EBITDA, VM.EV_EBIT}
SHARES = D(100_000_000)  # a request assumption in these synthetic tests (ones)
BASIS = Path(__file__).parent / "fixtures" / "reporting_basis"
SCREENER = Path(__file__).parent / "fixtures" / "screener"


def multiple(method, scenario=S.BASE, year=2027, value="1.5", **kw):
    return MultipleInput(method=method, scenario=scenario, fiscal_year=year,
                         multiple=D(value), reference="test request assumption", **kw)


def shares(count=SHARES):
    return AssumptionInput(kind="SHARE_COUNT", value=count, unit="COUNT", scale="ONES")


def lender(rows=None, industry=Industry.NBFC, cls=None):
    data = make(rows or lender_rows(), industry)
    if cls is not None:
        data = data.model_copy(update={"business_class": cls})
    return data, build_forecast(data, generated_at=NOW)


def value(data, f, *multiples, with_shares=True, **request):
    req = ValuationRequest(multiples=tuple(multiples),
                           assumptions=(shares(),) if with_shares else (), **request)
    return build_valuation(f, request=req, box4=data, generated_at=NOW)


def result(v, method, scenario=S.BASE):
    return next(r for r in v.results if r.method == method and r.scenario == scenario)


def box10(f, line, scenario=S.BASE, year=2027):
    return next(x for x in f.values if x.line == line and x.scenario == scenario
                and x.period.fiscal_year == year)


# --- A / B: industrial methods never value a lender ------------------------------------------


def test_a_b_lender_defaults_to_equity_methods_and_refuses_industrial_ones():
    data, f = lender()
    assert f.base.forecast_model == ForecastModel.NON_BANK_FINANCIAL
    default = value(data, f)
    assert {r.method for r in default.results} == {VM.PB, VM.PE}
    asked = value(data, f, methods=DEFAULT_METHODS)
    for r in asked.results:
        if r.method in INDUSTRIAL:
            assert r.reason == "UNSUPPORTED_SECTOR" and r.enterprise_value is None
            assert r.equity_value is None and not r.value_ids


# --- C-G: P/B and P/E price their own metric only ----------------------------------------------


def test_c_f_pb_prices_owners_book_equity_only():
    data, f = lender()
    r = result(value(data, f, multiple(VM.PB, value="1.5")), VM.PB)
    equity = box10(f, L.EQUITY)
    assert r.status.value == "COMPLETE"
    assert r.equity_value == equity.value * D("1.5")
    assert (r.multiple.metric, r.multiple.metric_value_id) == ("BOOK_EQUITY", equity.value_id)
    assert r.multiple.metric_value_id != box10(f, L.PAT).value_id  # F: never PAT


def test_d_g_pe_prices_owners_pat_only():
    data, f = lender(lender_rows(basis=B.STANDALONE))  # standalone: PAT is the owners'
    r = result(value(data, f, multiple(VM.PE, value="10")), VM.PE)
    pat = box10(f, L.PAT)
    assert r.status.value == "COMPLETE" and r.equity_value == pat.value * 10
    assert (r.multiple.metric, r.multiple.metric_value_id) == ("PAT_OWNERS", pat.value_id)
    assert r.multiple.metric_value_id != box10(f, L.EQUITY).value_id  # G: never equity


def test_consolidated_pe_needs_evidence_that_pat_is_the_owners():
    data, f = lender()
    r = result(value(data, f, multiple(VM.PE, value="10")), VM.PE)
    assert r.status.value == "UNAVAILABLE" and r.reason == "ATTRIBUTION_UNKNOWN"


def test_e_without_a_multiple_nothing_is_assumed():
    data, f = lender()
    v = value(data, f)
    assert v.status.value == "UNAVAILABLE"
    assert {r.reason for r in v.results} == {"MULTIPLE_MISSING"}
    assert all(r.equity_value is None for r in v.results)


# --- H-K: scenario identity and basis ----------------------------------------------------------


def test_h_i_j_each_scenario_prices_its_own_forecast():
    data, f = lender()
    v = value(data, f, *(multiple(VM.PB, s, value="2") for s in (S.BASE, S.BULL, S.BEAR)))
    for scenario in (S.BASE, S.BULL, S.BEAR):
        r = result(v, VM.PB, scenario)
        equity = box10(f, L.EQUITY, scenario)
        assert r.multiple.metric_value_id == equity.value_id
        assert r.equity_value == equity.value * 2
    assert len({result(v, VM.PB, s).equity_value for s in (S.BASE, S.BULL, S.BEAR)}) == 3


def test_k_reporting_basis_is_preserved():
    data, f = lender()
    v = value(data, f, multiple(VM.PB))
    assert v.reporting_basis == f.base.reporting_basis == B.CONSOLIDATED
    with pytest.raises(ValuationInputError) as refused:
        value(data, f, multiple(VM.PB), reporting_basis=B.STANDALONE)
    assert refused.value.code == "BASIS_MISMATCH"


# --- L / M: wrong or broken forecasts ------------------------------------------------------------


def test_l_a_wrong_model_forecast_is_refused():
    data, f = lender()
    wrong = f.model_copy(update={"base": f.base.model_copy(
        update={"forecast_model": ForecastModel.NON_FINANCIAL})})
    assert _model_problem(wrong, data)[0] == "FORECAST_MODEL_MISMATCH"
    with pytest.raises(ValuationInputError):
        build_valuation(wrong, box4=data, generated_at=NOW)


def test_m_a_rejected_forecast_input_leaves_valuation_unavailable():
    data = make(lender_rows(), Industry.NBFC)
    target = next(v for v in data.canonical_values
                  if v.metric == M.TOTAL_ASSETS and v.period == pit(2026))
    bad = data.model_copy(update={"observations": tuple(
        o.model_copy(update={"warnings": ("SPLIT_NUMBER_SUSPECT",)})
        if o.observation_id == target.selected_observation_id else o
        for o in data.observations)})
    f = build_forecast(bad, generated_at=NOW)
    v = build_valuation(f, request=ValuationRequest(multiples=(multiple(VM.PB),)),
                        box4=bad, generated_at=NOW)
    assert v.status.value == "UNAVAILABLE" and all(r.equity_value is None for r in v.results)


# --- N / O: equity value vs per-share value ------------------------------------------------------


def test_n_o_per_share_needs_the_validated_share_count():
    data, f = lender()
    with_shares = result(value(data, f, multiple(VM.PB, value="1.5")), VM.PB)
    with localcontext() as context:
        context.prec = 34
        expected = with_shares.equity_value * D(10) ** 7 / SHARES
    assert with_shares.fair_value_per_share == expected
    without = result(value(data, f, multiple(VM.PB, value="1.5"), with_shares=False), VM.PB)
    assert without.status.value == "PARTIAL" and without.fair_value_per_share is None
    assert without.equity_value == with_shares.equity_value  # crore, never a price


# --- P / Q / R / T / U: other models unchanged ---------------------------------------------------


def _company(case):
    from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
    with tempfile.TemporaryDirectory() as t:
        ds = build_screener_dataset(*asyncio.run(page(Path(t), SCREENER, case)), None)
    data = build_canonical_dataset(company_key=ds.company_key, company_name=ds.profile.name,
                                   screeners=(ds,), bundle=None, catalogue=None,
                                   generated_at=NOW)
    return data, build_forecast(data, generated_at=NOW)


@pytest.mark.parametrize("case", [YESBANK, INFY])  # P/U bank, Q/T industrial
def test_p_q_t_u_bank_and_industrial_keep_their_methods(case):
    data, f = _company(case)
    v = build_valuation(f, box4=data, generated_at=NOW)
    assert {r.method for r in v.results} == set(DEFAULT_METHODS)  # no P/B by default
    assert not {r.reason for r in v.results} & {"LENDER_METHOD_ONLY", "ATTRIBUTION_UNKNOWN"}
    pb = build_valuation(f, request=ValuationRequest(methods=(VM.PB,)), box4=data,
                         generated_at=NOW)
    assert {r.reason for r in pb.results} == {"LENDER_METHOD_ONLY"}


def test_r_unknown_model_gets_no_lender_valuation():
    data, f = lender(industry=Industry.UNKNOWN)
    assert f.base.forecast_model == ForecastModel.UNKNOWN
    v = build_valuation(f, box4=data, generated_at=NOW)
    assert VM.PB not in {r.method for r in v.results}
    assert all(r.equity_value is None for r in v.results if r.method in INDUSTRIAL)


# --- S: HUDCO, exact ---------------------------------------------------------------------------


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


def test_s_hudco_exact_lender_valuation(hudco):
    data, f = hudco
    # 1.0x P/B and 8x P/E are REQUEST ASSUMPTIONS for this test, not source facts; the
    # share count 2,001,900,000 is an explicit request input checked against EPS-implied
    req = ValuationRequest(
        multiples=(*(multiple(VM.PB, s, value="1.0") for s in (S.BASE, S.BULL, S.BEAR)),
                   multiple(VM.PE, value="8")),
        assumptions=(shares(D(2_001_900_000)),))
    v = build_valuation(f, request=req, box4=data, generated_at=NOW)
    assert (f.base.forecast_model, v.reporting_basis) == (ForecastModel.NON_BANK_FINANCIAL,
                                                          B.CONSOLIDATED)
    assert {r.method for r in v.results} == {VM.PB, VM.PE}  # no DCF, no EV multiple
    expected = {S.BASE: ("24541.971223", "122.593392"), S.BULL: ("25085.116740", "125.306542"),
                S.BEAR: ("24041.972372", "120.095771")}
    for scenario, (equity, per_share) in expected.items():
        r = result(v, VM.PB, scenario)
        assert r.status.value == "COMPLETE"
        assert (quantize(r.equity_value), quantize(r.fair_value_per_share)) == (
            D(equity), D(per_share))
        assert r.multiple.source.value == "EXPLICIT"
    pe = result(v, VM.PE)  # no Box 4 PAT attributable to owners for the consolidated basis
    assert (pe.status.value, pe.reason) == ("UNAVAILABLE", "ATTRIBUTION_UNKNOWN")
    for scenario in (S.BULL, S.BEAR):
        assert result(v, VM.PE, scenario).reason == "MULTIPLE_MISSING"


# --- V / W / X: Box 13, the report, no industrial lineage ----------------------------------------


@pytest.fixture(scope="module")
def lender_report():
    data, f = lender()
    v = value(data, f, multiple(VM.PB, value="1.5"))
    box5 = build_financial_analytics(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box10=f, box11=v)
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box4=data,
        box5=box5, box13=box13, box10=f, box11=v)
    return v, box13, report


def test_v_box13_accepts_the_lender_valuation(lender_report):
    v, box13, _report = lender_report
    assert any(s.layer.value == "BOX11" for s in box13.sources)
    assert v.status.value == "PARTIAL"  # P/B priced, P/E has no multiple: not COMPLETE


def test_w_report_exposes_model_method_scenario_assumption_and_provenance(lender_report):
    v, _box13, report = lender_report
    table = next(t for t in report.tables if t.code == "VALUATION_METHODS")
    assert "NON_BANK_FINANCIAL" in table.title
    assert any("Valuation model: NON_BANK_FINANCIAL" in n for n in table.footnotes)
    assert [c.key for c in table.columns] == ["STATUS", "METRIC", "MULTIPLE", "EQUITY_VALUE",
                                             "FAIR_VALUE_PER_SHARE"]
    pb = next(r for r in table.rows if r.key == "PB|BASE")
    texts = {c.column: c.text for c in pb.cells if c.text}
    assert texts["METRIC"] == "BOOK_EQUITY FY2027"
    assert texts["MULTIPLE"] == "1.5x (explicit: test request assumption)"
    pe = next(r for r in table.rows if r.key == "PE|BASE")
    equity_cell = next(c for c in pe.cells if c.column == "EQUITY_VALUE")
    assert equity_cell.claim_id is None and equity_cell.status.value != "AVAILABLE"  # not 0


def test_x_no_lender_result_has_industrial_lineage(lender_report):
    v, _box13, _report = lender_report
    values = {x.value_id: x for x in v.values}
    for r in v.results:
        for vid in r.value_ids:
            formula = values[vid].formula_id or ""
            assert not any(word in formula for word in (
                "ENTERPRISE", "EBITDA", "EBIT", "FCFF", "BRIDGE", "DCF")), formula
    bridge = {x.name: x for x in v.values if x.name in ("GROSS_DEBT", "CASH",
                                                         "MINORITY_INTEREST")}
    assert bridge and all(x.status == "UNAVAILABLE" and x.reason == "NOT_APPLICABLE_LENDER"
                          for x in bridge.values())
