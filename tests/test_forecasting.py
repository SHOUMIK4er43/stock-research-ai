"""DETAILED_RESEARCH_V1 Box 10: deterministic forecasting (offline, no valuation).

Companies come from the Box 5 Box-4 dataset factory (`make`), so every forecast is read
through the same reconciled canonical values the other boxes use. Expected values are
computed by hand from the fixture numbers, never read back from the engine.
"""

import ast
import asyncio
import time
import tracemalloc
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_financial_analytics import TTM, V, fy, make, pit, quarter
from test_screener_dataset import dataset as screener_dataset
from test_summary_input import INFY

from stock_research_ai.application import forecasting as engine
from stock_research_ai.application.financial_reconciliation import (
    build_canonical_dataset,
    canonical_dataset_json,
)
from stock_research_ai.application.forecasting import (
    FORMULAS,
    ForecastInputError,
    build_forecast,
    build_forecast_async,
    canonical_forecast_json,
    evaluate,
)
from stock_research_ai.domain import forecasting as domain
from stock_research_ai.domain import management_intelligence as mi
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    Outcome,
    PeriodType,
    ReportingBasis,
    Scale,
    ValueKind,
)
from stock_research_ai.domain.forecasting import (
    AssumptionInput,
    AssumptionSource,
    AssumptionUnit,
    DerivationMethod,
    Driver,
    ForecastDataset,
    ForecastLine,
    ForecastRequest,
    ForecastStatus,
    HistoryCompleteness,
    OperatingRoute,
    Scenario,
)

M = CanonicalMetric
B = ReportingBasis
L = ForecastLine
D = Driver
S = Scenario
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
LATER = datetime(2030, 1, 1, tzinfo=UTC)
GROWING = [1000, 1100, 1210, 1331, "1464.1", "1610.51"]  # exactly +10 % a year
P = 34  # the engine's working precision


def dec(x: object) -> Decimal:
    return Decimal(str(x))


def company(
    revenues: list[object | None],
    *,
    start: int = 2021,
    margin: object | list[object] = 20,
    other: object = 2,
    da: object = 3,
    rate: object = 8,
    tax: object = 25,
    debt: object = 1000,
    basis: ReportingBasis = B.CONSOLIDATED,
    capex: object = 5,
    wc: tuple[object, object, object] = (15, 10, 5),
    minority: object | None = None,
    drop: frozenset[tuple[CanonicalMetric, int]] = frozenset(),
    currency: Currency = Currency.INR,
    tax_metric: CanonicalMetric = M.TOTAL_TAX,
) -> list[V]:
    """A Screener-shaped company whose identities hold exactly (PBT = OP + OI - D&A - FC)."""
    rows: list[V] = []
    margins = margin if isinstance(margin, list) else [margin] * len(revenues)
    for i, raw in enumerate(revenues):
        year = start + i

        def add(
            metric: CanonicalMetric, value: Decimal, balance: bool = False, year: int = year
        ) -> None:
            if (metric, year) not in drop:
                period = pit(year) if balance else fy(year)
                rows.append(V(metric, period, value, basis=basis, currency=currency))

        b = dec(debt)
        add(M.TOTAL_BORROWINGS, b, True)
        if raw is None:
            continue
        rev = dec(raw)
        op, oi, d = rev * dec(margins[i]) / 100, rev * dec(other) / 100, rev * dec(da) / 100
        fc = b * dec(rate) / 100
        pbt = op + oi - d - fc
        tx = pbt * dec(tax) / 100 if pbt > 0 else Decimal(0)
        pat = pbt - tx
        add(M.REVENUE_FROM_OPERATIONS, rev)
        add(M.OPERATING_PROFIT, op)
        add(M.OTHER_INCOME, oi)
        add(M.DEPRECIATION_AMORTIZATION, d)
        add(M.FINANCE_COST, fc)
        add(M.PBT, pbt)
        add(tax_metric, tx)
        add(M.PAT, pat)
        if minority is not None:
            add(M.MINORITY_INTEREST, pat * dec(minority) / 100)
        add(M.PURCHASE_OF_PPE, -rev * dec(capex) / 100)
        add(M.TRADE_RECEIVABLES, rev * dec(wc[0]) / 100, True)
        add(M.INVENTORY, rev * dec(wc[1]) / 100, True)
        add(M.TRADE_PAYABLES, rev * dec(wc[2]) / 100, True)
    return rows


def run(
    rows: list[V] | CanonicalFinancialDataset,
    *,
    fy_end: int | None = 3,
    industry: Industry = Industry.GENERAL,
    management: mi.ManagementIntelligenceDataset | None = None,
    **request: object,
) -> ForecastDataset:
    data = rows if isinstance(rows, CanonicalFinancialDataset) else make(rows, industry, fy_end)
    return build_forecast(
        data,
        request=ForecastRequest.model_validate(request),
        management=management,
        generated_at=NOW,
    )


def explicit(
    driver: Driver, value: object, scenario: Scenario = S.BASE, year: int = 2027, **kw: object
) -> AssumptionInput:
    unit = domain.DRIVER_SPECS[driver].unit
    extra: dict[str, object] = {}
    if unit == AssumptionUnit.MONEY:
        extra = {"currency": Currency.INR, "scale": Scale.CRORES}
    elif unit == AssumptionUnit.COUNT:
        extra = {"scale": Scale.ONES}
    return AssumptionInput(
        driver=driver,
        scenario=scenario,
        fiscal_year=year,
        value=dec(value),
        unit=unit,
        **(extra | kw),  # type: ignore[arg-type]
    )


def val(f: ForecastDataset, line: ForecastLine, scenario: Scenario = S.BASE, year: int = 2027):
    return f.value(line, scenario, year)


def num(f: ForecastDataset, line: ForecastLine, scenario: Scenario = S.BASE, year: int = 2027):
    value = val(f, line, scenario, year).value
    assert value is not None, (line, scenario, year, val(f, line, scenario, year).reason)
    return value


def codes(f: ForecastDataset) -> set[str]:
    return {i.code for i in f.issues}


