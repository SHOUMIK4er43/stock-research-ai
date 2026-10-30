"""Financial-business P0: synthetic applicability controls plus retained INFY/YESBANK.

No new live/retained NBFC, HFC or insurance evidence is claimed. Synthetic inputs
intentionally contain industrial-looking figures to prove those cannot leak into claims.
Existing independently checked golden files are not changed.
"""
from datetime import date

import pytest
from test_due_diligence import NOW, run
from test_financial_analytics import B, M, V, analyse, company, fy, make, metric, pit
from test_screener_dataset import dataset
from test_summary_input import INFY, YESBANK, replay

from stock_research_ai.application.business_registry import sector_family
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_reconciliation import (
    build_canonical_dataset,
    detect_industry,
)
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.fundamental_valuation import build_valuation
from stock_research_ai.application.red_flags import FORMULAS, RULES
from stock_research_ai.application.report_html import render_html
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.application.summary_generation import build_summary_claims
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.financial_analytics import CalculationStatus as CS
from stock_research_ai.domain.financial_business import (
    RESTRICTED_INDUSTRIES,
    classify_business,
    declaration_compatible,
)
from stock_research_ai.domain.financial_canonical import BusinessClass, Industry
from stock_research_ai.domain.forecasting import ForecastLine, OperatingRoute
from stock_research_ai.domain.screener_dataset import ClassificationLevel

IND = Industry
CASES = [
    ("Banks", "Commercial bank offering deposits and advances", ["Revenue", "Deposits",
       "Advances", "Interest Income"], IND.BANK),
    ("Non-Banking Financial Company", "Loans and financing income; no banking deposits",
       ["Revenue", "Financing Income", "Loans"], IND.NBFC),
    ("Housing Finance", "Housing loans", ["Revenue", "Interest", "Financing Profit"], IND.NBFC),
    ("Life Insurance", "Premiums and claims", ["Premium Income", "Claims"], IND.INSURANCE),
    ("General Insurance", "Underwriting", ["Gross Written Premium", "Claims"], IND.INSURANCE),
    ("Manufacturing", "Manufactures equipment", ["Sales", "EBITDA", "Inventory"], IND.GENERAL),
    ("IT - Software", "Software services for banking and insurance customers",
       ["Sales", "Operating Profit"], IND.GENERAL),
    ("Financial Services", "Diversified financial business", ["Revenue"], IND.UNKNOWN),
]


@pytest.mark.parametrize("name,about,labels,expected", CASES)
def test_business_contract(name, about, labels, expected):
    assert classify_business([name], description=about, labels=labels) == expected


@pytest.mark.parametrize("labels", [
    ["Financing Profit"], ["Deposits"], ["Interest Income"], ["AUM"], ["NIM"], ["GNPA"],
    ["Solvency Ratio"], ["Loan Book"], ["Deposits", "Advances", "Financing Profit"], [],
])
def test_one_financial_hint_never_grants_bank_or_industrial_rules(labels):
    assert classify_business(labels=labels) == IND.UNKNOWN


@pytest.mark.parametrize("names,labels", [
    (["Banks", "Housing Finance"], []), (["Insurance", "Banks"], []),
    (["Banks"], ["Gross Written Premium", "Claims"]),
    (["Housing Finance"], ["Premium Income", "Claims"]),
])
def test_conflicts_are_unknown(names, labels):
    assert classify_business(names, labels=labels) == IND.UNKNOWN


def test_structural_insurance_and_corroborated_descriptions():
    assert classify_business(labels=["Gross Written Premium", "Claims"]) == IND.INSURANCE
    assert classify_business(description="A registered NBFC providing loans",
                             labels=["Interest Income", "Loan Book"]) == IND.NBFC
    assert classify_business(description="A commercial bank",
                             labels=["Deposits", "Advances", "Interest Income"]) == IND.BANK
    assert classify_business(
        ["Housing Finance"], labels=["Deposits", "Financing Profit"]) == IND.NBFC
    assert classify_business(["  NON–BANKING FINANCIAL COMPANY  "]) == IND.NBFC


