"""DETAILED_RESEARCH_V1 Box 5: deterministic financial analytics & forensics (offline)."""

import ast
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError
from test_screener_dataset import dataset as screener_dataset
from test_summary_input import INFY, YESBANK

from stock_research_ai.application import (
    financial_analytics,
    financial_forensics,
    financial_metrics,
)
from stock_research_ai.application.financial_analytics import (
    build_financial_analytics,
    canonical_analytics_json,
    classify_trend,
)
from stock_research_ai.application.financial_metrics import cagr, compare_periods, growth
from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
from stock_research_ai.domain import financial_analytics as analytics_domain
from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.financial_analytics import (
    AnalyticsLimits,
    CalculationStatus,
    ComponentStatus,
    DerivedMetric,
    FinancialAnalyticsDataset,
    ForensicModel,
    HealthDimension,
    HealthState,
    MetricCategory,
    PeriodRole,
    SignalStatus,
    TrendClass,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialLimits,
    FinancialPeriod,
    FinancialStatistics,
    Industry,
    NumberStatus,
    Outcome,
    ParsedNumber,
    PeriodType,
    ReconciliationRecord,
    ReportingBasis,
    Scale,
    SourceFinancialObservation,
    SourceReference,
    SourceType,
    StatementType,
    TolerancePolicy,
    ValueKind,
)

M = CanonicalMetric
B = ReportingBasis
S = CalculationStatus
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
NS = UUID("0e4c1f8a-2b3d-4c5e-8f9a-1b2c3d4e5f6a")
FY26, FY25, FY24, FY21 = date(2026, 3, 31), date(2025, 3, 31), date(2024, 3, 31), date(2021, 3, 31)


# --- a compact Box 4 dataset factory -----------------------------------------------------------


def fy(year: int) -> FinancialPeriod:
    return FinancialPeriod(
        period_type=PeriodType.FY,
        label=f"Mar {year}",
        period_start=date(year - 1, 4, 1),
        period_end=date(year, 3, 31),
        duration_months=12,
        fiscal_year=year,
    )


def pit(year: int) -> FinancialPeriod:
    return FinancialPeriod(
        period_type=PeriodType.POINT_IN_TIME,
        label=f"Mar {year}",
        period_end=date(year, 3, 31),
        fiscal_year=year,
    )


def quarter(end: date, months: int = 3, ptype: PeriodType = PeriodType.QUARTER) -> FinancialPeriod:
    return FinancialPeriod(
        period_type=ptype, label=f"{ptype.value} {end}", period_end=end, duration_months=months
    )


TTM = FinancialPeriod(period_type=PeriodType.TTM, label="TTM", duration_months=12)


@dataclass(frozen=True)
class V:
    metric: CanonicalMetric
    period: FinancialPeriod
    value: Decimal | int | str | None  # None: no canonical value (see outcome)
    basis: ReportingBasis = B.CONSOLIDATED
    currency: Currency = Currency.INR
    kind: ValueKind = ValueKind.MONEY
    outcome: Outcome = Outcome.SINGLE_SOURCE
    confidence: Confidence = Confidence.HIGH
    source: SourceType = SourceType.FILING
    max_difference: Decimal | None = None


BALANCE = {
    M.EQUITY_SHARE_CAPITAL,
    M.RESERVES,
    M.OTHER_EQUITY,
    M.TOTAL_EQUITY,
    M.TOTAL_BORROWINGS,
    M.LONG_TERM_BORROWINGS,
    M.SHORT_TERM_BORROWINGS,
    M.CASH_AND_CASH_EQUIVALENTS,
    M.TOTAL_ASSETS,
    M.TOTAL_CURRENT_ASSETS,
    M.TOTAL_CURRENT_LIABILITIES,
    M.TRADE_RECEIVABLES,
    M.INVENTORY,
    M.TRADE_PAYABLES,
    M.PROPERTY_PLANT_EQUIPMENT,
    M.CWIP,
    M.NON_CURRENT_INVESTMENTS,
    M.GOODWILL,
    M.INTANGIBLE_ASSETS,
    M.TOTAL_EQUITY_AND_LIABILITIES,
    M.DEPOSITS,
    M.ADVANCES,
    M.TOTAL_LIABILITIES,
    M.FIXED_ASSETS,
    M.TOTAL_INVESTMENTS,
}
CASH = {M.CFO, M.PURCHASE_OF_PPE, M.CAPEX, M.DIVIDENDS_PAID, M.BUYBACK, M.CFI, M.CFF}