def mul(*xs: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = P
        out = Decimal(1)
        for x in xs:
            out *= x
        return out


# --- 1-6. growth shapes, margins, losses ------------------------------------------------------


def test_flat_growth_company():
    f = run(company([1000] * 6))
    for s in S:
        a = f.assumption(D.REVENUE_GROWTH, s, 2027)
        assert a is not None and a.value == 0
        assert [num(f, L.REVENUE, s, y) for y in (2027, 2028, 2029)] == [1000] * 3
    assert f.base.annual_periods[0] == "FY|2022-03-31|12"  # 5-year window ending FY2026
    assert f.base.latest_actual and f.base.latest_actual.fiscal_year == 2026


def test_growing_company_cagr_and_compounding():
    f = run(company(GROWING))
    base = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    assert base and (base.value, base.method) == (
        Decimal("10.000000"),
        DerivationMethod.HISTORICAL_CAGR,
    )
    assert base.historical_periods == ("FY|2022-03-31|12", "FY|2026-03-31|12")
    assert base.completeness == HistoryCompleteness.FULL
    assert num(f, L.REVENUE) == dec("1610.51") * dec("1.1")
    assert num(f, L.REVENUE, year=2029) == dec("1610.51") * dec("1.1") ** 3
    assert num(f, L.OPERATING_PROFIT) == num(f, L.REVENUE) * dec("0.2")


def test_shrinking_company():
    f = run(company([1000, 900, 810, 729, "656.1", "590.49"]))
    assert f.assumption(D.REVENUE_GROWTH, S.BASE, 2027).value == Decimal("-10.000000")
    assert num(f, L.REVENUE) == dec("590.49") * dec("0.9")


def test_negative_margin_company_needs_explicit_tax_and_pays_none_on_a_loss():
    rows = company([1000] * 6, margin=-30)
    f = run(rows)
    assert f.assumption(D.OPERATING_MARGIN, S.BASE, 2027).value == Decimal("-30.000000")
    assert num(f, L.PBT) < 0
    assert val(f, L.TAX).reason == "ASSUMPTION_MISSING"
    assert val(f, L.PAT).reason == "UPSTREAM_UNAVAILABLE" and val(f, L.PAT).value is None
    assert "TAX_ASSUMPTION_REQUIRED" in codes(f)
    taxed = run(
        rows,
        explicit_assumptions=[
            explicit(D.TAX_RATE, 25, s, y) for s in S for y in (2027, 2028, 2029)
        ],
    )
    tax = val(taxed, L.TAX)
    assert tax.value == 0 and "NO_TAX_ON_LOSS" in tax.warnings
    assert num(taxed, L.PAT) == num(taxed, L.PBT)


def test_loss_making_company_keeps_pbt_and_never_fabricates_pat():
    f = run(company([500, 520, 540], start=2024, margin=-5))
    assert num(f, L.PBT) < 0
    assert val(f, L.PAT).status == "UNAVAILABLE" and val(f, L.EPS).value is None
    assert f.status == ForecastStatus.PARTIAL


def test_company_moving_from_loss_to_profit():
    rows = company([1000, 1100, 1210], margin=[-10, -5, -2])
    assumptions = [explicit(D.OPERATING_MARGIN, 30, S.BASE, y) for y in (2024, 2025, 2026)]
    assumptions += [explicit(D.TAX_RATE, 25, S.BASE, y) for y in (2024, 2025, 2026)]
    f = run(rows, scenarios=[S.BASE], explicit_assumptions=assumptions)
    margin = f.assumption(D.OPERATING_MARGIN, S.BASE, 2024)
    assert margin.source == AssumptionSource.EXPLICIT
    assert margin.overrides and margin.overrides[0].startswith("HISTORICAL:HISTORICAL_MEDIAN:-5")
    pbt = num(f, L.PBT, year=2024)
    assert pbt > 0 and num(f, L.TAX, year=2024) == pbt * dec("0.25")
    assert num(f, L.PAT, year=2024) == pbt - num(f, L.TAX, year=2024)


# --- 7-9. missing years and metrics -----------------------------------------------------------


def test_missing_historical_year_breaks_the_chain_and_marks_sparse_history():
    rows = [r for r in company(GROWING) if r.period.fiscal_year != 2023]
    f = run(rows)
    assert f.base.annual_periods == ("FY|2024-03-31|12", "FY|2025-03-31|12", "FY|2026-03-31|12")
    assert ("FY|2022-03-31|12", "NOT_CONTIGUOUS") in f.base.excluded_periods
    growth = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    # CAGR over FY2024 -> FY2026 only: never across the gap
    assert growth.historical_periods == ("FY|2024-03-31|12", "FY|2026-03-31|12")
    assert growth.value == Decimal("10.000000")
    assert growth.completeness == HistoryCompleteness.SPARSE and "SPARSE_HISTORY" in growth.warnings
    assert growth.confidence != Confidence.HIGH


def test_missing_optional_metric_is_unavailable_not_zero():
    rows = company(GROWING, drop=frozenset((M.PURCHASE_OF_PPE, y) for y in range(2021, 2027)))
    f = run(rows)
    assert f.assumption(D.CAPEX_TO_REVENUE, S.BASE, 2027) is None
    for line in (L.CAPEX, L.FCF_ESTIMATE):
        v = val(f, line)
        assert (v.status, v.value) == ("UNAVAILABLE", None)
    assert val(f, L.CFO_ESTIMATE).status == "AVAILABLE" and val(f, L.PAT).status == "AVAILABLE"
    assert f.status == ForecastStatus.PARTIAL


def test_missing_critical_metric_produces_no_forecast():
    rows = company(GROWING, drop=frozenset({(M.REVENUE_FROM_OPERATIONS, 2026)}))
    f = run(rows)
    assert f.status == ForecastStatus.UNAVAILABLE and not f.values and not f.assumptions
    assert "REVENUE_BASE_MISSING" in codes(f)


def test_conflicted_latest_revenue_is_never_used():
    rows = [
        V(r.metric, r.period, None, outcome=Outcome.CONFLICT)
        if r.metric == M.REVENUE_FROM_OPERATIONS and r.period.fiscal_year == 2026
        else r
        for r in company(GROWING)
    ]
    f = run(rows)
    assert f.status == ForecastStatus.UNAVAILABLE
    assert "INPUT_CONFLICT" in next(i.detail for i in f.issues if i.code == "REVENUE_BASE_MISSING")


# --- 10-11. reporting basis -------------------------------------------------------------------


def _basis_of(data: CanonicalFinancialDataset, f: ForecastDataset) -> set[ReportingBasis]:
    by_id = {v.canonical_value_id: v.reporting_basis for v in data.canonical_values}
    ids = {i for a in f.assumptions for i in a.canonical_value_ids}
    ids |= {x.canonical_value_id for v in f.actuals for x in (v.actual, *v.actual_components) if x}
    return {by_id[i] for i in ids}


def test_consolidated_and_standalone_histories_are_never_blended():
    rows = company(GROWING) + company([500] * 6, basis=B.STANDALONE, margin=40)
    data = make(rows)
    cons = build_forecast(data, generated_at=NOW)
    assert cons.base.reporting_basis == B.CONSOLIDATED and cons.base.other_basis_available
    assert _basis_of(data, cons) == {B.CONSOLIDATED}
    assert num(cons, L.REVENUE) == dec("1610.51") * dec("1.1")
    alone = build_forecast(
        data, request=ForecastRequest(reporting_basis=B.STANDALONE), generated_at=NOW
    )
    assert alone.base.reporting_basis == B.STANDALONE and _basis_of(data, alone) == {B.STANDALONE}
    assert num(alone, L.REVENUE) == 500
    assert alone.assumption(D.OPERATING_MARGIN, S.BASE, 2027).value == Decimal("40.000000")
    assert val(alone, L.PAT_ATTRIBUTABLE).formula_id == "PAT_ATTRIBUTABLE/1/STANDALONE"


def test_standalone_only_history_and_missing_requested_basis():
    rows = company(GROWING, basis=B.STANDALONE)
    assert run(rows).base.reporting_basis == B.STANDALONE
    f = run(rows, reporting_basis=B.CONSOLIDATED)
    assert f.status == ForecastStatus.UNAVAILABLE and f.base.currency == Currency.UNKNOWN
    with pytest.raises(ValidationError):
        ForecastRequest(reporting_basis=B.UNKNOWN)


def test_minority_interest_drives_attributable_pat():
    f = run(company(GROWING, minority=10))
    assert f.assumption(D.MINORITY_SHARE_OF_PAT, S.BASE, 2027).value == Decimal("10.000000")
    assert num(f, L.MINORITY_INTEREST) == num(f, L.PAT) * dec("0.1")
    assert num(f, L.PAT_ATTRIBUTABLE) == num(f, L.PAT) - num(f, L.MINORITY_INTEREST)
    unknown = run(company(GROWING))  # consolidated without a minority line
    assert val(unknown, L.PAT_ATTRIBUTABLE).reason == "ATTRIBUTION_UNKNOWN"
    assert "ATTRIBUTION_UNKNOWN" in codes(unknown)


# --- 12. scenario independence ----------------------------------------------------------------


def _scenario_json(f: ForecastDataset, scenario: Scenario) -> list[str]:
    return [v.model_dump_json() for v in f.values if v.scenario == scenario] + [
        a.model_dump_json() for a in f.assumptions if a.scenario == scenario
    ]


def test_scenarios_are_independent():
    rows = company([1000, 1080, 1250, 1300, 1500, 1600], margin=[18, 22, 20, 19, 24, 21])
    full = run(rows)
    alone = run(rows, scenarios=[S.BASE])
    assert _scenario_json(full, S.BASE) == _scenario_json(alone, S.BASE)
    growth = {s: full.assumption(D.REVENUE_GROWTH, s, 2027).value for s in S}
    assert growth[S.BULL] > growth[S.BASE] > growth[S.BEAR]  # historical max / CAGR / min
    margin = {s: full.assumption(D.OPERATING_MARGIN, s, 2027).value for s in S}
    # the 5-year window is FY2022-FY2026: FY2021 (margin 18, revenue 1000) is outside it
    assert (margin[S.BULL], margin[S.BASE], margin[S.BEAR]) == (
        Decimal("24.000000"),
        Decimal("21.000000"),
        Decimal("19.000000"),
    )
    yoy = [dec(1250) / 1080, dec(1300) / 1250, dec(1500) / 1300, dec(1600) / 1500]
    assert growth[S.BULL] == round((max(yoy) - 1) * 100, 6)
    changed = run(rows, explicit_assumptions=[explicit(D.REVENUE_GROWTH, -50, S.BEAR)])
    assert _scenario_json(changed, S.BASE) == _scenario_json(full, S.BASE)
    assert _scenario_json(changed, S.BULL) == _scenario_json(full, S.BULL)
    assert num(changed, L.REVENUE, S.BEAR) == 800


def test_a_failing_bear_scenario_cannot_corrupt_base():
    rows = company(GROWING)
    huge = [explicit(D.REVENUE_GROWTH, 10**6, S.BEAR, y) for y in range(2027, 2032)]
    f = run(rows, horizon=5, explicit_assumptions=huge)
    bear = [v for v in f.series(L.REVENUE, S.BEAR) if v.reason == "OUT_OF_RANGE"]
    assert bear and any(
        i.code == "FORECAST_OUT_OF_RANGE" and i.scenario == S.BEAR for i in f.issues
    )
    assert _scenario_json(f, S.BASE) == _scenario_json(run(rows, horizon=5), S.BASE)
    clean = run(rows, horizon=5)
    assert f.completeness[0] == clean.completeness[0] and f.completeness[0].scenario == S.BASE
    assert f.status == ForecastStatus.PARTIAL


# --- 13-15. explicit, guidance, fallback ------------------------------------------------------


def test_explicit_assumption_overrides_derived_default_for_its_year_only():
    f = run(
        company(GROWING), explicit_assumptions=[explicit(D.REVENUE_GROWTH, 15, reference="analyst")]
    )
    first, second = (f.assumption(D.REVENUE_GROWTH, S.BASE, y) for y in (2027, 2028))
    assert (first.source, first.method, first.reference) == (
        AssumptionSource.EXPLICIT,
        DerivationMethod.EXPLICIT_VALUE,
        "analyst",
    )
    assert first.overrides == ("HISTORICAL:HISTORICAL_CAGR:10.000000",)
    assert second.source == AssumptionSource.HISTORICAL
    assert num(f, L.REVENUE) == dec("1610.51") * dec("1.15")
    assert num(f, L.REVENUE, year=2028) == dec("1610.51") * dec("1.15") * dec("1.1")


def guidance(
    subject: str = "REVENUE_GROWTH",
    *,
    target: str = "RANGE",
    low: object = 12,
    high: object = 15,
    value: object | None = None,
    year: int = 2027,
    basis: ReportingBasis = B.CONSOLIDATED,
    equivalence: mi.Equivalence = mi.Equivalence.EXACT,
    unit: mi.GuidanceUnit = mi.GuidanceUnit.PERCENT,
    lifecycle: mi.Lifecycle = mi.Lifecycle.ISSUED,
    confidence: mi.Confidence = mi.Confidence.HIGH,
    issued: date = date(2026, 5, 1),
    n: int = 1,
    qualifier: str | None = None,
    scale: object | None = None,
) -> mi.GuidanceItem:
    target_type = mi.TargetType(target)
    return mi.GuidanceItem(
        guidance_id=UUID(int=100 + n),
        statement_ids=(UUID(int=200 + n),),
        event_id=UUID(int=300),
        issued_date=issued,
        issued_period=None,
        subject=mi.SubjectLink(
            subject_code=subject,
            layer=mi.SubjectLayer.BOX5_METRIC,
            target_code=subject,
            equivalence=equivalence,
            reason="test",
        ),
        measure=mi.Measure.GROWTH,
        qualifier=qualifier,
        target_type=target_type,
        low=dec(low) if target_type == mi.TargetType.RANGE else None,
        high=dec(high) if target_type == mi.TargetType.RANGE else None,
        value=dec(value) if value is not None else None,
        direction=mi.Direction.INCREASE,
        unit=unit,
        currency=Currency.INR if unit == mi.GuidanceUnit.MONEY else None,
        scale_to_crore=dec(scale) if scale is not None else None,
        target_period=mi.ReportingPeriod(
            period_type=PeriodType.FY,
            fiscal_year=year,
            period_end=date(year, 3, 31),
            raw_text=f"FY{year}",
            method="EXPLICIT_LABEL",
        ),
        reporting_basis=basis,
        basis_method="STATEMENT_TEXT",
        raw_target_text=f"{low}% to {high}%",
        stated_change=mi.StatedChange.NONE,
        lifecycle=lifecycle,
        confidence=confidence,
    )


def box8(*items: mi.GuidanceItem, key: str = "screener:X") -> mi.ManagementIntelligenceDataset:
    return mi.ManagementIntelligenceDataset.model_construct(
        dataset_id=UUID(int=999), company_key=key, guidance=tuple(items)
    )


def test_guidance_range_maps_deterministically_and_links_evidence():
    f = run(company(GROWING), management=box8(guidance()))
    got = {s: f.assumption(D.REVENUE_GROWTH, s, 2027) for s in S}
    assert {s: (a.value, a.method) for s, a in got.items()} == {
        S.BASE: (Decimal("13.500000"), DerivationMethod.GUIDANCE_MIDPOINT),
        S.BULL: (Decimal("15.000000"), DerivationMethod.GUIDANCE_HIGH),
        S.BEAR: (Decimal("12.000000"), DerivationMethod.GUIDANCE_LOW),
    }
    ref = got[S.BASE].guidance
    assert ref and ref.guidance_id == UUID(int=101) and ref.statement_ids == (UUID(int=201),)
    assert ref.management_dataset_id == UUID(int=999) and f.management_dataset_id == UUID(int=999)
    assert got[S.BASE].source == AssumptionSource.MANAGEMENT_GUIDANCE
    assert got[S.BASE].overrides == ("HISTORICAL:HISTORICAL_CAGR:10.000000",)
    assert f.assumption(D.REVENUE_GROWTH, S.BASE, 2028).source == AssumptionSource.HISTORICAL
    assert num(f, L.REVENUE) == dec("1610.51") * dec("1.135")
    over = run(
        company(GROWING),
        management=box8(guidance()),
        explicit_assumptions=[explicit(D.REVENUE_GROWTH, 9)],
    )
    assert over.assumption(D.REVENUE_GROWTH, S.BASE, 2027).overrides == (
        "HISTORICAL:HISTORICAL_CAGR:10.000000",
        "MANAGEMENT_GUIDANCE:GUIDANCE_MIDPOINT:13.500000",
    )


@pytest.mark.parametrize(
    "item",
    [
        guidance(equivalence=mi.Equivalence.PROXY),
        guidance(basis=B.STANDALONE),
        guidance(basis=B.UNKNOWN),
        guidance(confidence=mi.Confidence.LOW),
        guidance(lifecycle=mi.Lifecycle.WITHDRAWN),
        guidance(qualifier="CONSTANT_CURRENCY"),
        guidance(target="MINIMUM", value=12),
        guidance(subject="EBITDA_MARGIN"),  # definition differs from the operating route
        guidance(year=2035),
    ],
)
def test_guidance_outside_the_policy_never_becomes_a_number(item):
    f = run(company(GROWING), management=box8(item))
    assert all(a.source != AssumptionSource.MANAGEMENT_GUIDANCE for a in f.assumptions)
    assert codes(f) & {"GUIDANCE_NOT_APPLIED", "GUIDANCE_OUTSIDE_HORIZON"}


def test_latest_guidance_wins_point_and_money_guidance():
    old = guidance(n=1, issued=date(2026, 4, 1))
    new = guidance(target="POINT", value=11, n=2, issued=date(2026, 7, 1))
    capex = guidance("CAPEX", target="POINT", value=5, unit=mi.GuidanceUnit.MONEY, scale=100, n=3)
    f = run(company(GROWING), management=box8(old, new, capex))
    assert {f.assumption(D.REVENUE_GROWTH, s, 2027).value for s in S} == {Decimal("11.000000")}
    assert f.assumption(D.REVENUE_GROWTH, S.BASE, 2027).guidance.guidance_id == UUID(int=102)
    assert num(f, L.CAPEX) == 500  # 5 x scale_to_crore 100
    assert val(f, L.CAPEX).formula_id == "CAPEX/1/AMOUNT"
    with pytest.raises(ForecastInputError):
        run(company(GROWING), management=box8(key="screener:OTHER"))


def test_fallback_assumption_is_visibly_marked():
    f = run(company(GROWING))
    debt = f.assumption(D.DEBT_CHANGE, S.BASE, 2027)
    assert (debt.source, debt.method, debt.value) == (
        AssumptionSource.FALLBACK,
        DerivationMethod.FALLBACK_CONSTANT,
        0,
    )
    assert "FALLBACK_ASSUMPTION" in debt.warnings and debt.confidence == Confidence.LOW
    assert {c.fallback_assumptions for c in f.completeness} == {3}
    assert any(i.code == "FALLBACK_ASSUMPTION" and i.subject == "DEBT_CHANGE" for i in f.issues)
    with pytest.raises(ValidationError):  # a fallback can never pose as evidence
        debt.model_validate(debt.model_dump() | {"warnings": ()})
    with pytest.raises(ValidationError):
        debt.model_validate(debt.model_dump() | {"source": AssumptionSource.HISTORICAL})


def test_industry_assumption_must_cite_evidence():
    with pytest.raises(ValidationError):
        explicit(D.REVENUE_GROWTH, 8, source=AssumptionSource.INDUSTRY_INTELLIGENCE)
    a = explicit(
        D.REVENUE_GROWTH,
        8,
        source=AssumptionSource.INDUSTRY_INTELLIGENCE,
        evidence_ids=(UUID(int=7),),
    )
    f = run(company(GROWING), explicit_assumptions=[a])
    got = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    assert got.source == AssumptionSource.INDUSTRY_INTELLIGENCE and got.evidence_ids == (
        UUID(int=7),
    )


# --- 16-17. zero and negative denominators ----------------------------------------------------


def test_zero_denominator_years_are_skipped_never_divided():
    f = run(company([1000, 0, 1210, 1331, "1464.1", "1610.51"]))
    margin = f.assumption(D.OPERATING_MARGIN, S.BASE, 2027)
    assert "FY|2022-03-31|12" not in margin.historical_periods
    assert len(margin.observations) == 4 and margin.completeness == HistoryCompleteness.SPARSE
    assert any(i.code == "ZERO_DENOMINATOR" and "2022" in (i.detail or "") for i in f.issues)
    growth = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)  # never grown from a zero base
    assert growth.historical_periods == ("FY|2023-03-31|12", "FY|2026-03-31|12")


