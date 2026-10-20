"""Non-bank financial (lender) analytics: FINANCIAL must not mean "no analysis".

Box 5 adds lender formulas that run only for Box 4 class FINANCIAL and NOT a bank
(financial_formulas.NON_BANK_FINANCIAL): borrowings / equity, its YoY change, borrowings
growth, finance cost / average borrowings, finance cost / total income, PAT / total income and
asset growth less equity growth. Inputs are Box 4 canonical facts (TOTAL_BORROWINGS,
FINANCE_COST, TOTAL_INCOME, PAT) and Box 5 equity - never a KPI inferred from other rows.
Box 7 keeps bank-only KPIs for BANK; Box 9 compares the same metric only; Box 13 adds three
persistence (not level-threshold) lender trend signals.

HUDCO (FINANCIAL, subtype unresolved) FY2026 consolidated:
  borrowings / equity 141,677 / 21,975.90 = 6.446926x; cost of borrowings 8,930.69 /
  avg(141,677, 107,297) = 7.173994 %; finance cost / total income 8,930.69 / 13,327.13 =
  67.011352 %; PAT / total income 4,034 / 13,327.13 = 30.269083 %.
Still UNAVAILABLE (no source evidence in the retained statements): interest income / yield,
NII, NIM, spread, GNPA / NNPA, Stage 3, credit cost, CRAR, AUM / disbursement growth.
"""

import asyncio
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_financial_analytics import B, M, V, company, fy, make, pit
from test_financial_cell_integrity import KEY, filing_observations, reconcile
from test_screener_dataset import page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import financial_reconciliation as fr
from stock_research_ai.application.business_registry import match_kpi
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_formulas import NON_BANK_FINANCIAL
from stock_research_ai.application.peer_financials import financial_benchmarks
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain.business_intelligence import KPIUnit, SectorFamily
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    FinancialLimits,
    Industry,
)

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
FY26 = date(2026, 3, 31)
LENDER = sorted(NON_BANK_FINANCIAL)
LENDER_SIGNALS = {"RISING_LENDER_LEVERAGE", "BALANCE_SHEET_OUTGROWING_EQUITY",
                  "RISING_COST_OF_BORROWINGS"}
BASIS = Path(__file__).parent / "fixtures" / "reporting_basis"
SCREENER = Path(__file__).parent / "fixtures" / "screener"


def lender_rows(**kw):
    """company() plus a lender's total income for every year (other rows unchanged)."""
    rows = company(**kw)
    basis = kw.get("basis", B.CONSOLIDATED)
    rows += [V(M.TOTAL_INCOME, fy(y), 1100 + 100 * (y - 2021), basis=basis)
             for y in range(2021, 2027)]
    return rows


def analytics(rows, industry, cls=None):
    data = make(rows, industry)
    if cls is not None:
        data = data.model_copy(update={"business_class": cls})
    return data, build_financial_analytics(data, generated_at=NOW)


def metric(box5, code, end=FY26, basis=B.CONSOLIDATED):
    found = [m for m in box5.all_metrics() if m.code == code and m.period.period_end == end
             and m.reporting_basis == basis and m.period.period_type.value == "FY"]
    assert len(found) == 1, (code, found)
    return found[0]


def status(box5, code):
    return {m.status.value for m in box5.all_metrics() if m.code == code}


# --- A-F: who receives which formulas -------------------------------------------------------


@pytest.mark.parametrize("industry,cls", [(Industry.UNKNOWN, BusinessClass.FINANCIAL),
                                          (Industry.NBFC, None)])
def test_a_b_c_non_bank_financial_gets_lender_not_industrial_or_bank(industry, cls):
    _data, box5 = analytics(lender_rows(), industry, cls)
    for code in LENDER:
        assert metric(box5, code).status.value == "CALCULATED", code  # A
    assert status(box5, "EBITDA_MARGIN") == {"UNSUPPORTED_SECTOR"}  # B
    assert status(box5, "DEBT_TO_EQUITY") == {"UNSUPPORTED_SECTOR"}
    for code in ("COST_TO_INCOME", "FINANCING_PROFIT_MARGIN", "GROSS_NPA_RATIO"):  # C
        assert "CALCULATED" not in status(box5, code)


