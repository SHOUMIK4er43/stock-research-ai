"""DETAILED_RESEARCH_V1 Box 10 integrity: is a ForecastDataset still what Box 10 computed?

`ForecastDataset` validation proves structure only: every reference resolves and every copied
input value equals the value it references. It does not prove that a number is the result of its
formula, so a value forged together with the copies of it held by its consumers still validates.
`verify_forecast_integrity` closes that gap without a second forecast engine. It reuses Box 10's
own formula registry and evaluator and checks, for every FORECAST value:

  - the formula is a registered Box 10 formula for the value's line, applicable on this
    forecast's operating route / basis / assumptions (the choice `project` makes);
  - each named input resolves BY ID to the authoritative object (never the copy in the input):
    an assumption of the same driver, scenario and year; a value of the same line, scenario and
    year; or, for a PRIOR / OPENING role, last year's value of that line (the actual anchor in
    the first forecast year);
  - the copied input value equals the authoritative value;
  - the recorded value equals `evaluate(formula, authoritative inputs)` exactly (Box 10 keeps
    full working precision: no rounding to compare through);
  - the graph is acyclic (Kahn's algorithm, iterative, linear in values + inputs).

Assumptions are checked against the Box 10 domain contract (driver, unit, currency, scenario,
year, source / method, uniqueness); an EXPLICIT or INDUSTRY assumption must be exactly the request
entry it came from. Historical and guidance assumptions are not re-derived (that is Box 10
policy, not arithmetic). Actual anchors are checked for shape and composite arithmetic; with the
forecast's own Box 4 (hash-checked by the caller) they are rebuilt by Box 10's own anchor code and
must be identical. Without Box 4, an anchor's Box 4 lineage is trusted as recorded.
"""

from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from stock_research_ai.application.financial_metrics import REVENUE
from stock_research_ai.application.forecasting import (
    ACTUAL_COMPOSITES,
    FORMULAS,
    LENDER_ANCHORS,
    YTD_METRICS,
    History,
    Issues,
    _anchor,
    _year_to_date,
    evaluate,
    lender_anchors,
    quantize,
)
from stock_research_ai.domain.financial_analytics import MetricInput
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    FinancialPeriod,
    ReportingBasis,
    Scale,
)
from stock_research_ai.domain.forecasting import (
    DRIVER_SPECS,
    FORECAST_FORMULA_VERSION,
    MAX_ABS_FORECAST,
    AssumptionSource,
    AssumptionUnit,
    DerivationMethod,
    Driver,
    ForecastAssumption,
    ForecastDataset,
    ForecastFormula,
    ForecastLine,
    ForecastModel,
    ForecastValue,
    OperatingRoute,
    Scenario,
)

L = ForecastLine
M = CanonicalMetric
SRC = AssumptionSource
DM = DerivationMethod