def test_negative_denominators_are_excluded_from_ratios():
    rows = company([1000] * 6, margin=[-30, -30, 20, 20, 20, 20])
    f = run(rows)
    tax = f.assumption(D.TAX_RATE, S.BASE, 2027)
    assert tax.value == Decimal("25.000000")
    assert not {"FY|2022-03-31|12"} & set(tax.historical_periods)
    assert any(i.code == "NEGATIVE_DENOMINATOR" and i.subject == "TAX_RATE" for i in f.issues)
    neg = run(company([-100, 50, 100], start=2024))  # negative revenue base: no growth from it
    growth = neg.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    assert growth.historical_periods == ("FY|2025-03-31|12", "FY|2026-03-31|12")
    assert growth.method == DerivationMethod.HISTORICAL_SINGLE_YOY


# --- 18-23. request validation, numerics, fiscal years ----------------------------------------


@pytest.mark.parametrize("horizon", [0, 6, -1])
def test_invalid_horizon_rejected(horizon):
    with pytest.raises(ValidationError):
        ForecastRequest(horizon=horizon)


def test_supported_horizons():
    for horizon in range(1, 6):
        f = run(company(GROWING), horizon=horizon)
        assert [p.fiscal_year for p in f.forecast_years] == list(range(2027, 2027 + horizon))


