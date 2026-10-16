"""DETAILED_RESEARCH_V1 Box 11: ForecastDataset (+ Box 4) + explicit inputs -> ValuationDataset.

Pipeline:
  integrity (the forecast re-validates; every Box 10 number is recomputed from its anchors and
  assumptions by Box 10's own formulas; Box 4, when given, is the forecast's own Box 4 by hash
  and rebuilds the anchors) ->
  request checks (scenarios forecast, basis, currency, horizon, multiple years, WACC weights) ->
  scenario-free facts at the valuation date (gross debt, cash, non-controlling interest, other
  adjustment, share count; each from Box 10 / Box 4 or an explicit cited input, never zero-filled)
  -> per scenario, by a pure function of that scenario's own forecast values and assumptions:
  operating EBITDA / EBIT, NOPAT, FCFF, WACC, discount factors, present values, terminal value,
  enterprise value, the enterprise-to-equity bridge, equity value, value per share; and the
  trading-multiple methods -> results per scenario and method, never averaged.

No AI, no network, no clock: `generated_at` is injected and kept out of every ID; IDs are uuid5
over content. Box 11 values; it never recommends, rates or sizes a position.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

from stock_research_ai.application.financial_metrics import Fact
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.forecast_integrity import (
    ForecastIntegrityError,
    verify_forecast_integrity,
)
from stock_research_ai.application.forecasting import (
    EPS_METRICS,
    History,
    canonical_forecast_json,
)
from stock_research_ai.domain.financial_analytics import MetricInput
from stock_research_ai.domain.financial_business import RESTRICTED_INDUSTRIES
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Confidence,
    Currency,
    ReportingBasis,
    Scale,
)
from stock_research_ai.domain.forecasting import (
    Driver,
    ForecastAssumption,
    ForecastDataset,
    ForecastLine,
    OperatingRoute,
    Scenario,
)
from stock_research_ai.domain.fundamental_valuation import (
    CRORE,
    DCF_METHODS,
    MAX_ABS_VALUE,
    MAX_ISSUES,
    MULTIPLE_BASIS,
    MULTIPLE_METHODS,
    VALUATION_ENGINE_VERSION,
    VALUATION_FORMULA_VERSION,
    VALUATION_POLICY_VERSION,
    AssumptionInput,
    AssumptionKind,
    AssumptionUnit,
    DcfPeriod,
    ForecastSource,
    FormulaInput,
    InputKind,
    MethodResult,
    MultipleBasis,
    MultipleDetail,
    MultipleInput,
    Severity,
    ValuationAssumption,
    ValuationDataset,
    ValuationFormula,
    ValuationIssue,
    ValuationManifest,
    ValuationMethod,
    ValuationRequest,
    ValuationStatus,
    ValuationValue,
    ValueUnit,
)

NS = UUID("b3e1f6a2-7c59-4d0e-9a8b-2f4c6d1e8a70")
K = AssumptionKind
L = ForecastLine
VM = ValuationMethod
U = ValueUnit
D0 = Decimal(0)
HUNDRED = Decimal(100)
PRECISION = 34
#: an explicit share count this many times larger or smaller than the count implied by the
#: latest reported EPS is a unit error (lakhs / millions / crore vs ones): never used
SHARE_UNIT_MISMATCH_FACTOR = Decimal(100)
#: within the unit-error factor but outside this one: kept, flagged (buyback, bonus, split)
SHARE_COUNT_TOLERANCE = Decimal(2)
OWNERS_PAT = (CanonicalMetric.PAT_ATTRIBUTABLE_TO_OWNERS,)


class ValuationInputError(ValueError):
    """The request cannot be applied to this forecast (fails closed, never guessed)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# --- formulas ---------------------------------------------------------------------------------

Fn = Callable[[dict[str, Decimal], tuple[Decimal, ...]], Decimal]
FORMULAS: dict[str, tuple[ValuationFormula, Fn]] = {}


def _formula(
    formula_id: str,
    expression: str,
    inputs: tuple[str, ...],
    unit: ValueUnit,
    fn: Fn,
    *,
    variadic: str | None = None,
    notes: str = "",
) -> None:
    assert formula_id not in FORMULAS, formula_id
    FORMULAS[formula_id] = (
        ValuationFormula(
            formula_id=formula_id,
            expression=expression,
            inputs=inputs,
            variadic=variadic,
            unit=unit,
            notes=notes,
        ),
        fn,
    )


def _operating_tax(v: dict[str, Decimal], _: tuple[Decimal, ...]) -> Decimal:
    return v["OPERATING_EBIT"] * v["TAX_RATE"] / HUNDRED if v["OPERATING_EBIT"] > 0 else D0


def _equity(v: dict[str, Decimal], _: tuple[Decimal, ...]) -> Decimal:
    out = v["ENTERPRISE_VALUE"] - v["GROSS_DEBT"] + v["CASH"] - v["MINORITY_INTEREST"]
    return out + v.get("OTHER_EQUITY_ADJUSTMENT", D0)