@pytest.mark.parametrize("name,about,labels,expected", CASES)
async def test_box4_uses_profile_and_statement_evidence(tmp_path, fixture_dir,
                                                      name, about, labels, expected):
    base = await dataset(tmp_path, fixture_dir, INFY)
    profile = base.profile.model_copy(update={
        "classification": (ClassificationLevel(level="Industry", name=name,
                            url="https://www.screener.in/"),), "about": about, "key_points": None})
    table = base.tables[0].model_copy(update={"rows": tuple(
        base.tables[0].rows[0].model_copy(update={
            "label": label, "cells": tuple(c.model_copy(update={
                "locator": f"{c.locator}/synthetic/{i}"}) for c in base.tables[0].rows[0].cells)
        }) for i, label in enumerate(labels))})
    synthetic = base.model_copy(update={"profile": profile, "tables": (table,)})
    result = build_canonical_dataset(company_key=base.company_key, company_name=base.profile.name,
                                     screeners=[synthetic], bundle=None, catalogue=None,
                                     generated_at=NOW)
    assert result.industry == expected
    assert all(v.reporting_basis == B.STANDALONE for v in result.canonical_values)
    if expected != IND.BANK:
        assert not any(v.metric == M.BANK_FINANCING_PROFIT for v in result.canonical_values)
    if expected == IND.UNKNOWN:
        assert "BUSINESS_CLASSIFICATION_UNRESOLVED" in {w.code for w in result.warnings}
    assert result.company_key == base.company_key


async def test_conflicting_scope_profiles_are_conservative(tmp_path, fixture_dir):
    base = await dataset(tmp_path, fixture_dir, YESBANK)
    other = base.model_copy(update={"profile": base.profile.model_copy(update={
        "classification": (ClassificationLevel(level="Industry", name="Insurance",
                            url="https://www.screener.in/"),)})})
    assert detect_industry([base, other]) == IND.UNKNOWN
    assert detect_industry([]) == IND.UNKNOWN


INDUSTRIAL = {
    "EBITDA", "EBITDA_MARGIN", "OPERATING_MARGIN", "ROCE", "ASSET_TURNOVER",
    "RECEIVABLE_DAYS", "INVENTORY_DAYS", "CFO_TO_EBITDA", "CFO_TO_PAT",
    "CASH_CONVERSION_CYCLE", "CASH_FLOW_ACCRUAL_RATIO", "NET_DEBT_TO_EBITDA",
    "INTEREST_COVERAGE", "OTHER_INCOME_TO_PBT", "DEBT_TO_EQUITY",
}


@pytest.mark.parametrize("industry", sorted(RESTRICTED_INDUSTRIES))
def test_restricted_metric_matrix_preserves_common_metrics_and_scopes(industry):
    data = analyse(company() + company(basis=B.STANDALONE), industry=industry)
    general = analyse(company() + company(basis=B.STANDALONE))
    for basis in (B.CONSOLIDATED, B.STANDALONE):
        for code in INDUSTRIAL:
            found = [m for m in data.all_metrics() if m.code == code and m.reporting_basis == basis]
            assert found, code
            assert all(m.value is None and m.status in
                       {CS.UNSUPPORTED_SECTOR, CS.NOT_APPLICABLE} for m in found), code
        for code in ("ROE", "ROA", "REVENUE_GROWTH_YOY", "PAT_GROWTH_YOY", "TAX_RATE"):
            current = metric(data, code, basis=basis)
            control = metric(general, code, basis=basis)
            assert current.status == CS.CALCULATED
            assert current.value == control.value
            assert current.period == control.period
        assert all(i.reporting_basis == m.reporting_basis
                   for m in data.all_metrics() for i in m.provenance)