def test_duplicate_assumptions_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        ForecastRequest(explicit_assumptions=[explicit(D.TAX_RATE, 20), explicit(D.TAX_RATE, 21)])


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_nan_and_inf_rejected(bad):
    with pytest.raises(ValidationError):
        explicit(D.REVENUE_GROWTH, bad)


@pytest.mark.parametrize(
    ("driver", "value"),
    [
        (D.REVENUE_GROWTH, -100),
        (D.REVENUE_GROWTH, 10**7),
        (D.TAX_RATE, 101),
        (D.TAX_RATE, -1),
        (D.DA_TO_REVENUE, -1),
        (D.SHARE_COUNT, 0),
        (D.CAPEX_AMOUNT, 10**16),
    ],
)
def test_logically_impossible_values_rejected(driver, value):
    with pytest.raises(ValidationError):
        explicit(driver, value)


def test_decimal_written_percent_is_flagged():
    f = run(company(GROWING), explicit_assumptions=[explicit(D.REVENUE_GROWTH, "0.12")])
    assert "PERCENT_BELOW_ONE" in codes(f)
    assert num(f, L.REVENUE) == dec("1610.51") * dec("1.0012")  # applied as stated, flagged
    assert "PERCENT_BELOW_ONE" not in codes(run(company(GROWING)))


