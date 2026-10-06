"""Provider-neutral valuation gates and explicit scenario mathematics."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from uuid import UUID, uuid4

from stock_research_ai.application.hashing import digest as digest
from stock_research_ai.application.numerics import root_growth
from stock_research_ai.application.ports import SemanticResolver
from stock_research_ai.domain.calculations import CalculationIssue, CalculationSnapshot
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation
from stock_research_ai.domain.valuation import (
    METHODS,
    EvidencePin,
    SolverPolicy,
    SolverTrace,
    ValuationDefinition,
    ValuationInput,
    ValuationPrecision,
    ValuationRequest,
    ValuationResult,
    ValuationScenario,
    ValuationSnapshot,
    ValuationSource,
    ValuationValue,
)

IT, BANK = "IT_SERVICES_PROFILE", "BANK_PROFILE"
DCF_FORMULA = (
    "FCFF_next=FCFF_N*(1+g); TV=FCFF_next/(w-g); "
    "EV=sum(FCFF_t/(1+w)^t)+TV/(1+w)^N; equity=EV-net_debt; per_share=equity/shares"
)
DEFINITIONS = tuple(
    ValuationDefinition.model_validate(
        dict(
            method=name,
            formula_id=name,
            expression=expression,
            profiles=profiles,
            classification="CALCULATION" if name in METHODS[:2] else "SCENARIO",
            category="OBSERVED_MULTIPLE_CALCULATION"
            if name in METHODS[:2]
            else "SCENARIO_CALCULATION"
            if name == "FCFF_DCF"
            else "SCENARIO_EXPECTATION",
        )
    )
    for name, expression, profiles in (
        ("PRICE_TO_EARNINGS", "price/source_reported_annual_EPS", (IT, BANK)),
        ("PRICE_TO_BOOK", "price/verified_BVPS", (IT, BANK)),
        ("FCFF_DCF", DCF_FORMULA, (IT,)),
        (
            "IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE",
            "FCFF_t=FCFF_0*(1+growth)^t; solve DCF_per_share-price=0",
            (IT,),
        ),
        (
            "REQUIRED_EPS_CAGR_FOR_RETURN",
            "future_price=price*(1+r)^N; future_EPS=future_price/exit_PE; "
            "growth=(future_EPS/EPS)^(1/N)-1",
            (IT, BANK),
        ),
        (
            "REQUIRED_BVPS_CAGR_FOR_RETURN",
            "future_price=price*(1+r)^N; future_BVPS=future_price/exit_PB; "
            "growth=(future_BVPS/BVPS)^(1/N)-1",
            (BANK,),
        ),
    )
)
DEFAULT_PRECISION = ValuationPrecision()
Calculation = CalculationSnapshot | SectorKPIEvaluation


def issue(code: str, error: bool = False) -> CalculationIssue:
    return CalculationIssue(code=code, message=code, severity="ERROR" if error else "LIMITATION")


class Refusal(Exception):
    def __init__(self, status: str, code: str, trace: SolverTrace | None = None) -> None:
        self.status, self.code, self.trace = status, code, trace


def require(condition: bool, code: str, status: str = "INVALID_INPUT") -> None:
    if not condition:
        raise Refusal(status, code)


def pins(request: ValuationRequest) -> tuple[EvidencePin, ...]:
    result: list[EvidencePin] = []
    for s in request.selections:
        result.extend(p for p in (s.price, s.denominator) if p is not None)
        if s.scenario:
            result.extend(a.evidence for a in s.scenario.assumptions if a.evidence is not None)
    return tuple(result)


class Evidence:
    def __init__(
        self,
        request: ValuationRequest,
        financial: CompanyExtractionResult | None,
        markets: dict[UUID, MarketExtractionResult],
        calculations: dict[UUID, Calculation],
        resolver: SemanticResolver,
    ) -> None:
        self.request, self.financial, self.markets, self.calculations = (
            request,
            financial,
            markets,
            calculations,
        )
        self.resolver = resolver
        if request.financial_extraction_id is not None and (
            financial is None
            or financial.metadata.id != request.financial_extraction_id
            or financial.company is None
            or financial.company.company_id != request.company_id
        ):
            raise ValueError("Wrong pinned financial extraction/company")
        if request.classification.provenance and (
            financial is None
            or request.classification.provenance.raw_artifact_id
            not in {a.id for a in financial.raw_artifacts}
        ):
            raise ValueError("Classification evidence missing")
        # Pin validity is independent of sector eligibility or numerical success.
        for pin in pins(request):
            self.resolve(pin, "VALIDATE_REFERENCE_ONLY")

    def resolve(self, pin: EvidencePin, semantic: str) -> ValuationInput:
        source = self.financial
        if pin.kind == "FINANCIAL":
            if source is None or source.metadata.id != pin.snapshot_id:
                raise ValueError("Wrong pinned financial extraction")
            observations = [
                o
                for section in (
                    source.top_metrics,
                    source.profit_loss,
                    source.quarterly,
                    source.balance_sheet,
                    source.cashflow,
                    source.ratios,
                )
                for o in section
                if o.id == pin.observation_id
            ]
            if len(observations) != 1:
                raise ValueError("Wrong or ambiguous financial observation")
            o = observations[0]
            if o.company_id != self.request.company_id:
                raise ValueError("Wrong financial company")
            matches = [
                m for m in self.resolver.resolve(source, (semantic,)) if m.observation_id == o.id
            ]
            mapping = matches[0] if len(matches) == 1 else None
            return ValuationInput(
                classification="FACT",
                pin=pin,
                semantic=semantic,
                value=o.value,
                unit="CURRENCY_PER_SHARE",
                currency=o.unit.currency,
                scale=o.unit.scale,
                financial_observation=o,
                mapping=mapping,
                semantic_verified=bool(
                    mapping and mapping.status == "VERIFIED" and o.unit.kind == "money"
                ),
            )
        if pin.kind == "MARKET":
            market = self.markets.get(pin.snapshot_id)
            if (
                market is None
                or market.instrument is None
                or market.instrument.id != pin.instrument_id
            ):
                raise ValueError("Wrong pinned market extraction/instrument")
            instrument = market.instrument
            if (
                instrument.kind != "EQUITY"
                or source is None
                or source.company is None
                or not any(
                    x.exchange == instrument.exchange and x.symbol == instrument.symbol
                    for x in source.company.listings
                )
            ):
                raise ValueError("Market instrument not linked to company evidence")
            matches_market = [o for o in market.observations if o.id == pin.observation_id]
            if len(matches_market) != 1 or matches_market[0].trading_date != pin.observation_date:
                raise ValueError("Wrong pinned market observation/date")
            observation = matches_market[0]
            return ValuationInput(
                classification="FACT",
                pin=pin,
                semantic=semantic,
                value=observation.close,
                unit="CURRENCY_PER_SHARE",
                currency=instrument.currency,
                market_observation=observation,
                semantic_verified=semantic == "PRICE"
                and instrument.price_unit == "PRICE"
                and instrument.currency is not None,
            )
        calculation = self.calculations.get(pin.snapshot_id)
        if (
            calculation is None
            or source is None
            or calculation.request.company_id != self.request.company_id
            or calculation.request.extraction_id != source.metadata.id
        ):
            raise ValueError("Wrong pinned calculation identity")
        if calculation.source_payload_sha256 != digest(source):
            raise ValueError("Calculation source hash mismatch")
        matched = [
            r
            for r in calculation.results
            if (r.metric_id if hasattr(r, "metric_id") else r.definition.metric_id) == pin.metric_id
        ]
        if len(matched) != 1:
            raise ValueError("Calculation metric missing or ambiguous")
        # Existing 4A/4C percentages are not EPS, BVPS, net debt or FCFF amounts.
        r = matched[0]
        return ValuationInput(
            classification="CALCULATION",
            pin=pin,
            semantic=semantic,
            value=r.value,
            unit=r.unit if hasattr(r, "unit") else r.definition.unit,
            currency=None,
            semantic_verified=False,
        )

    def sources(self) -> tuple[ValuationSource, ...]:
        values = []
        if self.financial:
            f = self.financial
            values.append(
                ValuationSource(
                    kind="FINANCIAL",
                    snapshot_id=f.metadata.id,
                    payload_sha256=digest(f),
                    artifacts=f.raw_artifacts,
                    retrievals=f.retrievals,
                )
            )
        for m in self.markets.values():
            values.append(
                ValuationSource(
                    kind="MARKET",
                    snapshot_id=m.id,
                    payload_sha256=digest(m),
                    artifacts=m.raw_artifacts,
                    retrievals=m.retrievals,
                )
            )
        for c in self.calculations.values():
            values.append(
                ValuationSource(kind="CALCULATION", snapshot_id=c.id, payload_sha256=digest(c))
            )
        return tuple(values)


def validate_fact(item: ValuationInput, scope: str) -> Decimal:
    require(item.semantic_verified, "UNVERIFIED_" + item.semantic, "UNVERIFIED_METRIC_SEMANTICS")
    require(item.value is not None, "MISSING_" + item.semantic, "INSUFFICIENT_INPUT")
    if item.financial_observation and item.semantic != "PRICE":
        o = item.financial_observation
        require(
            scope != "unknown" and o.reporting_scope == scope,
            "FINANCIAL_SCOPE_MISMATCH",
            "INCOMPATIBLE_SCOPE",
        )
        if item.semantic == "EPS":
            require(
                o.period.frequency == "annual" and o.period.end_date is not None,
                "ANNUAL_EPS_PERIOD_REQUIRED",
                "INCOMPATIBLE_PERIOD",
            )
        if item.semantic == "BVPS":
            require(
                o.period.kind == "instant" and o.period.end_date is not None,
                "DATED_BVPS_REQUIRED",
                "INCOMPATIBLE_PERIOD",
            )
    assert item.value is not None
    return item.value * item.scale


def assumptions(
    scenario: ValuationScenario, evidence: Evidence, retained: list[ValuationInput]
) -> tuple[dict[str, Decimal], str]:
    values: dict[str, Decimal] = {}
    currencies = set()
    for a in scenario.assumptions:
        unit = (
            "CURRENCY"
            if a.semantic.startswith("fcff_") or a.semantic in {"net_debt", "starting_fcff"}
            else "SHARES"
            if a.semantic == "diluted_shares"
            else "YEARS"
            if a.semantic == "horizon"
            else "MULTIPLE_X"
            if a.semantic in {"exit_pe", "exit_pb"}
            else "DECIMAL_RATE"
        )
        require(a.unit == unit, "ASSUMPTION_UNIT_MISMATCH")
        if unit not in {"CURRENCY", "SHARES"}:
            require(a.scale == 1, "NON_MONETARY_SCALE_MISMATCH")
        if a.currency:
            currencies.add(a.currency)
        if a.evidence:
            fact = evidence.resolve(a.evidence, a.semantic.upper())
            retained.append(fact)
            v = validate_fact(fact, evidence.request.reporting_scope)
            require(
                v == a.value * a.scale and fact.currency == a.currency,
                "ASSUMPTION_EVIDENCE_MISMATCH",
            )
        values[a.semantic] = a.value * a.scale
    require(len(currencies) <= 1, "CURRENCY_MISMATCH")
    return values, next(iter(currencies), "")


def need(values: dict[str, Decimal], *names: str) -> None:
    require(all(n in values for n in names), "MISSING_SCENARIO_ASSUMPTION", "INSUFFICIENT_INPUT")


def horizon(values: dict[str, Decimal]) -> int:
    need(values, "horizon")
    n = values["horizon"]
    require(n >= 1 and n == n.to_integral_value(), "INVALID_FORECAST_HORIZON")
    require(n <= 10000, "NUMERICAL_HORIZON_LIMIT")
    return int(n)


def dcf(values: dict[str, Decimal], flows: list[Decimal]) -> dict[str, Decimal]:
    need(values, "wacc", "terminal_growth", "net_debt", "diluted_shares")
    w, g, shares = values["wacc"], values["terminal_growth"], values["diluted_shares"]
    require(w > g and w > -1 and g > -1, "INVALID_TERMINAL_RATES")
    require(shares > 0, "NONPOSITIVE_DILUTED_SHARES")
    out = {}
    pv = Decimal(0)
    for t, cash in enumerate(flows, 1):
        discount = (1 + w) ** t
        out[f"fcff_{t}"] = cash
        out[f"discount_denominator_{t}"] = discount
        out[f"pv_fcff_{t}"] = cash / discount
        pv += cash / discount
    out["terminal_fcff"] = flows[-1] * (1 + g)
    out["terminal_value"] = out["terminal_fcff"] / (w - g)
    out["pv_terminal"] = out["terminal_value"] / (1 + w) ** len(flows)
    out["enterprise_value"] = pv + out["pv_terminal"]
    out["equity_value"] = out["enterprise_value"] - values["net_debt"]
    out["scenario_value_per_share"] = out["equity_value"] / shares
    return out


def reverse(
    values: dict[str, Decimal], price: Decimal, n: int, policy: SolverPolicy
) -> tuple[Decimal, SolverTrace]:
    need(values, "starting_fcff", "lower_growth", "upper_growth")
    base, low, high = values["starting_fcff"], values["lower_growth"], values["upper_growth"]
    require(base != 0, "ZERO_FCFF_GROWTH_NOT_IDENTIFIABLE")
    require(-1 < low < high, "INVALID_GROWTH_BRACKET")

    def residual(growth: Decimal) -> Decimal:
        return (
            dcf(values, [base * (1 + growth) ** t for t in range(1, n + 1)])[
                "scenario_value_per_share"
            ]
            - price
        )

    lower, upper = low, high
    fl, fh = residual(low), residual(high)
    original_fl, original_fh = fl, fh

    def trace(i: int, r: Decimal, reason: str) -> SolverTrace:
        return SolverTrace(
            policy=policy,
            lower=lower,
            upper=upper,
            lower_residual=original_fl,
            upper_residual=original_fh,
            iterations=i,
            final_lower=low,
            final_upper=high,
            final_residual=r,
            termination=reason,
        )

    if abs(fl) <= policy.residual_tolerance:
        return low, trace(0, fl, "LOWER_ENDPOINT")
    if abs(fh) <= policy.residual_tolerance:
        return high, trace(0, fh, "UPPER_ENDPOINT")
    if (fl > 0) == (fh > 0):
        raise Refusal(
            "INVALID_INPUT", "NO_SOLUTION_IN_DECLARED_BRACKET", trace(0, fl, "NO_BRACKET")
        )
    fm = fl
    for i in range(1, policy.max_iterations + 1):
        mid = (low + high) / 2
        fm = residual(mid)
        if fm == 0 or (
            abs(fm) <= policy.residual_tolerance and high - low <= policy.growth_tolerance
        ):
            return mid, trace(i, fm, "EXACT_ROOT" if fm == 0 else "TOLERANCES_SATISFIED")
        if mid in {low, high}:
            break
        if (fl > 0) == (fm > 0):
            low, fl = mid, fm
        else:
            high, fh = mid, fm
    raise Refusal(
        "INVALID_INPUT", "SOLVER_DID_NOT_CONVERGE", trace(i, fm, "ITERATION_OR_PRECISION_LIMIT")
    )


def stored(
    name: str, value: Decimal, unit: str, currency: str | None, method: str
) -> ValuationValue:
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        persisted = +value
    with localcontext(Context(prec=max(70, value.adjusted() + 5), rounding=ROUND_HALF_EVEN)):
        display = value.quantize(Decimal(".01"))
    return ValuationValue.model_validate(
        dict(
            name=name,
            value=persisted,
            display_value=display,
            unit=unit,
            currency=currency,
            numerical_method=method,
        )
    )


def evaluate(
    request: ValuationRequest,
    evidence: Evidence,
    precision: ValuationPrecision = DEFAULT_PRECISION,
    definitions: tuple[ValuationDefinition, ...] = DEFINITIONS,
) -> ValuationSnapshot:
    snapshot_id = uuid4()
    results = []
    with localcontext(Context(prec=precision.working_digits, rounding=ROUND_HALF_EVEN)):
        for index, selection in enumerate(request.selections):
            definition = next(d for d in definitions if d.method == selection.method)
            retained: list[ValuationInput] = []
            issues = [issue("POINT_IN_TIME_AVAILABILITY_UNVERIFIED")]
            outputs: list[ValuationValue] = []
            solver = None
            status = "AVAILABLE"
            scenario = selection.scenario
            try:
                # Retain pinned facts even for numerical refusals.
                price_item = evidence.resolve(selection.price, "PRICE") if selection.price else None
                denominator_semantic = (
                    "BVPS"
                    if selection.method in {"PRICE_TO_BOOK", "REQUIRED_BVPS_CAGR_FOR_RETURN"}
                    else "EPS"
                )
                denominator = (
                    evidence.resolve(selection.denominator, denominator_semantic)
                    if selection.denominator
                    else None
                )
                retained.extend(v for v in (price_item, denominator) if v is not None)
                valid_profile = {IT: "NON_FINANCIAL", BANK: "BANK"}.get(request.profile_id)
                require(
                    request.profile_version == 1
                    and valid_profile is not None
                    and request.classification.classification == valid_profile,
                    "UNKNOWN_OR_INCOMPATIBLE_PROFILE",
                    "NOT_APPLICABLE",
                )
                require(
                    request.profile_id in definition.profiles,
                    "METHOD_NOT_APPLICABLE_TO_PROFILE",
                    "NOT_APPLICABLE",
                )
                price, denominator_value, currency = Decimal(0), Decimal(0), None
                if selection.method != "FCFF_DCF":
                    require(price_item is not None, "PINNED_PRICE_REQUIRED", "INSUFFICIENT_INPUT")
                    assert price_item
                    price = validate_fact(price_item, request.reporting_scope)
                    require(price > 0, "NONPOSITIVE_PRICE")
                    currency = price_item.currency
                    issues.append(issue("UNVERIFIED_ADJUSTMENT"))
                if selection.method in {METHODS[0], METHODS[1], METHODS[4], METHODS[5]}:
                    require(
                        denominator is not None, "PINNED_DENOMINATOR_REQUIRED", "INSUFFICIENT_INPUT"
                    )
                    assert denominator
                    denominator_value = validate_fact(denominator, request.reporting_scope)
                    require(denominator.currency == currency, "CURRENCY_MISMATCH")
                    prefix = "ZERO" if denominator_value == 0 else "NEGATIVE"
                    require(
                        denominator_value > 0,
                        f"{prefix}_{denominator_semantic}_MULTIPLE_NOT_MEANINGFUL"
                        if selection.method in METHODS[:2]
                        else f"NONPOSITIVE_{denominator_semantic}_CAGR_NOT_MEANINGFUL",
                    )
                    if denominator_semantic == "EPS":
                        issues.append(
                            issue("SOURCE_REPORTED_ANNUAL_EPS_BASIC_DILUTED_BASIS_UNSPECIFIED")
                        )
                if selection.method in METHODS[:2]:
                    outputs.append(
                        stored(
                            "multiple",
                            price / denominator_value,
                            "MULTIPLE_X",
                            None,
                            "DECIMAL_DIVISION",
                        )
                    )
                else:
                    require(scenario is not None, "SCENARIO_REQUIRED", "INSUFFICIENT_INPUT")
                    assert scenario
                    v, scenario_currency = assumptions(scenario, evidence, retained)
                    n = horizon(v)
                    if selection.method in {METHODS[2], METHODS[3]}:
                        require(bool(scenario_currency), "SCENARIO_CURRENCY_REQUIRED")
                        if currency is not None:
                            require(currency == scenario_currency, "CURRENCY_MISMATCH")
                        currency = scenario_currency
                        if selection.method == "FCFF_DCF":
                            cash_keys = [
                                a.semantic
                                for a in scenario.assumptions
                                if a.semantic.startswith("fcff_")
                            ]
                            require(
                                cash_keys == [f"fcff_{i}" for i in range(1, n + 1)],
                                "FORECAST_YEARS_MUST_BE_ORDERED_CONSECUTIVE",
                            )
                            require(
                                set(v)
                                == set(cash_keys)
                                | {
                                    "horizon",
                                    "wacc",
                                    "terminal_growth",
                                    "net_debt",
                                    "diluted_shares",
                                },
                                "UNUSED_SCENARIO_ASSUMPTION",
                            )
                            out = dcf(v, [v[k] for k in cash_keys])
                            if out["terminal_fcff"] <= 0:
                                issues.append(issue("NONPOSITIVE_TERMINAL_FCFF_SCENARIO"))
                            for key in ("enterprise_value", "equity_value"):
                                if out[key] < 0:
                                    issues.append(issue("NEGATIVE_" + key.upper() + "_SCENARIO"))
                            for name, value in out.items():
                                unit = (
                                    "NUMBER"
                                    if name.startswith("discount_")
                                    else "CURRENCY_PER_SHARE"
                                    if name == "scenario_value_per_share"
                                    else "CURRENCY"
                                )
                                outputs.append(
                                    stored(
                                        name,
                                        value,
                                        unit,
                                        currency if unit != "NUMBER" else None,
                                        "DECIMAL_INTEGER_POWER",
                                    )
                                )
                        else:
                            require(
                                set(v)
                                == {
                                    "horizon",
                                    "wacc",
                                    "terminal_growth",
                                    "net_debt",
                                    "diluted_shares",
                                    "starting_fcff",
                                    "lower_growth",
                                    "upper_growth",
                                },
                                "UNUSED_OR_MISSING_REVERSE_ASSUMPTION",
                            )
                            if v["starting_fcff"] < 0:
                                issues.append(issue("NEGATIVE_STARTING_FCFF_SCENARIO"))
                            root, solver = reverse(v, price, n, scenario.solver)
                            outputs.append(
                                stored(
                                    "implied_growth",
                                    root * 100,
                                    "PERCENT",
                                    None,
                                    "DECIMAL_BISECTION",
                                )
                            )
                    else:
                        exit_key = "exit_pb" if denominator_semantic == "BVPS" else "exit_pe"
                        need(v, "required_return", exit_key)
                        require(
                            set(v) == {"horizon", "required_return", exit_key},
                            "UNUSED_SCENARIO_ASSUMPTION",
                        )
                        require(v["required_return"] > -1, "INVALID_REQUIRED_RETURN")
                        require(v[exit_key] > 0, "NONPOSITIVE_EXIT_MULTIPLE")
                        future = price * (1 + v["required_return"]) ** n
                        future_metric = future / v[exit_key]
                        growth = root_growth(future_metric / denominator_value, Decimal(n)) * 100
                        outputs.extend(
                            (
                                stored(
                                    "required_future_price",
                                    future,
                                    "CURRENCY_PER_SHARE",
                                    currency,
                                    "DECIMAL_INTEGER_POWER",
                                ),
                                stored(
                                    "required_future_" + denominator_semantic.lower(),
                                    future_metric,
                                    "CURRENCY_PER_SHARE",
                                    currency,
                                    "DECIMAL_DIVISION",
                                ),
                                stored("required_growth", growth, "PERCENT", None, "DECIMAL_ROOT"),
                            )
                        )
                        issues.append(issue("PRICE_RETURN_ONLY_EXCLUDES_DISTRIBUTIONS"))
            except (Refusal, ArithmeticError) as exc:
                status = exc.status if isinstance(exc, Refusal) else "INVALID_INPUT"
                issues.append(
                    issue(exc.code if isinstance(exc, Refusal) else "NUMERICAL_RANGE_ERROR", True)
                )
                solver = exc.trace if isinstance(exc, Refusal) else solver
                outputs = []
            results.append(
                ValuationResult.model_validate(
                    dict(
                        id=uuid4(),
                        snapshot_id=snapshot_id,
                        selection_index=index,
                        definition=definition,
                        status=status,
                        inputs=tuple(retained),
                        assumption_ids=tuple(a.assumption_id for a in scenario.assumptions)
                        if scenario
                        else (),
                        scenario_id=scenario.scenario_id if scenario else None,
                        values=tuple(outputs),
                        solver=solver,
                        issues=tuple(issues),
                    )
                )
            )
    return ValuationSnapshot(
        id=snapshot_id,
        request=request,
        evaluated_at=datetime.now(UTC),
        precision=precision,
        sources=evidence.sources(),
        results=tuple(results),
        source_reported_multiples=tuple(
            o
            for o in evidence.financial.top_metrics
            if o.metric_id in {"price_earnings", "price_book"}
        )
        if evidence.financial
        else (),
    )