#: a role naming last year's value of a line (the actual anchor in the first forecast year)
PRIOR_ROLES = {
    "PRIOR_REVENUE": L.REVENUE,
    "OPENING_GROSS_DEBT": L.GROSS_DEBT,
    "PRIOR_TRADE_WORKING_CAPITAL": L.TRADE_WORKING_CAPITAL,
    "PRIOR_TOTAL_ASSETS": L.TOTAL_ASSETS,
    "PRIOR_COMMON_EQUITY": L.COMMON_EQUITY,
    "PRIOR_TOTAL_BORROWINGS": L.TOTAL_BORROWINGS,
}
ANCHOR_LINES = frozenset({L.REVENUE, L.GROSS_DEBT, L.TRADE_WORKING_CAPITAL})
#: the NON_BANK_FINANCIAL model: its own lines, anchors and the one formula per line
LENDER_FORMULAS: dict[ForecastLine, str] = {
    L.TOTAL_ASSETS: "TOTAL_ASSETS/1",
    L.PAT: "PAT/1/ROA_ON_AVERAGE_ASSETS",
    L.COMMON_EQUITY: "COMMON_EQUITY/1/EQUITY_CHANGE_TO_PAT",
    L.NON_EQUITY_FUNDING: "NON_EQUITY_FUNDING/1",
    L.TOTAL_BORROWINGS: "TOTAL_BORROWINGS/1/SHARE_OF_FUNDING",
    L.FINANCE_COST: "FINANCE_COST/1/COST_OF_BORROWINGS_PROXY",
    L.TOTAL_INCOME: "TOTAL_INCOME/1/INCOME_ON_AVERAGE_ASSETS",
}
LENDER_ONLY_LINES = frozenset(LENDER_FORMULAS) - {L.PAT, L.FINANCE_COST}
#: the Box 4 metrics a single-value anchor may carry (as History.flow / History.gross_debt)
ANCHOR_METRICS: dict[ForecastLine, frozenset[CanonicalMetric]] = {
    L.REVENUE: frozenset(REVENUE),
    L.GROSS_DEBT: frozenset({M.TOTAL_DEBT, M.TOTAL_BORROWINGS}),
    L.TRADE_WORKING_CAPITAL: frozenset(),  # always the composite
    L.TOTAL_ASSETS: frozenset({M.TOTAL_ASSETS}),
    L.TOTAL_BORROWINGS: frozenset({M.TOTAL_BORROWINGS}),
    L.COMMON_EQUITY: frozenset(),  # always a composite: never total equity as reported
    L.PAT: frozenset({M.PAT}),
    L.FINANCE_COST: frozenset({M.FINANCE_COST}),
    L.TOTAL_INCOME: frozenset({M.TOTAL_INCOME}),
}
#: composite anchors: (component metrics in order, signs)
COMPOSITE_PARTS: dict[str, tuple[tuple[CanonicalMetric, ...], tuple[int, ...]]] = {
    "ACTUAL_TRADE_WORKING_CAPITAL/1": (
        (M.TRADE_RECEIVABLES, M.INVENTORY, M.TRADE_PAYABLES),
        (1, 1, -1),
    ),
    "ACTUAL_GROSS_DEBT/1/COMPONENTS": (
        (M.LONG_TERM_BORROWINGS, M.SHORT_TERM_BORROWINGS),
        (1, 1),
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES_LESS_HYBRID": (
        (M.EQUITY_SHARE_CAPITAL, M.RESERVES, M.HYBRID_EQUITY_INSTRUMENTS), (1, 1, -1),
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES": (
        (M.EQUITY_SHARE_CAPITAL, M.RESERVES), (1, 1),
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_OTHER_EQUITY": (
        (M.EQUITY_SHARE_CAPITAL, M.OTHER_EQUITY), (1, 1),
    ),
}
METHODS: dict[AssumptionSource, frozenset[DerivationMethod]] = {
    SRC.HISTORICAL: frozenset(m for m in DM if m.value.startswith("HISTORICAL_"))
    | {DM.YEAR_TO_DATE_TRAJECTORY},
    SRC.MANAGEMENT_GUIDANCE: frozenset(m for m in DM if m.value.startswith("GUIDANCE_")),
    SRC.EXPLICIT: frozenset({DM.EXPLICIT_VALUE}),
    SRC.INDUSTRY_INTELLIGENCE: frozenset({DM.EXPLICIT_VALUE}),
    SRC.FALLBACK: frozenset({DM.FALLBACK_CONSTANT}),
}
EXPLICIT_SOURCES = (SRC.EXPLICIT, SRC.INDUSTRY_INTELLIGENCE)
#: the role naming the prior-year revenue of the remaining months (first forecast year)
REMAINDER_ROLE = "PRIOR_YEAR_REMAINDER_REVENUE"
#: lines whose first forecast year is year-to-date + remainder when Box 10 recorded one
YTD_FORMULA_LINES = frozenset({L.REVENUE, L.OPERATING_PROFIT, L.OTHER_INCOME,
                               L.DEPRECIATION_AMORTIZATION, L.FINANCE_COST, L.PBT, L.TAX,
                               L.PAT})

Years = dict[int | None, FinancialPeriod]
Assumptions = dict[tuple[Driver, Scenario, int], ForecastAssumption]


class ForecastIntegrityError(ValueError):
    """The forecast is not what Box 10 computed (fails closed). The message names the value,
    never its numbers or the serialized forecast."""