def test_cagr_outside_the_yoy_range_never_inverts_scenarios():
    # FY2024 revenue is conflicted: the CAGR spans it, the YoY pairs around it do not exist
    rows = [
        V(r.metric, r.period, None, outcome=Outcome.CONFLICT)
        if r.metric == M.REVENUE_FROM_OPERATIONS and r.period.fiscal_year == 2024
        else r
        for r in company([1000, 1000, 1010, 2000, 2020, 2040])
    ]
    f = run(rows)
    g = {s: f.assumption(D.REVENUE_GROWTH, s, 2027).value for s in S}
    assert g[S.BULL] >= g[S.BASE] >= g[S.BEAR]
    assert "SCENARIO_RANGE_INVERTED" in codes(f)


def test_extreme_but_legitimate_values_accepted():
    for driver, value in (
        (D.REVENUE_GROWTH, 900),
        (D.OPERATING_MARGIN, -400),
        (D.REVENUE_GROWTH, "-99.9"),
    ):
        assert explicit(driver, value).value == dec(value)


def test_very_large_values():
    big = [dec(10) ** 16 * x for x in (1, 2, 4)]
    f = run(company(big, start=2024, debt=0), horizon=5)
    assert num(f, L.REVENUE) == dec(10) ** 16 * 8
    assert num(f, L.REVENUE, year=2030) == dec(10) ** 16 * 64
    assert val(f, L.REVENUE, year=2031).reason == "OUT_OF_RANGE"  # 1.28e18 > 1e18
    huge = run(company([dec(10) ** 19] * 3, start=2024))
    assert huge.status == ForecastStatus.UNAVAILABLE and "REVENUE_BASE_MISSING" in codes(huge)


def test_fiscal_year_ordering_and_labels():
    f = run(company(GROWING))
    years = f.forecast_years
    assert [p.label for p in years] == ["FY2027E", "FY2028E", "FY2029E"]
    assert [p.period_end for p in years] == [
        date(2027, 3, 31),
        date(2028, 3, 31),
        date(2029, 3, 31),
    ]
    assert all(p.period_type == PeriodType.FY and p.duration_months == 12 for p in years)
    with pytest.raises(ValidationError, match="consecutive"):
        f.model_validate(f.model_dump() | {"forecast_years": (years[1], years[0], years[2])})


def test_december_year_end_is_not_off_by_one():
    rows = []
    for i, rev in enumerate((100, 110, 121)):
        end = date(2023 + i, 12, 31)
        period = FinancialPeriod(
            period_type=PeriodType.FY, label=f"Dec {end.year}", period_end=end, duration_months=12
        )
        rows.append(V(M.REVENUE_FROM_OPERATIONS, period, rev))
    f = run(rows, fy_end=12)
    assert f.base.latest_actual.fiscal_year == 2025
    assert [(p.fiscal_year, p.period_end) for p in f.forecast_years][0] == (
        2026,
        date(2026, 12, 31),
    )
    assert num(f, L.REVENUE, year=2026) == dec(121) * dec("1.1")


def test_malformed_identifiers_and_periods_rejected():
    for bad in (
        {"driver": "revenue growth"},
        {"fiscal_year": 1800},
        {"fiscal_year": "FY27"},
        {"scenario": "OPTIMISTIC"},
        {"unit": "RATIO"},
    ):
        payload = {
            "driver": "REVENUE_GROWTH",
            "scenario": "BASE",
            "fiscal_year": 2027,
            "value": "5",
            "unit": "PERCENT",
        } | bad
        with pytest.raises(ValidationError):
            AssumptionInput.model_validate(payload)
    with pytest.raises(ValidationError):
        ForecastRequest(scenarios=[S.BASE, S.BASE])
    with pytest.raises(ValidationError):
        ForecastRequest(scenarios=[S.BASE], explicit_assumptions=[explicit(D.TAX_RATE, 20, S.BULL)])


def test_explicit_assumptions_must_apply_to_this_company():
    rows = company(GROWING)
    for bad, code in (
        ([explicit(D.TAX_RATE, 20, year=2031)], "ASSUMPTION_OUTSIDE_HORIZON"),
        ([explicit(D.EBITDA_MARGIN, 20)], "UNUSED_EXPLICIT_ASSUMPTION"),
        ([explicit(D.MINORITY_SHARE_OF_PAT, 5)], "UNUSED_EXPLICIT_ASSUMPTION"),
        (
            [explicit(D.INTEREST_RATE_ON_DEBT, 9), explicit(D.FINANCE_COST_TO_REVENUE, 1)],
            "CONFLICTING_EXPLICIT_ASSUMPTIONS",
        ),
        ([explicit(D.DEBT_CHANGE, 10, currency=Currency.USD)], "CURRENCY_MISMATCH"),
    ):
        with pytest.raises(ForecastInputError) as err:
            run(rows, explicit_assumptions=bad)
        assert err.value.code == code


# --- 24-25. determinism and immutability ------------------------------------------------------


def test_deterministic_repeated_execution():
    data = make(company(GROWING, minority=5))
    request = ForecastRequest(horizon=5, explicit_assumptions=[explicit(D.SHARE_COUNT, 10**9)])
    first = build_forecast(data, request=request, generated_at=NOW)
    for at in (NOW, LATER):
        again = build_forecast(data, request=request, generated_at=at)
        assert canonical_forecast_json(again) == canonical_forecast_json(first)
        assert again.forecast_id == first.forecast_id
    assert "generated_at" not in canonical_forecast_json(first)
    assert asyncio.run(build_forecast_async(data, request=request, generated_at=NOW)) == first