_formula(
    "OPERATING_EBITDA/1/OPERATING_PROFIT",
    "Box 10 operating profit (EBITDA before other income: treasury income is not an "
    "operating cash flow; cash is added in the bridge instead)",
    ("OPERATING_PROFIT",),
    U.MONEY,
    lambda v, _: v["OPERATING_PROFIT"],
)
_formula(
    "OPERATING_EBITDA/1/REPORTED_EBITDA",
    "Box 10 EBITDA on the reported-EBITDA route (definition as reported)",
    ("EBITDA",),
    U.MONEY,
    lambda v, _: v["EBITDA"],
)
_formula(
    "OPERATING_EBIT/1",
    "operating EBITDA - D&A",
    ("OPERATING_EBITDA", "DEPRECIATION_AMORTIZATION"),
    U.MONEY,
    lambda v, _: v["OPERATING_EBITDA"] - v["DEPRECIATION_AMORTIZATION"],
)
_formula(
    "OPERATING_TAX/1",
    "operating EBIT x Box 10 tax rate / 100 when operating EBIT > 0, else 0 (no credit on a "
    "loss, as Box 10)",
    ("OPERATING_EBIT", "TAX_RATE"),
    U.MONEY,
    _operating_tax,
)
_formula(
    "NOPAT/1",
    "operating EBIT - operating tax",
    ("OPERATING_EBIT", "OPERATING_TAX"),
    U.MONEY,
    lambda v, _: v["OPERATING_EBIT"] - v["OPERATING_TAX"],
)
_formula(
    "FCFF/1",
    "NOPAT + D&A - working capital change - capex (Box 10 lines of the same scenario and year)",
    ("NOPAT", "DEPRECIATION_AMORTIZATION", "WORKING_CAPITAL_CHANGE", "CAPEX"),
    U.MONEY,
    lambda v, _: (
        v["NOPAT"] + v["DEPRECIATION_AMORTIZATION"] - v["WORKING_CAPITAL_CHANGE"] - v["CAPEX"]
    ),
)
_formula(
    "COST_OF_EQUITY/1/CAPM",
    "risk-free rate + beta x equity risk premium (all in %)",
    ("RISK_FREE_RATE", "BETA", "EQUITY_RISK_PREMIUM"),
    U.PERCENT,
    lambda v, _: v["RISK_FREE_RATE"] + v["BETA"] * v["EQUITY_RISK_PREMIUM"],
)
_formula(
    "COST_OF_EQUITY/1/EXPLICIT",
    "the explicit cost of equity",
    ("COST_OF_EQUITY",),
    U.PERCENT,
    lambda v, _: v["COST_OF_EQUITY"],
)
_formula(
    "AFTER_TAX_COST_OF_DEBT/1",
    "pre-tax cost of debt x (1 - WACC tax rate / 100)",
    ("PRE_TAX_COST_OF_DEBT", "WACC_TAX_RATE"),
    U.PERCENT,
    lambda v, _: v["PRE_TAX_COST_OF_DEBT"] * (1 - v["WACC_TAX_RATE"] / HUNDRED),
)
_formula(
    "WACC/1",
    "equity weight / 100 x cost of equity + debt weight / 100 x after-tax cost of debt "
    "(weights sum to exactly 100)",
    ("EQUITY_WEIGHT", "COST_OF_EQUITY", "DEBT_WEIGHT", "AFTER_TAX_COST_OF_DEBT"),
    U.PERCENT,
    lambda v, _: (
        (v["EQUITY_WEIGHT"] * v["COST_OF_EQUITY"] + v["DEBT_WEIGHT"] * v["AFTER_TAX_COST_OF_DEBT"])
        / HUNDRED
    ),
)
_formula(
    "DISCOUNT_FACTOR/1",
    "1 / (1 + WACC / 100) ^ t, t = years from the valuation date to the fiscal year end "
    "(end-of-period convention)",
    ("WACC", "PERIOD"),
    U.FACTOR,
    lambda v, _: 1 / (1 + v["WACC"] / HUNDRED) ** int(v["PERIOD"]),
)
_formula(
    "PRESENT_VALUE/1",
    "cash flow x discount factor",
    ("CASH_FLOW", "DISCOUNT_FACTOR"),
    U.MONEY,
    lambda v, _: v["CASH_FLOW"] * v["DISCOUNT_FACTOR"],
)
_formula(
    "SUM_PRESENT_VALUE/1",
    "sum of the forecast-year present values",
    (),
    U.MONEY,
    lambda _, xs: sum(xs, D0),
    variadic="PRESENT_VALUE",
)
_formula(
    "TERMINAL_CASH_FLOW/1",
    "FCFF of the final forecast year N x (1 + terminal growth / 100) = FCFF(N+1)",
    ("FINAL_CASH_FLOW", "TERMINAL_GROWTH"),
    U.MONEY,
    lambda v, _: v["FINAL_CASH_FLOW"] * (1 + v["TERMINAL_GROWTH"] / HUNDRED),
)
_formula(
    "TERMINAL_VALUE/1/GORDON",
    "FCFF(N+1) / ((WACC - terminal growth) / 100), at the end of year N; WACC > g required",
    ("TERMINAL_CASH_FLOW", "WACC", "TERMINAL_GROWTH"),
    U.MONEY,
    lambda v, _: v["TERMINAL_CASH_FLOW"] / ((v["WACC"] - v["TERMINAL_GROWTH"]) / HUNDRED),
)
_formula(
    "TERMINAL_VALUE/1/EXIT_EV_EBITDA",
    "operating EBITDA of year N x exit EV/EBITDA, at the end of year N",
    ("FINAL_OPERATING_EBITDA", "TERMINAL_EV_EBITDA"),
    U.MONEY,
    lambda v, _: v["FINAL_OPERATING_EBITDA"] * v["TERMINAL_EV_EBITDA"],
)
_formula(
    "PRESENT_VALUE_TERMINAL/1",
    "terminal value x discount factor of year N (discounted N periods, not N+1)",
    ("TERMINAL_VALUE", "DISCOUNT_FACTOR"),
    U.MONEY,
    lambda v, _: v["TERMINAL_VALUE"] * v["DISCOUNT_FACTOR"],
)
_formula(
    "ENTERPRISE_VALUE/1/DCF",
    "sum of forecast present values + present value of the terminal value",
    ("SUM_PRESENT_VALUE", "PRESENT_VALUE_TERMINAL"),
    U.MONEY,
    lambda v, _: v["SUM_PRESENT_VALUE"] + v["PRESENT_VALUE_TERMINAL"],
)
_formula(
    "ENTERPRISE_VALUE/1/MULTIPLE",
    "metric x enterprise multiple (EV/EBITDA on operating EBITDA, EV/EBIT on operating EBIT)",
    ("METRIC", "MULTIPLE"),
    U.MONEY,
    lambda v, _: v["METRIC"] * v["MULTIPLE"],
)
_formula(
    "EQUITY_VALUE/1/BRIDGE",
    "enterprise value - gross debt + cash - non-controlling interest",
    ("ENTERPRISE_VALUE", "GROSS_DEBT", "CASH", "MINORITY_INTEREST"),
    U.MONEY,
    _equity,
)
_formula(
    "EQUITY_VALUE/1/BRIDGE_WITH_ADJUSTMENT",
    "enterprise value - gross debt + cash - non-controlling interest + other equity adjustment",
    ("ENTERPRISE_VALUE", "GROSS_DEBT", "CASH", "MINORITY_INTEREST", "OTHER_EQUITY_ADJUSTMENT"),
    U.MONEY,
    _equity,
)
_formula(
    "EQUITY_VALUE/1/PE",
    "attributable PAT x P/E (an equity multiple: no bridge)",
    ("METRIC", "MULTIPLE"),
    U.MONEY,
    lambda v, _: v["METRIC"] * v["MULTIPLE"],
)
_formula(
    "FAIR_VALUE_PER_SHARE/1",
    "equity value (crore) x 10^7 / shares outstanding (ones)",
    ("EQUITY_VALUE", "SHARE_COUNT"),
    U.PER_SHARE,
    lambda v, _: v["EQUITY_VALUE"] * CRORE / v["SHARE_COUNT"],
)
_formula(
    "FAIR_VALUE_VS_PRICE/1",
    "(fair value per share - stated market price) / stated market price x 100",
    ("FAIR_VALUE_PER_SHARE", "MARKET_PRICE"),
    U.PERCENT,
    lambda v, _: (v["FAIR_VALUE_PER_SHARE"] - v["MARKET_PRICE"]) / v["MARKET_PRICE"] * HUNDRED,
)
for _item, _what in (
    ("GROSS_DEBT", "gross debt"),
    ("CASH", "cash and cash equivalents"),
    ("MINORITY_INTEREST", "non-controlling interest"),
    ("OTHER_EQUITY_ADJUSTMENT", "other equity adjustment"),
    ("SHARE_COUNT", "shares outstanding"),
):
    _formula(
        f"{_item}/1/EXPLICIT",
        f"{_what} as explicitly stated (cited)",
        ("STATED",),
        U.COUNT if _item == "SHARE_COUNT" else U.MONEY,
        lambda v, _: v["STATED"],
    )
_formula(
    "GROSS_DEBT/1/BOX10_ACTUAL",
    "the Box 10 gross-debt anchor at the valuation date (total debt / borrowings as reported, "
    "else long-term + short-term borrowings)",
    ("ACTUAL",),
    U.MONEY,
    lambda v, _: v["ACTUAL"],
)
_formula(
    "CASH/1/BOX4",
    "Box 4 cash and cash equivalents at the valuation date (other bank balances and "
    "investments are not added)",
    ("ACTUAL",),
    U.MONEY,
    lambda v, _: v["ACTUAL"],
)
_formula(
    "MINORITY_INTEREST/1/STANDALONE",
    "a standalone entity has no non-controlling interest (0 by definition, not by default)",
    (),
    U.MONEY,
    lambda v, _: D0,
)