def _where(v: ForecastValue) -> str:
    tag = v.scenario.value if v.scenario else "ACTUAL"
    return f"{v.line.value} {tag} FY{v.period.fiscal_year}"


def _manifest_formulas() -> tuple[ForecastFormula, ...]:
    return tuple(f for f, _ in FORMULAS.values()) + tuple(
        ForecastFormula(formula_id=k, line=line, expression=text, inputs=())
        for k, (line, text) in ACTUAL_COMPOSITES.items()
    )


def verify_forecast_integrity(
    forecast: ForecastDataset, box4: CanonicalFinancialDataset | None = None
) -> None:
    """Raises ForecastIntegrityError unless every number in `forecast` is reproducible from its
    anchors and assumptions by the registered Box 10 formulas. `box4`, when given, must already
    be the forecast's own Box 4 (the caller checks the hash): the anchors are then rebuilt from
    it."""
    manifest = forecast.manifest
    if manifest.formula_version != FORECAST_FORMULA_VERSION or (
        manifest.formulas != _manifest_formulas()
    ):
        raise ForecastIntegrityError("the formula manifest is not the Box 10 registry")
    years: Years = {p.fiscal_year: p for p in forecast.forecast_years}
    first = forecast.forecast_years[0].fiscal_year if forecast.forecast_years else None
    _check_anchors(forecast, box4)
    assumptions = _check_assumptions(forecast, years)
    _check_year_to_date(forecast, box4)
    by_id: dict[UUID, ForecastValue] = {
        v.value_id: v for v in (*forecast.actuals, *forecast.year_to_date_actuals)}
    keys = Counter((v.line, v.scenario, v.period.fiscal_year) for v in forecast.values)
    if any(n > 1 for n in keys.values()):
        raise ForecastIntegrityError("a forecast line is recorded twice for one scenario and year")
    by_id.update((v.value_id, v) for v in forecast.values)
    by_key = {(v.line, v.scenario, v.period.fiscal_year): v for v in forecast.values}
    anchors = {v.line: v for v in forecast.actuals}
    assumption_ids = {a.assumption_id: a for a in forecast.assumptions}
    # Kahn's algorithm over FORECAST -> FORECAST edges: an order in which every input is verified
    # before its consumer, and a cycle (should the model ever admit one) is refused
    pending: dict[UUID, int] = {}
    consumers: dict[UUID, list[ForecastValue]] = {}
    value_ids = {v.value_id for v in forecast.values}
    for v in forecast.values:
        upstream = [i.ref_id for i in v.inputs if i.ref_id in value_ids]
        pending[v.value_id] = len(upstream)
        for ref in upstream:
            consumers.setdefault(ref, []).append(v)
    ready = deque(v for v in forecast.values if pending[v.value_id] == 0)
    ix = Index(forecast, years, first, by_id, by_key, anchors, assumption_ids, assumptions)
    done = 0
    while ready:
        v = ready.popleft()
        done += 1
        _check_value(v, ix)
        for consumer in consumers.get(v.value_id, ()):
            pending[consumer.value_id] -= 1
            if pending[consumer.value_id] == 0:
                ready.append(consumer)
    if done != len(forecast.values):
        raise ForecastIntegrityError("the forecast calculation graph has a cycle")


@dataclass(frozen=True)
class Index:
    """Built once: every lookup during the traversal is O(1)."""

    forecast: ForecastDataset
    years: Years
    first: int | None
    by_id: dict[UUID, ForecastValue]
    by_key: dict[tuple[ForecastLine, Scenario | None, int | None], ForecastValue]
    anchors: dict[ForecastLine, ForecastValue]
    assumption_ids: dict[UUID, ForecastAssumption]
    assumptions: Assumptions