def make(
    rows: list[V], industry: Industry = Industry.GENERAL, fy_end: int | None = 3
) -> CanonicalFinancialDataset:
    observations, records, values = [], [], []
    for n, row in enumerate(rows):
        statement = (
            StatementType.BALANCE_SHEET
            if row.metric in BALANCE
            else StatementType.CASH_FLOW
            if row.metric in CASH
            else StatementType.QUARTERLY_RESULTS
            if row.period.period_type == PeriodType.QUARTER
            else StatementType.PROFIT_AND_LOSS
        )
        oid = uuid5(NS, f"obs|{n}")
        raw = "0" if row.value is None else str(row.value)
        number = None if row.value is None else Decimal(str(row.value))
        source = (
            SourceReference(
                source_type=SourceType.FILING,
                source_url="https://bse/ar.pdf",
                retrieved_or_extracted_at=NOW,
                document_id=uuid5(NS, "doc"),
                artifact_sha256="a" * 64,
                extraction_id=uuid5(NS, "x"),
                page_number=10 + n,
                table_id=uuid5(NS, "t"),
                row_index=n,
                column_index=2,
                locator=f"page:{10 + n}/r{n}/c2",
            )
            if row.source == SourceType.FILING
            else SourceReference(
                source_type=SourceType.SCREENER,
                source_url="https://www.screener.in/company/X/",
                retrieved_or_extracted_at=NOW,
                locator=f"#cell{n}",
                raw_artifact_id=uuid5(NS, "page"),
            )
        )
        observations.append(
            SourceFinancialObservation(
                observation_id=oid,
                company_key="screener:X",
                metric=row.metric,
                statement_type=statement,
                raw_label=row.metric.value,
                raw_value=raw,
                parsed=ParsedNumber(
                    raw=raw,
                    status=NumberStatus.NUMBER if number else NumberStatus.ZERO_REPORTED,
                    value=number or Decimal(0),
                ),
                value_kind=row.kind,
                currency=row.currency,
                scale=Scale.CRORES if row.kind == ValueKind.MONEY else Scale.ONES,
                normalized_value=number,
                period=row.period,
                reporting_basis=row.basis,
                authority=SourceAuthority.PRIMARY_REGULATORY
                if row.source == SourceType.FILING
                else SourceAuthority.SECONDARY_AGGREGATOR,
                source=source,
                extraction_method="test",
                confidence=row.confidence,
            )
        )
        rid = uuid5(NS, f"rec|{n}")
        selected = None if row.value is None else oid
        records.append(
            ReconciliationRecord(
                record_id=rid,
                key=f"{row.metric.value}|{n}",
                metric=row.metric,
                statement_type=statement,
                period=row.period,
                reporting_basis=row.basis,
                currency=row.currency,
                value_kind=row.kind,
                outcome=row.outcome,
                selected_observation_id=selected,
                supporting_observation_ids=(oid,) if selected else (),
                conflicting_observation_ids=() if selected else (oid,),
                max_difference=row.max_difference,
                reason="test",
            )
        )
        if number is not None:
            values.append(
                CanonicalFinancialValue(
                    canonical_value_id=uuid5(NS, f"cv|{n}"),
                    metric=row.metric,
                    value=number,
                    value_kind=row.kind,
                    currency=row.currency,
                    scale=Scale.CRORES if row.kind == ValueKind.MONEY else Scale.ONES,
                    period=row.period,
                    reporting_basis=row.basis,
                    statement_type=statement,
                    selected_observation_id=oid,
                    supporting_observation_ids=(oid,),
                    conflicting_observation_ids=(),
                    reconciliation_record_id=rid,
                    outcome=row.outcome,
                    selection_reason="test",
                    confidence=row.confidence,
                )
            )
    return CanonicalFinancialDataset(
        dataset_id=uuid5(NS, f"dataset|{len(rows)}|{industry.value}|{rows!r}"),
        company_key="screener:X",
        company_name="Example Ltd",
        industry=industry,
        fiscal_year_end_month=fy_end,
        generated_at=NOW,
        screener_source_sha256s=(),
        evidence_bundle_id=None,
        tolerance=TolerancePolicy(),
        limits=FinancialLimits(),
        status="COMPLETE",
        observations=tuple(observations),
        unmapped_lines=(),
        reconciliation=tuple(records),
        canonical_values=tuple(values),
        statements=(),
        validations=(),
        warnings=(),
        statistics=FinancialStatistics(
            total_observations=len(observations),
            observations_by_source={},
            observations_by_statement={},
            mapped_observations=len(observations),
            unmapped_observations=0,
            unmapped_lines=0,
            canonical_values=len(values),
            outcomes={},
            validation_pass=0,
            validation_fail=0,
        ),
    )


def company_year(
    year: int, basis: ReportingBasis = B.CONSOLIDATED, **override: int | None
) -> list[V]:
    """A simple industrial company that grows 100 of revenue a year from FY2021."""
    k = year - 2021
    pbt = 270 + 40 * k
    tax = Decimal(pbt) / 4
    pat = Decimal(pbt) - tax
    flows = {
        M.REVENUE_FROM_OPERATIONS: 1000 + 100 * k,
        M.OPERATING_PROFIT: 300 + 40 * k,
        M.RAW_MATERIAL_COST: 400 + 30 * k,
        M.PURCHASES_OF_STOCK_IN_TRADE: 50,
        M.CHANGE_IN_INVENTORIES: -10,
        M.EMPLOYEE_COST: 150,
        M.OTHER_EXPENSES: 50,
        M.SELLING_EXPENSE: 30,
        M.ADMINISTRATIVE_EXPENSE: 40,
        M.DEPRECIATION_AMORTIZATION: 40,
        M.FINANCE_COST: 20,
        M.OTHER_INCOME: 30,
        M.PBT: pbt,
        M.TOTAL_TAX: tax,
        M.PAT: pat,
        M.CFO: pat + 30,
        M.PURCHASE_OF_PPE: -(60 + 5 * k),
        M.DIVIDENDS_PAID: -50,
    }
    balances = {
        M.EQUITY_SHARE_CAPITAL: 100,
        M.RESERVES: 900 + 100 * k,
        M.TOTAL_BORROWINGS: 300,
        M.LONG_TERM_BORROWINGS: 200,
        M.SHORT_TERM_BORROWINGS: 100,
        M.CASH_AND_CASH_EQUIVALENTS: 150 + 20 * k,
        M.TOTAL_ASSETS: 2000 + 150 * k,
        M.TOTAL_EQUITY_AND_LIABILITIES: 2000 + 150 * k,
        M.TOTAL_CURRENT_ASSETS: 800 + 60 * k,
        M.TOTAL_CURRENT_LIABILITIES: 400 + 20 * k,
        M.TRADE_RECEIVABLES: 200 + 25 * k,
        M.INVENTORY: 150 + 10 * k,
        M.TRADE_PAYABLES: 120 + 5 * k,
        M.PROPERTY_PLANT_EQUIPMENT: 600 + 30 * k,
        M.CWIP: 30,
        M.NON_CURRENT_INVESTMENTS: 200,
        M.GOODWILL: 50,
        M.INTANGIBLE_ASSETS: 20,
    }
    rows = []
    for metric, value in {**flows, **balances}.items():
        value = override.get(metric.value, value)
        if value is None:
            continue
        period = pit(year) if metric in balances else fy(year)
        rows.append(V(metric, period, value, basis))
    rows.append(V(M.EPS, fy(year), quantize_eps(pat), basis, kind=ValueKind.PER_SHARE))
    return rows