@pytest.mark.parametrize("industry,cls", [(Industry.BANK, None), (Industry.GENERAL, None),
                                          (Industry.UNKNOWN, None)])
def test_d_e_f_bank_general_and_unknown_get_no_lender_formula(industry, cls):
    _data, box5 = analytics(lender_rows(), industry, cls)
    for code in LENDER:
        assert status(box5, code) <= {"UNSUPPORTED_SECTOR"}, (industry, code)
    assert "CALCULATED" in status(box5, "ROE")  # business-neutral metrics remain
    if industry == Industry.GENERAL:
        assert "CALCULATED" in status(box5, "EBITDA_MARGIN")  # E: industrial unchanged


# --- G-N: definitions, averages, basis and integrity ------------------------------------------


def test_g_h_i_average_balances_need_the_opening_balance():
    _data, box5 = analytics(lender_rows(), Industry.NBFC)
    roa, roe, cost = (metric(box5, c) for c in ("ROA", "ROE", "COST_OF_BORROWINGS"))
    assert roa.status.value == roe.status.value == cost.status.value == "CALCULATED"
    # FY2021 is the first year: no opening balance, never a single-balance ratio
    for code in ("ROA", "ROE", "COST_OF_BORROWINGS"):
        first = metric(box5, code, date(2021, 3, 31))
        assert first.status.value != "CALCULATED" and first.value is None, code
    gap = [r for r in lender_rows() if not (r.metric == M.TOTAL_BORROWINGS
                                            and r.period == pit(2025))]
    _d, broken = analytics(gap, Industry.NBFC)
    assert metric(broken, "COST_OF_BORROWINGS").status.value != "CALCULATED"  # H


def test_j_borrowings_to_equity_uses_reported_borrowings_on_one_basis():
    data, box5 = analytics(lender_rows(), Industry.NBFC)
    m = metric(box5, "BORROWINGS_TO_EQUITY")
    borrowings = next(v.value for v in data.canonical_values
                      if v.metric == M.TOTAL_BORROWINGS and v.period.period_end == FY26)
    shareholders = next(x for x in box5.all_metrics() if x.code == "SHAREHOLDERS_EQUITY"
                        and x.period.period_end == FY26)
    assert m.value == (borrowings / shareholders.value).quantize(m.value)
    assert {i.reporting_basis for i in m.provenance} == {B.CONSOLIDATED}
    assert {i.metric for i in m.provenance} >= {M.TOTAL_BORROWINGS}


def test_k_l_total_income_ratios_never_substitute_revenue():
    _data, box5 = analytics(company(), Industry.NBFC)  # revenue, but no TOTAL_INCOME
    for code in ("FINANCE_COST_TO_TOTAL_INCOME", "PAT_TO_TOTAL_INCOME"):
        assert metric(box5, code).status.value == "INPUT_MISSING", code
    _d, ok = analytics(lender_rows(), Industry.NBFC)
    for code in ("FINANCE_COST_TO_TOTAL_INCOME", "PAT_TO_TOTAL_INCOME"):
        tops = {i.metric for i in metric(ok, code).provenance} & {
            M.TOTAL_INCOME, M.REVENUE, M.REVENUE_FROM_OPERATIONS}
        assert tops == {M.TOTAL_INCOME}, code


def test_m_cross_basis_inputs_never_combine():
    rows = [r for r in lender_rows() if r.metric != M.TOTAL_BORROWINGS]
    rows += [V(M.TOTAL_BORROWINGS, pit(y), 300, basis=B.STANDALONE) for y in range(2021, 2027)]
    _d, box5 = analytics(rows, Industry.NBFC)
    for code in ("BORROWINGS_TO_EQUITY", "COST_OF_BORROWINGS", "BORROWINGS_GROWTH_YOY"):
        assert metric(box5, code).status.value != "CALCULATED", code