POLICIES: tuple[tuple[str, str], ...] = (
    ("FORECAST_SOURCE", "every forecast number is a Box 10 value of the same scenario"),
    ("FCFF", "NOPAT on operating EBIT (other income excluded) + D&A - WC change - capex"),
    ("TAX_ON_OPERATING_EBIT", "Box 10 tax-rate assumption of the scenario and year; 0 on a loss"),
    ("WACC", "explicit inputs only: CAPM or explicit cost of equity; weights sum to 100"),
    ("DISCOUNTING", "end-of-period; t = 1 for the first forecast year after the valuation date"),
    ("TERMINAL_GORDON", "FCFF(N) x (1 + g) / (WACC - g), discounted N periods; WACC > g"),
    ("TERMINAL_EXIT", "operating EBITDA(N) x exit multiple; a separate method, never blended"),
    ("VALUATION_DATE", "the latest actual balance-sheet date of the Box 10 base"),
    ("BRIDGE", "equity = EV - gross debt + cash - non-controlling interest (+ cited adjustment)"),
    ("BRIDGE_MISSING", "a missing bridge item leaves equity value unavailable, never zero"),
    ("MINORITY", "standalone: none by definition; consolidated: explicit cited value required"),
    ("SHARES", "explicit shares outstanding in ones; >= 100x off the EPS-implied count rejected"),
    ("MULTIPLES", "explicit or peer-cited only; P/E on attributable PAT, EV multiples bridged"),
    ("NON_POSITIVE_METRIC", "a multiple is never applied to a zero or negative metric"),
    ("SCENARIOS", "each scenario valued from its own forecast and inputs; never derived"),
    ("AGGREGATION", "methods are reported separately; nothing is averaged or weighted"),
    ("MARKET_PRICE", "a stated, cited price only, for comparison; never fetched"),
    ("SECTOR", "no FCFF DCF and no EV multiples for banks, NBFCs and insurers"),
)


def evaluate(formula_id: str, values: dict[str, Decimal], extra: Sequence[Decimal] = ()) -> Decimal:
    """Pure: the registered formula over its named inputs at fixed working precision."""
    definition, fn = FORMULAS[formula_id]
    if set(values) != set(definition.inputs):
        raise ValueError(f"{formula_id} needs exactly {definition.inputs}")
    if extra and definition.variadic is None:
        raise ValueError(f"{formula_id} takes no repeated inputs")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return +fn(values, tuple(extra))


# --- sources ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Src:
    """One formula input: a value with its reference, or an unavailable one with its reason."""

    kind: InputKind
    ref_id: UUID | None
    value: Decimal | None
    reason: str | None = None
    lineage: tuple[MetricInput, ...] = ()


class Issues:
    def __init__(self) -> None:
        self.items: list[ValuationIssue] = []
        self.truncated = False

    def add(
        self,
        code: str,
        severity: Severity,
        detail: str | None = None,
        *,
        scenario: Scenario | None = None,
        method: ValuationMethod | None = None,
        fiscal_year: int | None = None,
        subject: str | None = None,
    ) -> None:
        if len(self.items) >= MAX_ISSUES - 1:
            self.truncated = True
            return
        self.items.append(
            ValuationIssue(
                code=code,
                severity=severity,
                scenario=scenario,
                method=method,
                fiscal_year=fiscal_year,
                subject=subject,
                detail=detail[:1000] if detail else None,
            )
        )


@dataclass
class Ledger:
    """The values of one scope (one scenario, or the scenario-free facts)."""

    tag: str  # forecast hash + scope: every ID is content-addressed inside its scope
    scenario: Scenario | None
    currency: Currency
    issues: Issues
    values: list[ValuationValue] = field(default_factory=list)

    def _id(self, *parts: object) -> UUID:
        return uuid5(NS, "|".join([self.tag, *(str(p) for p in parts)]))

    def unavailable(
        self,
        name: str,
        unit: ValueUnit,
        reason: str,
        missing: Sequence[str] = (),
        *,
        method: ValuationMethod | None = None,
        fiscal_year: int | None = None,
    ) -> ValuationValue:
        value = ValuationValue(
            value_id=self._id("U", name, method, fiscal_year, reason, *missing),
            name=name,
            scenario=self.scenario,
            method=method,
            fiscal_year=fiscal_year,
            status="UNAVAILABLE",
            value=None,
            unit=unit,
            currency=self.currency if unit in (U.MONEY, U.PER_SHARE) else None,
            reason=reason,
            missing_inputs=tuple(m[:1000] for m in missing),
        )
        self.values.append(value)
        return value

    def compute(
        self,
        name: str,
        formula_id: str,
        refs: Sequence[tuple[str, Src | None]],
        *,
        method: ValuationMethod | None = None,
        fiscal_year: int | None = None,
        warnings: tuple[str, ...] = (),
    ) -> ValuationValue:
        definition, _ = FORMULAS[formula_id]
        missing: list[str] = []
        upstream = False
        inputs: list[FormulaInput] = []
        lineage: list[MetricInput] = []
        for role, src in refs:
            if src is None:
                missing.append(f"{role}: no input")
            elif src.value is None:
                upstream = True
                missing.append(f"{role}: {src.reason}")
            else:
                inputs.append(
                    FormulaInput(role=role, kind=src.kind, ref_id=src.ref_id, value=src.value)
                )
                lineage.extend(src.lineage)
        if missing:
            reason = "UPSTREAM_UNAVAILABLE" if upstream else "INPUT_MISSING"
            return self.unavailable(
                name, definition.unit, reason, missing, method=method, fiscal_year=fiscal_year
            )
        named = {i.role: i.value for i in inputs if i.role != definition.variadic}
        repeated = [i.value for i in inputs if i.role == definition.variadic]
        value = evaluate(formula_id, named, repeated)
        if not value.is_finite() or abs(value) > MAX_ABS_VALUE:
            self.issues.add(
                "VALUE_OUT_OF_RANGE",
                Severity.ERROR,
                f"{name} exceeds {MAX_ABS_VALUE} in absolute value",
                scenario=self.scenario,
                method=method,
                fiscal_year=fiscal_year,
                subject=name,
            )
            return self.unavailable(
                name, definition.unit, "OUT_OF_RANGE", (), method=method, fiscal_year=fiscal_year
            )
        body = "|".join(f"{i.role}={i.kind.value}:{i.ref_id}:{i.value}" for i in inputs)
        out = ValuationValue(
            value_id=self._id("V", name, method, fiscal_year, formula_id, body, value),
            name=name,
            scenario=self.scenario,
            method=method,
            fiscal_year=fiscal_year,
            status="AVAILABLE",
            value=value,
            unit=definition.unit,
            currency=self.currency if definition.unit in (U.MONEY, U.PER_SHARE) else None,
            formula_id=formula_id,
            inputs=tuple(inputs),
            box4_inputs=tuple(lineage),
            warnings=warnings,
        )
        self.values.append(out)
        return out


def ref(v: ValuationValue) -> Src:
    return Src(InputKind.VALUATION, v.value_id, v.value, v.reason)


def stated(a: ValuationAssumption) -> Src:
    return Src(InputKind.ASSUMPTION, a.assumption_id, a.value)


# --- scenario-free facts ----------------------------------------------------------------------


@dataclass(frozen=True)
class Shared:
    """Facts at the valuation date, identical for every scenario (never scenario inputs)."""

    gross_debt: ValuationValue
    cash: ValuationValue
    minority: ValuationValue
    adjustment: ValuationValue | None
    shares: ValuationValue
    price: ValuationAssumption | None