def quantize_eps(pat: Decimal) -> Decimal:
    return (pat / 10).quantize(Decimal("0.01"))


def company(years: range = range(2021, 2027), **override: int | None) -> list[V]:
    return [row for year in years for row in company_year(year, **override)]


def analyse(rows: list[V], **kw) -> FinancialAnalyticsDataset:
    industry = kw.pop("industry", Industry.GENERAL)
    return build_financial_analytics(make(rows, industry), generated_at=NOW, **kw)


def metric(
    a: FinancialAnalyticsDataset,
    code: str,
    end: date = FY26,
    basis=B.CONSOLIDATED,
    ptype: PeriodType = PeriodType.FY,
) -> DerivedMetric:
    found = [
        m
        for m in a.metrics
        if m.code == code
        and m.reporting_basis == basis
        and m.period.period_end == end
        and m.period.period_type == ptype
    ]
    assert len(found) == 1, (code, end, found)
    return found[0]


def q(x: str) -> Decimal:
    return Decimal(x).quantize(Decimal("0.000001"))


BASE = analyse(company())


# --- A. period selection ------------------------------------------------------------------------


def test_a_period_selection():
    roles = {p.role: p for p in BASE.periods if p.reporting_basis == B.CONSOLIDATED}
    assert roles[PeriodRole.LATEST_FY].period.period_end == FY26
    assert roles[PeriodRole.PREVIOUS_FY].period.period_end == FY25
    assert roles[PeriodRole.WINDOW_3Y].start_period.period_end == date(2023, 3, 31)
    assert roles[PeriodRole.WINDOW_5Y].start_period.period_end == FY21
    assert roles[PeriodRole.WINDOW_10Y].status == "UNAVAILABLE"
    assert roles[PeriodRole.LATEST_QUARTER].status == "UNAVAILABLE"
    assert roles[PeriodRole.PREVIOUS_TTM].status == "UNAVAILABLE"
    rows = [
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2025, 6, 30)), 300),
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 3, 31)), 330),
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 6, 30)), 345),
        V(M.REVENUE_FROM_OPERATIONS, TTM, 1300, source=SourceType.SCREENER),
    ]
    a = analyse(rows)
    roles = {p.role: p for p in a.periods}
    assert roles[PeriodRole.LATEST_QUARTER].period.label == "Q1 FY2027"
    assert roles[PeriodRole.PRIOR_YEAR_QUARTER].period.period_end == date(2025, 6, 30)
    assert roles[PeriodRole.SEQUENTIAL_QUARTER].period.period_end == date(2026, 3, 31)
    assert roles[PeriodRole.LATEST_TTM].status == "SELECTED"
    assert roles[PeriodRole.LATEST_FY].status == "UNAVAILABLE"


# --- B-D. growth ------------------------------------------------------------------------------


def test_b_yoy_growth():
    m = metric(BASE, "REVENUE_GROWTH_YOY")
    assert (m.status, m.value) == (S.CALCULATED, q("7.142857"))  # 1500 / 1400 - 1
    assert m.comparison_period.period_end == FY25
    assert {i.role for i in m.inputs} == {"current", "prior"}
    assert {i.value for i in m.inputs} == {Decimal(1500), Decimal(1400)}
    assert metric(BASE, "REVENUE_GROWTH_YOY", FY21).status == S.INSUFFICIENT_DATA  # no FY2020


def test_c_cagr():
    m = metric(BASE, "REVENUE_CAGR_5Y")
    assert m.value == q("8.447177")  # (1500/1000)^(1/5) - 1
    assert m.comparison_period.period_end == FY21
    assert metric(BASE, "REVENUE_CAGR_3Y").value == q("7.721735")  # (1500/1200)^(1/3) - 1
    assert metric(BASE, "REVENUE_CAGR_10Y").status == S.INSUFFICIENT_DATA


def test_d_zero_and_negative_endpoints():
    assert growth(Decimal(5), Decimal(0)).status == S.ZERO_DENOMINATOR
    assert growth(Decimal(5), Decimal(-4)).interpretation == "NEGATIVE_BASE"
    assert cagr(Decimal(5), Decimal(-4), 3).status == S.NOT_APPLICABLE
    assert cagr(Decimal(-5), Decimal(4), 3).interpretation == "NON_POSITIVE_ENDPOINT"
    assert cagr(Decimal(5), Decimal(0), 3).status == S.ZERO_DENOMINATOR
    a = analyse(
        company(**{"PAT": None})
        + [
            V(M.PAT, fy(2021), -50),
            V(M.PAT, fy(2023), 0),
            V(M.PAT, fy(2025), 100),
            V(M.PAT, fy(2026), 120),
        ]
    )
    five = metric(a, "PAT_CAGR_5Y")
    assert (five.status, five.value, five.interpretation) == (
        S.NOT_APPLICABLE,
        None,
        "NON_POSITIVE_ENDPOINT",
    )
    assert metric(a, "PAT_CAGR_3Y").status == S.ZERO_DENOMINATOR
    assert metric(a, "PAT_GROWTH_YOY").value == q("20")


# --- E-F. margins ---------------------------------------------------------------------------------


