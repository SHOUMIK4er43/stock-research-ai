"""DETAILED_RESEARCH_V1 Box 11: deterministic valuation over a Box 10 forecast (offline).

Forecasts come from the Box 10 engine over the Box 5 Box-4 factory, so every valued number
is a real Box 10 value. The fixture company has flat revenue of 1000 and a 20 % operating
margin; explicit Box 10 assumptions (no growth, no D&A, no tax, steady working capital, an
explicit capex amount) make FCFF exactly `operating profit - capex`, so each test chooses its
FCFF series. Expected values are computed by hand with exact fractions, never with Box 11
helpers.
"""

import ast
import asyncio
import time
import tracemalloc
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_financial_analytics import V, fy, make, pit
from test_forecasting import company, explicit
from test_screener_dataset import dataset as screener_dataset
from test_summary_input import INFY

from stock_research_ai.application import fundamental_valuation as engine
from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
from stock_research_ai.application.forecasting import (
    build_forecast,
    build_forecast_async,
    canonical_forecast_json,
)
from stock_research_ai.application.fundamental_valuation import (
    FORMULAS,
    ValuationInputError,
    build_valuation,
    build_valuation_async,
    canonical_valuation_json,
    evaluate,
)
from stock_research_ai.domain import fundamental_valuation as domain
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Currency,
    Industry,
    ReportingBasis,
    Scale,
    ValueKind,
)
from stock_research_ai.domain.forecasting import (
    Driver,
    ForecastDataset,
    ForecastLine,
    ForecastRequest,
    Scenario,
)
from stock_research_ai.domain.fundamental_valuation import (
    DEFAULT_METHODS,
    AssumptionInput,
    AssumptionKind,
    InputKind,
    MethodResult,
    MultipleInput,
    MultipleSource,
    ValuationDataset,
    ValuationMethod,
    ValuationRequest,
    ValuationStatus,
    ValuationValue,
)

M = CanonicalMetric
B = ReportingBasis
D = Driver
S = Scenario
K = AssumptionKind
VM = ValuationMethod
L = ForecastLine
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
LATER = datetime(2031, 1, 1, tzinfo=UTC)
YEARS = [2027, 2028, 2029, 2030, 2031]
SHARES = 10**8  # latest PAT 82.5 crore / EPS 8.25 = 10^8 shares
DEBT, CASH = 1000, 300
TIGHT = Fraction(1, 10**24)


def F(x: object) -> Fraction:
    return Fraction(str(x))


def close(actual: Decimal | None, expected: Fraction) -> bool:
    assert actual is not None
    return abs(F(actual) - expected) <= TIGHT * max(1, abs(expected))


# --- fixtures ---------------------------------------------------------------------------------


def rows(
    *,
    revenue: object = 1000,
    margin: object = 20,
    basis: ReportingBasis = B.STANDALONE,
    cash: object | None = CASH,
    eps: object | None = "8.25",
    minority: object | None = None,
    debt: object = DEBT,
    drop: frozenset[tuple[CanonicalMetric, int]] = frozenset(),
) -> list[V]:
    out = company(
        [revenue] * 6, margin=margin, basis=basis, debt=debt, minority=minority, drop=drop
    )
    if cash is not None:
        out.append(V(M.CASH_AND_CASH_EQUIVALENTS, pit(2026), cash, basis=basis))
    if eps is not None:
        out.append(V(M.EPS, fy(2026), eps, basis=basis, kind=ValueKind.PER_SHARE))
        if basis == B.CONSOLIDATED:
            pat = next(r.value for r in out if r.metric == M.PAT and r.period == fy(2026))
            share = Decimal(str(minority or 0)) / 100
            out.append(V(M.PAT_ATTRIBUTABLE_TO_OWNERS, fy(2026), pat * (1 - share), basis=basis))
    return out


def driven(
    fcff: dict[Scenario, list[object]] | list[object],
    *,
    revenue: object = 1000,
    margin: object = 20,
    tax: object = 0,
    da: object = 0,
) -> list[Any]:
    """Box 10 assumptions making FCFF = NOPAT + D&A - capex exactly (no WC change)."""
    series = fcff if isinstance(fcff, dict) else {s: fcff for s in S}
    rev = Decimal(str(revenue))
    op = rev * Decimal(str(margin)) / 100
    dep = rev * Decimal(str(da)) / 100
    out = []
    for s, flows in series.items():
        for i, flow in enumerate(flows):
            y = YEARS[i]
            ebit = op - dep
            nopat = ebit * (1 - Decimal(str(tax)) / 100) if ebit > 0 else ebit
            capex = nopat + dep - Decimal(str(flow))
            out += [
                explicit(D.REVENUE_GROWTH, 0, s, y),
                explicit(D.DA_TO_REVENUE, da, s, y),
                explicit(D.TAX_RATE, tax, s, y),
                explicit(D.WORKING_CAPITAL_TO_REVENUE, 20, s, y),
                explicit(D.CAPEX_AMOUNT, capex, s, y),
            ]
    return out


def forecast(
    fcff: dict[Scenario, list[object]] | list[object] = (100, 110, 121),  # type: ignore[assignment]
    *,
    data: CanonicalFinancialDataset | None = None,
    industry: Industry = Industry.GENERAL,
    revenue: object = 1000,
    margin: object = 20,
    tax: object = 0,
    da: object = 0,
    **kw: Any,
) -> tuple[ForecastDataset, CanonicalFinancialDataset]:
    flows = fcff if isinstance(fcff, dict) else list(fcff)
    horizon = len(next(iter(flows.values())) if isinstance(flows, dict) else flows)
    box4 = data or make(rows(revenue=revenue, margin=margin, **kw), industry)
    scenarios = tuple(flows) if isinstance(flows, dict) else tuple(S)
    f = build_forecast(
        box4,
        request=ForecastRequest(
            horizon=horizon,
            scenarios=scenarios,
            explicit_assumptions=driven(flows, revenue=revenue, margin=margin, tax=tax, da=da),
        ),
        generated_at=NOW,
    )
    return f, box4


def a(kind: AssumptionKind, value: object, scenario: Scenario | None = None, **kw: Any):
    spec = domain.ASSUMPTION_SPECS[kind]
    extra: dict[str, Any] = {}
    if spec.unit == domain.AssumptionUnit.MONEY:
        extra = {"currency": Currency.INR, "scale": Scale.CRORES}
    elif spec.unit == domain.AssumptionUnit.PER_SHARE:
        extra = {"currency": Currency.INR, "scale": Scale.ONES}
    elif spec.unit == domain.AssumptionUnit.COUNT:
        extra = {"scale": Scale.ONES}
    if spec.citation_required:
        extra["reference"] = "annual report FY2026, note 12"
    return AssumptionInput(
        kind=kind,
        scenario=scenario,
        value=Decimal(str(value)),
        unit=spec.unit,
        **(extra | kw),
    )


def wacc(
    s: Scenario,
    *,
    rf: object = 7,
    beta: object = 1,
    erp: object = 3,
    kd: object = 8,
    t: object = 25,
    we: object = 100,
    wd: object = 0,
    g: object | None = 5,
) -> list[AssumptionInput]:
    out = [
        a(K.RISK_FREE_RATE, rf, s),
        a(K.BETA, beta, s),
        a(K.EQUITY_RISK_PREMIUM, erp, s),
        a(K.PRE_TAX_COST_OF_DEBT, kd, s),
        a(K.WACC_TAX_RATE, t, s),
        a(K.EQUITY_WEIGHT, we, s),
        a(K.DEBT_WEIGHT, wd, s),
    ]
    if g is not None:
        out.append(a(K.TERMINAL_GROWTH, g, s))
    return out