def test_n_an_invalid_canonical_input_makes_dependents_unavailable():
    data = make(lender_rows(), Industry.NBFC)
    target = next(v for v in data.canonical_values
                  if v.metric == M.TOTAL_BORROWINGS and v.period.period_end == FY26)
    bad = data.model_copy(update={"observations": tuple(
        o.model_copy(update={"warnings": ("SPLIT_NUMBER_SUSPECT",)})
        if o.observation_id == target.selected_observation_id else o
        for o in data.observations)})
    box5 = build_financial_analytics(bad, generated_at=NOW)
    for code in ("BORROWINGS_TO_EQUITY", "COST_OF_BORROWINGS", "BORROWINGS_GROWTH_YOY"):
        m = metric(box5, code)  # the engine's convention: INPUT_MISSING + the exact reason
        assert m.status.value != "CALCULATED" and m.value is None, code
        assert any("extraction integrity rejected" in n for n in m.missing_inputs), code


# --- O / P / Q: Box 7, Box 9, Box 13 -----------------------------------------------------------


def test_o_box7_lender_kpis_for_any_financial_bank_kpis_for_banks_only():
    fin = SectorFamily.FINANCIAL
    assert match_kpi("gross npa", fin, KPIUnit.PERCENT).definition.code == "GROSS_NPA"
    assert match_kpi("aum", fin, KPIUnit.MONEY).definition.code == "AUM"
    assert match_kpi("crar", fin, KPIUnit.PERCENT).definition.code == "CAPITAL_ADEQUACY"
    for alias in ("casa", "cet1 ratio", "atms"):  # bank-only: never for a non-bank
        assert match_kpi(alias, fin, None).reason == "SECTOR_NOT_APPLICABLE", alias
    assert match_kpi("casa", fin, KPIUnit.PERCENT, True).definition.code == "CASA_RATIO"
    for family in (SectorFamily.UNKNOWN, SectorFamily.GENERAL):
        assert match_kpi("gross npa", family, KPIUnit.PERCENT).definition is None


def _layers(key, rows, industry, cls=None):
    data, box5 = analytics(rows, industry, cls)
    data = data.model_copy(update={"company_key": key})
    return CompanyLayers(key, box4=data, box5=build_financial_analytics(data, generated_at=NOW))


def test_p_box9_compares_the_same_lender_metric_only():
    target = _layers("screener:1", lender_rows(), Industry.UNKNOWN, BusinessClass.FINANCIAL)
    lender_peer = _layers("screener:2", lender_rows(), Industry.NBFC)
    bank_peer = _layers("screener:3", lender_rows(), Industry.BANK)
    benchmarks, _blocked, _w = financial_benchmarks(target, [lender_peer, bank_peer],
                                                    "FINANCIAL", 1)
    cost = next(b for b in benchmarks if b.metric_code == "COST_OF_BORROWINGS")
    by_peer = {o.company_key: o.status.value for o in cost.peers}
    assert by_peer == {"screener:2": "COMPARABLE", "screener:3": "NOT_CALCULATED"}
    assert cost.statistics.peer_count == 1  # the bank is never substituted


def test_q_box13_lender_signals_carry_provenance_and_only_for_lenders():
    data, box5 = analytics(lender_rows(), Industry.NBFC)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW)
    found = {s.code: s for s in box13.signals if s.code in LENDER_SIGNALS}
    assert set(found) == LENDER_SIGNALS
    metric_ids = {m.metric_id for m in box5.all_metrics()}
    for s in found.values():
        if s.status.value == "EVALUATED":
            assert s.record_ids and set(s.record_ids) & metric_ids
    for industry in (Industry.BANK, Industry.GENERAL, Industry.UNKNOWN):
        d, b = analytics(lender_rows(), industry)
        other = build_due_diligence(d, b, request=DueDiligenceRequest(
            as_of=NOW.date(), company_key=d.company_key), generated_at=NOW)
        assert not {s.code for s in other.signals} & LENDER_SIGNALS, industry


# --- R / S / T: HUDCO exact, YESBANK, INFY -----------------------------------------------------


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
    box5 = build_financial_analytics(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=KEY), generated_at=NOW)
    report = build_final_report(request=FinalReportRequest(as_of=NOW.date(), company_key=KEY),
                                generated_at=NOW, box4=data, box5=box5, box13=box13)
    return data, box5, box13, report