def test_e_margins():
    ebitda = metric(BASE, "EBITDA", ptype=PeriodType.FY)
    assert (ebitda.value, ebitda.formula_id) == (Decimal(470 + 20 + 40), "EBITDA/1/DERIVED")
    assert metric(BASE, "EBITDA_MARGIN").value == q("35.333333")  # 530 / 1500
    assert metric(BASE, "OPERATING_MARGIN").value == q("33.333333")  # 500 / 1500
    gross = metric(BASE, "GROSS_MARGIN")  # COGS = 550 + 50 - 10
    assert gross.value == q(str(Decimal(1500 - 590) * 100 / 1500))
    assert metric(BASE, "TAX_RATE").value == q("25")
    assert metric(BASE, "TAX_RATE").formula_id == "TAX_RATE/1/TOTAL_TAX"
    assert metric(BASE, "OTHER_INCOME_TO_PBT").value == q(str(Decimal(30) * 100 / 470))
    assert metric(BASE, "POWER_AND_FUEL_TO_REVENUE").status == S.INSUFFICIENT_DATA


def test_f_margin_changes_in_basis_points():
    pat26, pat25 = Decimal("352.5") / 1500 * 100, Decimal("322.5") / 1400 * 100
    change = metric(BASE, "PAT_MARGIN_CHANGE_YOY")
    assert change.value == q(str((q(str(pat26)) - q(str(pat25))) * 100))
    assert change.unit.value == "BASIS_POINTS"
    assert metric(BASE, "PAT_MARGIN_CHANGE_5Y").comparison_period.period_end == FY21


# --- G-J. returns and DuPont ------------------------------------------------------------------


def test_g_roe_on_average_equity():
    m = metric(BASE, "ROE")
    # equity = share capital + reserves: 1400 (FY25) and 1500 (FY26); PAT FY26 = 352.5
    assert m.value == q(str(Decimal("352.5") * 100 / 1450))
    assert m.formula_id == "ROE/1/AVERAGE"
    roles = {i.role for i in m.inputs}
    assert {"pat", "equity_close.share_capital", "equity_open.reserves"} <= roles


def test_h_roce_and_i_roic():
    ce26, ce25 = Decimal(1500 + 300), Decimal(1400 + 300)
    assert metric(BASE, "ROCE").value == q(str(Decimal(490) * 100 / ((ce26 + ce25) / 2)))
    ic26, ic25 = ce26 - 250, ce25 - 230
    nopat = Decimal(490) * Decimal("0.75")
    assert metric(BASE, "ROIC").value == q(str(q(str(nopat)) * 100 / ((ic26 + ic25) / 2)))
    assert metric(BASE, "INCREMENTAL_ROIC_3Y").status == S.CALCULATED
    tangible = metric(BASE, "RETURN_ON_TANGIBLE_CAPITAL")
    assert "ENDING_BALANCE_USED" in tangible.warnings and tangible.confidence == Confidence.MEDIUM


def test_j_dupont():
    roe = metric(BASE, "ROE").value
    dupont = metric(BASE, "DUPONT_ROE")
    assert abs(dupont.value - roe) < Decimal("0.0001")
    assert len(dupont.derived_from) == 3
    assert metric(BASE, "DUPONT_EQUITY_MULTIPLIER").value == q(str(Decimal(2675) / Decimal(1450)))
    assert metric(BASE, "DUPONT_NET_MARGIN_CHANGE_YOY").status == S.CALCULATED


# --- K-N. leverage and liquidity ---------------------------------------------------------------


def test_k_leverage():
    assert metric(BASE, "DEBT_TO_EQUITY").value == q(str(Decimal(300) / 1500))
    assert metric(BASE, "DEBT_TO_EBITDA").value == q(str(Decimal(300) / 530))
    assert metric(BASE, "NET_DEBT_TO_EBITDA").value == q(str(Decimal(50) / 530))
    assert metric(BASE, "INTEREST_COVERAGE").value == q("24.5")  # EBIT 490 / 20
    net = next(m for m in BASE.metrics if m.code == "NET_DEBT" and m.period.period_end == FY26)
    assert (net.value, net.interpretation) == (Decimal(50), None)


def test_l_net_cash():
    a = analyse(company(**{"CASH_AND_CASH_EQUIVALENTS": 900}))
    net = next(m for m in a.metrics if m.code == "NET_DEBT" and m.period.period_end == FY26)
    assert (net.value, net.interpretation) == (Decimal(-600), "NET_CASH")
    ratio = metric(a, "NET_DEBT_TO_EBITDA")
    assert (ratio.status, ratio.value, ratio.interpretation) == (S.NOT_APPLICABLE, None, "NET_CASH")
    assert metric(a, "NET_DEBT_TO_EQUITY").interpretation == "NET_CASH"
    health = {h.dimension: h for h in a.health}
    assert health[HealthDimension.BALANCE_SHEET].state == HealthState.STRONG


def test_m_interest_coverage_without_finance_cost():
    a = analyse(company(**{"FINANCE_COST": 0}))
    assert metric(a, "INTEREST_COVERAGE").status == S.ZERO_DENOMINATOR
    assert metric(a, "INTEREST_COVERAGE").value is None


def test_n_liquidity():
    assert metric(BASE, "CURRENT_RATIO").value == q(str(Decimal(1100) / 500))
    assert metric(BASE, "QUICK_RATIO").value == q(str(Decimal(1100 - 200) / 500))
    assert metric(BASE, "CASH_RATIO").value == q(str(Decimal(250) / 500))
    no_inventory = analyse(company(**{"INVENTORY": None}))
    assert metric(no_inventory, "QUICK_RATIO").status == S.INSUFFICIENT_DATA  # never zero


# --- O-R. working capital -----------------------------------------------------------------------


