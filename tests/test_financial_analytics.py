"""DETAILED_RESEARCH_V1 Box 5: deterministic financial analytics (offline).

Fixtures (see the factory below): A healthy industrial, B deteriorating, C negative FCF,
D high debt, E loss-making, F negative starting CAGR, G missing periods, H conflicted Box 4
inputs, I standalone + consolidated, J FY + TTM + quarterly, K bank, L YESBANK, M missing
capex, N missing working-capital components, O zero denominator, P restated periods,
T deterministic serialization. Q/R/S (forensic models) live in test_financial_forensics.py.
"""

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
    financial_formulas,
    financial_metrics,
    financial_rules,
)
from stock_research_ai.application.financial_analytics import (
    build_financial_analytics,
    canonical_analytics_json,
    classify_trend,
)
from stock_research_ai.application.financial_formulas import (
    FORMULAS,
    average,
    cagr,
    cash_conversion_cycle,
    days,
    dupont_roe,
    free_cash_flow,
    growth,
    nopat,
    positive_ratio,
    ratio,
)
from stock_research_ai.application.financial_metrics import compare_periods
from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
from stock_research_ai.domain import financial_analytics as analytics_domain
from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.financial_analytics import (
    AnalyticsLimits,
    AnalyticsUnit,
    CalculatedMetric,
    CalculationStatus,
    FinancialAnalyticsDataset,
    HealthDimension,
    HealthState,
    MetricCategory,
    PeriodRole,
    Sector,
    Severity,
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
    UnmappedFinancialLine,
    ValueKind,
)

M = CanonicalMetric
B = ReportingBasis
S = CalculationStatus
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
NS = UUID("0e4c1f8a-2b3d-4c5e-8f9a-1b2c3d4e5f6a")
FY26, FY25, FY21 = date(2026, 3, 31), date(2025, 3, 31), date(2021, 3, 31)


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
    M.LEASE_LIABILITIES,
    M.TOTAL_NON_CURRENT_LIABILITIES,
}
CASH = {
    M.CFO,
    M.PURCHASE_OF_PPE,
    M.CAPEX,
    M.DIVIDENDS_PAID,
    M.BUYBACK,
    M.CFI,
    M.CFF,
    M.TAX_PAID,
    M.INTEREST_PAID,
}


def make(
    rows: list[V],
    industry: Industry = Industry.GENERAL,
    fy_end: int | None = 3,
    unmapped: tuple[UnmappedFinancialLine, ...] = (),
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
        if isinstance(row.value, Decimal):
            # These fixtures model statement cells, whose parser accepts decimal
            # notation, not Decimal's automatic exponent notation for tiny values.
            # Keep the exact financial value and every expected result unchanged.
            raw = format(row.value, "f")
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
        unmapped_lines=unmapped,
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
            unmapped_lines=len(unmapped),
            canonical_values=len(values),
            outcomes={},
            validation_pass=0,
            validation_fail=0,
        ),
    )


def company_year(year: int, basis: ReportingBasis = B.CONSOLIDATED, **override) -> list[V]:
    """Fixture A: a healthy industrial company growing 100 of revenue a year from FY2021."""
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
        M.BUYBACK: -10,
        M.TAX_PAID: -(tax + 10),
        M.INTEREST_PAID: -20,
    }
    balances = {
        M.EQUITY_SHARE_CAPITAL: 100,
        M.RESERVES: 900 + 100 * k,
        M.TOTAL_BORROWINGS: 300,
        M.LONG_TERM_BORROWINGS: 200,
        M.SHORT_TERM_BORROWINGS: 100,
        M.LEASE_LIABILITIES: 40,
        M.CASH_AND_CASH_EQUIVALENTS: 150 + 20 * k,
        M.TOTAL_ASSETS: 2000 + 150 * k,
        M.TOTAL_EQUITY_AND_LIABILITIES: 2000 + 150 * k,
        M.TOTAL_CURRENT_ASSETS: 800 + 60 * k,
        M.TOTAL_CURRENT_LIABILITIES: 400 + 20 * k,
        M.TOTAL_NON_CURRENT_LIABILITIES: 600 + 30 * k,
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
        rows.append(V(metric, pit(year) if metric in balances else fy(year), value, basis))
    rows.append(
        V(M.EPS, fy(year), (pat / 10).quantize(Decimal("0.01")), basis, kind=ValueKind.PER_SHARE)
    )
    return rows


def company(years: range = range(2021, 2027), **override) -> list[V]:
    return [row for year in years for row in company_year(year, **override)]


def replace(rows: list[V], metric: CanonicalMetric, values: dict[int, object]) -> list[V]:
    """Rows with `metric` replaced by explicit per-year values (None drops a year)."""
    balance = metric in BALANCE
    kept = [r for r in rows if r.metric != metric]
    return kept + [
        V(metric, pit(y) if balance else fy(y), v) for y, v in values.items() if v is not None
    ]


def analyse(rows: list[V], **kw) -> FinancialAnalyticsDataset:
    industry = kw.pop("industry", Industry.GENERAL)
    unmapped = kw.pop("unmapped", ())
    return build_financial_analytics(
        make(rows, industry, unmapped=unmapped), generated_at=NOW, **kw
    )


def metric(
    a: FinancialAnalyticsDataset,
    code: str,
    end: date = FY26,
    basis=B.CONSOLIDATED,
    ptype: PeriodType = PeriodType.FY,
) -> CalculatedMetric:
    found = [
        m
        for m in a.all_metrics()
        if m.code == code
        and m.reporting_basis == basis
        and m.period.period_end == end
        and m.period.period_type == ptype
    ]
    assert len(found) == 1, (code, end, found)
    return found[0]


def q(x: object) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.000001"))


A = analyse(company())


# --- formula registry and pure arithmetic -----------------------------------------------------


def test_formula_registry_is_complete_and_versioned():
    assert financial_formulas.FORMULA_REGISTRY_VERSION == "formulas/2"
    for code, f in FORMULAS.items():
        assert f.formula_id == f"{code}/2" and f.required_inputs and f.periods and f.sectors
    used = {m.formula_id for m in A.all_metrics()}
    for formula_id in used:
        code, _, *variant = formula_id.split("/")
        definition = FORMULAS[code]
        assert not variant or variant[0] in {v.name for v in definition.variants}
    assert {f.formula_id for f in A.manifest.formulas} == {
        FORMULAS[m.code].formula_id for m in A.all_metrics()
    }
    assert A.manifest.rule_registry_version == financial_rules.RULE_REGISTRY_VERSION


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (growth(Decimal(110), Decimal(100)), Decimal(10)),
        (ratio(Decimal(1), Decimal(4), 100), Decimal(25)),
        (positive_ratio(Decimal(3), Decimal(4)), Decimal("0.75")),
        (average(Decimal(100), Decimal(200)), Decimal(150)),
        (days(Decimal(100), Decimal(365)), Decimal(100)),
        (cash_conversion_cycle(Decimal(60), Decimal(30), Decimal(45)), Decimal(45)),
        (free_cash_flow(Decimal(100), Decimal(-30)), Decimal(70)),
        (free_cash_flow(Decimal(100), Decimal(30)), Decimal(70)),
        (dupont_roe(Decimal(10), Decimal(2), Decimal("1.5")), Decimal(30)),
        (nopat(Decimal(200), Decimal(25)), Decimal(150)),
    ],
)
def test_pure_formulas(result, expected):
    assert result == expected