def _check_value(v: ForecastValue, ix: Index) -> None:
    forecast, years, first, by_id, by_key = ix.forecast, ix.years, ix.first, ix.by_id, ix.by_key
    where = _where(v)
    fy = v.period.fiscal_year
    if (
        v.scenario is None
        or v.scenario not in forecast.scenarios
        or fy is None
        or years.get(fy) != v.period
    ):
        raise ForecastIntegrityError(f"{where}: not a forecast scenario and year")
    per_share = v.line == L.EPS
    if (
        v.currency != forecast.base.currency
        or v.unit != ("PER_SHARE" if per_share else "MONEY")
        or v.scale != (Scale.ONES if per_share else Scale.CRORES)
    ):
        raise ForecastIntegrityError(f"{where}: unit, scale or currency differs from Box 10")
    if v.status == "UNAVAILABLE":
        if v.formula_id is not None or v.inputs:
            raise ForecastIntegrityError(f"{where}: an unavailable value carries a formula")
        return
    registered = FORMULAS.get(v.formula_id or "")
    if registered is None:
        raise ForecastIntegrityError(f"{where}: formula is not a registered Box 10 formula")
    definition, _ = registered
    if definition.line != v.line or v.formula_text != definition.expression:
        raise ForecastIntegrityError(f"{where}: formula belongs to another line")
    if tuple(i.role for i in v.inputs) != definition.inputs:
        raise ForecastIntegrityError(f"{where}: formula inputs are not {definition.inputs}")
    _check_formula_choice(v, ix)
    authoritative: dict[str, Decimal] = {}
    for i in v.inputs:
        if i.role in PRIOR_ROLES:
            line = PRIOR_ROLES[i.role]
            expected = (
                ix.anchors.get(line) if fy == first else by_key.get((line, v.scenario, fy - 1))
            )
            if i.kind != ("ACTUAL" if fy == first else "FORECAST"):
                raise ForecastIntegrityError(f"{where}: {i.role} is not last year's {line.value}")
            source: ForecastValue | ForecastAssumption | None = by_id.get(i.ref_id)
            if source is None or source is not expected:
                raise ForecastIntegrityError(f"{where}: {i.role} is not last year's {line.value}")
            got = source.value
        elif i.role.startswith("YTD_") or i.role == REMAINDER_ROLE:
            ytd = forecast.base.year_to_date
            source = by_id.get(i.ref_id)
            ytd_line = L.REVENUE if i.role == REMAINDER_ROLE else L.__members__.get(i.role[4:])
            if (
                ytd is None
                or fy != first
                or i.kind != "ACTUAL"
                or source is None
                or source not in forecast.year_to_date_actuals
                or source.line != ytd_line
                or (source.period == ytd.period) == (i.role == REMAINDER_ROLE)
            ):
                raise ForecastIntegrityError(
                    f"{where}: {i.role} is not this forecast's reported year to date")
            got = source.value
        elif i.role in Driver.__members__:
            a = ix.assumption_ids.get(i.ref_id)
            if (
                i.kind != "ASSUMPTION"
                or a is None
                or a.driver != Driver(i.role)
                or a.scenario != v.scenario
                or a.fiscal_year != fy
            ):
                raise ForecastIntegrityError(
                    f"{where}: {i.role} is not this scenario and year's assumption"
                )
            got = a.value
        elif i.role in L.__members__:
            source = by_id.get(i.ref_id)
            if (
                i.kind != "FORECAST"
                or source is None
                or source is not by_key.get((L(i.role), v.scenario, fy))
            ):
                raise ForecastIntegrityError(
                    f"{where}: {i.role} is not this scenario and year's {i.role}"
                )
            got = source.value
        else:
            raise ForecastIntegrityError(f"{where}: unknown formula role {i.role}")
        if got is None:
            raise ForecastIntegrityError(f"{where}: {i.role} references an unavailable value")
        if i.value != got:
            raise ForecastIntegrityError(f"{where}: copied {i.role} differs from its source")
        authoritative[i.role] = got
    assert v.formula_id is not None and v.value is not None  # AVAILABLE (contract)
    recomputed = evaluate(v.formula_id, authoritative)
    if not recomputed.is_finite() or abs(recomputed) > MAX_ABS_FORECAST:
        raise ForecastIntegrityError(f"{where}: Box 10 would have refused this value")
    if recomputed != v.value or str(recomputed) != str(v.value):
        raise ForecastIntegrityError(f"{where}: value is not its formula's result")