def test_o_p_q_r_days_and_cash_conversion_cycle():
    receivable = Decimal(325 + 300) / 2 * 365 / 1500
    inventory = Decimal(200 + 190) / 2 * 365 / 590
    payable = Decimal(145 + 140) / 2 * 365 / 590
    assert metric(BASE, "RECEIVABLE_DAYS").value == q(str(receivable))
    assert metric(BASE, "INVENTORY_DAYS").value == q(str(inventory))
    assert metric(BASE, "PAYABLE_DAYS").value == q(str(payable))
    ccc = metric(BASE, "CASH_CONVERSION_CYCLE")
    assert ccc.value == q(str(q(str(receivable)) + q(str(inventory)) - q(str(payable))))
    no_cogs = analyse(company(**{"RAW_MATERIAL_COST": None}))
    assert metric(no_cogs, "INVENTORY_DAYS").status == S.INSUFFICIENT_DATA
    assert metric(no_cogs, "CASH_CONVERSION_CYCLE").status == S.INSUFFICIENT_DATA
    assert metric(no_cogs, "RECEIVABLE_DAYS").status == S.CALCULATED
    assert metric(BASE, "NWC_CHANGE").value == Decimal(40)


# --- S-V. cash flow and accruals --------------------------------------------------------------


def test_s_t_u_cash_conversion_and_fcf():
    assert metric(BASE, "CFO_TO_PAT").value == q(str(Decimal("382.5") / Decimal("352.5")))
    fcf = metric(BASE, "FCF")
    assert (fcf.value, fcf.formula_id) == (Decimal("382.5") - 85, "FCF/1")
    capex = metric(BASE, "CAPEX")
    assert (capex.value, capex.formula_id) == (Decimal(85), "CAPEX/1/PURCHASE_OF_PPE")
    pats = [Decimal(270 + 40 * k) * Decimal("0.75") for k in (3, 4, 5)]
    assert metric(BASE, "CUMULATIVE_CFO_TO_PAT_3Y").value == q(str((sum(pats) + 90) / sum(pats)))
    assert metric(BASE, "CUMULATIVE_FCF_3Y").status == S.CALCULATED
    no_capex = analyse(company(**{"PURCHASE_OF_PPE": None}))
    assert metric(no_capex, "FCF").status == S.INSUFFICIENT_DATA  # capex is never assumed zero


def test_v_accruals():
    m = metric(BASE, "ACCRUAL_RATIO")
    assert m.value == q(str(Decimal(-30) * 100 / Decimal(2675)))
    assert metric(BASE, "TOTAL_ACCRUALS").value == Decimal(-30)
    assert metric(BASE, "BALANCE_SHEET_ACCRUAL_RATIO").status == S.CALCULATED
    assert metric(BASE, "ACCRUAL_RATIO_CHANGE_YOY").status == S.CALCULATED


# --- W-AA. forensic models -----------------------------------------------------------------------


def model(a, kind, variant=None, basis=B.CONSOLIDATED):
    return next(
        r
        for r in a.forensic_models
        if r.model == kind
        and r.reporting_basis == basis
        and (variant is None or r.variant == variant)
    )


def test_w_piotroski_components():
    f = model(BASE, ForensicModel.PIOTROSKI_F_SCORE)
    assert f.status == S.CALCULATED and f.components_available == 9
    statuses = {c.name: c.status for c in f.components}
    assert statuses["F1_ROA_POSITIVE"] == ComponentStatus.PASS
    assert statuses["F4_CFO_EXCEEDS_PAT"] == ComponentStatus.PASS
    assert statuses["F7_NO_EQUITY_ISSUANCE"] == ComponentStatus.PASS
    assert f.score == Decimal(f.components_passed)
    assert all(c.canonical_value_ids for c in f.components)


def test_x_incomplete_piotroski_is_not_scored():
    a = analyse(company(**{"RAW_MATERIAL_COST": None}))
    f = model(a, ForensicModel.PIOTROSKI_F_SCORE)
    assert (f.status, f.score, f.components_available) == (S.INSUFFICIENT_DATA, None, 8)
    assert f.coverage == q(str(Decimal(8) / 9))
    gross = next(c for c in f.components if c.name == "F8_GROSS_MARGIN_IMPROVED")
    assert gross.status == ComponentStatus.UNAVAILABLE and gross.missing_inputs
    assert f.components_passed is not None  # reported, but never turned into a score