def request(
    *,
    scenarios: tuple[Scenario, ...] = tuple(S),
    methods: tuple[ValuationMethod, ...] = (VM.DCF_FCFF_GORDON,),
    shares: object | None = SHARES,
    per_scenario: dict[Scenario, dict[str, Any]] | None = None,
    extra: list[AssumptionInput] = (),  # type: ignore[assignment]
    multiples: list[MultipleInput] = (),  # type: ignore[assignment]
    **kw: Any,
) -> ValuationRequest:
    assumptions: list[AssumptionInput] = []
    for s in scenarios:
        assumptions += wacc(s, **((per_scenario or {}).get(s, {})))
    if shares is not None:
        assumptions.append(a(K.SHARE_COUNT, shares))
    return ValuationRequest(
        scenarios=scenarios,
        methods=methods,
        assumptions=(*assumptions, *extra),
        multiples=tuple(multiples),
        **kw,
    )


def value(
    f: ForecastDataset,
    box4: CanonicalFinancialDataset | None = None,
    req: ValuationRequest | None = None,
    **kw: Any,
) -> ValuationDataset:
    return build_valuation(f, request=req or request(**kw), box4=box4, generated_at=NOW)


def result(
    v: ValuationDataset,
    method: ValuationMethod,
    scenario: Scenario = S.BASE,
    year: int | None = None,
) -> MethodResult:
    found = [
        r
        for r in v.results
        if r.method == method
        and r.scenario == scenario
        and (year is None or (r.multiple and r.multiple.fiscal_year == year))
    ]
    assert len(found) == 1, (method, scenario, year, found)
    return found[0]


def named(v: ValuationDataset, name: str, scenario: Scenario | None = S.BASE, **kw: Any):
    found = [
        x
        for x in v.values
        if x.name == name
        and x.scenario == scenario
        and all(getattr(x, k) == want for k, want in kw.items())
    ]
    assert len(found) == 1, (name, scenario, kw, found)
    return found[0]


def codes(v: ValuationDataset) -> set[str]:
    return {i.code for i in v.issues}


def gordon(flows: list[object], w: object, g: object) -> dict[str, Fraction]:
    """Independent DCF: end-of-year discounting, TV at the end of year N, discounted N years."""
    w_, g_ = F(w) / 100, F(g) / 100
    n = len(flows)
    pv = sum((F(c) / (1 + w_) ** t for t, c in enumerate(flows, start=1)), Fraction(0))
    tv = F(flows[-1]) * (1 + g_) / (w_ - g_)
    pv_tv = tv / (1 + w_) ** n
    return {"pv": pv, "tv": tv, "pv_tv": pv_tv, "ev": pv + pv_tv}


def per_share(equity: Fraction, shares: object = SHARES) -> Fraction:
    return equity * 10**7 / F(shares)


# --- DCF sanity (independent arithmetic) ------------------------------------------------------


def test_spec_example_100_110_121_at_10_percent_and_5_percent_growth():
    f, box4 = forecast([100, 110, 121])
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    # 100/1.1 + 110/1.21 + 121/1.331 = 3 x 1000/11; TV = 121 x 1.05 / 0.05 = 2541
    assert close(r.sum_present_value_forecast, Fraction(3000, 11))
    assert F(r.terminal_value) == 2541
    assert close(r.present_value_of_terminal_value, Fraction(21000, 11))  # 2541 / 1.331
    assert close(r.enterprise_value, Fraction(24000, 11))
    equity = Fraction(24000, 11) - DEBT + CASH  # standalone: no non-controlling interest
    assert close(r.equity_value, equity)
    assert close(r.fair_value_per_share, equity * 10**7 / SHARES)
    assert r.status == ValuationStatus.COMPLETE and r.wacc == 10 and r.terminal_growth == 5
    assert [p.period_index for p in r.schedule] == [1, 2, 3]
    assert [p.fiscal_year for p in r.schedule] == [2027, 2028, 2029]
    assert [F(p.cash_flow) for p in r.schedule] == [100, 110, 121]
    assert close(r.schedule[0].discount_factor, Fraction(10, 11))
    assert close(r.schedule[2].discount_factor, Fraction(1000, 1331))
    for p in r.schedule:
        assert close(p.present_value, Fraction(1000, 11))


@pytest.mark.parametrize(
    ("flows", "w", "g"),
    [
        ([100, 100, 100], 10, 5),  # flat
        ([100, 110, 121], 10, 5),  # growing
        ([100, 90, 81], 10, 5),  # shrinking
        ([-50, -20, 10], 10, 5),  # negative early cash flows
        ([100, 110, 121], 60, 5),  # high WACC
        ([100, 110, 121], 10, -2),  # negative terminal growth
        ([100], 10, 5),  # 1-year horizon
        ([100, 105, "110.25", "115.7625", "121.550625"], 12, 4),  # 5-year horizon
    ],
)
def test_dcf_matches_independent_calculation(flows, w, g):
    f, box4 = forecast(flows)
    rf = F(w) - 3  # CAPM: rf + 1 x 3 = w
    v = value(f, box4, per_scenario={s: {"rf": rf, "g": g} for s in S})
    r = result(v, VM.DCF_FCFF_GORDON)
    want = gordon(flows, w, g)
    assert close(r.sum_present_value_forecast, want["pv"])
    assert close(r.terminal_value, want["tv"])
    assert close(r.present_value_of_terminal_value, want["pv_tv"])
    assert close(r.enterprise_value, want["ev"])
    assert close(r.equity_value, want["ev"] - DEBT + CASH)
    assert close(r.fair_value_per_share, per_share(want["ev"] - DEBT + CASH))
    assert len(r.schedule) == len(flows)


def test_negative_final_cash_flow_is_valued_and_flagged():
    f, box4 = forecast([100, 50, -40])
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    want = gordon([100, 50, -40], 10, 5)
    assert want["tv"] < 0 and close(r.terminal_value, want["tv"])
    assert "TERMINAL_CASH_FLOW_NOT_POSITIVE" in codes(v)
    assert "NEGATIVE_EQUITY_VALUE" in codes(v) and r.equity_value < 0


@pytest.mark.parametrize(("rf", "g"), [(2, 5), ("1.5", "4.5")])  # WACC = g exactly
def test_wacc_equal_to_terminal_growth_fails_closed(rf, g):
    f, box4 = forecast()
    v = value(f, box4, per_scenario={S.BASE: {"rf": rf, "g": g}})
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.UNAVAILABLE and r.reason == "WACC_NOT_ABOVE_TERMINAL_GROWTH"
    assert r.enterprise_value is None and r.terminal_value is None
    assert "WACC_NOT_ABOVE_TERMINAL_GROWTH" in codes(v)
    # the other scenarios are untouched
    assert result(v, VM.DCF_FCFF_GORDON, S.BULL).status == ValuationStatus.COMPLETE


def test_wacc_below_terminal_growth_fails_closed():
    f, box4 = forecast()
    v = value(f, box4, per_scenario={S.BEAR: {"rf": 1, "g": 6}})  # WACC 4 < g 6
    r = result(v, VM.DCF_FCFF_GORDON, S.BEAR)
    assert r.status == ValuationStatus.UNAVAILABLE and r.fair_value_per_share is None
    issue = next(i for i in v.issues if i.code == "WACC_NOT_ABOVE_TERMINAL_GROWTH")
    assert issue.scenario == S.BEAR and issue.method == VM.DCF_FCFF_GORDON


def test_terminal_value_is_discounted_n_periods_not_n_plus_one():
    f, box4 = forecast([100, 100])
    r = result(value(f, box4), VM.DCF_FCFF_GORDON)
    tv = Fraction(100) * F("1.05") / F("0.05")  # FCFF(N+1) = FCFF(N) x (1+g): 2100
    assert F(r.terminal_value) == tv
    assert close(r.present_value_of_terminal_value, tv / F("1.1") ** 2)
    assert not close(r.present_value_of_terminal_value, tv / F("1.1") ** 3)
    # FCFF(N), not FCFF(N+1), is never the Gordon numerator
    assert not close(r.terminal_value, Fraction(100) / F("0.05"))