def _shared(
    forecast: ForecastDataset,
    box4: CanonicalFinancialDataset | None,
    explicit: dict[tuple[AssumptionKind, Scenario | None], ValuationAssumption],
    ledger: Ledger,
) -> Shared:
    base = forecast.base
    end = base.latest_actual.period_end if base.latest_actual else None
    history = History(box4, base.reporting_basis, base.currency) if box4 and end else None

    def given(kind: AssumptionKind) -> ValuationAssumption | None:
        return explicit.get((kind, None))

    # gross debt: explicit, else the Box 10 anchor (the same debt Box 10 forecast from)
    debt_input = given(K.GROSS_DEBT)
    anchor = next((v for v in forecast.actuals if v.line == L.GROSS_DEBT), None)
    if debt_input is not None:
        debt = ledger.compute(
            "GROSS_DEBT", "GROSS_DEBT/1/EXPLICIT", [("STATED", stated(debt_input))]
        )
    elif anchor is None or anchor.value is None:
        why = anchor.reason if anchor else "no Box 10 gross-debt anchor"
        debt = ledger.unavailable(
            "GROSS_DEBT", U.MONEY, "DEBT_UNAVAILABLE", (f"Box 10 gross debt: {why}",)
        )
    elif not _unit_ok(anchor.currency, anchor.scale, base.currency) or anchor.value < 0:
        debt = ledger.unavailable(
            "GROSS_DEBT", U.MONEY, "DEBT_UNUSABLE", (f"Box 10 gross debt {anchor.value}",)
        )
    else:
        debt = ledger.compute(
            "GROSS_DEBT",
            "GROSS_DEBT/1/BOX10_ACTUAL",
            [("ACTUAL", Src(InputKind.BOX10_ACTUAL, anchor.value_id, anchor.value))],
            warnings=anchor.warnings,
        )
    if debt_input is None and history is not None and end is not None:
        lease = history.balance([CanonicalMetric.LEASE_LIABILITIES], end)
        if isinstance(lease, Fact) and lease.value != 0:
            ledger.issues.add(
                "LEASE_LIABILITIES_REPORTED",
                Severity.INFO,
                f"lease liabilities {lease.value} are reported separately; FCFF is before lease "
                "payments: state GROSS_DEBT explicitly if borrowings exclude them",
                subject="GROSS_DEBT",
            )
    # cash: explicit, else Box 4 cash and cash equivalents (never assumed zero)
    cash_input = given(K.CASH)
    if cash_input is not None:
        cash = ledger.compute("CASH", "CASH/1/EXPLICIT", [("STATED", stated(cash_input))])
    elif history is None or end is None:
        cash = ledger.unavailable(
            "CASH",
            U.MONEY,
            "CASH_UNAVAILABLE",
            ("Box 4 not supplied or no valuation date: state CASH explicitly",),
        )
    else:
        got = history.balance([CanonicalMetric.CASH_AND_CASH_EQUIVALENTS], end)
        if not isinstance(got, Fact):
            cash = ledger.unavailable("CASH", U.MONEY, "CASH_UNAVAILABLE", (got.what,))
        elif got.value < 0:
            cash = ledger.unavailable("CASH", U.MONEY, "CASH_UNUSABLE", (f"cash {got.value}",))
        else:
            item = got.inputs[0]
            warn = got.warnings + (
                ("LOW_CONFIDENCE_INPUT",) if got.confidence == Confidence.LOW else ()
            )
            cash = ledger.compute(
                "CASH",
                "CASH/1/BOX4",
                [
                    (
                        "ACTUAL",
                        Src(
                            InputKind.BOX4_VALUE, item.canonical_value_id, item.value, None, (item,)
                        ),
                    )
                ],
                warnings=tuple(dict.fromkeys(warn)),
            )
    # non-controlling interest: Box 4 carries no balance-sheet NCI line
    mi_input = given(K.MINORITY_INTEREST)
    if mi_input is not None:
        minority = ledger.compute(
            "MINORITY_INTEREST", "MINORITY_INTEREST/1/EXPLICIT", [("STATED", stated(mi_input))]
        )
    elif base.reporting_basis == ReportingBasis.STANDALONE:
        minority = ledger.compute("MINORITY_INTEREST", "MINORITY_INTEREST/1/STANDALONE", [])
    else:
        minority = ledger.unavailable(
            "MINORITY_INTEREST",
            U.MONEY,
            "MINORITY_INTEREST_UNAVAILABLE",
            (
                "consolidated basis: Box 4 has no non-controlling-interest balance; state "
                "MINORITY_INTEREST explicitly",
            ),
        )
    adj_input = given(K.OTHER_EQUITY_ADJUSTMENT)
    adjustment = (
        ledger.compute(
            "OTHER_EQUITY_ADJUSTMENT",
            "OTHER_EQUITY_ADJUSTMENT/1/EXPLICIT",
            [("STATED", stated(adj_input))],
        )
        if adj_input is not None
        else None
    )
    shares = _shares(given(K.SHARE_COUNT), history, end, base.reporting_basis, ledger)
    return Shared(debt, cash, minority, adjustment, shares, given(K.MARKET_PRICE))


def _unit_ok(currency: Currency, scale: Scale, expected: Currency) -> bool:
    return currency == expected and scale == Scale.CRORES


def _shares(
    given: ValuationAssumption | None,
    history: History | None,
    end: date | None,
    basis: ReportingBasis,
    ledger: Ledger,
) -> ValuationValue:
    if given is None:
        return ledger.unavailable(
            "SHARE_COUNT",
            U.COUNT,
            "SHARE_COUNT_UNAVAILABLE",
            ("no reliable share count: state SHARE_COUNT (shares outstanding, ones)",),
        )
    implied: Decimal | None = None
    if history is not None and end is not None:
        eps = history.flow(EPS_METRICS, end)
        pat = history.flow(
            OWNERS_PAT if basis == ReportingBasis.CONSOLIDATED else [CanonicalMetric.PAT], end
        )
        if isinstance(eps, Fact) and isinstance(pat, Fact) and eps.value > 0 and pat.value > 0:
            with localcontext() as ctx:
                ctx.prec = PRECISION
                implied = pat.value * CRORE / eps.value
    if implied is None:
        ledger.issues.add(
            "SHARE_COUNT_UNVERIFIED",
            Severity.INFO,
            "no positive reported EPS and attributable PAT to check the share count's unit",
            subject="SHARE_COUNT",
        )
        return ledger.compute("SHARE_COUNT", "SHARE_COUNT/1/EXPLICIT", [("STATED", stated(given))])
    with localcontext() as ctx:
        ctx.prec = PRECISION
        factor = given.value / implied
    detail = (
        f"stated {given.value} vs ~{implied.quantize(Decimal(1))} implied by the latest reported "
        "EPS (shares are stated in ones)"
    )
    if factor >= SHARE_UNIT_MISMATCH_FACTOR or factor <= 1 / SHARE_UNIT_MISMATCH_FACTOR:
        ledger.issues.add(
            "SHARE_COUNT_UNIT_MISMATCH", Severity.ERROR, detail, subject="SHARE_COUNT"
        )
        return ledger.unavailable("SHARE_COUNT", U.COUNT, "SHARE_COUNT_UNIT_MISMATCH", (detail,))
    warnings: tuple[str, ...] = ()
    if factor > SHARE_COUNT_TOLERANCE or factor < 1 / SHARE_COUNT_TOLERANCE:
        ledger.issues.add(
            "SHARE_COUNT_INCONSISTENT_WITH_HISTORY", Severity.WARNING, detail, subject="SHARE_COUNT"
        )
        warnings = ("SHARE_COUNT_INCONSISTENT_WITH_HISTORY",)
    return ledger.compute(
        "SHARE_COUNT", "SHARE_COUNT/1/EXPLICIT", [("STATED", stated(given))], warnings=warnings
    )


# --- one scenario -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Everything a scenario valuation needs: immutable, shared, never written."""

    forecast: ForecastDataset
    forecast_hash: str
    currency: Currency
    methods: tuple[ValuationMethod, ...]
    years: tuple[int, ...]  # DCF years, oldest first
    all_years: tuple[int, ...]  # every forecast year (multiples may price any)
    financial: bool
    shared: Shared