def test_y_altman_variants_and_applicability():
    original = model(BASE, ForensicModel.ALTMAN_Z, "ORIGINAL_1968")
    assert original.status == S.INSUFFICIENT_DATA
    market = next(c for c in original.components if c.name == "X4_MARKET")
    assert market.status == ComponentStatus.UNAVAILABLE
    double = model(BASE, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995")
    x = {c.name: c.value for c in double.components}
    expected = (
        Decimal("6.56") * x["X1"]
        + Decimal("3.26") * x["X2"]
        + Decimal("6.72") * x["X3"]
        + Decimal("1.05") * x["X4_BOOK"]
    )
    assert double.score == q(str(expected)) and double.zone == "SAFE_ZONE"
    assert x["X1"] == q(str(Decimal(1100 - 500) / 2750))
    bank = analyse(company(), industry=Industry.BANK)
    assert model(bank, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995").status == (
        S.UNSUPPORTED_INDUSTRY
    )


def test_z_beneish_components():
    m = model(BASE, ForensicModel.BENEISH_M)
    assert m.status == S.CALCULATED and m.components_available == 8
    c = {x.name: x.value for x in m.components}
    assert c["SGI"] == q(str(Decimal(1500) / 1400))
    assert c["DSRI"] == q(str((Decimal(325) / 1500) / (Decimal(300) / 1400)))
    assert c["TATA"] == q(str(Decimal(-30) / 2750))
    expected = Decimal("-4.84") + sum(
        financial_forensics.BENEISH_WEIGHTS[n] * v for n, v in c.items()
    )
    assert m.score == q(str(expected))
    assert m.zone in ("ABOVE_SCREENING_THRESHOLD", "BELOW_SCREENING_THRESHOLD")


def test_aa_incomplete_beneish_is_not_scored():
    a = analyse(company(**{"SELLING_EXPENSE": None}))
    m = model(a, ForensicModel.BENEISH_M)
    assert (m.status, m.score, m.zone, m.components_available) == (
        S.INSUFFICIENT_DATA,
        None,
        None,
        7,
    )
    sgai = next(x for x in m.components if x.name == "SGAI")
    assert sgai.status == ComponentStatus.UNAVAILABLE and sgai.value is None


# --- AB-AD. trends, anomalies, earnings quality -------------------------------------------------


@pytest.mark.parametrize(
    ("values", "kind", "higher", "expected"),
    [
        (["10", "11", "12.5"], "LEVEL", True, TrendClass.IMPROVING),
        (["10", "13", "16"], "LEVEL", True, TrendClass.STRONGLY_IMPROVING),
        (["10", "13", "16"], "LEVEL", False, TrendClass.STRONGLY_DETERIORATING),
        (["10", "10.2", "10.4"], "LEVEL", True, TrendClass.STABLE),
        (["10", "20", "8", "19"], "LEVEL", True, TrendClass.VOLATILE),
        (["100", "110", "125"], "MONEY", True, TrendClass.IMPROVING),
        (["100", "120"], "LEVEL", True, TrendClass.INSUFFICIENT_DATA),
        (["0", "5", "9"], "MONEY", True, TrendClass.INSUFFICIENT_DATA),
        (["60", "70", "80"], "DAYS", False, TrendClass.STRONGLY_DETERIORATING),
    ],
)
def test_ab_trend_classification(values, kind, higher, expected):
    assert classify_trend([Decimal(v) for v in values], kind, higher)[0] == expected


def test_ab_trends_on_the_dataset():
    trends = {t.series_code: t for t in BASE.trends if t.reporting_basis == B.CONSOLIDATED}
    roe = trends["ROE"]
    assert roe.points_used == 5 and roe.rule_id == "trend-rules/1/LEVEL"
    assert len(roe.metric_ids) == 5
    assert trends["RECEIVABLE_DAYS"].higher_is_better is False
    assert "DEPOSITS_GROWTH_YOY" not in trends  # banking trend, not for an industrial company


def test_ac_anomalies_have_rules_thresholds_and_inputs():
    a = analyse(
        company(**{"TRADE_RECEIVABLES": None})
        + [
            V(M.TRADE_RECEIVABLES, pit(y), v)
            for y, v in (
                (2021, 200),
                (2022, 225),
                (2023, 250),
                (2024, 275),
                (2025, 300),
                (2026, 450),
            )
        ]
    )
    anomaly = next(x for x in a.anomalies if x.code == "RECEIVABLES_OUTGROWING_REVENUE")
    assert anomaly.period.period_end == FY26 and anomaly.severity == "ELEVATED"
    assert anomaly.threshold == Decimal(30) and anomaly.rule_id.startswith("anomaly-rules/1/")
    assert anomaly.canonical_value_ids and "not a conclusion" in anomaly.explanation
    assert not any(x.code == "RECEIVABLES_OUTGROWING_REVENUE" for x in BASE.anomalies)


def test_ad_earnings_quality_signals():
    signals = {s.code: s for s in BASE.forensic_signals}
    assert signals["OTHER_INCOME_DEPENDENCE"].status == SignalStatus.CLEAR
    assert signals["REPEATED_NEGATIVE_FCF"].status == SignalStatus.CLEAR
    assert signals["CUMULATIVE_PAT_VS_CFO"].observed > 1
    assert all(s.rule_id.startswith("forensic-rules/1/") for s in signals.values())
    stretched = analyse(
        company(**{"TRADE_RECEIVABLES": None})
        + [
            V(M.TRADE_RECEIVABLES, pit(y), v)
            for y, v in (
                (2021, 200),
                (2022, 225),
                (2023, 250),
                (2024, 275),
                (2025, 300),
                (2026, 400),
            )
        ]
    )
    receivables = next(s for s in stretched.forensic_signals if s.code == "RECEIVABLES_VS_REVENUE")
    assert receivables.status == SignalStatus.ELEVATED and receivables.canonical_value_ids
    no_cfo = analyse(company(**{"CFO": None}))
    cash = next(s for s in no_cfo.forensic_signals if s.code == "CUMULATIVE_PAT_VS_CFO")
    assert cash.status == SignalStatus.INSUFFICIENT_DATA and cash.observed is None
    text = json.dumps([s.explanation for s in BASE.forensic_signals]).lower()
    for word in ("fraud", "manipulation proven", "buy", "sell"):
        assert word not in text


# --- AE-AJ. conflicts, confidence and isolation -----------------------------------------------


def test_ae_conflict_propagation():
    rows = [r for r in company() if not (r.metric == M.TOTAL_ASSETS and r.period == pit(2026))]
    rows += [
        V(
            M.TOTAL_ASSETS,
            pit(2026),
            2750,
            outcome=Outcome.SOURCE_PRECEDENCE,
            confidence=Confidence.HIGH,
            max_difference=Decimal(20),
        )
    ]
    a = analyse(rows)
    roa = metric(a, "ROA")
    assert "INPUT_SOURCE_PRECEDENCE" in roa.warnings and roa.confidence == Confidence.MEDIUM
    assert any(i.outcome == Outcome.SOURCE_PRECEDENCE for i in roa.inputs)
    material = [r for r in rows if not (r.metric == M.TOTAL_ASSETS and r.period == pit(2026))]
    material += [
        V(
            M.TOTAL_ASSETS,
            pit(2026),
            2750,
            outcome=Outcome.SOURCE_PRECEDENCE,
            max_difference=Decimal(500),
        )
    ]
    roa = metric(analyse(material), "ROA")
    assert "MATERIAL_INPUT_CONFLICT" in roa.warnings and roa.confidence == Confidence.LOW
    conflicted = [r for r in company() if not (r.metric == M.PAT and r.period == fy(2026))]
    conflicted += [V(M.PAT, fy(2026), None, outcome=Outcome.CONFLICT)]
    a = analyse(conflicted)
    roe = metric(a, "ROE")
    assert (roe.status, roe.value) == (S.CONFLICTED_INPUT, None)
    assert any("CONFLICT" in note for note in roe.missing_inputs)
    assert any(w.code == "BOX4_UNRESOLVED_CONFLICTS" for w in a.warnings)


def test_af_low_confidence_inputs():
    rows = [r for r in company() if not (r.metric == M.PAT and r.period == fy(2026))]
    rows += [V(M.PAT, fy(2026), None, outcome=Outcome.NOT_COMPARABLE)]
    assert metric(analyse(rows), "PAT_MARGIN").status == S.LOW_CONFIDENCE_INPUT
    weakest = [r for r in company() if not (r.metric == M.CFO and r.period == fy(2026))]
    weakest += [V(M.CFO, fy(2026), "382.5", confidence=Confidence.MEDIUM)]
    m = metric(analyse(weakest), "CFO_TO_PAT")
    assert m.confidence == Confidence.MEDIUM  # never above the weakest input


def test_ag_standalone_and_consolidated_never_mix():
    rows = [r for r in company() if r.period not in (fy(2025), pit(2025))]
    rows += [r for r in company_year(2025, basis=B.STANDALONE)]
    a = analyse(rows)
    assert metric(a, "REVENUE_GROWTH_YOY").status == S.INSUFFICIENT_DATA
    assert {f.reporting_basis for f in a.frames} == {B.CONSOLIDATED, B.STANDALONE}
    for m in a.metrics:
        assert {i.reporting_basis for i in m.inputs} <= {m.reporting_basis}


def test_ah_quarter_and_ytd_never_mix():
    q1, q4 = date(2025, 12, 31), date(2024, 12, 31)
    rows = [
        V(M.REVENUE_FROM_OPERATIONS, quarter(q1), 420),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q4), 390),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q1, 9, PeriodType.NINE_MONTH), 1200),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q4, 9, PeriodType.NINE_MONTH), 1100),
    ]
    a = analyse(rows)
    m = metric(a, "REVENUE_GROWTH_YOY", q1, ptype=PeriodType.QUARTER)
    assert m.value == q(str((Decimal(420) / 390 - 1) * 100))
    assert {i.period.period_type for i in m.inputs} == {PeriodType.QUARTER}
    assert compare_periods(quarter(q1), quarter(q4, 9, PeriodType.NINE_MONTH), 12) == (
        S.INCOMPATIBLE_PERIODS
    )