def test_pure_formula_refusals():
    assert growth(Decimal(5), Decimal(0)).status == S.DIVISION_BY_ZERO
    assert growth(Decimal(5), Decimal(-4)).interpretation == "NEGATIVE_BASE"
    assert ratio(Decimal(1), Decimal(0)).status == S.DIVISION_BY_ZERO
    assert positive_ratio(Decimal(1), Decimal(-2)).status == S.NOT_APPLICABLE
    assert cagr(Decimal(5), Decimal(-4), 3).status == S.NOT_APPLICABLE
    assert cagr(Decimal(-5), Decimal(4), 3).interpretation == "NON_POSITIVE_ENDPOINT"
    assert cagr(Decimal(5), Decimal(0), 3).status == S.DIVISION_BY_ZERO
    assert cagr(Decimal(5), Decimal(4), 0).status == S.INVALID_INPUT
    assert q(cagr(Decimal(1500), Decimal(1000), 5)) == q("8.447177")


# --- period alignment ---------------------------------------------------------------------------


def test_period_selection():
    roles = {p.role: p for p in A.periods if p.reporting_basis == B.CONSOLIDATED}
    assert roles[PeriodRole.LATEST_FY].period.period_end == FY26
    assert roles[PeriodRole.PREVIOUS_FY].period.period_end == FY25
    assert roles[PeriodRole.WINDOW_5Y].start_period.period_end == FY21
    assert roles[PeriodRole.WINDOW_10Y].status == "UNAVAILABLE"
    assert roles[PeriodRole.PREVIOUS_TTM].status == "UNAVAILABLE"


def test_average_balances_record_opening_and_closing_inputs():
    roe = metric(A, "ROE")
    roles = {i.role: i for i in roe.provenance}
    assert roles["equity_open.share_capital"].period.period_end == FY25
    assert roles["equity_close.reserves"].period.period_end == FY26
    assert roe.comparison_period.period_end == FY25
    assert roe.input_periods == tuple(sorted({i.period.key() for i in roe.provenance}))
    first = metric(A, "ROE", FY21)  # no FY2020 opening balance: no average, no proxy
    assert (first.status, first.value) == (S.INSUFFICIENT_HISTORY, None)


# --- A. healthy industrial: exact formulas ---------------------------------------------------


def test_growth_yoy_and_cagr():
    m = metric(A, "REVENUE_GROWTH_YOY")
    assert (m.status, m.value, m.formula_id) == (
        S.CALCULATED,
        q("7.142857"),
        "REVENUE_GROWTH_YOY/2",
    )
    assert {i.value for i in m.provenance} == {Decimal(1500), Decimal(1400)}
    assert metric(A, "REVENUE_CAGR_5Y").value == q("8.447177")
    assert metric(A, "REVENUE_CAGR_3Y").value == q("7.721735")
    assert metric(A, "REVENUE_CAGR_10Y").status == S.INSUFFICIENT_HISTORY
    assert metric(A, "EBIT_GROWTH_YOY").value == q(str((Decimal(490) / 450 - 1) * 100))
    assert metric(A, "REVENUE_GROWTH_YOY", FY21).status == S.INSUFFICIENT_HISTORY