def _check_formula_choice(v: ForecastValue, ix: Index) -> None:
    """The formula `project` would have chosen for this line (route, basis, assumptions)."""
    fy, scenario = v.period.fiscal_year, v.scenario
    assert fy is not None and scenario is not None
    route = ix.forecast.base.operating_route

    def a(driver: Driver) -> ForecastAssumption | None:
        return ix.assumptions.get((driver, scenario, fy))

    ytd = ix.forecast.base.year_to_date
    ytd_year = ytd is not None and fy == ix.first
    second = "/2/" in (v.formula_id or "")
    if ytd_year and (v.line in YTD_FORMULA_LINES or (
            v.line == L.EBITDA and route == OperatingRoute.REPORTED_EBITDA)):
        assert ytd is not None
        rate = ix.assumptions.get((Driver.INTEREST_RATE_ON_DEBT, scenario, fy))
        ratio = ix.assumptions.get((Driver.FINANCE_COST_TO_REVENUE, scenario, fy))
        stated_ratio = ratio is not None and ratio.source != SRC.HISTORICAL
        uses_rate = rate is not None and (rate.source != SRC.HISTORICAL or not stated_ratio)
        expected = (f"FINANCE_COST/2/RATE_YTD_REMAINING_{12 - ytd.months}M"
                    if v.line == L.FINANCE_COST and uses_rate
                    else f"{v.line.value}/2/YTD_PLUS_REMAINDER")
        if v.formula_id != expected:
            raise ForecastIntegrityError(f"{_where(v)}: not the formula Box 10 applies here")
        return
    if second:  # a year-to-date formula outside the first year of a year-to-date forecast
        raise ForecastIntegrityError(f"{_where(v)}: not the formula Box 10 applies here")
    if _lender(ix.forecast):
        if LENDER_FORMULAS.get(v.line) != v.formula_id:
            raise ForecastIntegrityError(f"{_where(v)}: not the formula Box 10 applies here")
        return
    if v.line in LENDER_ONLY_LINES or v.formula_id in LENDER_FORMULAS.values():
        wrong = True  # a lender line / formula in another model's forecast
    elif route == OperatingRoute.UNAVAILABLE and v.line != L.REVENUE:
        wrong = True
    elif v.line in (L.OPERATING_PROFIT, L.OTHER_INCOME):
        wrong = route != OperatingRoute.OPERATING_PROFIT
    elif v.line == L.EBITDA:
        wrong = v.formula_id != (
            "EBITDA/1/OPERATING_PROFIT_ROUTE"
            if route == OperatingRoute.OPERATING_PROFIT
            else "EBITDA/1/REPORTED_MARGIN"
        )
    elif v.line == L.FINANCE_COST:
        rate, ratio = a(Driver.INTEREST_RATE_ON_DEBT), a(Driver.FINANCE_COST_TO_REVENUE)
        stated_ratio = ratio is not None and ratio.source != SRC.HISTORICAL
        uses_rate = rate is not None and (rate.source != SRC.HISTORICAL or not stated_ratio)
        wrong = v.formula_id != (
            "FINANCE_COST/1/RATE_ON_OPENING_DEBT"
            if uses_rate
            else "FINANCE_COST/1/RATIO_TO_REVENUE"
        )
    elif v.line in (L.MINORITY_INTEREST, L.PAT_ATTRIBUTABLE):
        consolidated = ix.forecast.base.reporting_basis == ReportingBasis.CONSOLIDATED
        wrong = v.formula_id not in (
            ("MINORITY_INTEREST/1", "PAT_ATTRIBUTABLE/1/LESS_MINORITY")
            if consolidated
            else ("PAT_ATTRIBUTABLE/1/STANDALONE",)
        )
    elif v.line == L.CAPEX:
        wrong = v.formula_id != (
            "CAPEX/1/AMOUNT" if a(Driver.CAPEX_AMOUNT) is not None else "CAPEX/1/RATIO_TO_REVENUE"
        )
    else:
        wrong = False
    if wrong:
        raise ForecastIntegrityError(f"{_where(v)}: not the formula Box 10 applies here")