def test_ai_fy_and_ttm_never_mix():
    assert compare_periods(fy(2026), TTM, 12) == S.INCOMPATIBLE_PERIODS
    assert compare_periods(TTM, TTM, 12) == S.INCOMPATIBLE_PERIODS  # TTM has no end date
    a = analyse(company() + [V(M.REVENUE_FROM_OPERATIONS, TTM, 1550), V(M.PAT, TTM, 360)])
    ttm = [m for m in a.metrics if m.period.period_type == PeriodType.TTM]
    assert {m.code for m in ttm if m.status == S.CALCULATED} >= {"PAT_MARGIN"}
    assert not any(m.category == MetricCategory.GROWTH for m in ttm)


def test_aj_currencies_never_mix():
    rows = company() + [V(M.TOTAL_ASSETS, pit(2027), 30, currency=Currency.USD)]
    rows += [V(M.EMPLOYEE_COST, fy(2026), 18, currency=Currency.USD)]
    rows = [
        r
        for r in rows
        if not (r.metric == M.EMPLOYEE_COST and r.currency == Currency.INR and r.period == fy(2026))
    ]
    a = analyse(rows)
    assert [f.currency for f in a.frames] == [Currency.INR]
    assert a.frames[0].excluded_other_currency_values == 2
    assert any(w.code == "OTHER_CURRENCY_EXCLUDED" for w in a.warnings)
    assert metric(a, "EMPLOYEE_COST_TO_REVENUE").status == S.INCOMPATIBLE_CURRENCY
    assert all(i.currency == Currency.INR for m in a.metrics for i in m.inputs)


# --- AK-AM. determinism, provenance, bounds -------------------------------------------------------


def test_ak_deterministic_serialization():
    again = analyse(company())
    encoded = canonical_analytics_json(BASE)
    assert encoded == canonical_analytics_json(again)
    assert FinancialAnalyticsDataset.model_validate_json(encoded) == BASE
    assert json.loads(encoded)["schema_version"] == "FINANCIAL_ANALYTICS_DATASET_V1"


def test_al_provenance_reaches_box4():
    data = make(company())
    a = build_financial_analytics(data, generated_at=NOW)
    canonical = {v.canonical_value_id: v for v in data.canonical_values}
    ids = {m.metric_id for m in a.metrics}
    for m in a.metrics:
        if m.status == S.CALCULATED:
            assert m.inputs
        for i in m.inputs:
            source = canonical[i.canonical_value_id]
            assert (i.value, i.selected_observation_id, i.reconciliation_record_id) == (
                source.value,
                source.selected_observation_id,
                source.reconciliation_record_id,
            )
        assert set(m.derived_from) <= ids
    for s in a.forensic_signals:
        assert set(s.canonical_value_ids) <= set(canonical)


def test_am_resource_bounds():
    a = analyse(company(), limits=AnalyticsLimits(max_derived_metrics=50, max_periods_per_series=2))
    assert a.status == "PARTIAL" and len(a.metrics) == 50
    assert any(w.code == "DERIVED_METRIC_LIMIT_REACHED" for w in a.warnings)
    assert all(len(s.points) <= 2 for s in a.series)
    small = analyse(company(), limits=AnalyticsLimits(max_canonical_values=5))
    assert small.status == "PARTIAL" and small.statistics.canonical_values_consumed == 5


# --- AN-AP. banks and real fixtures -------------------------------------------------------------