def value_scenario(
    plan: Plan,
    scenario: Scenario,
    explicit: dict[AssumptionKind, ValuationAssumption],
    multiples: Sequence[tuple[UUID, MultipleInput]],
    issues: Issues,
) -> tuple[list[ValuationValue], list[MethodResult]]:
    """Pure: one scenario from the shared plan and that scenario's own inputs only."""
    f = plan.forecast
    ledger = Ledger(f"{plan.forecast_hash}|{scenario.value}", scenario, plan.currency, issues)
    lines = {(v.line, v.period.fiscal_year): v for v in f.values if v.scenario == scenario}
    taxes = {
        a.fiscal_year: a
        for a in f.assumptions
        if a.scenario == scenario and a.driver == Driver.TAX_RATE
    }

    def box10(line: ForecastLine, fy: int) -> Src:
        v = lines.get((line, fy))
        if v is None:
            return Src(InputKind.BOX10_FORECAST, None, None, f"Box 10 has no {line.value} FY{fy}")
        if v.status != "AVAILABLE" or v.value is None:
            return Src(InputKind.BOX10_FORECAST, v.value_id, None, f"Box 10 {v.reason}")
        if v.unit != "MONEY" or not _unit_ok(v.currency, v.scale, plan.currency):
            return Src(InputKind.BOX10_FORECAST, v.value_id, None, "UNIT_MISMATCH")
        return Src(InputKind.BOX10_FORECAST, v.value_id, v.value)

    def tax(fy: int) -> Src:
        a: ForecastAssumption | None = taxes.get(fy)
        if a is None:
            return Src(InputKind.BOX10_ASSUMPTION, None, None, f"Box 10 has no TAX_RATE FY{fy}")
        return Src(InputKind.BOX10_ASSUMPTION, a.assumption_id, a.value)

    route = f.base.operating_route
    route_warn = (
        ("EBITDA_DEFINITION_AS_REPORTED",) if route == OperatingRoute.REPORTED_EBITDA else ()
    )
    wants_dcf = any(m in plan.methods for m in DCF_METHODS)
    wants_ev = any(m in plan.methods for m in (VM.EV_EBITDA, VM.EV_EBIT))
    operating: dict[int, tuple[ValuationValue, ValuationValue]] = {}

    def operating_lines(fy: int) -> tuple[ValuationValue, ValuationValue]:
        if fy not in operating:
            if route == OperatingRoute.OPERATING_PROFIT:
                ebitda = ledger.compute(
                    "OPERATING_EBITDA",
                    "OPERATING_EBITDA/1/OPERATING_PROFIT",
                    [("OPERATING_PROFIT", box10(L.OPERATING_PROFIT, fy))],
                    fiscal_year=fy,
                )
            elif route == OperatingRoute.REPORTED_EBITDA:
                ebitda = ledger.compute(
                    "OPERATING_EBITDA",
                    "OPERATING_EBITDA/1/REPORTED_EBITDA",
                    [("EBITDA", box10(L.EBITDA, fy))],
                    fiscal_year=fy,
                    warnings=route_warn,
                )
            else:
                ebitda = ledger.unavailable(
                    "OPERATING_EBITDA", U.MONEY, "OPERATING_ROUTE_UNAVAILABLE", fiscal_year=fy
                )
            ebit = ledger.compute(
                "OPERATING_EBIT",
                "OPERATING_EBIT/1",
                [
                    ("OPERATING_EBITDA", ref(ebitda)),
                    ("DEPRECIATION_AMORTIZATION", box10(L.DEPRECIATION_AMORTIZATION, fy)),
                ],
                fiscal_year=fy,
                warnings=route_warn,
            )
            operating[fy] = (ebitda, ebit)
        return operating[fy]

    results: list[MethodResult] = []
    if plan.financial and (wants_dcf or wants_ev):
        issues.add(
            "UNSUPPORTED_SECTOR",
            Severity.WARNING,
            f"{f.industry.value}: FCFF DCF and enterprise multiples do not apply to lenders",
            scenario=scenario,
        )
        results.extend(
            _refused(plan, scenario, m, "UNSUPPORTED_SECTOR", ())
            for m in DCF_METHODS
            if m in plan.methods
        )
    elif wants_dcf:
        results.extend(_dcf(plan, scenario, explicit, ledger, box10, tax, operating_lines))
    for method in MULTIPLE_METHODS:
        if method not in plan.methods:
            continue
        mine = [(i, m) for i, m in multiples if m.method == method]
        if plan.financial and method != VM.PE:
            results.append(_refused(plan, scenario, method, "UNSUPPORTED_SECTOR", ()))
            continue
        if not mine:
            results.append(
                _refused(
                    plan,
                    scenario,
                    method,
                    "MULTIPLE_MISSING",
                    (f"no {method.value} multiple for {scenario.value}",),
                )
            )
            continue
        for multiple_id, m in sorted(mine, key=lambda x: x[1].fiscal_year):
            results.append(
                _multiple(plan, scenario, multiple_id, m, ledger, box10, operating_lines)
            )
    for r in results:
        if r.status != ValuationStatus.COMPLETE:
            issues.add(
                f"METHOD_{r.status.value}",
                Severity.WARNING,
                f"{r.reason}: " + "; ".join(r.missing_inputs)[:900],
                scenario=scenario,
                method=r.method,
                fiscal_year=r.multiple.fiscal_year if r.multiple else None,
            )
    return ledger.values, results


def _refused(
    plan: Plan,
    scenario: Scenario,
    method: ValuationMethod,
    reason: str,
    missing: tuple[str, ...],
    value_ids: tuple[UUID, ...] = (),
) -> MethodResult:
    return MethodResult(
        scenario=scenario,
        method=method,
        status=ValuationStatus.UNAVAILABLE,
        reason=reason,
        missing_inputs=missing,
        currency=plan.currency,
        value_ids=value_ids,
    )


def _wacc(
    scenario: Scenario,
    explicit: dict[AssumptionKind, ValuationAssumption],
    ledger: Ledger,
) -> ValuationValue:
    def a(kind: AssumptionKind) -> Src | None:
        got = explicit.get(kind)
        return stated(got) if got else None

    if K.COST_OF_EQUITY in explicit:
        ke = ledger.compute(
            "COST_OF_EQUITY", "COST_OF_EQUITY/1/EXPLICIT", [("COST_OF_EQUITY", a(K.COST_OF_EQUITY))]
        )
    else:
        ke = ledger.compute(
            "COST_OF_EQUITY",
            "COST_OF_EQUITY/1/CAPM",
            [(k.value, a(k)) for k in (K.RISK_FREE_RATE, K.BETA, K.EQUITY_RISK_PREMIUM)],
        )
    kd = ledger.compute(
        "AFTER_TAX_COST_OF_DEBT",
        "AFTER_TAX_COST_OF_DEBT/1",
        [(k.value, a(k)) for k in (K.PRE_TAX_COST_OF_DEBT, K.WACC_TAX_RATE)],
    )
    wacc = ledger.compute(
        "WACC",
        "WACC/1",
        [
            ("EQUITY_WEIGHT", a(K.EQUITY_WEIGHT)),
            ("COST_OF_EQUITY", ref(ke)),
            ("DEBT_WEIGHT", a(K.DEBT_WEIGHT)),
            ("AFTER_TAX_COST_OF_DEBT", ref(kd)),
        ],
    )
    if wacc.value is not None and wacc.value <= -HUNDRED:
        ledger.issues.add(
            "WACC_OUT_OF_RANGE", Severity.ERROR, f"WACC {wacc.value} % <= -100 %", scenario=scenario
        )
        return ledger.unavailable("WACC", U.PERCENT, "WACC_OUT_OF_RANGE")
    if wacc.value is not None and wacc.value <= 0:
        ledger.issues.add(
            "NON_POSITIVE_WACC", Severity.WARNING, f"WACC {wacc.value} %", scenario=scenario
        )
    return wacc