def test_dcf_horizon_shorter_than_the_forecast_uses_the_first_years():
    f, box4 = forecast([100, 110, 121, 133])
    r = result(value(f, box4, dcf_horizon=2), VM.DCF_FCFF_GORDON)
    want = gordon([100, 110], 10, 5)
    assert [p.fiscal_year for p in r.schedule] == [2027, 2028]
    assert close(r.enterprise_value, want["ev"])
    with pytest.raises(ValuationInputError) as err:
        value(f, box4, dcf_horizon=5)
    assert err.value.code == "HORIZON_EXCEEDS_FORECAST"
    with pytest.raises(ValidationError):
        ValuationRequest(dcf_horizon=0)
    with pytest.raises(ValidationError):
        ValuationRequest(dcf_horizon=6)


def test_fcff_is_nopat_plus_da_minus_wc_change_minus_capex_from_box10_values():
    """Tax, D&A and a working-capital change all present: rebuild FCFF from Box 10 values."""
    box4 = make(rows())
    f = build_forecast(
        box4,
        request=ForecastRequest(
            horizon=3,
            explicit_assumptions=[
                explicit(d, x, s, y)
                for s in S
                for y in (2027, 2028, 2029)
                for d, x in (
                    (D.REVENUE_GROWTH, 10),
                    (D.TAX_RATE, 25),
                    (D.DA_TO_REVENUE, 3),
                    (D.CAPEX_TO_REVENUE, 5),
                    (D.WORKING_CAPITAL_TO_REVENUE, 22),
                )
            ],
        ),
        generated_at=NOW,
    )
    v = value(f, box4)
    for y in (2027, 2028, 2029):
        g = {
            x.line: F(x.value)
            for x in f.values
            if x.scenario == S.BASE and x.period.fiscal_year == y and x.value is not None
        }
        ebit = g[L.OPERATING_PROFIT] - g[L.DEPRECIATION_AMORTIZATION]  # other income excluded
        nopat = ebit * F("0.75")
        want = nopat + g[L.DEPRECIATION_AMORTIZATION] - g[L.WORKING_CAPITAL_CHANGE] - g[L.CAPEX]
        assert close(named(v, "FCFF", fiscal_year=y).value, want)
        # Box 10 FCF (after interest) is a different number: never reused as FCFF
        assert not close(named(v, "FCFF", fiscal_year=y).value, g[L.FCF_ESTIMATE])
        assert g[L.OTHER_INCOME] > 0  # the excluded treasury-style income exists
    assert close(named(v, "OPERATING_TAX", fiscal_year=2027).value, (F(220) - 33) * F("0.25"))


def test_no_tax_credit_on_an_operating_loss():
    f, box4 = forecast([-10, -10, -10], margin=1, tax=25)  # operating EBIT 10 > 0 here
    v = value(f, box4)
    assert close(named(v, "OPERATING_TAX", fiscal_year=2027).value, F("2.5"))
    loss, box4b = forecast([-100, -100, -100], margin=-5, tax=25)  # operating EBIT -50
    w = value(loss, box4b)
    assert named(w, "OPERATING_TAX", fiscal_year=2027).value == 0
    assert named(w, "NOPAT", fiscal_year=2027).value == -50


# --- WACC -------------------------------------------------------------------------------------


def test_wacc_is_weighted_capm_and_after_tax_debt():
    f, box4 = forecast()
    v = value(
        f,
        box4,
        per_scenario={
            S.BASE: {
                "rf": "7.1",
                "beta": "1.2",
                "erp": "5.5",
                "kd": "9",
                "t": "25.17",
                "we": 70,
                "wd": 30,
            }
        },
    )
    ke = F("7.1") + F("1.2") * F("5.5")  # 13.7
    kd = F(9) * (1 - F("25.17") / 100)
    w = (70 * ke + 30 * kd) / 100
    assert F(named(v, "COST_OF_EQUITY").value) == ke
    assert F(named(v, "AFTER_TAX_COST_OF_DEBT").value) == kd
    assert close(named(v, "WACC").value, w)
    assert close(result(v, VM.DCF_FCFF_GORDON).wacc, w)
    assert named(v, "COST_OF_EQUITY").formula_id == "COST_OF_EQUITY/1/CAPM"


def test_explicit_cost_of_equity_replaces_capm_and_never_mixes():
    f, box4 = forecast()
    explicit_ke = [
        a(K.COST_OF_EQUITY, 12, S.BASE),
        a(K.PRE_TAX_COST_OF_DEBT, 8, S.BASE),
        a(K.WACC_TAX_RATE, 25, S.BASE),
        a(K.EQUITY_WEIGHT, 100, S.BASE),
        a(K.DEBT_WEIGHT, 0, S.BASE),
        a(K.TERMINAL_GROWTH, 5, S.BASE),
        a(K.SHARE_COUNT, SHARES),
    ]
    req = ValuationRequest(
        scenarios=(S.BASE,), methods=(VM.DCF_FCFF_GORDON,), assumptions=tuple(explicit_ke)
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    assert named(v, "COST_OF_EQUITY").formula_id == "COST_OF_EQUITY/1/EXPLICIT"
    assert named(v, "WACC").value == 12
    with pytest.raises(ValidationError, match="conflict"):
        ValuationRequest(scenarios=(S.BASE,), assumptions=(*explicit_ke, a(K.BETA, 1, S.BASE)))


@pytest.mark.parametrize(("we", "wd"), [(80, 30), (70, 20), ("99.99", 0)])
def test_weights_must_sum_to_exactly_100(we, wd):
    f, box4 = forecast()
    with pytest.raises(ValuationInputError) as err:
        value(f, box4, per_scenario={S.BULL: {"we": we, "wd": wd}})
    assert err.value.code == "WEIGHTS_DO_NOT_SUM_TO_100"


def test_missing_wacc_input_makes_the_dcf_unavailable_not_defaulted():
    f, box4 = forecast()
    inputs = [x for x in wacc(S.BASE) if x.kind != K.BETA] + [a(K.SHARE_COUNT, SHARES)]
    req = ValuationRequest(
        scenarios=(S.BASE,), methods=(VM.DCF_FCFF_GORDON,), assumptions=tuple(inputs)
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.UNAVAILABLE and r.enterprise_value is None
    assert named(v, "COST_OF_EQUITY").reason == "INPUT_MISSING"
    assert any("BETA" in m for m in named(v, "COST_OF_EQUITY").missing_inputs)


def test_missing_terminal_growth_makes_the_gordon_dcf_unavailable():
    f, box4 = forecast()
    v = value(f, box4, per_scenario={S.BASE: {"g": None}})
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.UNAVAILABLE and r.terminal_value is None
    assert r.sum_present_value_forecast is not None  # the explicit years are still shown


def test_percent_given_as_a_decimal_is_flagged():
    f, box4 = forecast()
    v = value(f, box4, per_scenario={S.BASE: {"rf": "0.07", "erp": "0.05", "g": "0.03"}})
    flagged = {i.subject for i in v.issues if i.code == "PERCENT_BELOW_ONE"}
    assert {"RISK_FREE_RATE", "EQUITY_RISK_PREMIUM", "TERMINAL_GROWTH"} <= flagged
    assert next(
        x for x in v.assumptions if x.kind == K.RISK_FREE_RATE and x.scenario == S.BASE
    ).warnings == ("PERCENT_BELOW_ONE",)


def test_wacc_at_or_below_minus_100_is_unusable():
    f, box4 = forecast()
    v = value(f, box4, per_scenario={S.BASE: {"rf": -99, "beta": 1, "erp": -5, "g": -50}})
    assert "WACC_OUT_OF_RANGE" in codes(v)  # cost of equity -104 % -> WACC -104 %
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.UNAVAILABLE and r.reason == "WACC_OUT_OF_RANGE"
    assert all(p.discount_factor is None for p in r.schedule)
    slightly, box4b = forecast()
    w = value(slightly, box4b, per_scenario={S.BASE: {"rf": -9, "g": -50}})  # WACC -6 %
    assert "NON_POSITIVE_WACC" in codes(w)
    assert close(
        result(w, VM.DCF_FCFF_GORDON).enterprise_value, gordon([100, 110, 121], -6, -50)["ev"]
    )


# --- bridge, minority interest, shares --------------------------------------------------------


def test_bridge_signs_debt_subtracted_cash_added_minority_subtracted():
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10)
    req = request(extra=[a(K.MINORITY_INTEREST, 150)])
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    r = result(v, VM.DCF_FCFF_GORDON)
    ev = gordon([100, 110, 121], 10, 5)["ev"]
    assert close(r.equity_value, ev - DEBT + CASH - 150)
    equity = named(v, "EQUITY_VALUE", method=VM.DCF_FCFF_GORDON)
    roles = {i.role: (i.kind, i.value) for i in equity.inputs}
    assert roles["GROSS_DEBT"][1] == DEBT and roles["CASH"][1] == CASH
    assert roles["MINORITY_INTEREST"][1] == 150
    assert named(v, "GROSS_DEBT", None).formula_id == "GROSS_DEBT/1/BOX10_ACTUAL"
    debt_input = named(v, "GROSS_DEBT", None).inputs[0]
    assert debt_input.kind == InputKind.BOX10_ACTUAL
    assert debt_input.ref_id == next(x for x in f.actuals if x.line == L.GROSS_DEBT).value_id
    cash = named(v, "CASH", None)
    assert (
        cash.formula_id == "CASH/1/BOX4"
        and cash.box4_inputs[0].metric == M.CASH_AND_CASH_EQUIVALENTS
    )


def test_other_equity_adjustment_is_signed_and_cited():
    f, box4 = forecast()
    v = value(f, box4, extra=[a(K.OTHER_EQUITY_ADJUSTMENT, -75)])
    ev = gordon([100, 110, 121], 10, 5)["ev"]
    assert close(result(v, VM.DCF_FCFF_GORDON).equity_value, ev - DEBT + CASH - 75)
    assert named(v, "EQUITY_VALUE", method=VM.DCF_FCFF_GORDON).formula_id == (
        "EQUITY_VALUE/1/BRIDGE_WITH_ADJUSTMENT"
    )
    with pytest.raises(ValidationError, match="cites"):
        AssumptionInput(
            kind=K.OTHER_EQUITY_ADJUSTMENT,
            value=Decimal(1),
            unit=domain.AssumptionUnit.MONEY,
            currency=Currency.INR,
            scale=Scale.CRORES,
        )


def test_consolidated_without_minority_balance_is_partial_never_zero():
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10)
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.PARTIAL and r.enterprise_value is not None
    assert r.equity_value is None and r.fair_value_per_share is None
    assert named(v, "MINORITY_INTEREST", None).reason == "MINORITY_INTEREST_UNAVAILABLE"