def test_an_bank_applicability():
    a = analyse(
        company() + [V(M.DEPOSITS, pit(y), 5000 + 400 * (y - 2021)) for y in range(2021, 2027)],
        industry=Industry.BANK,
    )
    for code in (
        "DEBT_TO_EBITDA",
        "CURRENT_RATIO",
        "CASH_CONVERSION_CYCLE",
        "EBITDA_MARGIN",
        "DUPONT_ROE",
        "ROCE",
    ):
        m = metric(a, code)
        assert (m.status, m.interpretation) == (S.NOT_APPLICABLE, "INDUSTRY_NOT_APPLICABLE")
    assert metric(a, "DEPOSITS_GROWTH_YOY").value == q(str((Decimal(7000) / 6600 - 1) * 100))
    assert metric(a, "ROE").status == S.CALCULATED
    assert not [m for m in a.metrics if m.code == "GROSS_DEBT" and m.status == S.CALCULATED]
    assert {r.status for r in a.forensic_models} == {S.UNSUPPORTED_INDUSTRY}
    assert {s.code for s in a.forensic_signals} <= {
        "EXCEPTIONAL_ITEM_DEPENDENCE",
        "TAX_RATE_VOLATILITY",
        "EQUITY_DILUTION",
    }


async def test_ao_yesbank_fixture(tmp_path, fixture_dir):
    screener = await screener_dataset(tmp_path, fixture_dir, YESBANK)
    data = build_canonical_dataset(
        company_key=screener.company_key,
        company_name="Yes Bank",
        screeners=[screener],
        bundle=None,
        catalogue=None,
        generated_at=NOW,
    )
    a = build_financial_analytics(data, generated_at=NOW)
    assert a.industry == Industry.BANK
    deposits = metric(a, "DEPOSITS_GROWTH_YOY")
    assert deposits.value == q(str((Decimal(318969) / 284420 - 1) * 100))
    for m in a.metrics:  # deposits never feed a leverage calculation
        if m.category == MetricCategory.LEVERAGE:
            assert all(i.metric != M.DEPOSITS for i in m.inputs)
    assert metric(a, "DEBT_TO_EBITDA").status == S.NOT_APPLICABLE
    assert metric(a, "GROSS_NPA_RATIO").status == S.INSUFFICIENT_DATA
    assert metric(a, "ROA").status == S.CALCULATED
    assert {r.status for r in a.forensic_models} == {S.UNSUPPORTED_INDUSTRY}


async def test_ap_infy_fixture(tmp_path, fixture_dir):
    screener = await screener_dataset(tmp_path, fixture_dir, INFY)
    data = build_canonical_dataset(
        company_key=screener.company_key,
        company_name="Infosys",
        screeners=[screener],
        bundle=None,
        catalogue=None,
        generated_at=NOW,
    )
    a = build_financial_analytics(data, generated_at=NOW)
    growth_ = metric(a, "REVENUE_GROWTH_YOY", basis=B.STANDALONE)
    assert growth_.value == q(str((Decimal(148819) / 136592 - 1) * 100))
    assert {i.selected_source for i in growth_.inputs} == {SourceType.SCREENER}
    assert metric(a, "OPERATING_MARGIN", basis=B.STANDALONE).value == q(
        str(Decimal(36013) * 100 / 148819)
    )
    assert metric(a, "EBITDA_MARGIN", basis=B.STANDALONE).status == S.CALCULATED
    assert metric(a, "ROE", basis=B.STANDALONE).status == S.CALCULATED
    # Screener carries no current assets: liquidity and Altman Z'' stay honest
    assert metric(a, "CURRENT_RATIO", basis=B.STANDALONE).status == S.INSUFFICIENT_DATA
    assert model(a, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995", B.STANDALONE).status == (
        S.INSUFFICIENT_DATA
    )


# --- AQ-AS. missing data, zero denominators, architecture ----------------------------------------


def test_aq_missing_data_is_never_zero():
    a = analyse(
        [V(M.REVENUE_FROM_OPERATIONS, fy(2025), 1000), V(M.REVENUE_FROM_OPERATIONS, fy(2026), 1100)]
    )
    assert metric(a, "REVENUE_GROWTH_YOY").value == q("10")
    pat = metric(a, "PAT_MARGIN")
    assert (pat.status, pat.value) == (S.INSUFFICIENT_DATA, None)
    assert pat.missing_inputs and "PAT" in pat.missing_inputs[0]
    for m in a.metrics:
        if m.status != S.CALCULATED:
            assert m.value is None and m.confidence is None
    assert {r.status for r in a.forensic_models} == {S.INSUFFICIENT_DATA}
    states = {h.dimension: h.state for h in a.health}
    assert states[HealthDimension.RETURNS] == HealthState.INSUFFICIENT_DATA


def test_ar_division_by_zero():
    a = analyse(company(**{"REVENUE_FROM_OPERATIONS": 0}))
    for code in ("PAT_MARGIN", "EBITDA_MARGIN", "RECEIVABLE_DAYS"):
        assert metric(a, code).status == S.ZERO_DENOMINATOR
    assert metric(a, "REVENUE_GROWTH_YOY").status == S.ZERO_DENOMINATOR


def test_contract_requires_lineage():
    m = metric(BASE, "ROE")
    with pytest.raises(ValidationError):
        DerivedMetric.model_validate({**m.model_dump(), "inputs": ()})
    with pytest.raises(ValidationError):
        DerivedMetric.model_validate({**m.model_dump(), "status": "INSUFFICIENT_DATA"})
    with pytest.raises(ValidationError):
        FinancialAnalyticsDataset.model_validate({**BASE.model_dump(), "surprise": 1})


def test_as_no_ai_or_network_dependencies():
    forbidden = (
        "httpx",
        "anthropic",
        "aicredits",
        "openai",
        "summary",
        "socket",
        "adapters",
        "urllib",
        "requests",
    )
    for module in (financial_analytics, financial_forensics, financial_metrics, analytics_domain):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not any(word in name for word in forbidden), (module.__name__, name)