def _check_assumptions(forecast: ForecastDataset, years: Years) -> Assumptions:
    requested = {
        (a.driver, a.scenario, a.fiscal_year): a for a in forecast.request.explicit_assumptions
    }
    out: Assumptions = {}
    currency = forecast.base.currency
    for a in forecast.assumptions:
        where = f"{a.driver.value} {a.scenario.value} FY{a.fiscal_year}"
        key = (a.driver, a.scenario, a.fiscal_year)
        if key in out:
            raise ForecastIntegrityError(f"{where}: assumption recorded twice")
        out[key] = a
        unit = DRIVER_SPECS[a.driver].unit
        if (
            a.fiscal_year not in years
            or a.unit != unit
            or a.currency != (currency if unit == AssumptionUnit.MONEY else None)
            or a.method not in METHODS[a.source]
        ):
            raise ForecastIntegrityError(f"{where}: assumption violates the Box 10 contract")
        if a.source == SRC.FALLBACK and (a.driver != Driver.DEBT_CHANGE or a.value != 0):
            raise ForecastIntegrityError(f"{where}: not a Box 10 fallback")
        if a.source == SRC.MANAGEMENT_GUIDANCE and (
            a.guidance is None or a.guidance.management_dataset_id != forecast.management_dataset_id
        ):
            raise ForecastIntegrityError(f"{where}: guidance from another Box 8 dataset")
        given = requested.get(key)
        if (given is None) != (a.source not in EXPLICIT_SOURCES):
            raise ForecastIntegrityError(f"{where}: explicit assumption not in the request")
        if given is not None and not _is_input(a, given.model_dump()):
            raise ForecastIntegrityError(f"{where}: differs from the requested assumption")
    return out


def _is_input(a: ForecastAssumption, given: dict[str, object]) -> bool:
    value = given["value"]
    assert isinstance(value, Decimal)
    expected = quantize(value) if a.unit == AssumptionUnit.PERCENT else value
    return (
        a.value == expected
        and str(a.value) == str(expected)
        and a.source == given["source"]
        and a.currency == given["currency"]
        and a.reference == given["reference"]
        and a.evidence_ids == given["evidence_ids"]
    )


def _signed_sum(components: tuple[MetricInput, ...], signs: tuple[int, ...]) -> Decimal:
    return sum((c.value * s for c, s in zip(components, signs, strict=True)), Decimal(0))


def _lender(forecast: ForecastDataset) -> bool:
    return forecast.base.forecast_model == ForecastModel.NON_BANK_FINANCIAL


def _check_year_to_date(forecast: ForecastDataset,
                        box4: CanonicalFinancialDataset | None) -> None:
    """The reported year-to-date anchors: their shape and sums, and - with the forecast's own
    Box 4 - exactly what Box 10's own year-to-date code rebuilds from it."""
    ytd, anchors = forecast.base.year_to_date, forecast.year_to_date_actuals
    if ytd is None:
        return
    first = forecast.forecast_years[0] if forecast.forecast_years else None
    if first is None or first.fiscal_year != ytd.fiscal_year:
        raise ForecastIntegrityError("the year to date is not of the first forecast year")
    for v in anchors:
        where = f"{_where(v)} (year to date)"
        if (v.status != "AVAILABLE" or v.value is None or v.formula_id not in ACTUAL_COMPOSITES
                or ACTUAL_COMPOSITES[v.formula_id][0] != v.line or v.actual is not None
                or v.currency != forecast.base.currency or v.scale != Scale.CRORES
                or any(c.reporting_basis != forecast.base.reporting_basis
                       or c.currency != v.currency for c in v.actual_components)):
            raise ForecastIntegrityError(f"{where}: not a Box 10 year-to-date anchor")
        summed = v.formula_id.startswith("ACTUAL_YTD/1/") and v.line in YTD_METRICS
        if summed and (v.period != ytd.period or sum(
                (c.value for c in v.actual_components), Decimal(0)) != v.value):
            raise ForecastIntegrityError(f"{where}: not the sum of its reported quarters")
    if box4 is None or forecast.base.latest_actual is None:
        return
    end = forecast.base.latest_actual.period_end
    assert end is not None
    base = forecast.base
    rebuilt = _year_to_date(forecast.source_box4.canonical_sha256, base.reporting_basis,
                            base.currency, History(box4, base.reporting_basis, base.currency),
                            end, base.operating_route, Issues())
    if rebuilt is None or rebuilt.base != ytd or rebuilt.anchors() != anchors:
        raise ForecastIntegrityError("the year-to-date anchors differ from Box 4")


