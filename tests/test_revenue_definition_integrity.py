"""Revenue / income definition integrity: numeric agreement is not semantic agreement.

HUDCO FY2026 (2026-10-18 audit), INR crore:
  13,150.40  filing 'Total Revenue from Operations (A)' (consolidated P&L p.364 r5)
  13,327.13  filing 'Total Income I (A+B)' = 13,150.40 + 'B Other Income' 176.73
  13,150     Screener consolidated 'Revenue' and 13,294 Screener standalone 'Revenue':
             Screener's lender (financing) format, Revenue - Interest - Expenses = Financing
             Profit; Screener's own split of total income (13,150 + 177 = 13,294 + 33 =
             13,327), not the filing's revenue from operations (FY2025: 10,345 vs 10,311.29).

Contract: revenue from operations, total income, a reported / provider 'Revenue' and
interest income are different concepts; a frame, a table row, a peer comparison and a
standalone / consolidated comparison each use ONE definition.

  A/H same concept reconciles (rounding)   B/C/I different concepts stay distinct
  D/E provider semantics by statement     F/G authority never beats semantics
  J/K/M one top line per frame            L peers   N/O/P/Q surfaces + HUDCO   R non-financial
"""

import asyncio
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_financial_analytics import B, M, V, company, fy, make
from test_financial_cell_integrity import KEY, NOW, filing_observations, reconcile
from test_industry_intelligence import IT, PEER_ROWS, layers, screener
from test_screener_dataset import page
from test_summary_input import INFY

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import financial_reconciliation as fr
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_parsing import map_filing_label
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.industry_intelligence import build_industry_intelligence
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import (
    FinancialLimits,
    Industry,
    Outcome,
    SourceType,
    StatementType,
)

PL = StatementType.PROFIT_AND_LOSS
FY26 = date(2026, 3, 31)
BASIS_FIXTURES = Path(__file__).parent / "fixtures" / "reporting_basis"
SCREENER_FIXTURES = Path(__file__).parent / "fixtures" / "screener"
HUDCO_PAGES = (("hudco_consolidated.html", "https://www.screener.in/company/HUDCO/consolidated/"),
               ("hudco_standalone.html", "https://www.screener.in/company/HUDCO/"))
TOP = {M.REVENUE, M.REVENUE_FROM_OPERATIONS, M.TOTAL_INCOME, M.OTHER_INCOME}


def obs(metric, value, *, primary: bool, basis=B.CONSOLIDATED, period=None, tag=""):
    o = make([V(metric, period or fy(2026), value, basis=basis)]).observations[0]
    return o.model_copy(update={
        "observation_id": uuid5(NAMESPACE_URL, f"{tag}|{metric}|{value}|{basis}|{primary}"),
        "company_key": KEY,
        "authority": SourceAuthority.PRIMARY_REGULATORY if primary
        else SourceAuthority.SECONDARY_AGGREGATOR,
        "source": o.source.model_copy(update={
            "source_type": SourceType.FILING if primary else SourceType.SCREENER}),
    })


def canonical(data, metric, basis=B.CONSOLIDATED, end=FY26):
    found = [v for v in data.canonical_values if v.metric == metric
             and v.reporting_basis == basis and v.period.period_end == end
             and v.period.period_type.value == "FY"]
    assert len(found) <= 1
    return found[0] if found else None


async def _screener(tmp: Path, fixtures: Path, case):
    tmp.mkdir(parents=True, exist_ok=True)
    return build_screener_dataset(*(await page(tmp, fixtures, case)), None)


def screener_obs(ds, industry):
    c = fr._Collector(KEY, FinancialLimits())
    fr.screener_observations(ds.model_copy(update={"company_key": KEY}), c, industry, 3)
    return c.observations


@pytest.fixture(scope="module")
def hudco_pages(tmp_path_factory):
    root = tmp_path_factory.mktemp("hudco")
    return [asyncio.run(_screener(root / str(i), BASIS_FIXTURES, case))
            for i, case in enumerate(HUDCO_PAGES)]


@pytest.fixture(scope="module")
def hudco(hudco_pages):
    observations = list(filing_observations())
    for ds in hudco_pages:
        observations += screener_obs(ds, Industry.UNKNOWN)
    data = reconcile(observations)
    box5 = build_financial_analytics(data, generated_at=NOW)
    box10 = build_forecast(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=KEY), generated_at=NOW, box10=box10)
    report = build_final_report(request=FinalReportRequest(as_of=NOW.date(), company_key=KEY),
                                generated_at=NOW, box4=data, box5=box5, box13=box13,
                                box10=box10)
    return observations, data, box5, box10, box13, report