def test_profitability():
    assert metric(A, "EBITDA").value == Decimal(530)
    assert metric(A, "EBITDA").formula_id == "EBITDA/2/DERIVED"
    assert metric(A, "EBITDA_MARGIN").value == q(str(Decimal(530) * 100 / 1500))
    assert metric(A, "OPERATING_MARGIN").value == q(str(Decimal(500) * 100 / 1500))
    assert metric(A, "EBIT_MARGIN").value == q(str(Decimal(490) * 100 / 1500))
    assert metric(A, "PAT_MARGIN").value == q(str(Decimal("352.5") * 100 / 1500))
    assert metric(A, "GROSS_MARGIN").value == q(str(Decimal(1500 - 590) * 100 / 1500))
    assert metric(A, "TAX_RATE").value == q("25") and metric(A, "TAX_RATE").formula_id.endswith(
        "/TOTAL_TAX"
    )
    assert metric(A, "OTHER_INCOME_TO_PBT").value == q(str(Decimal(30) * 100 / 470))
    assert metric(A, "POWER_AND_FUEL_TO_REVENUE").status == S.INPUT_MISSING
    pat26, pat25 = q(str(Decimal("352.5") * 100 / 1500)), q(str(Decimal("322.5") * 100 / 1400))
    assert metric(A, "PAT_MARGIN_CHANGE_YOY").value == q(str((pat26 - pat25) * 100))
    assert metric(A, "PAT_MARGIN_CHANGE_YOY").unit == AnalyticsUnit.BASIS_POINTS


def test_returns_and_dupont():
    assert metric(A, "ROE").value == q(str(Decimal("352.5") * 100 / 1450))
    assert metric(A, "ROA").value == q(str(Decimal("352.5") * 100 / 2675))
    assert metric(A, "ROCE").value == q(str(Decimal(490) * 100 / 1750))
    assert metric(A, "ROIC").value == q(str(q(str(Decimal(490) * Decimal("0.75"))) * 100 / 1510))
    assert metric(A, "RETURN_ON_TANGIBLE_CAPITAL").value == q(str(Decimal(490) * 100 / 1680))
    implied, direct = metric(A, "DUPONT_IMPLIED_ROE"), metric(A, "ROE")
    difference = metric(A, "DUPONT_ROE_DIFFERENCE")
    assert difference.value == implied.value - direct.value
    assert abs(difference.value) <= financial_formulas.DUPONT_TOLERANCE_PP
    assert "DUPONT_TOLERANCE_EXCEEDED" not in difference.warnings
    assert metric(A, "DUPONT_EQUITY_MULTIPLIER").value == q(str(Decimal(2675) / 1450))


def test_leverage_and_liquidity():
    assert metric(A, "DEBT_TO_EQUITY").value == q(str(Decimal(300) / 1500))
    assert metric(A, "LEASE_ADJUSTED_DEBT_TO_EQUITY").value == q(str(Decimal(340) / 1500))
    assert metric(A, "DEBT_TO_CAPITAL").value == q(str(Decimal(300) / 1800))
    assert metric(A, "LIABILITIES_TO_ASSETS").value == q(str(Decimal(1250) / 2750))
    net = next(m for m in A.building_blocks if m.code == "NET_DEBT" and m.period.period_end == FY26)
    assert (net.value, net.interpretation) == (Decimal(50), None)
    assert metric(A, "DEBT_TO_EBITDA").value == q(str(Decimal(300) / 530))
    assert metric(A, "INTEREST_COVERAGE").value == q("24.5")
    assert metric(A, "CURRENT_RATIO").value == q(str(Decimal(1100) / 500))
    assert metric(A, "QUICK_RATIO").value == q(str(Decimal(900) / 500))
    assert metric(A, "CASH_RATIO").value == q(str(Decimal(250) / 500))


def test_working_capital_days():
    receivable = Decimal(625) / 2 * 365 / 1500
    inventory = Decimal(390) / 2 * 365 / 590
    payable = Decimal(285) / 2 * 365 / 590
    assert metric(A, "RECEIVABLE_DAYS").value == q(receivable)
    assert metric(A, "INVENTORY_DAYS").value == q(inventory)
    assert metric(A, "PAYABLE_DAYS").value == q(payable)
    assert metric(A, "CASH_CONVERSION_CYCLE").value == q(q(receivable) + q(inventory) - q(payable))
    assert metric(A, "NWC_CHANGE").value == Decimal(40)