def _dcf(
    plan: Plan,
    scenario: Scenario,
    explicit: dict[AssumptionKind, ValuationAssumption],
    ledger: Ledger,
    box10: Callable[[ForecastLine, int], Src],
    tax: Callable[[int], Src],
    operating_lines: Callable[[int], tuple[ValuationValue, ValuationValue]],
) -> list[MethodResult]:
    fcff: list[ValuationValue] = []
    for fy in plan.years:
        _, ebit = operating_lines(fy)
        op_tax = ledger.compute(
            "OPERATING_TAX",
            "OPERATING_TAX/1",
            [("OPERATING_EBIT", ref(ebit)), ("TAX_RATE", tax(fy))],
            fiscal_year=fy,
        )
        nopat = ledger.compute(
            "NOPAT",
            "NOPAT/1",
            [("OPERATING_EBIT", ref(ebit)), ("OPERATING_TAX", ref(op_tax))],
            fiscal_year=fy,
        )
        fcff.append(
            ledger.compute(
                "FCFF",
                "FCFF/1",
                [
                    ("NOPAT", ref(nopat)),
                    ("DEPRECIATION_AMORTIZATION", box10(L.DEPRECIATION_AMORTIZATION, fy)),
                    ("WORKING_CAPITAL_CHANGE", box10(L.WORKING_CAPITAL_CHANGE, fy)),
                    ("CAPEX", box10(L.CAPEX, fy)),
                ],
                fiscal_year=fy,
            )
        )
    wacc = _wacc(scenario, explicit, ledger)
    factors: list[ValuationValue] = []
    pvs: list[ValuationValue] = []
    schedule: list[DcfPeriod] = []
    for t, (fy, cf) in enumerate(zip(plan.years, fcff, strict=True), start=1):
        df = ledger.compute(
            "DISCOUNT_FACTOR",
            "DISCOUNT_FACTOR/1",
            [("WACC", ref(wacc)), ("PERIOD", Src(InputKind.PERIOD_INDEX, None, Decimal(t)))],
            fiscal_year=fy,
        )
        pv = ledger.compute(
            "PRESENT_VALUE",
            "PRESENT_VALUE/1",
            [("CASH_FLOW", ref(cf)), ("DISCOUNT_FACTOR", ref(df))],
            fiscal_year=fy,
        )
        factors.append(df)
        pvs.append(pv)
        schedule.append(
            DcfPeriod(
                fiscal_year=fy,
                period_index=t,
                cash_flow=cf.value,
                discount_factor=df.value,
                present_value=pv.value,
                cash_flow_id=cf.value_id,
                discount_factor_id=df.value_id,
                present_value_id=pv.value_id,
            )
        )
    total = ledger.compute(
        "SUM_PRESENT_VALUE", "SUM_PRESENT_VALUE/1", [("PRESENT_VALUE", ref(pv)) for pv in pvs]
    )
    common = [*fcff, wacc, *factors, *pvs, total]
    out: list[MethodResult] = []
    final_year = plan.years[-1]
    for method in DCF_METHODS:
        if method not in plan.methods:
            continue
        built: list[ValuationValue] = []
        growth = explicit.get(K.TERMINAL_GROWTH) if method == VM.DCF_FCFF_GORDON else None
        if method == VM.DCF_FCFF_GORDON:
            g = stated(growth) if growth else None
            if growth and wacc.value is not None and wacc.value <= growth.value:
                ledger.issues.add(
                    "WACC_NOT_ABOVE_TERMINAL_GROWTH",
                    Severity.ERROR,
                    f"WACC {wacc.value} % <= terminal growth {growth.value} %: the Gordon "
                    "terminal value is undefined",
                    scenario=scenario,
                    method=method,
                )
                tv = ledger.unavailable(
                    "TERMINAL_VALUE",
                    U.MONEY,
                    "WACC_NOT_ABOVE_TERMINAL_GROWTH",
                    (f"WACC {wacc.value} <= g {growth.value}",),
                    method=method,
                )
                built.append(tv)
            else:
                tcf = ledger.compute(
                    "TERMINAL_CASH_FLOW",
                    "TERMINAL_CASH_FLOW/1",
                    [("FINAL_CASH_FLOW", ref(fcff[-1])), ("TERMINAL_GROWTH", g)],
                    method=method,
                    fiscal_year=final_year + 1,
                )
                tv = ledger.compute(
                    "TERMINAL_VALUE",
                    "TERMINAL_VALUE/1/GORDON",
                    [("TERMINAL_CASH_FLOW", ref(tcf)), ("WACC", ref(wacc)), ("TERMINAL_GROWTH", g)],
                    method=method,
                    fiscal_year=final_year,
                )
                built += [tcf, tv]
                if tcf.value is not None and tcf.value <= 0:
                    ledger.issues.add(
                        "TERMINAL_CASH_FLOW_NOT_POSITIVE",
                        Severity.WARNING,
                        f"FCFF(N+1) = {tcf.value}: a perpetuity of a non-positive cash flow",
                        scenario=scenario,
                        method=method,
                    )
        else:
            ebitda, _ = operating_lines(final_year)
            exit_multiple = explicit.get(K.TERMINAL_EV_EBITDA)
            if ebitda.value is not None and ebitda.value <= 0:
                tv = ledger.unavailable(
                    "TERMINAL_VALUE",
                    U.MONEY,
                    "NON_POSITIVE_METRIC",
                    (f"operating EBITDA FY{final_year} = {ebitda.value}",),
                    method=method,
                )
            else:
                tv = ledger.compute(
                    "TERMINAL_VALUE",
                    "TERMINAL_VALUE/1/EXIT_EV_EBITDA",
                    [
                        ("FINAL_OPERATING_EBITDA", ref(ebitda)),
                        ("TERMINAL_EV_EBITDA", stated(exit_multiple) if exit_multiple else None),
                    ],
                    method=method,
                    fiscal_year=final_year,
                )
            built += [ebitda, tv]
        pv_tv = ledger.compute(
            "PRESENT_VALUE_TERMINAL",
            "PRESENT_VALUE_TERMINAL/1",
            [("TERMINAL_VALUE", ref(tv)), ("DISCOUNT_FACTOR", ref(factors[-1]))],
            method=method,
        )
        ev = ledger.compute(
            "ENTERPRISE_VALUE",
            "ENTERPRISE_VALUE/1/DCF",
            [("SUM_PRESENT_VALUE", ref(total)), ("PRESENT_VALUE_TERMINAL", ref(pv_tv))],
            method=method,
        )
        built += [pv_tv, ev]
        if ev.value is not None and pv_tv.value is not None and ev.value > 0:
            share = pv_tv.value / ev.value
            if share > Decimal("0.75"):
                ledger.issues.add(
                    "TERMINAL_VALUE_DOMINATES",
                    Severity.INFO,
                    f"the terminal value is {round(share * 100, 1)} % of enterprise value",
                    scenario=scenario,
                    method=method,
                )
        result = _finish(plan, scenario, method, ledger, ev, None, [*common, *built])
        out.append(
            result.model_copy(
                update={
                    "wacc": wacc.value,
                    "terminal_growth": growth.value if growth else None,
                    "schedule": tuple(schedule),
                    "terminal_value": tv.value,
                    "present_value_of_terminal_value": pv_tv.value,
                    "sum_present_value_forecast": total.value,
                }
            )
        )
    return out