def test_inputs_are_not_mutated():
    data = make(company(GROWING))
    request = ForecastRequest(explicit_assumptions=[explicit(D.REVENUE_GROWTH, 15)])
    management = box8(guidance())
    before = (canonical_dataset_json(data), request.model_dump_json(), management.guidance)
    build_forecast(data, request=request, management=management, generated_at=NOW)
    assert (canonical_dataset_json(data), request.model_dump_json(), management.guidance) == before


# --- financial identities ---------------------------------------------------------------------


def test_financial_identities_hold_exactly():
    f = run(
        company(GROWING, minority=8),
        horizon=5,
        explicit_assumptions=[
            explicit(D.SHARE_COUNT, 2 * 10**9, s, y) for s in S for y in range(2027, 2032)
        ],
    )
    assert f.status == ForecastStatus.COMPLETE
    with localcontext() as ctx:
        ctx.prec = P
        for s in S:
            prior_debt = next(a for a in f.actuals if a.line == L.GROSS_DEBT).value
            prior_wc = next(a for a in f.actuals if a.line == L.TRADE_WORKING_CAPITAL).value
            for y in range(2027, 2032):
                g = {line: num(f, line, s, y) for line in L}
                assert g[L.EBITDA] == g[L.OPERATING_PROFIT] + g[L.OTHER_INCOME]
                assert g[L.EBIT] == g[L.EBITDA] - g[L.DEPRECIATION_AMORTIZATION]
                assert g[L.PBT] == g[L.EBIT] - g[L.FINANCE_COST]
                assert g[L.PAT] == g[L.PBT] - g[L.TAX]
                assert g[L.PAT_ATTRIBUTABLE] == g[L.PAT] - g[L.MINORITY_INTEREST]
                assert g[L.EPS] == g[L.PAT_ATTRIBUTABLE] * 10**7 / (2 * 10**9)
                assert g[L.WORKING_CAPITAL_CHANGE] == g[L.TRADE_WORKING_CAPITAL] - prior_wc
                assert (
                    g[L.CFO_ESTIMATE]
                    == g[L.PAT] + g[L.DEPRECIATION_AMORTIZATION] - g[L.WORKING_CAPITAL_CHANGE]
                )
                assert g[L.FCF_ESTIMATE] == g[L.CFO_ESTIMATE] - g[L.CAPEX]
                assert g[L.GROSS_DEBT] == prior_debt  # flat by the flagged fallback
                assert g[L.FINANCE_COST] == prior_debt * dec(8) / 100
                prior_debt, prior_wc = g[L.GROSS_DEBT], g[L.TRADE_WORKING_CAPITAL]


def test_historical_driver_values_match_hand_calculation():
    f = run(company(GROWING))
    expected = {
        D.OPERATING_MARGIN: 20,
        D.OTHER_INCOME_TO_REVENUE: 2,
        D.DA_TO_REVENUE: 3,
        D.INTEREST_RATE_ON_DEBT: 8,
        D.TAX_RATE: 25,
        D.CAPEX_TO_REVENUE: 5,
        D.WORKING_CAPITAL_TO_REVENUE: 20,  # 15 + 10 - 5
    }
    for driver, value in expected.items():
        assert f.assumption(driver, S.BASE, 2027).value == dec(value), driver
    assert (
        f.assumption(D.CAPEX_TO_REVENUE, S.BASE, 2027).method == DerivationMethod.HISTORICAL_MEDIAN
    )
    twc = next(a for a in f.actuals if a.line == L.TRADE_WORKING_CAPITAL)
    assert twc.value == dec("1610.51") * dec("0.2") and len(twc.actual_components) == 3


def test_debt_change_and_negative_debt():
    rows = company(GROWING)
    down = run(rows, explicit_assumptions=[explicit(D.DEBT_CHANGE, -400)])
    assert num(down, L.GROSS_DEBT) == 600
    assert num(down, L.FINANCE_COST, year=2028) == dec(600) * dec(8) / 100  # opening debt
    bad = run(rows, explicit_assumptions=[explicit(D.DEBT_CHANGE, -2000)])
    assert val(bad, L.GROSS_DEBT).reason == "NEGATIVE_DEBT" and "NEGATIVE_DEBT" in codes(bad)
    assert val(bad, L.FINANCE_COST, year=2028).value is None  # never computed on negative debt


def test_eps_needs_a_share_count_in_ones():
    rows = company(GROWING, basis=B.STANDALONE)
    rows += [V(M.EPS, fy(2026), "20", basis=B.STANDALONE, kind=ValueKind.PER_SHARE)]
    f = run(rows)
    assert val(f, L.EPS).reason == "SHARE_COUNT_UNAVAILABLE" and val(f, L.PAT).value is not None
    pat26 = (dec("1610.51") * dec("0.19") - 80) * dec("0.75")
    shares = (pat26 * 10**7 / 20).quantize(Decimal(1))
    ok = run(rows, explicit_assumptions=[explicit(D.SHARE_COUNT, shares)])
    with localcontext() as ctx:
        ctx.prec = P
        assert num(ok, L.EPS) == num(ok, L.PAT_ATTRIBUTABLE) * 10**7 / shares
    assert "SHARE_COUNT_INCONSISTENT_WITH_HISTORY" not in codes(ok)
    millions = run(rows, explicit_assumptions=[explicit(D.SHARE_COUNT, shares / 10**6)])
    assert "SHARE_COUNT_INCONSISTENT_WITH_HISTORY" in codes(millions)
    with pytest.raises(ValidationError):
        explicit(D.SHARE_COUNT, shares, scale=Scale.MILLIONS)


# --- annual / quarterly / TTM / irregular / currency ------------------------------------------


def test_quarterly_and_ttm_data_never_become_a_fiscal_year():
    rows = company(GROWING)
    rows += [
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 6, 30)), 9999),
        V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 9, 30), 6, PeriodType.HALF_YEAR), 9999),
        V(M.REVENUE_FROM_OPERATIONS, TTM, 99999),
    ]
    f = run(rows)
    assert f.base.latest_actual.period_end == date(2026, 3, 31)
    assert f.base.ignored_period_types == {"HALF_YEAR": 1, "QUARTER": 1, "TTM": 1}
    assert num(f, L.REVENUE) == dec("1610.51") * dec("1.1")
    assert (
        next(a for a in f.actuals if a.line == L.REVENUE).actual.period.period_type == PeriodType.FY
    )


def test_only_quarterly_data_gives_no_forecast():
    f = run([V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 6, 30)), 100)])
    assert f.status == ForecastStatus.UNAVAILABLE and "NO_ANNUAL_HISTORY" in codes(f)