def test_cash_flow_and_capital_allocation():
    assert metric(A, "FCF").value == Decimal("382.5") - 85
    assert metric(A, "CAPEX").formula_id == "CAPEX/2/PURCHASE_OF_PPE"
    assert metric(A, "CFO_TO_PAT").value == q(str(Decimal("382.5") / Decimal("352.5")))
    assert metric(A, "CFO_TO_EBITDA").value == q(str(Decimal("382.5") / 530))
    assert metric(A, "FCF_TO_PAT").value == q(str(Decimal("297.5") / Decimal("352.5")))
    assert metric(A, "CAPEX_TO_REVENUE").value == q(str(Decimal(85) * 100 / 1500))
    assert metric(A, "CAPEX_TO_DEPRECIATION").value == q(str(Decimal(85) / 40))
    assert metric(A, "DIVIDEND_PAYOUT").value == q(str(Decimal(50) * 100 / Decimal("352.5")))
    assert metric(A, "SHAREHOLDER_DISTRIBUTION_TO_FCF").value == q(
        str(Decimal(60) / Decimal("297.5"))
    )
    assert metric(A, "CASH_TAX_RATE").value == q(str(Decimal("127.5") * 100 / 470))
    assert metric(A, "CASH_TAX_VS_BOOK_TAX_GAP").value == q(str(Decimal("127.5") * 100 / 470)) - 25
    assert metric(A, "CASH_INTEREST_COVERAGE").value == q(str(Decimal("402.5") / 20))
    assert metric(A, "CASH_CHANGE").value == Decimal(20)
    assert metric(A, "NET_DEBT_CHANGE").value == Decimal(-20)
    reinvest = metric(A, "REINVESTMENT_RATE")
    assert reinvest.value == q(
        str((Decimal(85) - 40 + 40) * 100 / q(str(Decimal(490) * Decimal("0.75"))))
    )
    pats = [Decimal(270 + 40 * k) * Decimal("0.75") for k in (3, 4, 5)]
    assert metric(A, "CUMULATIVE_CFO_TO_PAT_3Y").value == q(str((sum(pats) + 90) / sum(pats)))
    assert metric(A, "ACQUISITION_SPEND").status == S.INPUT_MISSING  # never zero


def test_accruals():
    assert metric(A, "TOTAL_ACCRUALS").value == Decimal(-30)
    assert metric(A, "CASH_FLOW_ACCRUAL_RATIO").value == q(str(Decimal(-30) * 100 / 2675))
    bs = (Decimal(60) - Decimal(20) - Decimal(20) - 40) * 100 / 2675
    assert metric(A, "BALANCE_SHEET_ACCRUAL_RATIO").value == q(str(bs))
    assert metric(A, "NON_CASH_EARNINGS_SHARE").value == q(
        str(Decimal(-30) * 100 / Decimal("352.5"))
    )
    assert (
        metric(A, "CASH_FLOW_ACCRUAL_RATIO").formula_id
        != metric(A, "BALANCE_SHEET_ACCRUAL_RATIO").formula_id
    )


# --- B-G. other company shapes -----------------------------------------------------------------


def test_b_deteriorating_company():
    rows = replace(
        company(), M.OPERATING_PROFIT, {y: 400 - 40 * (y - 2021) for y in range(2021, 2027)}
    )
    rows = replace(rows, M.CFO, {y: 400 - 50 * (y - 2021) for y in range(2021, 2027)})
    a = analyse(rows)
    trends = {t.series_code: t for t in a.trends}
    assert trends["OPERATING_MARGIN"].classification == TrendClass.STRONGLY_DETERIORATING
    assert trends["OPERATING_MARGIN"].direction_consistency == q("1")
    assert any(x.code == "REVENUE_UP_CFO_DOWN" for x in a.anomalies)
    health = {h.dimension: h.state for h in a.health}
    assert health[HealthDimension.CASH_CONVERSION] in (
        HealthState.WATCH,
        HealthState.WEAK,
        HealthState.NEUTRAL,
    )


def test_c_negative_fcf_company():
    rows = replace(company(), M.PURCHASE_OF_PPE, {y: -600 for y in range(2021, 2027)})
    a = analyse(rows)
    assert metric(a, "FCF").value < 0
    assert metric(a, "FCF_TO_PAT").value < 0
    assert metric(a, "DIVIDENDS_TO_FCF").status == S.NOT_APPLICABLE  # negative FCF denominator
    assert any(
        x.code == "REPEATED_NEGATIVE_FCF" and x.severity == Severity.HIGH for x in a.anomalies
    )


def test_d_high_debt_company():
    rows = replace(
        company(), M.TOTAL_BORROWINGS, {y: 3000 + 200 * (y - 2021) for y in range(2021, 2027)}
    )
    rows = replace(rows, M.FINANCE_COST, {y: 250 for y in range(2021, 2027)})
    a = analyse(rows)
    assert metric(a, "DEBT_TO_EQUITY").value == q(str(Decimal(4000) / 1500))
    assert metric(a, "INTEREST_COVERAGE").value == q(str(Decimal(470 + 250) / 250))
    assert metric(a, "NET_DEBT_TO_EBITDA").value > 1
    states = {h.dimension: h.state for h in a.health}
    assert states[HealthDimension.BALANCE_SHEET] in (HealthState.WATCH, HealthState.WEAK)


def test_e_loss_making_company():
    pats = {y: Decimal(270 + 40 * (y - 2021)) * Decimal("0.75") for y in range(2021, 2027)}
    rows = replace(company(), M.PAT, pats | {2025: 50, 2026: -40})
    a = analyse(rows)
    assert metric(a, "PAT_MARGIN").value == q(str(Decimal(-40) * 100 / 1500))
    assert metric(a, "PAT_GROWTH_YOY").value == q("-180")
    assert metric(a, "CFO_TO_PAT").status == S.NOT_APPLICABLE  # negative PAT denominator
    assert metric(a, "PAT_CAGR_5Y").interpretation == "NON_POSITIVE_ENDPOINT"