def test_standalone_minority_is_zero_by_definition():
    f, box4 = forecast()
    v = value(f, box4)
    mi = named(v, "MINORITY_INTEREST", None)
    assert mi.value == 0 and mi.formula_id == "MINORITY_INTEREST/1/STANDALONE"


def test_missing_cash_is_never_zero():
    f, box4 = forecast(cash=None)
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.PARTIAL and r.equity_value is None
    assert named(v, "CASH", None).reason == "CASH_UNAVAILABLE"
    # without Box 4 there is nothing to read cash from either
    w = value(f, None)
    assert named(w, "CASH", None).reason == "CASH_UNAVAILABLE"
    assert result(w, VM.DCF_FCFF_GORDON).equity_value is None
    # an explicit, cited cash balance completes it
    x = value(f, None, extra=[a(K.CASH, 250)])
    ev = gordon([100, 110, 121], 10, 5)["ev"]
    assert close(result(x, VM.DCF_FCFF_GORDON).equity_value, ev - DEBT + 250)


def test_missing_debt_is_never_zero():
    f, box4 = forecast(drop=frozenset({(M.TOTAL_BORROWINGS, 2026)}))
    v = value(f, box4)
    assert named(v, "GROSS_DEBT", None).reason == "DEBT_UNAVAILABLE"
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.equity_value is None and r.status in (
        ValuationStatus.PARTIAL,
        ValuationStatus.UNAVAILABLE,
    )
    fixed = value(f, box4, extra=[a(K.GROSS_DEBT, 0)])  # an explicit zero is a statement
    assert named(fixed, "GROSS_DEBT", None).formula_id == "GROSS_DEBT/1/EXPLICIT"


def test_share_count_absent_keeps_equity_value_but_no_per_share():
    f, box4 = forecast()
    v = value(f, box4, shares=None)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.PARTIAL and r.equity_value is not None
    assert r.fair_value_per_share is None and r.reason == "SHARE_COUNT_UNAVAILABLE"
    assert named(v, "SHARE_COUNT", None).reason == "SHARE_COUNT_UNAVAILABLE"


@pytest.mark.parametrize("shares", [0, -5])
def test_zero_and_negative_share_counts_are_rejected(shares):
    with pytest.raises(ValidationError):
        a(K.SHARE_COUNT, shares)


@pytest.mark.parametrize("shares", [100, 10**6, 10**13, 10**15])  # millions / crore / thousands
def test_share_count_unit_mismatch_is_rejected(shares):
    f, box4 = forecast()
    v = value(f, box4, shares=shares)
    assert "SHARE_COUNT_UNIT_MISMATCH" in codes(v)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.fair_value_per_share is None and r.equity_value is not None


def test_share_count_change_within_tolerance_is_flagged_not_rejected():
    f, box4 = forecast()
    v = value(f, box4, shares=3 * 10**8)  # e.g. a bonus issue after year end
    assert "SHARE_COUNT_INCONSISTENT_WITH_HISTORY" in codes(v)
    equity = F(result(v, VM.DCF_FCFF_GORDON).equity_value)
    assert close(result(v, VM.DCF_FCFF_GORDON).fair_value_per_share, per_share(equity, 3 * 10**8))
    unverified = value(*forecast(eps=None))
    assert "SHARE_COUNT_UNVERIFIED" in codes(unverified)


def test_per_share_converts_crore_to_rupees_exactly():
    f, box4 = forecast()
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert F(r.fair_value_per_share) == F(r.equity_value) * 10**7 / SHARES
    assert named(v, "FAIR_VALUE_PER_SHARE", method=VM.DCF_FCFF_GORDON).unit == "PER_SHARE"
    assert named(v, "EQUITY_VALUE", method=VM.DCF_FCFF_GORDON).unit == "MONEY"


def test_market_price_comparison_is_arithmetic_only():
    f, box4 = forecast()
    v = value(f, box4, extra=[a(K.MARKET_PRICE, 120)])
    r = result(v, VM.DCF_FCFF_GORDON)
    want = (F(r.fair_value_per_share) - 120) / 120 * 100
    assert close(r.fair_value_vs_price_percent, want)
    assert value(f, box4).results[0].fair_value_vs_price_percent is None  # never invented


# --- multiples --------------------------------------------------------------------------------


def multiple(method: ValuationMethod, x: object, s: Scenario = S.BASE, y: int = 2027, **kw: Any):
    return MultipleInput(method=method, scenario=s, fiscal_year=y, multiple=Decimal(str(x)), **kw)