# --- A / H: the same concept reconciles normally ------------------------------------------------


def test_a_h_same_concept_different_sources_reconcile_within_rounding():
    data = reconcile([obs(M.REVENUE_FROM_OPERATIONS, "13150.40", primary=True),
                      obs(M.REVENUE_FROM_OPERATIONS, "13150", primary=False)])
    value = canonical(data, M.REVENUE_FROM_OPERATIONS)
    assert value.outcome == Outcome.MATCH_WITHIN_TOLERANCE
    assert value.value == D("13150.40")  # the exact primary value, precision kept
    assert len(value.supporting_observation_ids) == 2


# --- B / C / I: different concepts stay distinct -------------------------------------------------


@pytest.mark.parametrize("label,metric", [
    ("Total Revenue from Operations (A)", M.REVENUE_FROM_OPERATIONS),
    ("I Revenue from operations", M.REVENUE_FROM_OPERATIONS),
    ("B Other Income", M.OTHER_INCOME),
    ("Total Income I (A+B)", M.TOTAL_INCOME),
    ("Revenue from operations", M.REVENUE_FROM_OPERATIONS),  # existing aliases unchanged
    ("Total income", M.TOTAL_INCOME),
])
def test_b_schedule_iii_top_line_rows_keep_their_own_concept(label, metric):
    assert map_filing_label(PL, label, [], False) == metric


@pytest.mark.parametrize("label", ["(a) Interest Income", "Interest Income", "A Interest Income",
                                   "(b) Dividend Income 41(8)", "C Fees (A)", "Total (A+B)"])
def test_c_interest_income_and_components_are_never_revenue(label):
    assert map_filing_label(PL, label, [], False) not in TOP


def test_b_i_revenue_from_operations_total_income_and_provider_revenue_never_reconcile():
    data = reconcile([obs(M.REVENUE_FROM_OPERATIONS, "13150.40", primary=True),
                      obs(M.TOTAL_INCOME, "13327.13", primary=True),
                      obs(M.REVENUE, "13294", primary=False, basis=B.STANDALONE)])
    assert canonical(data, M.REVENUE_FROM_OPERATIONS).value == D("13150.40")
    assert canonical(data, M.TOTAL_INCOME).value == D("13327.13")
    assert canonical(data, M.REVENUE, B.STANDALONE).value == D("13294")
    for record in data.reconciliation:  # no record ever joins two concepts
        ids = {*record.supporting_observation_ids, *record.conflicting_observation_ids}
        assert len({o.metric for o in data.observations if o.observation_id in ids}) <= 1


# --- D / E: provider semantics by statement -------------------------------------------------------


def test_d_ordinary_company_sales_mapping_is_unchanged(tmp_path):
    ds = asyncio.run(_screener(tmp_path, SCREENER_FIXTURES, INFY))
    assert not fr.financing_format(ds)
    rows = screener_obs(ds, Industry.GENERAL)
    sales = [o for o in rows if o.raw_label.startswith("Sales") and o.period.period_end == FY26]
    assert sales and all(o.metric == M.REVENUE_FROM_OPERATIONS and not o.warnings for o in sales)
    other = [o for o in rows if o.raw_label.startswith("Other Income")]
    assert other and all(o.metric == M.OTHER_INCOME for o in other)


@pytest.mark.parametrize("industry", [Industry.UNKNOWN, Industry.GENERAL])
def test_e_financing_format_revenue_is_the_providers_own_split(hudco_pages, industry):
    # GENERAL too: a misclassified lender page never becomes revenue from operations
    for ds in hudco_pages:
        assert fr.financing_format(ds)
        rows = screener_obs(ds, industry)
        revenue = [o for o in rows if o.raw_label.startswith("Revenue")]
        assert revenue and all(o.metric == M.REVENUE and fr.PROVIDER_DEFINED_TOPLINE
                               in o.warnings for o in revenue)
        assert all(o.metric is None for o in rows if o.raw_label.startswith("Other Income"))
        assert not any(o.metric == M.REVENUE_FROM_OPERATIONS for o in rows)


# --- F / G: authority never beats semantics -------------------------------------------------------


def test_f_high_authority_other_concept_never_replaces_the_requested_concept(hudco_pages):
    # the primary filing states TOTAL INCOME only; the secondary states revenue from operations
    data = reconcile([obs(M.TOTAL_INCOME, "13327.13", primary=True),
                      obs(M.REVENUE_FROM_OPERATIONS, "13150", primary=False)])
    value = canonical(data, M.REVENUE_FROM_OPERATIONS)
    assert value.value == D("13150") and value.outcome == Outcome.SINGLE_SOURCE
    # and a GENERAL-classified lender page never contests the filing's revenue from operations
    rows = [*screener_obs(hudco_pages[0], Industry.GENERAL),
            obs(M.REVENUE_FROM_OPERATIONS, "13150.40", primary=True, tag="filing")]
    mixed = reconcile(rows)
    assert canonical(mixed, M.REVENUE_FROM_OPERATIONS).outcome == Outcome.SINGLE_SOURCE
    assert canonical(mixed, M.REVENUE).value == D("13150")