def _multiple(
    plan: Plan,
    scenario: Scenario,
    multiple_id: UUID,
    m: MultipleInput,
    ledger: Ledger,
    box10: Callable[[ForecastLine, int], Src],
    operating_lines: Callable[[int], tuple[ValuationValue, ValuationValue]],
) -> MethodResult:
    fy = m.fiscal_year
    basis = MULTIPLE_BASIS[m.method]
    built: list[ValuationValue] = []
    if m.method == VM.PE:
        metric_name = "PAT_ATTRIBUTABLE"
        metric = box10(L.PAT_ATTRIBUTABLE, fy)
    else:
        ebitda, ebit = operating_lines(fy)
        chosen = ebitda if m.method == VM.EV_EBITDA else ebit
        metric_name = chosen.name
        metric = ref(chosen)
        built.append(chosen)
    detail = MultipleDetail(
        metric=metric_name,
        fiscal_year=fy,
        multiple=m.multiple,
        basis=basis,
        source=m.source,
        reference=m.reference,
        evidence_ids=m.evidence_ids,
        metric_value=metric.value,
        metric_value_id=metric.ref_id,
    )
    multiple = Src(InputKind.MULTIPLE, multiple_id, m.multiple)
    if metric.value is not None and metric.value <= 0:
        missing = (f"{metric_name} FY{fy} = {metric.value}",)
        value = ledger.unavailable(
            "EQUITY_VALUE" if basis == MultipleBasis.EQUITY else "ENTERPRISE_VALUE",
            U.MONEY,
            "NON_POSITIVE_METRIC",
            missing,
            method=m.method,
            fiscal_year=fy,
        )
        return _refused(
            plan,
            scenario,
            m.method,
            "NON_POSITIVE_METRIC",
            missing,
            tuple(v.value_id for v in [*built, value]),
        ).model_copy(update={"multiple": detail})
    if basis == MultipleBasis.EQUITY:
        equity = ledger.compute(
            "EQUITY_VALUE",
            "EQUITY_VALUE/1/PE",
            [("METRIC", metric), ("MULTIPLE", multiple)],
            method=m.method,
            fiscal_year=fy,
        )
        result = _finish(plan, scenario, m.method, ledger, None, equity, [*built, equity], fy)
    else:
        ev = ledger.compute(
            "ENTERPRISE_VALUE",
            "ENTERPRISE_VALUE/1/MULTIPLE",
            [("METRIC", metric), ("MULTIPLE", multiple)],
            method=m.method,
            fiscal_year=fy,
        )
        result = _finish(plan, scenario, m.method, ledger, ev, None, [*built, ev], fy)
    return result.model_copy(update={"multiple": detail})


def _finish(
    plan: Plan,
    scenario: Scenario,
    method: ValuationMethod,
    ledger: Ledger,
    ev: ValuationValue | None,
    equity: ValuationValue | None,
    built: list[ValuationValue],
    fiscal_year: int | None = None,
) -> MethodResult:
    """EV -> equity (bridge) -> per share -> vs a stated price; status from what exists."""
    s = plan.shared
    if ev is not None:
        refs: list[tuple[str, Src | None]] = [
            ("ENTERPRISE_VALUE", ref(ev)),
            ("GROSS_DEBT", ref(s.gross_debt)),
            ("CASH", ref(s.cash)),
            ("MINORITY_INTEREST", ref(s.minority)),
        ]
        formula = "EQUITY_VALUE/1/BRIDGE"
        bridge = [s.gross_debt, s.cash, s.minority]
        if s.adjustment is not None:
            refs.append(("OTHER_EQUITY_ADJUSTMENT", ref(s.adjustment)))
            formula = "EQUITY_VALUE/1/BRIDGE_WITH_ADJUSTMENT"
            bridge.append(s.adjustment)
        equity = ledger.compute(
            "EQUITY_VALUE", formula, refs, method=method, fiscal_year=fiscal_year
        )
        built = [*built, *bridge, equity]
    assert equity is not None
    per_share = ledger.compute(
        "FAIR_VALUE_PER_SHARE",
        "FAIR_VALUE_PER_SHARE/1",
        [("EQUITY_VALUE", ref(equity)), ("SHARE_COUNT", ref(s.shares))],
        method=method,
        fiscal_year=fiscal_year,
    )
    built += [s.shares, per_share]
    if equity.value is not None and equity.value < 0:
        ledger.issues.add(
            "NEGATIVE_EQUITY_VALUE",
            Severity.WARNING,
            f"equity value {equity.value}",
            scenario=scenario,
            method=method,
            fiscal_year=fiscal_year,
        )
    gap: ValuationValue | None = None
    if s.price is not None and per_share.value is not None:
        gap = ledger.compute(
            "FAIR_VALUE_VS_PRICE",
            "FAIR_VALUE_VS_PRICE/1",
            [("FAIR_VALUE_PER_SHARE", ref(per_share)), ("MARKET_PRICE", stated(s.price))],
            method=method,
            fiscal_year=fiscal_year,
        )
        built.append(gap)
    reason: str | None = None
    missing: tuple[str, ...] = ()
    if per_share.value is not None:
        status = ValuationStatus.COMPLETE
    else:
        status = (
            ValuationStatus.PARTIAL
            if (ev is not None and ev.value is not None) or equity.value is not None
            else ValuationStatus.UNAVAILABLE
        )
        # the root cause: the first value (in dependency order) that failed for its own reason
        failed = [v for v in built if v.value is None]
        stop = next((v for v in failed if v.reason != "UPSTREAM_UNAVAILABLE"), failed[0])
        reason, missing = stop.reason, stop.missing_inputs
    return MethodResult(
        scenario=scenario,
        method=method,
        status=status,
        reason=reason,
        missing_inputs=missing,
        currency=plan.currency,
        enterprise_value=ev.value if ev is not None else None,
        equity_value=equity.value,
        fair_value_per_share=per_share.value,
        fair_value_vs_price_percent=gap.value if gap is not None else None,
        value_ids=tuple(dict.fromkeys(v.value_id for v in built)),
    )


# --- the service ------------------------------------------------------------------------------


def _record(a: AssumptionInput, forecast_hash: str) -> ValuationAssumption:
    warnings: tuple[str, ...] = ()
    if a.unit == AssumptionUnit.PERCENT and D0 < abs(a.value) < 1:
        warnings = ("PERCENT_BELOW_ONE",)
    body = f"{a.kind.value}|{a.scenario}|{a.value}|{a.unit.value}|{a.currency}|{a.scale}"
    return ValuationAssumption(
        assumption_id=uuid5(NS, f"{forecast_hash}|A|{body}|{a.reference}|{a.evidence_ids}"),
        kind=a.kind,
        scenario=a.scenario,
        value=a.value,
        unit=a.unit,
        currency=a.currency,
        scale=a.scale,
        reference=a.reference,
        evidence_ids=a.evidence_ids,
        warnings=warnings,
    )