def test_f_negative_starting_value_cagr():
    a = analyse(replace(company(), M.PAT, {2021: -50, 2023: 0, 2025: 100, 2026: 120}))
    five = metric(a, "PAT_CAGR_5Y")
    assert (five.status, five.value, five.interpretation) == (
        S.NOT_APPLICABLE,
        None,
        "NON_POSITIVE_ENDPOINT",
    )
    assert metric(a, "PAT_CAGR_3Y").status == S.DIVISION_BY_ZERO
    assert metric(a, "PAT_GROWTH_YOY").value == q("20")


def test_g_missing_periods():
    rows = [r for r in company() if r.period not in (fy(2024), pit(2024))]
    a = analyse(rows)
    assert metric(a, "REVENUE_GROWTH_YOY", date(2025, 3, 31)).status == S.INSUFFICIENT_HISTORY
    assert metric(a, "ROE", date(2025, 3, 31)).status == S.INSUFFICIENT_HISTORY
    assert metric(a, "REVENUE_CAGR_5Y").status == S.CALCULATED  # endpoints exist
    assert metric(a, "CUMULATIVE_CFO_TO_PAT_3Y").status == S.INSUFFICIENT_HISTORY


# --- H, P. conflicts, restatements, confidence ------------------------------------------------


def test_h_conflicted_inputs():
    rows = replace(
        company(),
        M.PAT,
        {y: Decimal(270 + 40 * (y - 2021)) * Decimal("0.75") for y in range(2021, 2026)},
    )
    rows += [V(M.PAT, fy(2026), None, outcome=Outcome.CONFLICT)]
    a = analyse(rows)
    roe = metric(a, "ROE")
    assert (roe.status, roe.value) == (S.INPUT_CONFLICT, None)
    assert any("CONFLICT" in note for note in roe.missing_inputs)
    assert any(w.code == "BOX4_UNRESOLVED_CONFLICTS" for w in a.warnings)
    precedence = [
        r for r in company() if not (r.metric == M.TOTAL_ASSETS and r.period == pit(2026))
    ]
    precedence += [
        V(
            M.TOTAL_ASSETS,
            pit(2026),
            2750,
            outcome=Outcome.SOURCE_PRECEDENCE,
            max_difference=Decimal(20),
        )
    ]
    roa = metric(analyse(precedence), "ROA")
    assert "INPUT_SOURCE_PRECEDENCE" in roa.warnings and roa.confidence == Confidence.MEDIUM
    material = precedence[:-1] + [
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
    low = [r for r in company() if not (r.metric == M.PAT and r.period == fy(2026))]
    low += [V(M.PAT, fy(2026), None, outcome=Outcome.NOT_COMPARABLE)]
    assert metric(analyse(low), "PAT_MARGIN").status == S.LOW_CONFIDENCE_INPUT


def test_p_restated_inputs_are_flagged():
    rows = [
        r for r in company() if not (r.metric == M.REVENUE_FROM_OPERATIONS and r.period == fy(2025))
    ]
    rows += [V(M.REVENUE_FROM_OPERATIONS, fy(2025), 1400, outcome=Outcome.RESTATEMENT)]
    m = metric(analyse(rows), "REVENUE_GROWTH_YOY")
    assert m.status == S.CALCULATED and "INPUT_RESTATEMENT" in m.warnings
    assert m.confidence == Confidence.MEDIUM
    assert any(i.outcome == Outcome.RESTATEMENT for i in m.provenance)


def test_confidence_reflects_source_authority():
    filing = metric(A, "CFO_TO_PAT")
    assert filing.confidence == Confidence.HIGH
    rows = [
        V(r.metric, r.period, r.value, source=SourceType.SCREENER, kind=r.kind) for r in company()
    ]
    aggregator = metric(analyse(rows), "CFO_TO_PAT")
    assert aggregator.value == filing.value
    assert aggregator.confidence == Confidence.MEDIUM
    assert "INPUT_AGGREGATOR_ONLY" in aggregator.warnings
    weaker = replace(company(), M.CFO, {2026: "382.5"})
    weaker = [
        r
        if not (r.metric == M.CFO and r.period == fy(2026))
        else V(M.CFO, fy(2026), "382.5", confidence=Confidence.MEDIUM)
        for r in weaker
    ]
    assert metric(analyse(weaker), "CFO_TO_PAT").confidence == Confidence.MEDIUM


# --- I, J. basis / period / currency safety -------------------------------------------------


def test_i_standalone_and_consolidated_never_mix():
    rows = [r for r in company() if r.period not in (fy(2025), pit(2025))]
    rows += company_year(2025, basis=B.STANDALONE)
    a = analyse(rows)
    assert metric(a, "REVENUE_GROWTH_YOY").status == S.INSUFFICIENT_HISTORY
    assert set(a.reporting_bases) == {B.CONSOLIDATED, B.STANDALONE}
    for m in a.all_metrics():
        assert {i.reporting_basis for i in m.provenance} <= {m.reporting_basis}


def test_j_fy_ttm_quarter_and_ytd_never_mix():
    q1, q0 = date(2025, 12, 31), date(2024, 12, 31)
    rows = company() + [
        V(M.REVENUE_FROM_OPERATIONS, TTM, 1550, source=SourceType.SCREENER),
        V(M.PAT, TTM, 360, source=SourceType.SCREENER),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q1), 420),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q0), 390),
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2025, 9, 30)), 400),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q1, 9, PeriodType.NINE_MONTH), 1200),
        V(M.REVENUE_FROM_OPERATIONS, quarter(q0, 9, PeriodType.NINE_MONTH), 1100),
    ]
    a = analyse(rows)
    yoy = metric(a, "REVENUE_GROWTH_YOY", q1, ptype=PeriodType.QUARTER)
    assert yoy.value == q(str((Decimal(420) / 390 - 1) * 100))
    assert {i.period.period_type for i in yoy.provenance} == {PeriodType.QUARTER}
    qoq = metric(a, "REVENUE_GROWTH_QOQ", q1, ptype=PeriodType.QUARTER)
    assert qoq.value == q(str((Decimal(420) / 400 - 1) * 100))
    ttm = [m for m in a.all_metrics() if m.period.period_type == PeriodType.TTM]
    assert {m.code for m in ttm if m.status == S.CALCULATED} >= {"PAT_MARGIN"}
    assert not any(m.category == MetricCategory.GROWTH for m in ttm)
    annual = metric(a, "REVENUE_GROWTH_YOY")
    assert {i.period.period_type for i in annual.provenance} == {PeriodType.FY}
    assert compare_periods(fy(2026), TTM, 12) == S.PERIOD_MISMATCH
    assert compare_periods(quarter(q1), quarter(q0, 9, PeriodType.NINE_MONTH), 12) == (
        S.PERIOD_MISMATCH
    )