@pytest.mark.parametrize("industry", list(IND))
def test_bank_specific_formulas_are_bank_only_even_with_numeric_inputs(industry):
    rows = company() + [V(M.DEPOSITS, pit(y), 1000 + 100 * (y - 2021))
                        for y in range(2021, 2027)]
    rows += [V(M.BANK_FINANCING_PROFIT, fy(y), 50) for y in range(2021, 2027)]
    result = analyse(rows, industry=industry)
    for code in ("DEPOSITS_GROWTH_YOY", "FINANCING_PROFIT_MARGIN"):
        m = metric(result, code)
        assert m.status == (CS.CALCULATED if industry == IND.BANK else CS.UNSUPPORTED_SECTOR)
        assert (m.value is not None) == (industry == IND.BANK)


@pytest.mark.parametrize("industry", sorted(RESTRICTED_INDUSTRIES))
def test_no_industrial_values_flags_or_claims_reach_boxes13_to15(industry):
    b4 = make(company(), industry)
    b5 = build_financial_analytics(b4, generated_at=NOW)
    d = run(pair=(b4, b5))
    for s in d.signals:
        # industrial (NON_FINANCIAL-only) rules; FINANCIAL-only lender rules may evaluate
        if tuple(g.value for g in RULES[s.code].sectors) == ("NON_FINANCIAL",):
            assert s.status.value == "UNSUPPORTED_SECTOR_RULE"
            assert s.severity is None and s.value is None
    for m in (*d.financial_forensics.metrics, *d.earnings_quality.adjusted):
        if m.code in FORMULAS and FORMULAS[m.code].applicability == "non-financial":
            assert m.status == "NOT_APPLICABLE" and m.value is None, m.code
    assert all(m.status == "NOT_APPLICABLE" and m.score is None
               for m in d.financial_forensics.models)
    r = build_final_report(request=FinalReportRequest(as_of=date(2026, 10, 10),
                         company_key=b4.company_key), generated_at=NOW, box4=b4, box5=b5, box13=d)
    forbidden = INDUSTRIAL | {k for k, v in FORMULAS.items()
                              if v.applicability == "non-financial"} | {"OPERATING_PROFIT"}
    assert not [c for c in r.claims if c.value is not None and c.metric in forbidden]
    presentation = build_presentation(r)
    assert render_html(presentation) and render_markdown(presentation)


@pytest.mark.parametrize("industry", sorted(RESTRICTED_INDUSTRIES))
def test_forecast_and_valuation_do_not_use_industrial_drivers(industry):
    import test_fundamental_valuation as tv
    data = make(tv.rows(), industry)
    forecast = build_forecast(data, generated_at=NOW)
    assert forecast.base.operating_route == OperatingRoute.UNAVAILABLE
    lender = forecast.base.forecast_model.value == "NON_BANK_FINANCIAL"
    allowed = {"TOTAL_ASSETS", "PAT", "EQUITY", "NON_EQUITY_FUNDING", "TOTAL_BORROWINGS",
               "FINANCE_COST", "TOTAL_INCOME"} if lender else {"REVENUE"}
    assert all(v.line.value in allowed for v in forecast.values)  # never an industrial line
    assert not {v.line for v in forecast.values} & {
        ForecastLine.EBITDA, ForecastLine.CAPEX, ForecastLine.TRADE_WORKING_CAPITAL,
        ForecastLine.GROSS_DEBT, ForecastLine.FCF_ESTIMATE}
    valuation = build_valuation(forecast, request=tv.request(
        methods=(tv.VM.DCF_FCFF_GORDON, tv.VM.EV_EBITDA, tv.VM.PE), scenarios=(tv.S.BASE,),
        multiples=[tv.multiple(tv.VM.EV_EBITDA, 8), tv.multiple(tv.VM.PE, 12)]),
        box4=data, generated_at=NOW)
    for method in (tv.VM.DCF_FCFF_GORDON, tv.VM.EV_EBITDA):
        result = tv.result(valuation, method)
        assert result.reason == "UNSUPPORTED_SECTOR" and result.enterprise_value is None
    # Box 7's family refines Box 4's ONE classification; it never re-guesses an UNKNOWN one
    # from breadcrumbs (formerly UNKNOWN + 'Manufacturing' became GENERAL)
    assert sector_family(industry, ["Manufacturing"])[0].value == (
        "UNKNOWN" if industry == IND.UNKNOWN else "FINANCIAL"
    )
    assert sector_family(IND.UNKNOWN, ["Financial Services"])[0].value == "UNKNOWN"
    assert sector_family(IND.UNKNOWN, ["Financial Services"],
                         BusinessClass.FINANCIAL)[0].value == "FINANCIAL"