def test_pe_equity_value_is_attributable_pat_times_multiple():
    f, box4 = forecast()
    req = request(methods=(VM.PE,), scenarios=(S.BASE,), multiples=[multiple(VM.PE, 18)])
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    r = result(v, VM.PE)
    pat = F(f.value(L.PAT_ATTRIBUTABLE, S.BASE, 2027).value)
    assert close(r.equity_value, pat * 18) and r.enterprise_value is None
    assert close(F(r.equity_value) / pat, Fraction(18))  # P/E = equity / earnings
    assert close(r.fair_value_per_share, per_share(pat * 18))
    assert (
        r.multiple.basis == domain.MultipleBasis.EQUITY and r.multiple.metric == "PAT_ATTRIBUTABLE"
    )
    # an equity multiple is never bridged
    assert all(i.role != "GROSS_DEBT" for i in named(v, "EQUITY_VALUE", method=VM.PE).inputs)


def test_ev_ebitda_and_ev_ebit_are_bridged_to_equity():
    f, box4 = forecast(da=3, tax=25)
    req = request(
        methods=(VM.EV_EBITDA, VM.EV_EBIT),
        scenarios=(S.BASE,),
        multiples=[multiple(VM.EV_EBITDA, 9, y=2028), multiple(VM.EV_EBIT, 12, y=2028)],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    op = F(f.value(L.OPERATING_PROFIT, S.BASE, 2028).value)
    da = F(f.value(L.DEPRECIATION_AMORTIZATION, S.BASE, 2028).value)
    ebitda_box10 = F(f.value(L.EBITDA, S.BASE, 2028).value)
    assert ebitda_box10 != op  # Box 10 EBITDA includes other income; the EV multiple does not
    for method, metric, x in ((VM.EV_EBITDA, op, 9), (VM.EV_EBIT, op - da, 12)):
        r = result(v, method)
        assert close(r.enterprise_value, metric * x)
        assert close(F(r.enterprise_value) / metric, F(x))  # EV/metric = multiple
        assert close(r.equity_value, metric * x - DEBT + CASH)
        assert close(r.fair_value_per_share, per_share(metric * x - DEBT + CASH))
        assert (
            r.multiple.basis == domain.MultipleBasis.ENTERPRISE and r.multiple.fiscal_year == 2028
        )


def test_multiple_on_a_non_positive_metric_is_unavailable():
    f, box4 = forecast([-100, -100, -100], margin=-5)
    req = request(
        methods=(VM.PE, VM.EV_EBITDA),
        scenarios=(S.BASE,),
        multiples=[multiple(VM.PE, 15), multiple(VM.EV_EBITDA, 8)],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    for method in (VM.PE, VM.EV_EBITDA):
        r = result(v, method)
        assert r.status == ValuationStatus.UNAVAILABLE and r.reason == "NON_POSITIVE_METRIC"


def test_requested_multiple_method_without_a_multiple_is_unavailable():
    f, box4 = forecast()
    v = value(f, box4, methods=(VM.PE,), scenarios=(S.BASE,))
    r = result(v, VM.PE)
    assert r.status == ValuationStatus.UNAVAILABLE and r.reason == "MULTIPLE_MISSING"


def test_huge_but_finite_multiple_and_out_of_range_rejection():
    f, box4 = forecast()
    req = request(methods=(VM.PE,), scenarios=(S.BASE,), multiples=[multiple(VM.PE, 10**6)])
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    pat = F(f.value(L.PAT_ATTRIBUTABLE, S.BASE, 2027).value)
    assert close(result(v, VM.PE).equity_value, pat * 10**6)
    with pytest.raises(ValidationError):
        multiple(VM.PE, 10**6 + 1)
    with pytest.raises(ValidationError):
        multiple(VM.PE, 0)
    with pytest.raises(ValidationError):
        multiple(VM.EV_EBITDA, -3)


def test_peer_multiple_must_cite_and_never_comes_from_text():
    with pytest.raises(ValidationError, match="cites"):
        multiple(VM.PE, 20, source=MultipleSource.PEER_REFERENCE)
    ok = multiple(VM.PE, 20, source=MultipleSource.PEER_REFERENCE, reference="peer set table")
    assert ok.reference == "peer set table"


def test_exit_multiple_dcf_is_a_separate_method():
    f, box4 = forecast()
    req = request(
        methods=(VM.DCF_FCFF_GORDON, VM.DCF_FCFF_EXIT_MULTIPLE),
        scenarios=(S.BASE,),
        extra=[a(K.TERMINAL_EV_EBITDA, 8, S.BASE)],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    exit_ = result(v, VM.DCF_FCFF_EXIT_MULTIPLE)
    gordon_ = result(v, VM.DCF_FCFF_GORDON)
    tv = F(200) * 8  # operating EBITDA (operating profit) FY2029 x 8
    assert F(exit_.terminal_value) == tv
    assert close(exit_.enterprise_value, Fraction(3000, 11) + tv / F("1.331"))
    assert close(gordon_.enterprise_value, Fraction(24000, 11))  # never blended
    assert exit_.terminal_growth is None


def test_methods_are_reported_separately_never_averaged():
    f, box4 = forecast()
    req = request(
        methods=(VM.DCF_FCFF_GORDON, VM.PE), scenarios=(S.BASE,), multiples=[multiple(VM.PE, 15)]
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    assert {r.method for r in v.results} == {VM.DCF_FCFF_GORDON, VM.PE}
    assert all(x.name not in {"AVERAGE", "BLENDED_FAIR_VALUE"} for x in v.values)
    assert not any(
        "WEIGHT" in f_.formula_id and "WACC" not in f_.formula_id for f_ in v.manifest.formulas
    )


# --- scenarios --------------------------------------------------------------------------------


def test_each_scenario_is_valued_from_its_own_forecast():
    flows = {S.BASE: [100, 110, 121], S.BULL: [120, 140, 160], S.BEAR: [80, 70, 60]}
    f, box4 = forecast(flows)
    v = value(f, box4, per_scenario={S.BULL: {"rf": 6}, S.BEAR: {"rf": 9, "g": 3}})
    for s, w, g in ((S.BASE, 10, 5), (S.BULL, 9, 5), (S.BEAR, 12, 3)):
        want = gordon(flows[s], w, g)
        assert close(result(v, VM.DCF_FCFF_GORDON, s).enterprise_value, want["ev"]), s
    # BULL is not BASE x a premium
    base = F(result(v, VM.DCF_FCFF_GORDON, S.BASE).enterprise_value)
    bull = F(result(v, VM.DCF_FCFF_GORDON, S.BULL).enterprise_value)
    assert bull != base and not close(bull, base * F("1.2"))


def test_changing_bear_inputs_leaves_base_and_bull_byte_identical():
    f, box4 = forecast()
    one = value(f, box4)
    two = value(f, box4, per_scenario={S.BEAR: {"rf": 11, "g": 2, "beta": "1.4"}})

    def scoped(v: ValuationDataset, s: Scenario) -> list[dict[str, Any]]:
        return [x.model_dump(mode="json") for x in v.values if x.scenario == s] + [
            r.model_dump(mode="json") for r in v.results if r.scenario == s
        ]

    for s in (S.BASE, S.BULL):
        assert scoped(one, s) == scoped(two, s)
    assert scoped(one, S.BEAR) != scoped(two, S.BEAR)


def test_every_input_belongs_to_its_own_scenario():
    flows = {S.BASE: [100, 110, 121], S.BULL: [120, 140, 160], S.BEAR: [80, 70, 60]}
    f, box4 = forecast(flows)
    v = value(f, box4, per_scenario={S.BULL: {"rf": 6}})
    box10 = {x.value_id: x for x in f.values}
    box10_assumptions = {x.assumption_id: x for x in f.assumptions}
    for x in v.values:
        for i in x.inputs:
            if i.kind == InputKind.BOX10_FORECAST:
                assert box10[i.ref_id].scenario == x.scenario and box10[i.ref_id].value == i.value
            if i.kind == InputKind.BOX10_ASSUMPTION:
                assert box10_assumptions[i.ref_id].scenario == x.scenario


def test_scenario_not_forecast_is_rejected():
    f, box4 = forecast({S.BASE: [100, 110, 121]})
    with pytest.raises(ValuationInputError) as err:
        value(f, box4)  # requests BULL and BEAR
    assert err.value.code == "SCENARIO_NOT_FORECAST"
    ok = value(f, box4, scenarios=(S.BASE,))
    assert {r.scenario for r in ok.results} == {S.BASE}


# --- request validation -----------------------------------------------------------------------


def test_duplicate_assumptions_and_multiples_are_rejected():
    with pytest.raises(ValidationError, match="duplicate assumption"):
        ValuationRequest(assumptions=(a(K.BETA, 1, S.BASE), a(K.BETA, "1.1", S.BASE)))
    with pytest.raises(ValidationError, match="duplicate assumption"):
        ValuationRequest(assumptions=(a(K.SHARE_COUNT, 10), a(K.SHARE_COUNT, 11)))
    with pytest.raises(ValidationError, match="duplicate multiple"):
        ValuationRequest(methods=(VM.PE,), multiples=(multiple(VM.PE, 10), multiple(VM.PE, 11)))
    with pytest.raises(ValidationError):
        ValuationRequest(scenarios=(S.BASE, S.BASE))
    with pytest.raises(ValidationError):
        ValuationRequest(methods=(VM.PE, VM.PE))


@pytest.mark.parametrize(
    "bad",
    [
        {"methods": ["DCF"]},
        {"methods": ["dcf_fcff_gordon"]},
        {"methods": ["BUY"]},
        {"methods": []},
        {"scenarios": ["BASE", "STRESS"]},
        {"unknown_field": 1},
    ],
)
def test_malformed_method_ids_and_fields_are_rejected(bad):
    with pytest.raises(ValidationError):
        ValuationRequest.model_validate(bad)


@pytest.mark.parametrize(
    ("kind", "value", "kw"),
    [
        (K.TERMINAL_GROWTH, "-100", {}),  # g <= -100 %
        (K.WACC_TAX_RATE, "101", {}),
        (K.EQUITY_WEIGHT, "-1", {}),
        (K.BETA, "1001", {}),
        (K.RISK_FREE_RATE, "NaN", {}),
        (K.RISK_FREE_RATE, "Infinity", {}),
    ],
)
def test_out_of_bounds_and_non_finite_assumptions_are_rejected(kind, value, kw):
    with pytest.raises(ValidationError):
        a(kind, value, S.BASE, **kw)


def test_units_and_scopes_are_enforced():
    with pytest.raises(ValidationError, match="expressed in"):
        AssumptionInput(kind=K.BETA, scenario=S.BASE, value=Decimal(1), unit="PERCENT")
    with pytest.raises(ValidationError, match="per scenario"):
        AssumptionInput(kind=K.BETA, value=Decimal(1), unit="RATIO")
    with pytest.raises(ValidationError, match="scenario-free"):
        a(K.SHARE_COUNT, SHARES, S.BASE)
    with pytest.raises(ValidationError, match="CRORES"):
        a(K.CASH, 10, scale=Scale.ONES)
    with pytest.raises(ValidationError, match="ONES"):
        a(K.SHARE_COUNT, SHARES, scale=Scale.MILLIONS)
    with pytest.raises(ValidationError, match="currency"):
        a(K.CASH, 10, currency=None)
    with pytest.raises(ValidationError):
        AssumptionInput(kind=K.BETA, scenario=S.BASE, value=1.0, unit="RATIO")  # float


def test_conflicting_currency_is_rejected():
    f, box4 = forecast()
    with pytest.raises(ValuationInputError) as err:
        value(f, box4, extra=[a(K.CASH, 10, currency=Currency.USD)])
    assert err.value.code == "CURRENCY_MISMATCH"
    with pytest.raises(ValuationInputError):
        value(f, box4, extra=[a(K.MARKET_PRICE, 10, currency=Currency.USD)])


def test_unsupported_basis_and_foreign_box4_are_rejected():
    f, box4 = forecast()
    with pytest.raises(ValuationInputError) as err:
        value(f, box4, reporting_basis=B.CONSOLIDATED)
    assert err.value.code == "BASIS_MISMATCH"
    other = make(rows(cash=301))
    with pytest.raises(ValuationInputError) as err:
        value(f, other)
    assert err.value.code == "BOX4_MISMATCH"


def test_multiple_for_a_year_that_was_not_forecast_is_rejected():
    f, box4 = forecast()
    req = request(methods=(VM.PE,), multiples=[multiple(VM.PE, 15, y=2031)])
    with pytest.raises(ValuationInputError) as err:
        build_valuation(f, request=req, box4=box4, generated_at=NOW)
    assert err.value.code == "MULTIPLE_YEAR_NOT_FORECAST"
    with pytest.raises(ValidationError, match="unrequested method"):
        ValuationRequest(methods=(VM.DCF_FCFF_GORDON,), multiples=(multiple(VM.PE, 15),))
    with pytest.raises(ValidationError, match="DCF_FCFF_EXIT_MULTIPLE"):
        ValuationRequest(
            assumptions=(a(K.TERMINAL_EV_EBITDA, 8, S.BASE),), methods=(VM.DCF_FCFF_GORDON,)
        )


def test_tampered_forecast_is_rejected_by_the_box10_contract():
    f, box4 = forecast()
    bad_value = f.values[0].model_copy(update={"value": Decimal(999)})
    forged = ForecastDataset.model_construct(**{**dict(f), "values": (bad_value, *f.values[1:])})
    with pytest.raises(ValidationError):
        build_valuation(forged, request=request(), box4=box4, generated_at=NOW)


# --- unavailable paths ------------------------------------------------------------------------


def test_missing_forecast_lines_make_the_dcf_unavailable():
    """A Screener-shaped history without capex: Box 10 cannot forecast capex -> no FCFF."""
    data = make([r for r in rows() if r.metric != M.PURCHASE_OF_PPE])
    f = build_forecast(data, request=ForecastRequest(horizon=3), generated_at=NOW)
    assert f.value(L.CAPEX, S.BASE, 2027).status == "UNAVAILABLE"
    v = value(f, data)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert r.status == ValuationStatus.UNAVAILABLE and r.enterprise_value is None
    assert named(v, "FCFF", fiscal_year=2027).reason == "UPSTREAM_UNAVAILABLE"
    assert "METHOD_UNAVAILABLE" in codes(v)


def test_financial_sector_gets_no_dcf_and_no_ev_multiples():
    data = make(rows(), Industry.BANK)
    f = build_forecast(data, request=ForecastRequest(horizon=3), generated_at=NOW)
    req = request(
        methods=(VM.DCF_FCFF_GORDON, VM.EV_EBITDA, VM.PE),
        scenarios=(S.BASE,),
        multiples=[multiple(VM.EV_EBITDA, 8), multiple(VM.PE, 12)],
    )
    v = build_valuation(f, request=req, box4=data, generated_at=NOW)
    assert result(v, VM.DCF_FCFF_GORDON).reason == "UNSUPPORTED_SECTOR"
    assert result(v, VM.EV_EBITDA).reason == "UNSUPPORTED_SECTOR"
    assert result(v, VM.PE).status == ValuationStatus.UNAVAILABLE  # Box 10 has no bank PAT


def test_reported_ebitda_route_is_flagged():
    base = [r for r in rows() if r.metric != M.OPERATING_PROFIT]
    base += [V(M.EBITDA, fy(y), 220, basis=B.STANDALONE) for y in range(2021, 2027)]
    data = make(base)
    f = build_forecast(
        data,
        request=ForecastRequest(
            horizon=3,
            explicit_assumptions=[
                explicit(D.CAPEX_AMOUNT, 50, s, y) for s in S for y in (2027, 2028, 2029)
            ],
        ),
        generated_at=NOW,
    )
    v = value(f, data, scenarios=(S.BASE,))
    ebitda = named(v, "OPERATING_EBITDA", fiscal_year=2027)
    assert ebitda.formula_id == "OPERATING_EBITDA/1/REPORTED_EBITDA"
    assert "EBITDA_DEFINITION_AS_REPORTED" in ebitda.warnings


def test_huge_but_finite_cash_flows_and_out_of_range():
    big = 10**12  # crore: revenue 10^13 crore, operating profit 2 x 10^12
    f, box4 = forecast([big, big, big], revenue=10**13, debt=0, cash=0, eps=None)
    v = value(f, box4)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert close(r.enterprise_value, gordon([big] * 3, 10, 5)["ev"])
    # a WACC barely above g pushes the terminal value beyond the arithmetic guard
    w = value(f, box4, per_scenario={S.BASE: {"rf": "2.000000000000000000001"}})
    assert result(w, VM.DCF_FCFF_GORDON).status == ValuationStatus.UNAVAILABLE
    assert "VALUE_OUT_OF_RANGE" in codes(w)


# --- provenance, determinism, immutability ----------------------------------------------------


def test_every_number_has_a_traceable_path():
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10)
    req = request(
        methods=DEFAULT_METHODS,  # every industrial method (P/B is the lender model's)
        extra=[
            a(K.MINORITY_INTEREST, 150),
            a(K.MARKET_PRICE, 100),
            *(a(K.TERMINAL_EV_EBITDA, 8, s) for s in S),
        ],
        multiples=[multiple(m, 10, s) for m in (VM.PE, VM.EV_EBITDA, VM.EV_EBIT) for s in S],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    assert v.status == ValuationStatus.COMPLETE
    box10 = {x.value_id: x for x in (*f.values, *f.actuals)}
    box10_a = {x.assumption_id for x in f.assumptions}
    own = {x.value_id: x for x in v.values}
    assumptions = {x.assumption_id for x in v.assumptions}
    formulas = {fm.formula_id: fm for fm in v.manifest.formulas}
    for x in v.values:
        if x.status == "UNAVAILABLE":
            assert x.reason
            continue
        assert x.formula_id in formulas
        assert x.formula_id in FORMULAS
        for i in x.inputs:
            match i.kind:
                case InputKind.BOX10_FORECAST | InputKind.BOX10_ACTUAL:
                    assert box10[i.ref_id].value == i.value
                case InputKind.BOX10_ASSUMPTION:
                    assert i.ref_id in box10_a
                case InputKind.ASSUMPTION:
                    assert i.ref_id in assumptions
                case InputKind.VALUATION:
                    assert own[i.ref_id].value == i.value
                case InputKind.MULTIPLE:
                    assert i.ref_id in v.multiple_ids
                case InputKind.BOX4_VALUE:
                    assert {m.canonical_value_id for m in x.box4_inputs} >= {i.ref_id}
                case InputKind.PERIOD_INDEX:
                    assert i.ref_id is None and i.value >= 1
        # re-evaluating the registered formula on the recorded inputs gives the recorded value
        fm = formulas[x.formula_id]
        fixed = {i.role: i.value for i in x.inputs if i.role != fm.variadic}
        repeated = [i.value for i in x.inputs if i.role == fm.variadic]
        assert evaluate(x.formula_id, fixed, repeated) == x.value
    for r in v.results:
        assert set(r.value_ids) <= set(own)
        if r.fair_value_per_share is not None:
            assert r.fair_value_per_share in {own[i].value for i in r.value_ids}


def test_contract_rejects_a_number_without_formula_or_a_foreign_scenario_input():
    with pytest.raises(ValidationError):
        ValuationValue(
            value_id=UUID(int=1),
            name="WACC",
            scenario=S.BASE,
            method=None,
            status="AVAILABLE",
            value=Decimal(10),
            unit="PERCENT",
            currency=None,
        )
    f, box4 = forecast()
    v = value(f, box4)
    bull_wacc = named(v, "WACC", S.BULL)
    base_pv = named(v, "PRESENT_VALUE", fiscal_year=2027)
    leaked = base_pv.model_copy(
        update={
            "inputs": tuple(
                i.model_copy(update={"ref_id": bull_wacc.value_id, "value": bull_wacc.value})
                if i.role == "DISCOUNT_FACTOR"
                else i
                for i in base_pv.inputs
            )
        }
    )
    with pytest.raises(ValidationError):
        ValuationDataset.model_validate(
            {
                **v.model_dump(),
                "values": [
                    leaked.model_dump() if x.value_id == base_pv.value_id else x.model_dump()
                    for x in v.values
                ],
            }
        )


def test_formula_registry_is_pure_and_complete():
    for formula_id, (definition, _) in FORMULAS.items():
        assert definition.formula_id == formula_id
    assert close(
        evaluate("DISCOUNT_FACTOR/1", {"WACC": Decimal(10), "PERIOD": Decimal(2)}),
        Fraction(100, 121),
    )
    with pytest.raises(ValueError):
        evaluate("WACC/1", {"EQUITY_WEIGHT": Decimal(1)})
    with pytest.raises(ValueError):
        evaluate(
            "NOPAT/1", {"OPERATING_EBIT": Decimal(1), "OPERATING_TAX": Decimal(0)}, [Decimal(1)]
        )


def test_deterministic_repeated_execution():
    f, box4 = forecast()
    req = request()
    one = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    two = build_valuation(f, request=req, box4=box4, generated_at=LATER)
    assert canonical_valuation_json(one) == canonical_valuation_json(two)
    assert one.valuation_id == two.valuation_id
    runs = {
        canonical_valuation_json(build_valuation(f, request=req, box4=box4, generated_at=NOW))
        for _ in range(3)
    }
    assert len(runs) == 1


def test_inputs_are_not_mutated():
    f, box4 = forecast()
    req = request(extra=[a(K.MARKET_PRICE, 100)])
    before = (canonical_forecast_json(f), box4.model_dump_json(), req.model_dump_json())
    build_valuation(f, request=req, box4=box4, generated_at=NOW)
    after = (canonical_forecast_json(f), box4.model_dump_json(), req.model_dump_json())
    assert before == after


def test_output_does_not_duplicate_the_forecast_or_box4():
    f, box4 = forecast()
    v = value(f, box4)
    dumped = v.model_dump()
    assert "values" in dumped and "actuals" not in dumped and "canonical_values" not in dumped
    assert len(canonical_valuation_json(v)) < len(canonical_forecast_json(f)) * 2


async def test_async_wrapper_matches_and_runs_off_the_loop(monkeypatch):
    f, box4 = forecast()
    calls: list[str] = []
    real = asyncio.to_thread

    async def spy(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append(fn.__name__)
        return await real(fn, *args, **kw)

    monkeypatch.setattr(engine.asyncio, "to_thread", spy)
    got = await build_valuation_async(f, request=request(), box4=box4, generated_at=NOW)
    assert calls == ["build_valuation"]
    assert canonical_valuation_json(got) == canonical_valuation_json(value(f, box4))


def test_no_recommendation_leakage():
    forbidden = {
        "buy",
        "sell",
        "hold",
        "recommend",
        "recommendation",
        "overweight",
        "underweight",
        "rating",
        "position",
        "allocation",
        "sizing",
        "target",
        "conviction",
        "upside",
    }
    root = Path(engine.__file__).parent.parent
    for path in (
        root / "application/fundamental_valuation.py",
        root / "domain/fundamental_valuation.py",
    ):
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.ClassDef)}
        tokens = {t for x in names for t in x.lower().split("_")}
        assert not tokens & forbidden, path
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            for alias in n.names
        } | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        banned = {"httpx", "requests", "socket", "urllib", "random", "time", "secrets", "os"}
        assert not {m.split(".")[0] for m in imported} & banned, path
        assert not {m for m in imported if "aicredits" in m or "anthropic" in m}, path
        assert "now" not in names and "today" not in names, path  # no clock
    vocabulary = {
        t
        for e in (ValuationMethod, AssumptionKind, InputKind)
        for m in e
        for t in m.value.lower().split("_")
    }
    assert not vocabulary & forbidden
    fields = {
        t
        for m in vars(domain).values()
        if isinstance(m, type) and hasattr(m, "model_fields")
        for name in m.model_fields
        for t in name.split("_")
    }
    assert not fields & forbidden


# --- stress / performance ---------------------------------------------------------------------


def test_maximum_horizon_three_scenarios_all_methods():
    flows = {s: [100 + 10 * i + k for i in range(5)] for k, s in enumerate(S)}
    f, box4 = forecast(flows)
    req = request(
        methods=DEFAULT_METHODS,  # every industrial method (P/B is the lender model's)
        extra=[a(K.MARKET_PRICE, 100), *(a(K.TERMINAL_EV_EBITDA, 8, s) for s in S)],
        multiples=[
            multiple(m, 10, s, y)
            for m in (VM.PE, VM.EV_EBITDA, VM.EV_EBIT)
            for s in S
            for y in YEARS
        ],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    assert v.status == ValuationStatus.COMPLETE
    assert len(v.results) == 3 * (2 + 3 * 5)
    for s in S:
        want = gordon(flows[s], 10, 5)
        assert close(result(v, VM.DCF_FCFF_GORDON, s).enterprise_value, want["ev"])


def test_performance_and_memory_are_modest():
    f1, box4 = forecast({S.BASE: [100, 110, 121, 133, 146]})
    f3, box4_3 = forecast([100, 110, 121, 133, 146])
    full = request(
        methods=(VM.DCF_FCFF_GORDON, VM.PE, VM.EV_EBITDA),
        multiples=[multiple(m, 10, s) for m in (VM.PE, VM.EV_EBITDA) for s in S],
    )
    cases = {
        "one scenario DCF": (f1, box4, request(scenarios=(S.BASE,))),
        "three scenario DCF": (f3, box4_3, request()),
        "DCF + multiples": (f3, box4_3, full),
    }
    for label, (f, data, req) in cases.items():
        tracemalloc.start()
        started = time.perf_counter()
        for _ in range(5):
            build_valuation(f, request=req, box4=data, generated_at=NOW)
        elapsed = (time.perf_counter() - started) / 5
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert elapsed < 2 and peak < 50 * 1024 * 1024, (label, elapsed, peak)


# --- the real Screener fixture ----------------------------------------------------------------


async def test_infy_fixture_valuation(tmp_path, fixture_dir):
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
    req = ValuationRequest(
        methods=(VM.DCF_FCFF_GORDON, VM.PE),
        assumptions=tuple(x for s in S for x in wacc(s)),
        multiples=tuple(multiple(VM.PE, 22, s) for s in S),
    )
    v = await build_valuation_async(f, request=req, box4=data, generated_at=NOW)
    assert v.currency == Currency.INR and v.reporting_basis == B.STANDALONE
    # Screener has no capex history: Box 10 cannot forecast FCFF, so no DCF
    assert result(v, VM.DCF_FCFF_GORDON).status == ValuationStatus.UNAVAILABLE
    pe = result(v, VM.PE)
    pat = F(f.value(L.PAT_ATTRIBUTABLE, S.BASE, 2027).value)
    assert close(pe.equity_value, pat * 22)
    # no explicit share count: equity value, never a per-share value
    assert pe.status == ValuationStatus.PARTIAL and pe.fair_value_per_share is None
    assert v.valuation_date.period_end == f.base.latest_actual.period_end


# --- financial-error audit (section 29) -------------------------------------------------------


def test_pe_uses_attributable_pat_not_total_pat():
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10)
    req = request(
        methods=(VM.PE,),
        scenarios=(S.BASE,),
        multiples=[multiple(VM.PE, 15)],
        extra=[a(K.MINORITY_INTEREST, 150)],
    )
    v = build_valuation(f, request=req, box4=box4, generated_at=NOW)
    total = F(f.value(L.PAT, S.BASE, 2027).value)
    owners = F(f.value(L.PAT_ATTRIBUTABLE, S.BASE, 2027).value)
    assert owners == total * F("0.9")
    assert close(result(v, VM.PE).equity_value, owners * 15)
    assert not close(result(v, VM.PE).equity_value, total * 15)
    # P/E prices owners' equity directly: the non-controlling interest is not subtracted again
    assert all(i.role != "MINORITY_INTEREST" for i in named(v, "EQUITY_VALUE", method=VM.PE).inputs)


def test_cash_is_read_on_the_forecast_basis_only():
    data = rows(basis=B.CONSOLIDATED, cash=None)
    data.append(V(M.CASH_AND_CASH_EQUIVALENTS, pit(2026), 500, basis=B.STANDALONE))
    f, box4 = forecast(data=make(data))
    assert f.base.reporting_basis == B.CONSOLIDATED
    v = value(f, box4, extra=[a(K.MINORITY_INTEREST, 0)])
    assert named(v, "CASH", None).reason == "CASH_UNAVAILABLE"  # never the standalone 500


def test_forecast_without_years_or_currency_fails_closed():
    # the only (hence analytical) basis really has no known currency: Box 10 forecasts on it
    no_currency = make([V(M.TOTAL_BORROWINGS, pit(2026), 100, basis=B.STANDALONE,
                          currency=Currency.UNKNOWN)])
    f = build_forecast(no_currency, generated_at=NOW)
    v = value(f, no_currency)
    assert v.status == ValuationStatus.UNAVAILABLE and "CURRENCY_UNKNOWN" in codes(v)
    assert all(r.reason == "CURRENCY_UNKNOWN" for r in v.results) and not v.values
    # a Box 10 dataset in a known currency but without forecast years (contract-valid)
    f2, box4 = forecast()
    g = ForecastDataset.model_validate(
        {**f2.model_dump(), "forecast_years": [], "values": [], "assumptions": []}
    )
    assert g.base.currency == Currency.INR
    w = value(g, box4)
    assert w.status == ValuationStatus.UNAVAILABLE and "NO_FORECAST_YEARS" in codes(w)
    assert all(r.reason == "NO_FORECAST_YEARS" for r in w.results) and not w.values


def test_full_precision_is_kept_no_intermediate_rounding():
    f, box4 = forecast([100, 110, 121])
    v = value(f, box4, per_scenario={S.BASE: {"rf": "4.123456789", "beta": "1.0987654321"}})
    w = F("4.123456789") + F("1.0987654321") * 3
    want = gordon([100, 110, 121], w, 5)
    r = result(v, VM.DCF_FCFF_GORDON)
    assert close(r.enterprise_value, want["ev"])
    assert close(r.fair_value_per_share, per_share(want["ev"] - DEBT + CASH))


def test_consolidated_minority_balance_is_read_from_box4():
    """RC1 (TECHM): the consolidated balance sheet's NCI line (461.6 crore) is the bridge's
    minority interest - it was ignored and every DCF stayed without an equity value."""
    data = make(rows(basis=B.CONSOLIDATED, minority=10)
                + [V(M.MINORITY_INTEREST, pit(2026), "461.6", basis=B.CONSOLIDATED)],
                Industry.GENERAL)
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10, data=data)
    v = value(f, box4)
    mi = named(v, "MINORITY_INTEREST", None)
    assert (mi.value, mi.formula_id) == (Decimal("461.6"), "MINORITY_INTEREST/1/BOX4")
    ev = result(v, VM.DCF_FCFF_GORDON).enterprise_value
    assert ev is not None and close(result(v, VM.DCF_FCFF_GORDON).equity_value,
                                    F(ev) - DEBT + CASH - F("461.6"))