def test_point_in_time_denominators_use_the_fiscal_year_end():
    for code in ("DEBT_TO_EQUITY", "CURRENT_RATIO"):
        m = metric(A, code)
        balance = [i for i in m.provenance if i.period.period_type == PeriodType.POINT_IN_TIME]
        assert balance and {i.period.period_end for i in balance} == {FY26}


def test_currency_mismatch_is_rejected():
    rows = company() + [
        V(M.TOTAL_ASSETS, pit(2027), 30, currency=Currency.USD),
        V(M.EMPLOYEE_COST, fy(2026), 18, currency=Currency.USD),
    ]
    rows = [
        r
        for r in rows
        if not (r.metric == M.EMPLOYEE_COST and r.currency == Currency.INR and r.period == fy(2026))
    ]
    a = analyse(rows)
    assert [f.currency for f in a.frames] == [Currency.INR]
    assert a.frames[0].excluded_other_currency_values == 2
    assert metric(a, "EMPLOYEE_COST_TO_REVENUE").status == S.CURRENCY_MISMATCH
    assert all(i.currency == Currency.INR for m in a.all_metrics() for i in m.provenance)


# --- K, L. banks --------------------------------------------------------------------------------


def test_k_bank_safety():
    rows = company() + [V(M.DEPOSITS, pit(y), 5000 + 400 * (y - 2021)) for y in range(2021, 2027)]
    a = analyse(rows, industry=Industry.BANK)
    assert a.sector == Sector.FINANCIAL
    for code in (
        "EBITDA_MARGIN",
        "ROCE",
        "ROIC",
        "INVENTORY_DAYS",
        "PAYABLE_DAYS",
        "CASH_CONVERSION_CYCLE",
        "DEBT_TO_EBITDA",
        "DUPONT_IMPLIED_ROE",
        "EBITDA",
    ):
        m = metric(a, code)
        assert (m.status, m.interpretation) == (S.UNSUPPORTED_SECTOR, "SECTOR_NOT_APPLICABLE")
    assert metric(a, "DEPOSITS_GROWTH_YOY").value == q(str((Decimal(7000) / 6600 - 1) * 100))
    assert metric(a, "ROE").status == S.CALCULATED and metric(a, "ROA").status == S.CALCULATED
    assert not [m for m in a.all_metrics() if m.code == "GROSS_DEBT" and m.value is not None]
    assert {m.status for m in a.forensic_scores} == {S.UNSUPPORTED_SECTOR}
    npa = metric(a, "GROSS_NPA_RATIO")
    assert (npa.status, npa.value) == (S.INPUT_MISSING, None) and npa.missing_inputs
    states = {h.dimension: h.state for h in a.health}
    assert states[HealthDimension.BALANCE_SHEET] == HealthState.NOT_APPLICABLE


async def test_l_yesbank_fixture(tmp_path, fixture_dir):
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
    assert a.industry == Industry.BANK and a.sector == Sector.FINANCIAL
    assert metric(a, "DEPOSITS_GROWTH_YOY").value == q(str((Decimal(318969) / 284420 - 1) * 100))
    for m in a.all_metrics():  # deposits never feed a leverage calculation
        if m.category == MetricCategory.LEVERAGE:
            assert all(i.metric != M.DEPOSITS for i in m.provenance)
        assert all(i.metric != M.FINANCE_COST for i in m.provenance)  # bank interest is not
    for code in ("EBITDA", "DEBT_TO_EBITDA", "ROIC", "INVENTORY_DAYS", "PAYABLE_DAYS"):
        assert metric(a, code).status == S.UNSUPPORTED_SECTOR
    assert metric(a, "GROSS_NPA_RATIO").status == S.INPUT_MISSING
    assert metric(a, "ROA").status == S.CALCULATED
    assert {r.status for r in a.forensic_scores} == {S.UNSUPPORTED_SECTOR}
    assert any(
        "npa" in r.raw_label.lower() and "GROSS_NPA_RATIO" in r.review_hints
        for r in a.unmapped.references
    )