def test_g_same_concept_high_authority_keeps_precedence():
    data = reconcile([obs(M.REVENUE_FROM_OPERATIONS, "13150.40", primary=True),
                      obs(M.REVENUE_FROM_OPERATIONS, "13100", primary=False)])
    value = canonical(data, M.REVENUE_FROM_OPERATIONS)
    assert value.outcome == Outcome.SOURCE_PRECEDENCE and value.value == D("13150.40")


# --- J / K / M: one top-line definition per frame -------------------------------------------------


def _two_definitions():
    """A reported 'Revenue' for FY2021-FY2026 and revenue from operations for FY2025-26 only."""
    rows = [V(M.REVENUE, r.period, r.value) if r.metric == M.REVENUE_FROM_OPERATIONS else r
            for r in company()]
    rows += [V(M.REVENUE_FROM_OPERATIONS, fy(2025), 1390), V(M.REVENUE_FROM_OPERATIONS,
                                                              fy(2026), 1490)]
    return make(rows)


def _topline(box5, m):
    return {i.metric for i in m.provenance if i.metric in (M.REVENUE, M.REVENUE_FROM_OPERATIONS)}


def test_j_growth_and_cagr_never_splice_two_definitions():
    data = _two_definitions()
    box5 = build_financial_analytics(data, generated_at=NOW)
    used = set()
    for m in box5.all_metrics():
        if m.status.value == "CALCULATED" and (bases := _topline(box5, m)):
            assert len(bases) == 1, m.code
            used |= bases
    assert used == {M.REVENUE}  # the definition with most periods, in every period
    growth = next(m for m in box5.all_metrics() if m.code == "REVENUE_GROWTH_YOY"
                  and m.period.period_end == FY26 and m.reporting_basis == B.CONSOLIDATED)
    assert growth.value == D("7.142857")  # 1500 / 1400 on the reported 'Revenue'


def test_k_margin_numerator_and_denominator_use_the_frame_definition():
    box5 = build_financial_analytics(_two_definitions(), generated_at=NOW)
    for code in ("PAT_MARGIN", "EMPLOYEE_COST_TO_REVENUE", "OPERATING_MARGIN"):
        for m in box5.all_metrics():
            if m.code == code and m.status.value == "CALCULATED":
                assert _topline(box5, m) == {M.REVENUE}, code


def test_m_forecast_history_uses_one_definition():
    data = _two_definitions()
    f = build_forecast(data, generated_at=NOW)
    metrics = {v.actual.metric for v in f.actuals if v.line.value == "REVENUE" and v.actual}
    assert metrics == {M.REVENUE}


# --- L: peers -------------------------------------------------------------------------------------


def test_l_peers_compare_only_the_same_top_line_definition():
    reported = [V(M.REVENUE, r.period, r.value) if r.metric == M.REVENUE_FROM_OPERATIONS else r
                for r in company()]
    data = make(reported).model_copy(update={"company_key": "screener:1489"})
    target = CompanyLayers("screener:1489", screener("1489", "Widget Co", "WIDGET", IT,
                                                     PEER_ROWS), data,
                           build_financial_analytics(data, generated_at=NOW))
    box9 = build_industry_intelligence(
        target=target, peers=[layers("screener:2001", "Rival Systems", "RIVAL", IT)],
        generated_at=NOW)
    growth = next(b for b in box9.financial_benchmarks if b.metric_code == "REVENUE_GROWTH_YOY")
    rival = next(o for o in growth.peers if o.company_key == "screener:2001")
    assert rival.status.value == "DEFINITION_MISMATCH"
    assert "REVENUE_FROM_OPERATIONS vs REVENUE" in (rival.detail or "")
    assert growth.statistics.peer_count == 0
    pat = next(b for b in box9.financial_benchmarks if b.metric_code == "PAT_GROWTH_YOY")
    assert any(o.status.value == "COMPARABLE" for o in pat.peers)  # no top line: unchanged


# --- N / O / P / Q: HUDCO, exact ------------------------------------------------------------------