def _check_anchors(forecast: ForecastDataset, box4: CanonicalFinancialDataset | None) -> None:
    actuals = forecast.actuals
    latest = forecast.base.latest_actual
    if not actuals:
        return
    lines = [v.line for v in actuals]
    expected = LENDER_ANCHORS if _lender(forecast) else ANCHOR_LINES
    if len(set(lines)) != len(lines) or set(lines) != expected or latest is None:
        raise ForecastIntegrityError("actual anchors are not Box 10's anchors")
    for v in actuals:
        where = _where(v)
        if (
            v.currency != forecast.base.currency
            or v.unit != "MONEY"
            or v.scale != Scale.CRORES
            or v.period.period_end != latest.period_end
            or (v.line in (L.REVENUE, L.PAT, L.FINANCE_COST, L.TOTAL_INCOME)
                and v.period != latest)
        ):
            raise ForecastIntegrityError(f"{where}: anchor is not at the latest fiscal year")
        if v.status == "UNAVAILABLE" or v.value is None:
            if v.inputs or v.actual is not None or v.actual_components:
                raise ForecastIntegrityError(f"{where}: an unavailable anchor carries a value")
            continue
        if abs(v.value) > MAX_ABS_FORECAST or v.inputs:
            raise ForecastIntegrityError(f"{where}: Box 10 would have refused this anchor")
        components = (v.actual,) if v.actual is not None else v.actual_components
        if any(
            c.reporting_basis != forecast.base.reporting_basis
            or c.currency != v.currency
            or c.period.period_end != latest.period_end
            for c in components
        ):
            raise ForecastIntegrityError(f"{where}: anchor from another basis, currency or date")
        if v.formula_id is None:
            if (
                v.actual is None
                or v.actual_components
                or v.actual.metric not in ANCHOR_METRICS[v.line]
                or v.actual.value != v.value
            ):
                raise ForecastIntegrityError(f"{where}: anchor differs from its Box 4 value")
            continue
        parts = COMPOSITE_PARTS.get(v.formula_id)
        if (
            parts is None
            or ACTUAL_COMPOSITES[v.formula_id][0] != v.line
            or v.actual is not None
            or tuple(c.metric for c in v.actual_components) != parts[0]
            or _signed_sum(v.actual_components, parts[1]) != v.value
        ):
            raise ForecastIntegrityError(f"{where}: anchor differs from its Box 4 components")
    if box4 is None or latest.period_end is None:
        return
    # the forecast's own Box 4 (hash already checked): rebuild each anchor with Box 10's code
    base = forecast.base
    history = History(box4, base.reporting_basis, base.currency)
    end = latest.period_end
    pit = history.periods.pit(end)
    box4_hash = forecast.source_box4.canonical_sha256
    if _lender(forecast):
        lender = lender_anchors(box4_hash, base.reporting_basis, base.currency, history, latest,
                                end)
        for v in actuals:
            if lender[v.line] != v:
                raise ForecastIntegrityError(f"{_where(v)}: anchor differs from Box 4")
        return
    rebuilt = {
        L.REVENUE: _anchor(
            box4_hash,
            base.reporting_basis,
            base.currency,
            L.REVENUE,
            latest,
            history.flow(REVENUE, end),
        ),
        L.GROSS_DEBT: _anchor(
            box4_hash,
            base.reporting_basis,
            base.currency,
            L.GROSS_DEBT,
            pit,
            history.gross_debt(end),
        ),
        L.TRADE_WORKING_CAPITAL: _anchor(
            box4_hash,
            base.reporting_basis,
            base.currency,
            L.TRADE_WORKING_CAPITAL,
            pit,
            history.trade_working_capital(end),
        ),
    }
    for v in actuals:
        if rebuilt[v.line] != v:
            raise ForecastIntegrityError(f"{_where(v)}: anchor differs from Box 4")