async def test_infy_fixture(tmp_path, fixture_dir):
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
    m = metric(a, "REVENUE_GROWTH_YOY", basis=B.STANDALONE)
    assert m.value == q(str((Decimal(148819) / 136592 - 1) * 100))
    assert {i.selected_source for i in m.provenance} == {SourceType.SCREENER}
    assert m.confidence == Confidence.MEDIUM  # aggregator-only inputs
    assert metric(a, "OPERATING_MARGIN", basis=B.STANDALONE).value == q(
        str(Decimal(36013) * 100 / 148819)
    )
    assert metric(a, "CURRENT_RATIO", basis=B.STANDALONE).status == S.INPUT_MISSING


# --- M, N, O. missing inputs and zero denominators ------------------------------------------


def test_m_missing_capex():
    a = analyse(company(**{"PURCHASE_OF_PPE": None}))
    for code in ("CAPEX", "FCF", "FCF_TO_PAT", "CAPEX_TO_REVENUE", "CUMULATIVE_FCF_3Y"):
        m = metric(a, code)
        assert m.status == S.INPUT_MISSING and m.value is None, code
    assert "PURCHASE_OF_PPE" in metric(a, "CAPEX").missing_inputs[0]


def test_n_missing_working_capital_components():
    a = analyse(company(**{"RAW_MATERIAL_COST": None, "INVENTORY": None}))
    for code in (
        "INVENTORY_DAYS",
        "PAYABLE_DAYS",
        "CASH_CONVERSION_CYCLE",
        "QUICK_RATIO",
        "GROSS_MARGIN",
    ):
        assert metric(a, code).status == S.INPUT_MISSING, code
    assert metric(a, "RECEIVABLE_DAYS").status == S.CALCULATED  # revenue never replaces COGS


def test_o_zero_denominators():
    a = analyse(company(**{"REVENUE_FROM_OPERATIONS": 0, "FINANCE_COST": 0}))
    for code in (
        "PAT_MARGIN",
        "EBITDA_MARGIN",
        "RECEIVABLE_DAYS",
        "REVENUE_GROWTH_YOY",
        "INTEREST_COVERAGE",
    ):
        m = metric(a, code)
        assert (m.status, m.value) == (S.DIVISION_BY_ZERO, None), code


def test_missing_data_is_never_zero():
    a = analyse(
        [V(M.REVENUE_FROM_OPERATIONS, fy(2025), 1000), V(M.REVENUE_FROM_OPERATIONS, fy(2026), 1100)]
    )
    assert metric(a, "REVENUE_GROWTH_YOY").value == q("10")
    for m in a.all_metrics():
        if m.status != S.CALCULATED:
            assert m.value is None and m.confidence is None
    assert metric(a, "PAT_MARGIN").status == S.INPUT_MISSING


# --- unmapped lines, series, statistics, bounds, determinism ---------------------------------


def test_unmapped_lines_are_referenced_with_review_hints():
    line = UnmappedFinancialLine(
        line_id=uuid5(NS, "line"),
        statement_type=StatementType.CASH_FLOW,
        reporting_basis=B.CONSOLIDATED,
        raw_label="Expenditure on property, plant and equipment",
        label_context=None,
        source_type=SourceType.FILING,
        observation_ids=(),
    )
    a = analyse(company(**{"PURCHASE_OF_PPE": None}), unmapped=(line,))
    (ref,) = a.unmapped.references
    assert ref.review_hints == ("CAPEX", "FCF")
    fragment = UnmappedFinancialLine(
        line_id=uuid5(NS, "fragment"),
        statement_type=StatementType.CASH_FLOW,
        reporting_basis=B.CONSOLIDATED,
        raw_label="(Refer to Note",
        label_context="cash flows from investing activities > expenditure on property, plant",
        source_type=SourceType.FILING,
        observation_ids=(),
    )
    unrelated = fragment.model_copy(
        update={"line_id": uuid5(NS, "unrelated"), "raw_label": "Certificates of deposit"}
    )
    b = analyse(company(**{"PURCHASE_OF_PPE": None}), unmapped=(fragment, unrelated))
    hints = {r.raw_label: r.review_hints for r in b.unmapped.references}
    assert hints == {"(Refer to Note": ("CAPEX", "FCF"), "Certificates of deposit": ()}
    assert ref.hinted_calculations_unavailable == ("CAPEX", "FCF")
    assert a.unmapped.lines_with_unavailable_hints == 1
    assert any(w.code == "UNMAPPED_LINES_MAY_AFFECT_ANALYTICS" for w in a.warnings)
    assert a.statistics.unmapped_lines_referenced == 1


def test_series_are_sorted_and_referenced():
    codes = {(s.code, s.source) for s in A.series}
    assert {("REVENUE", "CANONICAL"), ("ROE", "CALCULATED"), ("FCF", "CALCULATED")} <= codes
    ids = {m.metric_id for m in A.all_metrics()}
    canonical = {cv.canonical_value_id for cv in make(company()).canonical_values}
    for s in A.series:
        ends = [p.period.period_end for p in s.points]
        assert ends == sorted(ends)
        for p in s.points:
            assert (p.metric_id in ids) if p.metric_id else (p.canonical_value_id in canonical)