def test_r_hudco_exact_lender_analytics(hudco):
    data, box5, box13, report = hudco
    assert (data.industry, data.classification) == (Industry.UNKNOWN, BusinessClass.FINANCIAL)
    expected = {"BORROWINGS_TO_EQUITY": "6.446926", "COST_OF_BORROWINGS": "7.173994",
                "FINANCE_COST_TO_TOTAL_INCOME": "67.011352", "PAT_TO_TOTAL_INCOME": "30.269083",
                "BORROWINGS_GROWTH_YOY": "32.041902", "BORROWINGS_TO_EQUITY_CHANGE_YOY":
                "0.475332", "ASSETS_VS_EQUITY_GROWTH": "7.532615",
                "ROA": "2.731822", "ROE": "20.198379"}
    for code, value in expected.items():
        assert metric(box5, code).value == D(value), code
    cost = metric(box5, "COST_OF_BORROWINGS")
    inputs = {(i.metric, i.period.period_end, i.value) for i in cost.provenance}
    assert (M.FINANCE_COST, FY26, D("8930.69")) in inputs  # the filing's finance cost
    assert {(M.TOTAL_BORROWINGS, FY26, D(141677)),
            (M.TOTAL_BORROWINGS, date(2025, 3, 31), D(107297))} <= inputs
    for code in ("EBITDA_MARGIN", "DEBT_TO_EQUITY", "INTEREST_COVERAGE"):  # industrial
        assert metric(box5, code).status.value == "UNSUPPORTED_SECTOR", code
    for code in ("COST_TO_INCOME", "FINANCING_PROFIT_MARGIN"):  # bank-only
        assert metric(box5, code).status.value == "UNSUPPORTED_SECTOR", code
    signals = {s.code: s for s in box13.signals}
    assert (signals["RISING_LENDER_LEVERAGE"].status.value,
            signals["RISING_LENDER_LEVERAGE"].severity.value) == ("EVALUATED", "YELLOW")
    assert signals["BALANCE_SHEET_OUTGROWING_EQUITY"].severity.value == "YELLOW"
    # only 2 filing years of finance cost: FY2024 cost of borrowings cannot be calculated
    assert signals["RISING_COST_OF_BORROWINGS"].status.value == "UNAVAILABLE"
    table = next(t for t in report.tables if t.code == "LENDER_ANALYTICS")
    claims = {c.claim_id: c for c in report.claims}
    shown = {claims[c.claim_id].metric for r in table.rows for c in r.cells if c.claim_id}
    assert {"BORROWINGS_TO_EQUITY", "COST_OF_BORROWINGS", "PAT_TO_TOTAL_INCOME"} <= shown


def _company(case):
    from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
    with tempfile.TemporaryDirectory() as t:
        ds = build_screener_dataset(*asyncio.run(page(Path(t), SCREENER, case)), None)
    data = build_canonical_dataset(company_key=ds.company_key, company_name=ds.profile.name,
                                   screeners=(ds,), bundle=None, catalogue=None,
                                   generated_at=NOW)
    box5 = build_financial_analytics(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW)
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box4=data,
        box5=box5, box13=box13)
    return data, box5, box13, report


@pytest.mark.parametrize("case,industry,basis,roe", [
    (YESBANK, Industry.BANK, B.CONSOLIDATED, "7.095595"),  # S
    (INFY, Industry.GENERAL, B.STANDALONE, "34.732411"),  # T
])
def test_s_t_bank_and_industrial_are_unchanged(case, industry, basis, roe):
    data, box5, box13, report = _company(case)
    assert data.industry == industry
    assert metric(box5, "ROE", basis=basis).value == D(roe)  # the P0 retained-control value
    for code in LENDER:
        assert status(box5, code) <= {"UNSUPPORTED_SECTOR"}, code
    assert not {s.code for s in box13.signals} & LENDER_SIGNALS
    assert not any(t.code == "LENDER_ANALYTICS" for t in report.tables)