def test_q_hudco_every_top_line_value_keeps_its_own_concept(hudco):
    observations, data, *_ = hudco
    by_raw = {(o.raw_label, o.raw_value, o.reporting_basis): o for o in observations
              if o.period.period_end == FY26 and o.period.period_type.value == "FY"}
    expected = {
        ("Total Revenue from Operations (A)", "13,150.40", B.CONSOLIDATED):
            M.REVENUE_FROM_OPERATIONS,
        ("B Other Income", "176.73", B.CONSOLIDATED): M.OTHER_INCOME,
        ("Total Income I (A+B)", "13,327.13", B.CONSOLIDATED): M.TOTAL_INCOME,
        ("(a) Interest Income", "13,096.25", B.CONSOLIDATED): None,
        ("Revenue +", "13,150", B.CONSOLIDATED): M.REVENUE,
        ("Revenue +", "13,294", B.STANDALONE): M.REVENUE,
        ("Other Income +", "177", B.CONSOLIDATED): None,
        ("Other Income +", "33", B.STANDALONE): None,
    }
    for key, metric in expected.items():
        assert by_raw[key].metric == metric, key
    assert canonical(data, M.REVENUE_FROM_OPERATIONS).value == D("13150.40")
    assert canonical(data, M.TOTAL_INCOME).value == D("13327.13")
    assert canonical(data, M.OTHER_INCOME).value == D("176.73")
    assert canonical(data, M.REVENUE).value == D("13150")
    assert canonical(data, M.REVENUE, B.STANDALONE).value == D("13294")
    assert canonical(data, M.OTHER_INCOME, B.STANDALONE) is None  # never the provider residual


def test_q_hudco_analytics_use_one_definition_and_no_basis_story(hudco):
    _obs, _data, box5, box10, box13, _report = hudco
    for m in box5.all_metrics():
        if m.status.value == "CALCULATED" and (t := _topline(box5, m)):
            assert t == {M.REVENUE}, m.code  # Screener's full-history 'Revenue', every year
    assert {v.actual.metric for v in box10.actuals if v.line.value == "REVENUE" and v.actual} \
        == {M.REVENUE}
    revenue = [c for c in box13.standalone_consolidated_reconciliation if c.metric == "REVENUE"]
    assert revenue and all(c.status.value in ("NOT_COMPARABLE", "UNAVAILABLE") for c in revenue)
    latest = next(c for c in revenue if c.period == "FY2026")
    # BEFORE: 'REVENUE consolidated minus standalone -144: immaterial' (a consolidation story)
    assert latest.reason == fr.PROVIDER_DEFINED_TOPLINE and latest.absolute_difference is None


def test_o_p_hudco_report_rows_and_claims_keep_definitions_apart(hudco):
    *_, report = hudco
    claims = {c.claim_id: c for c in report.claims}
    for table in report.tables:
        for row in table.rows:
            metrics = {claims[c.claim_id].metric for c in row.cells
                       if c.claim_id is not None and claims[c.claim_id].metric in
                       {"REVENUE", "REVENUE_FROM_OPERATIONS", "TOTAL_INCOME", "OTHER_INCOME"}}
            assert len(metrics) <= 1, (table.code, row.key, metrics)
    annual = next(t for t in report.tables if t.code == "ANNUAL_FINANCIALS")
    rows = {r.key: r for r in annual.rows}
    assert rows["REVENUE"].label == "Revenue"
    assert rows["REVENUE__REVENUE_FROM_OPERATIONS"].label == "Revenue (revenue from operations)"
    shown = {(c.metric, c.basis.value, c.value) for c in report.claims if c.value is not None
             and c.period_end == FY26 and c.metric in ("REVENUE", "REVENUE_FROM_OPERATIONS")
             and "Q" not in (c.period or "")}
    assert {("REVENUE_FROM_OPERATIONS", "CONSOLIDATED", D("13150.40")),
            ("REVENUE", "CONSOLIDATED", D("13150")),
            ("REVENUE", "STANDALONE", D("13294"))} <= shown
    for entry in report.risks.risk_register:  # Negatives: no top-line basis 'difference'
        assert "consolidated minus standalone" not in claims[entry.claim_id].statement


def test_n_hudco_summary_revenue_is_the_providers_own_row(tmp_path):
    extraction, _ = asyncio.run(page(tmp_path, BASIS_FIXTURES, HUDCO_PAGES[0]))
    summary = project_summary_input(extraction)
    row = next(r for r in summary.annual.rows if r.metric_id == "revenue")
    assert row.label == "Revenue"  # Screener's label, verbatim - not 'revenue from operations'
    assert row.reporting_scope.value == "consolidated"
    latest = next(v for v in row.values if v.period == "Mar 2026")
    source = next(o for o in extraction.profit_loss if o.id == latest.source_observation_id)
    assert source.metric == "source:Revenue" and latest.value == D("13150")