def test_statistics():
    st = A.statistics
    assert st.calculated_metrics + st.unavailable_calculations == len(A.all_metrics())
    assert sum(st.metrics_by_status.values()) == len(A.all_metrics())
    assert st.annual_periods_analysed == 6 and st.series == len(A.series)
    assert st.forensic_models == {"COMPLETE": 3, "INCOMPLETE": 1}  # Altman 1968 needs market cap
    assert sum(st.confidence_counts.values()) == st.calculated_metrics


def test_resource_bounds():
    a = analyse(
        company(),
        limits=AnalyticsLimits(max_calculated_metrics=50, max_periods=3, max_provenance_refs=8),
    )
    assert a.status == "PARTIAL" and len(a.all_metrics()) == 50
    codes = {w.code for w in a.warnings}
    assert {"CALCULATED_METRIC_LIMIT_REACHED", "PERIOD_LIMIT_REACHED"} <= codes
    assert a.statistics.annual_periods_analysed == 3
    bounded = analyse(company(), limits=AnalyticsLimits(max_provenance_refs=4))
    cumulative = metric(bounded, "CUMULATIVE_CFO_TO_PAT_3Y")
    assert (cumulative.status, cumulative.provenance) == (S.INVALID_INPUT, ())
    assert any(w.code == "PROVENANCE_LIMIT_REACHED" for w in bounded.warnings)
    short = analyse(company(), limits=AnalyticsLimits(max_warning_chars=20))
    assert all(len(w.detail or "") <= 20 for w in short.warnings)


def test_t_deterministic_serialization():
    again = analyse(company())
    encoded = canonical_analytics_json(A)
    assert encoded == canonical_analytics_json(again)
    assert FinancialAnalyticsDataset.model_validate_json(encoded) == A
    assert json.loads(encoded)["schema_version"] == "FINANCIAL_ANALYTICS_DATASET_V2"
    assert A.source_box4.dataset_id == make(company()).dataset_id


def test_trend_rules():
    cases = [
        (["10", "11", "12.5"], "LEVEL", True, TrendClass.IMPROVING),
        (["10", "13", "16"], "LEVEL", True, TrendClass.STRONGLY_IMPROVING),
        (["10", "13", "16"], "LEVEL", False, TrendClass.STRONGLY_DETERIORATING),
        (["10", "10.2", "10.4"], "LEVEL", True, TrendClass.STABLE),
        (["10", "20", "8", "19"], "LEVEL", True, TrendClass.VOLATILE),
        (["10", "9", "12", "11", "13"], "LEVEL", True, TrendClass.STABLE),  # inconsistent, small
        (["21", "25", "28", "9.6"], "LEVEL", True, TrendClass.VOLATILE),  # large, one reversal
        (["100", "110", "125"], "MONEY", True, TrendClass.IMPROVING),
        (["100", "120"], "LEVEL", True, TrendClass.INSUFFICIENT_HISTORY),
        (["0", "5", "9"], "MONEY", True, TrendClass.INSUFFICIENT_HISTORY),
        (["60", "70", "80"], "DAYS", False, TrendClass.STRONGLY_DETERIORATING),
    ]
    for values, kind, higher, expected in cases:
        assert classify_trend([Decimal(v) for v in values], kind, higher)[0] == expected, values


# --- provenance and architecture -----------------------------------------------------------------


def test_every_calculation_reaches_box4_observations():
    data = make(company())
    a = build_financial_analytics(data, generated_at=NOW)
    canonical = {v.canonical_value_id: v for v in data.canonical_values}
    observations = {o.observation_id: o for o in data.observations}
    ids = {m.metric_id for m in a.all_metrics()}
    for m in a.all_metrics():
        if m.status == S.CALCULATED:
            assert m.provenance
        assert set(m.input_metric_ids) <= ids
        for i in m.provenance:
            value = canonical[i.canonical_value_id]
            observation = observations[value.selected_observation_id]
            assert (i.value, i.selected_observation_id, i.reconciliation_record_id) == (
                value.value,
                value.selected_observation_id,
                value.reconciliation_record_id,
            )
            assert i.source_locator == observation.source.locator
            assert i.selected_authority == observation.authority
    for check in a.earnings_quality:
        assert set(check.canonical_value_ids) <= set(canonical)
    for anomaly in a.anomalies:
        assert set(anomaly.canonical_value_ids) <= set(canonical)


def test_contract_is_strict():
    m = metric(A, "ROE")
    with pytest.raises(ValidationError):
        CalculatedMetric.model_validate({**m.model_dump(), "provenance": (), "input_value_ids": ()})
    with pytest.raises(ValidationError):
        CalculatedMetric.model_validate({**m.model_dump(), "status": "INPUT_MISSING"})
    with pytest.raises(ValidationError):
        CalculatedMetric.model_validate({**m.model_dump(), "input_value_ids": ()})
    with pytest.raises(ValidationError):
        FinancialAnalyticsDataset.model_validate({**A.model_dump(), "surprise": 1})
    misplaced = {**A.model_dump(), "growth": A.model_dump()["profitability"]}
    with pytest.raises(ValidationError):
        FinancialAnalyticsDataset.model_validate(misplaced)


def test_no_network_ai_or_secret_dependencies():
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
        "secret",
        "keychain",
        "browser",
        "config",
    )
    for module in (
        financial_analytics,
        financial_forensics,
        financial_metrics,
        financial_formulas,
        financial_rules,
        analytics_domain,
    ):
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