@pytest.mark.parametrize("name,expected", [
    ("Housing Finance", IND.NBFC), ("Insurance", IND.INSURANCE),
    ("Financial Services", IND.UNKNOWN),
])
async def test_summary_acquires_classification_and_gates_claims(
    tmp_path, fixture_dir, name, expected
):
    html = (fixture_dir / INFY[0]).read_text()
    for old in ("Information Technology", "IT - Software", "Computers - Software &amp; Consulting"):
        html = html.replace(old, name)
    source = await replay(tmp_path, fixture_dir, INFY, html)
    assert source.business_classification == expected
    summary = project_summary_input(source)
    assert summary.business_classification == expected
    claims = build_summary_claims(summary)
    assert not {c.metric_id for c in claims} & {"roce", "operating_profit", "source_free_cashflow"}
    assert {"roe", "net_profit", "eps"} <= {c.metric_id for c in claims}
    # A caller cannot restore an inappropriate field into the grounded claim catalogue.
    general = project_summary_input(source.model_copy(
        update={"business_classification": IND.GENERAL}))
    forged = general.model_copy(update={"business_classification": expected})
    assert not {c.metric_id for c in build_summary_claims(forged)} & {"roce", "operating_profit"}
    assert not declaration_compatible(expected, "NON_FINANCIAL")
    assert not declaration_compatible(expected, "BANK")


@pytest.mark.parametrize("case,expected", [(INFY, IND.GENERAL), (YESBANK, IND.BANK)])
async def test_retained_classification_agrees_across_paths(tmp_path, fixture_dir, case, expected):
    source = await replay(tmp_path, fixture_dir, case)
    (tmp_path / "detail").mkdir()
    detail = await dataset(tmp_path / "detail", fixture_dir, case)
    assert source.business_classification == detect_industry([detail]) == expected
    summary = project_summary_input(source)
    claims = build_summary_claims(summary)
    if expected == IND.GENERAL:
        assert {"roce", "operating_profit"} <= {c.metric_id for c in claims}
    else:
        assert "roce" not in {c.metric_id for c in claims}
        assert "financing_profit" in {c.metric_id for c in claims}
    observations = {o.id: o for group in (source.top_metrics, source.profit_loss,
                    source.quarterly, source.balance_sheet, source.cashflow) for o in group}
    # a market price has no reporting basis; every scoped input must match the claim's basis
    assert all(c.reporting_scope == observations[oid].reporting_scope
               for c in claims for oid in c.source_observation_ids
               if observations[oid].metric_id != "current_price")
    assert all(observations[oid].reporting_scope is ReportingScope.UNKNOWN
               for c in claims for oid in c.source_observation_ids
               if observations[oid].metric_id == "current_price")


@pytest.mark.parametrize("name", ["Holding Companies", "Capital Markets", "Mutual Funds",
                                  "Finance - Others", "Insurance Brokers", "Unclassified"])
def test_unsupported_financial_and_absent_models_do_not_default_to_general(name):
    assert classify_business([name]) == IND.UNKNOWN


@pytest.mark.parametrize("description", ["The company is engaged in financing vehicles",
    "The company provides loans for housing", "The company has lending operations"])
def test_ambiguous_financing_description_blocks_industrial_looking_rows(description):
    assert classify_business(description=description,
                             labels=["Sales", "Operating Profit"]) == IND.UNKNOWN