def test_irregular_latest_fiscal_period_fails_closed():
    rows = company(GROWING)
    odd = FinancialPeriod(
        period_type=PeriodType.FY,
        label="15m to Jun 2027",
        period_end=date(2027, 6, 30),
        duration_months=15,
    )
    f = run(rows + [V(M.REVENUE_FROM_OPERATIONS, odd, 2500)])
    assert f.status == ForecastStatus.UNAVAILABLE and "IRREGULAR_LATEST_FISCAL_PERIOD" in codes(f)
    older = FinancialPeriod(
        period_type=PeriodType.FY, label="9m", period_end=date(2019, 12, 31), duration_months=9
    )
    g = run(rows + [V(M.REVENUE_FROM_OPERATIONS, older, 100)])
    assert ("FY|2019-12-31|9", "IRREGULAR_FISCAL_PERIOD") in g.base.excluded_periods
    assert g.status != ForecastStatus.UNAVAILABLE


def test_other_currency_values_are_excluded_not_converted():
    rows = [
        V(r.metric, r.period, r.value, currency=Currency.USD)
        if r.metric == M.REVENUE_FROM_OPERATIONS and r.period.fiscal_year == 2021
        else r
        for r in company(GROWING)
    ]
    data = make(rows)
    f = build_forecast(data, generated_at=NOW)
    usd = {v.canonical_value_id for v in data.canonical_values if v.currency == Currency.USD}
    assert not usd & {i for a in f.assumptions for i in a.canonical_value_ids}
    assert "OTHER_CURRENCY_EXCLUDED" in codes(f) and f.base.currency == Currency.INR
    assert f.assumption(D.REVENUE_GROWTH, S.BASE, 2027).historical_periods[0] == "FY|2022-03-31|12"


def test_restated_and_low_confidence_inputs_carry_their_quality():
    rows = [
        V(r.metric, r.period, r.value, outcome=Outcome.RESTATEMENT)
        if r.metric == M.OPERATING_PROFIT and r.period.fiscal_year == 2025
        else V(r.metric, r.period, r.value, confidence=Confidence.LOW)
        if r.metric == M.OPERATING_PROFIT and r.period.fiscal_year == 2024
        else r
        for r in company(GROWING)
    ]
    f = run(rows)
    margin = f.assumption(D.OPERATING_MARGIN, S.BASE, 2027)
    assert "INPUT_RESTATEMENT" in margin.warnings and margin.confidence == Confidence.MEDIUM
    assert "FY|2024-03-31|12" not in margin.historical_periods  # LOW confidence never used


def test_financial_sector_forecasts_revenue_only():
    f = run(company(GROWING), industry=Industry.BANK)
    assert f.base.operating_route == OperatingRoute.UNAVAILABLE and "UNSUPPORTED_SECTOR" in codes(f)
    assert {v.line for v in f.values} == {L.REVENUE}
    with pytest.raises(ForecastInputError):
        run(
            company(GROWING),
            industry=Industry.BANK,
            explicit_assumptions=[explicit(D.TAX_RATE, 20)],
        )


def test_reported_ebitda_route():
    rows = [
        V(M.EBITDA, r.period, dec(r.value) * dec("0.25"))
        if r.metric == M.REVENUE_FROM_OPERATIONS
        else r
        for r in company(GROWING)
        if r.metric != M.OPERATING_PROFIT
    ] + [r for r in company(GROWING) if r.metric == M.REVENUE_FROM_OPERATIONS]
    f = run(rows)
    assert f.base.operating_route == OperatingRoute.REPORTED_EBITDA
    assert f.assumption(D.EBITDA_MARGIN, S.BASE, 2027).value == Decimal("25.000000")
    assert num(f, L.EBITDA) == num(f, L.REVENUE) * dec("0.25")
    assert L.OPERATING_PROFIT not in {v.line for v in f.values}


def test_pbt_bridge_residual_is_flagged():
    rows = [
        V(r.metric, r.period, dec(r.value) + 50)
        if r.metric == M.PBT and r.period.fiscal_year == 2026
        else r
        for r in company(GROWING)
    ]
    assert "PBT_BRIDGE_RESIDUAL" in codes(run(rows))
    assert "PBT_BRIDGE_RESIDUAL" not in codes(run(company(GROWING)))


def test_implied_tax_is_flagged():
    rows = company(GROWING, drop=frozenset((M.TOTAL_TAX, y) for y in range(2021, 2027)))
    tax = run(rows).assumption(D.TAX_RATE, S.BASE, 2027)
    assert tax.value == Decimal("25.000000") and "IMPLIED_TAX_FROM_PBT_AND_PAT" in tax.warnings


# --- provenance -------------------------------------------------------------------------------


def test_every_number_has_a_traceable_path():
    data = make(company(GROWING, minority=5))
    f = build_forecast(
        data,
        request=ForecastRequest(
            explicit_assumptions=[explicit(D.SHARE_COUNT, 10**9, reference="AR p.4")]
        ),
        management=box8(guidance()),
        generated_at=NOW,
    )
    canonical = {v.canonical_value_id: v for v in data.canonical_values}
    annual = set(f.base.annual_periods)
    for a in f.assumptions:
        if a.source == AssumptionSource.HISTORICAL:
            assert a.historical_periods and set(a.historical_periods) <= annual
            assert a.canonical_value_ids and set(a.canonical_value_ids) <= set(canonical)
            assert len(a.observations) >= 1
        elif a.source == AssumptionSource.EXPLICIT:
            assert a.method == DerivationMethod.EXPLICIT_VALUE and a.reference == "AR p.4"
        elif a.source == AssumptionSource.MANAGEMENT_GUIDANCE:
            assert a.guidance and a.guidance.statement_ids
    for anchor in f.actuals:
        for metric_input in (anchor.actual, *anchor.actual_components):
            if metric_input:
                source = canonical[metric_input.canonical_value_id]
                assert source.value == metric_input.value
    nodes = {v.value_id: v for v in (*f.actuals, *f.values)}
    assumption_ids = {a.assumption_id for a in f.assumptions}
    formulas = {x.formula_id for x in f.manifest.formulas}

    def roots(value_id: UUID) -> set[str]:
        node = nodes[value_id]
        if node.kind == "ACTUAL":
            return {"ACTUAL"}
        found: set[str] = set()
        for i in node.inputs:
            found |= {"ASSUMPTION"} if i.kind == "ASSUMPTION" else roots(i.ref_id)
        return found

    for v in f.values:
        if v.status == "AVAILABLE":
            assert v.formula_id in formulas and v.inputs
            assert evaluate(v.formula_id, {i.role: i.value for i in v.inputs}) == v.value
            assert all(i.ref_id in nodes or i.ref_id in assumption_ids for i in v.inputs)
            assert roots(v.value_id) <= {"ACTUAL", "ASSUMPTION"}
        else:
            assert v.value is None and v.reason


def test_a_forecast_number_without_formula_is_rejected_by_the_contract():
    f = run(company(GROWING))
    v = val(f, L.REVENUE)
    with pytest.raises(ValidationError):
        v.model_validate(v.model_dump() | {"inputs": (), "formula_id": None})
    with pytest.raises(ValidationError):
        v.model_validate(v.model_dump() | {"value": None})  # neither value nor reason
    tampered = v.inputs[0].model_copy(update={"value": Decimal(1)})
    with pytest.raises(ValidationError):
        f.model_validate(
            f.model_dump()
            | {
                "values": tuple(
                    x.model_copy(update={"inputs": (tampered, *x.inputs[1:])}) if x == v else x
                    for x in f.values
                )
            }
        )