def build_valuation(
    forecast: ForecastDataset,
    *,
    request: ValuationRequest | None = None,
    box4: CanonicalFinancialDataset | None = None,
    generated_at: datetime,
) -> ValuationDataset:
    """Deterministic Box 11 valuation. Raises ValuationInputError when the request cannot be
    applied to this forecast (wrong Box 4, scenario, basis, currency, horizon or weights)."""
    req = request or ValuationRequest()
    # a dataset built with model_construct (or edited) must still satisfy the Box 10 contract
    forecast = ForecastDataset.model_validate(forecast.model_dump())
    forecast_hash = hashlib.sha256(canonical_forecast_json(forecast).encode()).hexdigest()
    if box4 is not None:
        if box4.company_key != forecast.company_key:
            raise ValuationInputError("COMPANY_MISMATCH", "Box 4 dataset is for another company")
        digest = hashlib.sha256(canonical_dataset_json(box4).encode()).hexdigest()
        if digest != forecast.source_box4.canonical_sha256:
            raise ValuationInputError("BOX4_MISMATCH", "Box 4 is not the forecast's own Box 4")
    # a valid schema and resolving references do not make a number Box 10's: recompute them all
    try:
        verify_forecast_integrity(forecast, box4)
    except ForecastIntegrityError as exc:
        raise ValuationInputError("FORECAST_INTEGRITY_FAILED", str(exc)) from None
    base = forecast.base
    if req.reporting_basis is not None and req.reporting_basis != base.reporting_basis:
        raise ValuationInputError(
            "BASIS_MISMATCH",
            f"forecast is {base.reporting_basis.value}, request expects "
            f"{req.reporting_basis.value}",
        )
    for scenario in req.scenarios:
        if scenario not in forecast.scenarios:
            raise ValuationInputError("SCENARIO_NOT_FORECAST", f"{scenario.value} not forecast")
    currency = base.currency
    for a in req.assumptions:
        if a.currency is not None and a.currency != currency:
            raise ValuationInputError(
                "CURRENCY_MISMATCH",
                f"{a.kind.value} is in {a.currency.value}; the forecast is in {currency.value}",
            )
    all_years = tuple(p.fiscal_year for p in forecast.forecast_years if p.fiscal_year is not None)
    horizon = req.dcf_horizon or len(all_years)
    if all_years and horizon > len(all_years):
        raise ValuationInputError(
            "HORIZON_EXCEEDS_FORECAST",
            f"DCF horizon {horizon} > {len(all_years)} forecast years",
        )
    for m in req.multiples:
        if all_years and m.fiscal_year not in all_years:
            raise ValuationInputError(
                "MULTIPLE_YEAR_NOT_FORECAST", f"{m.method.value} FY{m.fiscal_year} not forecast"
            )
    recorded = [_record(a, forecast_hash) for a in req.assumptions]
    explicit = {(a.kind, a.scenario): a for a in recorded}
    for scenario in req.scenarios:
        weights = [explicit.get((k, scenario)) for k in (K.EQUITY_WEIGHT, K.DEBT_WEIGHT)]
        if any(weights) and not (
            weights[0] and weights[1] and weights[0].value + weights[1].value == HUNDRED
        ):
            raise ValuationInputError(
                "WEIGHTS_DO_NOT_SUM_TO_100",
                f"{scenario.value}: EQUITY_WEIGHT + DEBT_WEIGHT must be exactly 100",
            )
    multiple_ids = tuple(
        uuid5(
            NS,
            f"{forecast_hash}|M|{m.method.value}|{m.scenario.value}|{m.fiscal_year}|{m.multiple}"
            f"|{m.source.value}|{m.reference}|{m.evidence_ids}",
        )
        for m in req.multiples
    )
    issues = Issues()
    for r in recorded:
        if r.warnings:
            issues.add(
                "PERCENT_BELOW_ONE",
                Severity.WARNING,
                f"{r.kind.value} = {r.value} %: percent units (12.5 means 12.5 %, not 0.125)",
                scenario=r.scenario,
                subject=r.kind.value,
            )
    fatal: tuple[str, str] | None = None
    if currency in (Currency.UNKNOWN, Currency.OTHER):
        fatal = ("CURRENCY_UNKNOWN", "the forecast currency is not a known currency")
    elif not all_years or base.latest_actual is None:
        fatal = ("NO_FORECAST_YEARS", "the Box 10 forecast has no forecast years")
    values: list[ValuationValue] = []
    results: list[MethodResult] = []
    if fatal is not None:
        issues.add(fatal[0], Severity.ERROR, fatal[1])
        for scenario in req.scenarios:
            results.extend(
                MethodResult(
                    scenario=scenario,
                    method=method,
                    status=ValuationStatus.UNAVAILABLE,
                    reason=fatal[0],
                    currency=currency,
                    value_ids=(),
                )
                for method in req.methods
            )
    else:
        ledger = Ledger(f"{forecast_hash}|SHARED", None, currency, issues)
        shared = _shared(forecast, box4, explicit, ledger)
        values.extend(ledger.values)
        plan = Plan(
            forecast=forecast,
            forecast_hash=forecast_hash,
            currency=currency,
            methods=req.methods,
            years=all_years[:horizon],
            all_years=all_years,
            financial=forecast.industry in RESTRICTED_INDUSTRIES,
            shared=shared,
        )
        for scenario in req.scenarios:
            own = {k: a for (k, s), a in explicit.items() if s == scenario}
            mine = [
                (i, m)
                for i, m in zip(multiple_ids, req.multiples, strict=True)
                if m.scenario == scenario
            ]
            scenario_values, scenario_results = value_scenario(plan, scenario, own, mine, issues)
            values.extend(scenario_values)
            results.extend(scenario_results)
    if issues.truncated:
        issues.items.append(ValuationIssue(code="ISSUES_TRUNCATED", severity=Severity.WARNING))
    statuses = {r.status for r in results}
    status = (
        ValuationStatus.COMPLETE
        if statuses == {ValuationStatus.COMPLETE}
        else ValuationStatus.UNAVAILABLE
        if statuses <= {ValuationStatus.UNAVAILABLE}
        else ValuationStatus.PARTIAL
    )
    box4_sha = forecast.source_box4.canonical_sha256
    valuation_id = uuid5(
        NS,
        f"{forecast_hash}|{box4 is not None}|{req.model_dump_json()}|{VALUATION_ENGINE_VERSION}"
        f"|{VALUATION_FORMULA_VERSION}|{VALUATION_POLICY_VERSION}",
    )
    used = (
        tuple(p for p in forecast.forecast_years if p.fiscal_year in all_years[:horizon])
        if fatal is None
        else ()
    )
    return ValuationDataset(
        valuation_id=valuation_id,
        company_key=forecast.company_key,
        company_name=forecast.company_name,
        industry=forecast.industry,
        generated_at=generated_at,
        source_forecast=ForecastSource(
            schema_version=forecast.schema_version,
            engine_version=forecast.engine_version,
            forecast_id=forecast.forecast_id,
            canonical_sha256=forecast_hash,
            status=forecast.status,
            box4_dataset_id=forecast.source_box4.dataset_id,
            box4_canonical_sha256=box4_sha,
            box4_supplied=box4 is not None,
        ),
        request=req,
        status=status,
        reporting_basis=base.reporting_basis,
        currency=currency,
        valuation_date=(
            base.latest_actual.model_copy(update={"label": f"{base.latest_actual.label} close"})
            if base.latest_actual
            else None
        ),
        forecast_years=used,
        scenarios=req.scenarios,
        assumptions=tuple(recorded),
        multiples=req.multiples,
        multiple_ids=multiple_ids,
        values=tuple(values),
        results=tuple(results),
        issues=tuple(issues.items),
        manifest=ValuationManifest(
            engine_version=VALUATION_ENGINE_VERSION,
            formula_version=VALUATION_FORMULA_VERSION,
            policy_version=VALUATION_POLICY_VERSION,
            formulas=tuple(f for f, _ in FORMULAS.values()),
            policies=POLICIES,
        ),
    )


async def build_valuation_async(
    forecast: ForecastDataset,
    *,
    request: ValuationRequest | None = None,
    box4: CanonicalFinancialDataset | None = None,
    generated_at: datetime,
) -> ValuationDataset:
    """For async callers: the CPU work (and both hashes) runs off the event loop."""
    return await asyncio.to_thread(
        build_valuation, forecast, request=request, box4=box4, generated_at=generated_at
    )


def canonical_valuation_json(dataset: ValuationDataset) -> str:
    """Deterministic serialization; generated_at is excluded so equal inputs compare equal."""
    return json.dumps(
        dataset.model_dump(mode="json", exclude={"generated_at"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