def test_formula_registry_is_pure():
    assert evaluate("REVENUE/1", {"PRIOR_REVENUE": dec(100), "REVENUE_GROWTH": dec(5)}) == 105
    assert evaluate("TAX/1", {"PBT": dec(-10), "TAX_RATE": dec(25)}) == 0
    with pytest.raises(ValueError):
        evaluate("REVENUE/1", {"PRIOR_REVENUE": dec(100)})
    assert all(f.formula_id == k for k, (f, _) in FORMULAS.items())


def test_no_valuation_leakage():
    forbidden = (
        "wacc",
        "dcf",
        "terminal",
        "target_price",
        "targetprice",
        "enterprise_value",
        "equity_value",
        "fair_value",
        "fair_pe",
        "upside",
        "downside",
        "recommend",
        "cost_of_equity",
        "discount",
        "multiple",
        "price_target",
        "potential_return",
        "buy",
        "sell",
        "hold",
    )
    root = Path(engine.__file__).parent.parent
    for path in (root / "application/forecasting.py", root / "domain/forecasting.py"):
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)}
        names |= {
            t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign)
            for t in [n.target]
            if isinstance(t, ast.Name)
        }
        lowered = {x.lower() for x in names}
        assert not {x for x in lowered for word in forbidden if word in x}, path
    vocabulary = {m.value.lower() for e in (Driver, ForecastLine, DerivationMethod) for m in e}
    assert not {v for v in vocabulary for word in forbidden if word in v}
    fields = {
        f
        for m in vars(domain).values()
        if isinstance(m, type) and hasattr(m, "model_fields")
        for f in m.model_fields
    }
    assert not {f for f in fields for word in forbidden if word in f}


# --- stress / adversarial ---------------------------------------------------------------------


def test_max_horizon_with_many_assumptions_is_fast_and_bounded():
    rows = company(GROWING, minority=5)
    many = []
    for s in S:
        for y in range(2027, 2032):
            for driver, value in (
                (D.REVENUE_GROWTH, 7),
                (D.OPERATING_MARGIN, 21),
                (D.OTHER_INCOME_TO_REVENUE, 1),
                (D.DA_TO_REVENUE, 4),
                (D.INTEREST_RATE_ON_DEBT, 9),
                (D.TAX_RATE, 24),
                (D.MINORITY_SHARE_OF_PAT, 3),
                (D.CAPEX_TO_REVENUE, 6),
                (D.CAPEX_AMOUNT, 50),
                (D.WORKING_CAPITAL_TO_REVENUE, 18),
                (D.DEBT_CHANGE, -10),
                (D.SHARE_COUNT, 10**9),
            ):
                many.append(explicit(driver, value, s, y))
    started = time.perf_counter()
    f = run(rows, horizon=5, explicit_assumptions=many)
    assert time.perf_counter() - started < 5
    assert f.status == ForecastStatus.COMPLETE
    assert len(f.values) == 5 * 3 * len(L)
    assert all(a.source == AssumptionSource.EXPLICIT for a in f.assumptions)
    assert num(f, L.CAPEX) == 50  # an amount takes precedence over the ratio


def test_sparse_history_single_year_and_two_years():
    one = run(company([1000], start=2026))
    assert one.assumption(D.REVENUE_GROWTH, S.BASE, 2027) is None
    assert val(one, L.REVENUE).reason == "ASSUMPTION_MISSING"
    margin = one.assumption(D.OPERATING_MARGIN, S.BASE, 2027)
    assert (
        margin.completeness == HistoryCompleteness.SINGLE_POINT
        and margin.confidence == Confidence.LOW
    )
    assert "NO_HISTORICAL_DISPERSION" in margin.warnings
    rescued = run(
        company([1000], start=2026),
        explicit_assumptions=[
            explicit(D.REVENUE_GROWTH, 5, s, y) for s in S for y in (2027, 2028, 2029)
        ],
    )
    assert num(rescued, L.REVENUE) == 1050
    two = run(company([1000, 1200], start=2025))
    growth = two.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    assert (growth.value, growth.method) == (
        Decimal("20.000000"),
        DerivationMethod.HISTORICAL_SINGLE_YOY,
    )
    assert growth.completeness == HistoryCompleteness.SINGLE_POINT  # never a 5-year CAGR
    assert {two.assumption(D.REVENUE_GROWTH, s, 2027).value for s in S} == {growth.value}


def test_ten_year_history_window_is_respected():
    revenues = [1000 + 100 * i for i in range(12)]
    f = run(company(revenues, start=2015), history_years=10)
    assert len(f.base.annual_periods) == 10 and f.base.annual_periods[0] == "FY|2017-03-31|12"
    with pytest.raises(ValidationError):
        ForecastRequest(history_years=11)


def test_performance_and_memory_are_modest():
    rows = company(GROWING, minority=5)
    data = make(rows)
    tracemalloc.start()
    started = time.perf_counter()
    for _ in range(5):
        build_forecast(data, request=ForecastRequest(horizon=5), generated_at=NOW)
    elapsed = (time.perf_counter() - started) / 5
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 2 and peak < 50 * 1024 * 1024


# --- the real Screener fixture ----------------------------------------------------------------


async def test_infy_fixture_forecast(tmp_path, fixture_dir):
    screener = await screener_dataset(tmp_path, fixture_dir, INFY)
    data = build_canonical_dataset(
        company_key=screener.company_key,
        company_name="Infosys",
        screeners=[screener],
        bundle=None,
        catalogue=None,
        generated_at=NOW,
    )
    f = await build_forecast_async(data, generated_at=NOW)
    assert f.base.reporting_basis == B.STANDALONE and f.base.latest_actual.label == "FY2026"
    assert [p.fiscal_year for p in f.forecast_years] == [2027, 2028, 2029]
    assert (
        f.base.ignored_period_types.get("QUARTER", 0) > 0 and "TTM" in f.base.ignored_period_types
    )
    growth = f.assumption(D.REVENUE_GROWTH, S.BASE, 2027)
    revenue = {
        v.period.fiscal_year: v.value
        for v in data.canonical_values
        if v.metric == M.REVENUE_FROM_OPERATIONS and v.period.period_type == PeriodType.FY
    }
    with localcontext() as ctx:
        ctx.prec = P
        cagr = ((revenue[2026] / revenue[2022]).ln() / 4).exp() - 1
    assert growth.value == round(cagr * 100, 6)
    assert num(f, L.REVENUE) == mul(revenue[2026], 1 + growth.value / 100)
    assert f.status == ForecastStatus.PARTIAL  # Screener has no capex / working-capital detail
    assert val(f, L.CAPEX).status == "UNAVAILABLE" and val(f, L.PAT).status == "AVAILABLE"
    assert num(f, L.PBT) == num(f, L.EBIT) - num(f, L.FINANCE_COST)
