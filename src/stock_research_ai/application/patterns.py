"""Fixed pattern rules over verified evidence and explicit calculation prerequisites."""

import hashlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from typing import Protocol
from uuid import uuid4

from stock_research_ai.application import calculations as common
from stock_research_ai.application.ports import CalculationRepository, SemanticResolver
from stock_research_ai.application.sector_kpis import BANK, IT, PROFILES, profile_issue
from stock_research_ai.domain.calculations import (
    CalculationIssue,
    CalculationRequest,
    CalculationResult,
    CalculationSnapshot,
    CalculationSpec,
    SectorDeclaration,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.financial import FinancialObservation
from stock_research_ai.domain.patterns import (
    CalculationReference,
    ExactValue,
    Outcome,
    PatternDefinition,
    PatternExecution,
    PatternFinding,
    PatternRequest,
    PatternSnapshot,
    ThresholdPolicy,
)
from stock_research_ai.domain.sector_kpis import (
    SectorKPIEvaluation,
    SectorKPIRequest,
    SectorKPIResult,
    SectorProfile,
    SemanticResolution,
)

MARGIN_THRESHOLD = ThresholdPolicy(
    id="OPERATING_MARGIN_DETERIORATION_SYSTEM_V1",
    version=1,
    basis="SYSTEM_DEFINED",
    threshold=Decimal("-50"),
    unit="BASIS_POINTS",
    operator="LESS_THAN_OR_EQUAL",
    description=(
        "Internal review heuristic; not an industry, regulatory or institutional standard."
    ),
)

DEFINITIONS = (
    PatternDefinition(
        pattern_id="CFO_TO_NET_PROFIT",
        applicable_profiles=(IT,),
        required_semantics=("SOURCE_REPORTED_CFO", "NET_PROFIT"),
        required_calculations=("CFO_TO_NET_PROFIT",),
        annual_periods=1,
        rule_id="OBSERVE_RATIO",
        conditions=("OBSERVATION_ONLY; no threshold",),
    ),
    PatternDefinition(
        pattern_id="PROFIT_VS_CFO_DIVERGENCE",
        applicable_profiles=(IT,),
        required_semantics=("NET_PROFIT", "SOURCE_REPORTED_CFO"),
        annual_periods=2,
        rule_id="PROFIT_CFO_DIRECTION",
        conditions=(
            "current_profit > 0",
            "current_profit > prior_profit",
            "current_cfo < prior_cfo OR current_cfo < 0",
        ),
    ),
    PatternDefinition(
        pattern_id="REPEATED_NEGATIVE_CFO",
        applicable_profiles=(IT,),
        required_semantics=("SOURCE_REPORTED_CFO",),
        annual_periods=3,
        rule_id="THREE_NEGATIVE_CFO",
        conditions=("Exactly 3 consecutive annual values; each CFO < 0",),
    ),
    PatternDefinition(
        pattern_id="OPERATING_MARGIN_DETERIORATION",
        applicable_profiles=(IT,),
        required_semantics=("OPERATING_PROFIT", "SALES"),
        annual_periods=2,
        required_calculations=("OPERATING_MARGIN_CHANGE_BPS",),
        rule_id="MARGIN_THRESHOLD",
        conditions=("margin_change_bps <= threshold",),
        threshold=MARGIN_THRESHOLD,
    ),
    PatternDefinition(
        pattern_id="DEPOSIT_CONTRACTION",
        applicable_profiles=(BANK,),
        required_semantics=("DEPOSITS",),
        annual_periods=2,
        required_calculations=("DEPOSIT_GROWTH",),
        rule_id="NEGATIVE_DEPOSIT_GROWTH",
        conditions=("deposit_growth < 0 percent",),
        threshold=ThresholdPolicy(
            id="DEPOSIT_CONTRACTION_DIRECTION_V1",
            version=1,
            basis="DIRECTIONAL_CONDITION",
            threshold=Decimal(0),
            unit="PERCENT",
            operator="LESS_THAN",
            description="Direction of source-reported deposit growth; no external standard.",
        ),
    ),
    PatternDefinition(
        pattern_id="DEPOSIT_GROWTH_DECELERATION",
        applicable_profiles=(BANK,),
        required_semantics=("DEPOSITS",),
        annual_periods=3,
        required_calculations=("DEPOSIT_GROWTH_CURRENT", "DEPOSIT_GROWTH_PRIOR"),
        rule_id="OBSERVE_DEPOSIT_DIFFERENCE",
        conditions=("OBSERVATION_ONLY; no threshold",),
    ),
)


class Calculator(Protocol):
    async def calculate_financials(self, request: CalculationRequest) -> CalculationSnapshot: ...
    async def calculate_sector_kpis(self, request: SectorKPIRequest) -> SectorKPIEvaluation: ...


def digest(value: CalculationSnapshot | SectorKPIEvaluation | CompanyExtractionResult) -> str:
    return hashlib.sha256(value.model_dump_json().encode()).hexdigest()


def exact(name: str, value: Fraction, unit: str, formula: str | None = None) -> ExactValue:
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
        display = decimal.quantize(Decimal("0.01"))
    return ExactValue(
        name=name,
        numerator=value.numerator,
        denominator=value.denominator,
        value=decimal,
        display=display,
        unit=unit,
        formula=common.FORMULAS[formula] if formula else None,
    )


def fraction(value: ExactValue) -> Fraction:
    return Fraction(value.numerator, value.denominator)


def common_request(req: PatternRequest) -> CalculationRequest:
    return CalculationRequest(
        company_id=req.company_id,
        extraction_id=req.extraction_id,
        reporting_scope=req.reporting_scope,
        alignment=req.alignment,
        sector=SectorDeclaration.model_validate(
            req.classification.model_dump(exclude={"version", "effective_start", "effective_end"})
        ),
        calculations=(
            CalculationSpec(
                metric_id="CFO_TO_NET_PROFIT",
                formula_id="CFO_TO_NET_PROFIT",
                current_period=req.current_period,
            ),
        ),
    )


def roles(definition: PatternDefinition, req: PatternRequest) -> list[tuple[str, str, str, str]]:
    """Explicit semantic, extractor field, metric ID, fiscal key; no positional inference."""
    keys = [common.previous(req.current_period, n * 12) for n in range(definition.annual_periods)]
    if definition.pattern_id == "REPEATED_NEGATIVE_CFO":
        keys.reverse()
    metrics = {
        "SOURCE_REPORTED_CFO": ("cashflow", "cash_operating"),
        "NET_PROFIT": ("profit_loss", "net_profit"),
        "OPERATING_PROFIT": ("profit_loss", "operating_profit"),
        "SALES": ("profit_loss", "sales"),
        "DEPOSITS": ("balance_sheet", "deposits"),
    }
    return [
        (semantic, *metrics[semantic], key)
        for semantic in definition.required_semantics
        for key in keys
    ]


def verify_inputs(
    source: CompanyExtractionResult,
    req: PatternRequest,
    definition: PatternDefinition,
    resolver: SemanticResolver,
    used: list[FinancialObservation],
    mappings: list[SemanticResolution],
    issues: list[str],
) -> list[ExactValue]:
    if source.company and source.company.reporting_scope != req.reporting_scope:
        raise common.InputFailure("INCOMPATIBLE_SCOPE", "EXTRACTION_SCOPE_MISMATCH")
    if req.reporting_scope == "unknown":
        raise common.InputFailure("INCOMPATIBLE_SCOPE", "UNKNOWN_SCOPE")
    if int(req.current_period[5:]) != req.alignment.fiscal_year_end_month:
        raise common.InputFailure("INCOMPATIBLE_PERIOD", "FISCAL_ALIGNMENT_MISMATCH")
    bindings = resolver.resolve(source, definition.required_semantics)
    observed = []
    for semantic, field, metric, key in roles(definition, req):
        rows = getattr(source, field)
        candidates = [
            o
            for o in rows
            if (
                o.metric_id == metric
                or any(
                    m.observation_id == o.id and m.canonical_semantic == semantic for m in bindings
                )
            )
            and common.period_key(o) == key
        ]
        if not candidates:
            raise common.InputFailure("INSUFFICIENT_INPUT", f"MISSING_INPUT:{semantic}:{key}")
        links = [
            m
            for m in bindings
            if m.observation_id in {o.id for o in candidates} and m.canonical_semantic == semantic
        ]
        mappings.extend(links)
        if len(candidates) != 1 or len(links) != 1 or links[0].status != "VERIFIED":
            raise common.InputFailure(
                "UNVERIFIED_METRIC_SEMANTICS", f"UNVERIFIED_OR_DUPLICATE:{semantic}:{key}"
            )
        value = common.take_observation(
            source,
            common_request(req),
            metric,
            key,
            rows,
            used,
            issues,
            instant=field == "balance_sheet",
        )
        observed.append(
            exact(
                f"{semantic}:{key}",
                value,
                "INR" if used[-1].unit.currency == "INR" else str(used[-1].unit.currency),
            )
        )
    # Effective dates constrain every used observation with a known endpoint.
    for o in used:
        start, end, actual = (
            req.classification.effective_start,
            req.classification.effective_end,
            o.period.end_date,
        )
        if (start or end) and not actual:
            raise common.InputFailure("INCOMPATIBLE_PERIOD", "CLASSIFICATION_DATE_UNKNOWN")
        if actual and ((start and actual < start) or (end and actual > end)):
            raise common.InputFailure("NOT_APPLICABLE", "CLASSIFICATION_OUTSIDE_EFFECTIVE_PERIOD")
    return observed


def prerequisite_roles(definition: PatternDefinition, req: PatternRequest) -> list[tuple[str, str]]:
    if definition.pattern_id == "CFO_TO_NET_PROFIT":
        return [("CFO_TO_NET_PROFIT", req.current_period)]
    if definition.pattern_id == "OPERATING_MARGIN_DETERIORATION":
        return [("OPERATING_MARGIN_CHANGE_BPS", req.current_period)]
    if definition.pattern_id.startswith("DEPOSIT_"):
        return [
            ("DEPOSIT_GROWTH", common.previous(req.current_period, n * 12))
            for n in range(2 if definition.pattern_id == "DEPOSIT_GROWTH_DECELERATION" else 1)
        ]
    return []


def validate_calculation(
    snapshot: CalculationSnapshot | SectorKPIEvaluation,
    req: PatternRequest,
    metric: str,
    key: str,
    selected_metric: str,
    source: CompanyExtractionResult,
) -> CalculationReference:
    if (
        snapshot.request.extraction_id != req.extraction_id
        or snapshot.request.company_id != req.company_id
        or snapshot.request.reporting_scope != req.reporting_scope
        or snapshot.request.alignment != req.alignment
        or snapshot.source_payload_sha256 != digest(source)
    ):
        raise ValueError("Pinned calculation evidence/identity/scope/alignment mismatch")
    matches: list[CalculationResult | SectorKPIResult]
    if metric == "CFO_TO_NET_PROFIT":
        if (
            not isinstance(snapshot, CalculationSnapshot)
            or snapshot.request.sector != common_request(req).sector
        ):
            raise ValueError("Pinned calculation kind/classification mismatch")
        matches = [r for r in snapshot.results if r.metric_id == selected_metric]
        profile_id = None
        profile_version = None
    else:
        if not isinstance(snapshot, SectorKPIEvaluation) or (
            snapshot.request.profile_id,
            snapshot.request.profile_version,
            snapshot.request.classification,
        ) != (req.profile_id, req.profile_version, req.classification):
            raise ValueError("Pinned sector calculation profile/classification mismatch")
        matches = [r for r in snapshot.results if r.definition.metric_id == selected_metric]
        profile_id, profile_version = snapshot.profile.profile_id, snapshot.profile.version
    if len(matches) != 1:
        raise ValueError("Pinned metric must exist exactly once")
    result = matches[0]
    formula = result.formula if isinstance(result, CalculationResult) else result.definition.formula
    if formula is None:
        raise ValueError("Pinned calculation has no formula")
    expected_formula = "SOURCE_METRIC_GROWTH" if metric == "DEPOSIT_GROWTH" else metric
    if (
        selected_metric != metric
        or result.period != key
        or result.comparison != "ANNUAL_YOY"
        or formula != common.FORMULAS[expected_formula]
    ):
        raise ValueError("Pinned metric/formula/period mismatch")
    originals = {
        o.id: o
        for field in (source.profit_loss, source.balance_sheet, source.cashflow)
        for o in field
    }
    if any(originals.get(o.id) != o for o in result.inputs):
        raise ValueError("Pinned calculation inputs differ from retained evidence")
    return CalculationReference(
        role=f"{metric}:{key}",
        snapshot_id=snapshot.id,
        metric_id=selected_metric,
        extraction_id=req.extraction_id,
        formula=formula,
        profile_id=profile_id,
        profile_version=profile_version,
        payload_sha256=digest(snapshot),
    )


def outcome(definition: PatternDefinition, values: list[ExactValue]) -> tuple[Outcome, str]:
    """Fixed comparisons only; all numeric preparation is explicit and retained."""
    rule = definition.rule_id
    numbers = [fraction(v) for v in values]
    if rule == "OBSERVE_RATIO":
        return "OBSERVATION_ONLY", "Source-reported operating cash flow divided by net profit."
    if rule == "OBSERVE_DEPOSIT_DIFFERENCE":
        return "OBSERVATION_ONLY", "Difference between consecutive annual deposit growth rates."
    if rule == "PROFIT_CFO_DIRECTION":
        profit, prior_profit, cfo, prior_cfo = numbers
        triggered = profit > 0 and profit > prior_profit and (cfo < prior_cfo or cfo < 0)
        statement = (
            "Net profit increased while source-reported operating cash flow declined."
            if cfo < prior_cfo
            else "Net profit increased while source-reported operating cash flow was negative."
        )
        return (
            ("TRIGGERED", statement)
            if triggered
            else ("NOT_TRIGGERED", "The specified profit/CFO directional condition was not met.")
        )
    if rule == "THREE_NEGATIVE_CFO":
        return (
            ("TRIGGERED", "Three consecutive comparable annual CFO observations were negative.")
            if all(n < 0 for n in numbers)
            else (
                "NOT_TRIGGERED",
                "The three consecutive annual CFO observations were not all negative.",
            )
        )
    if rule in {"MARGIN_THRESHOLD", "NEGATIVE_DEPOSIT_GROWTH"}:
        if definition.threshold is None:
            raise ValueError("Threshold missing")
        value, boundary = numbers[-1], Fraction(definition.threshold.threshold)
        triggered = (
            value <= boundary
            if definition.threshold.operator == "LESS_THAN_OR_EQUAL"
            else value < boundary
        )
        return (
            "TRIGGERED" if triggered else "NOT_TRIGGERED",
            "Observed value met the recorded review condition."
            if triggered
            else "Observed value did not meet the recorded review condition.",
        )
    raise ValueError("Unimplemented rule")


async def evaluate(
    source: CompanyExtractionResult,
    req: PatternRequest,
    resolver: SemanticResolver,
    repository: CalculationRepository,
    calculator: Calculator,
    definitions: tuple[PatternDefinition, ...] = DEFINITIONS,
    profiles: tuple[SectorProfile, ...] = PROFILES,
) -> PatternSnapshot:
    if (
        not source.company
        or source.company.company_id != req.company_id
        or source.metadata.id != req.extraction_id
    ):
        raise ValueError("Pattern request must pin exact company/extraction")
    if req.classification.provenance and req.classification.provenance.raw_artifact_id not in {
        a.id for a in source.raw_artifacts
    }:
        raise ValueError("Classification provenance outside extraction")
    selected_profiles = [
        p for p in profiles if (p.profile_id, p.version) == (req.profile_id, req.profile_version)
    ]
    profile = selected_profiles[0] if len(selected_profiles) == 1 else None
    snapshot_id, now = uuid4(), datetime.now(UTC)
    created, findings, cache, consumed = [], [], {}, set()
    for selection in req.patterns:
        choices = [
            d
            for d in definitions
            if (d.pattern_id, d.version) == (selection.pattern_id, selection.version)
        ]
        if len(choices) != 1:
            raise ValueError("Unknown pattern definition/version")
        definition = choices[0]
        baseline = next((d for d in DEFINITIONS if d.pattern_id == definition.pattern_id), None)
        if (
            not baseline
            or definition.rule_id != baseline.rule_id
            or definition.rule_version != 1
            or definition.annual_periods != baseline.annual_periods
            or definition.conditions != baseline.conditions
        ):
            raise ValueError("Unsupported rule definition")
        used: list[FinancialObservation] = []
        mappings: list[SemanticResolution] = []
        issues: list[str] = []
        references: list[CalculationReference] = []
        values: list[ExactValue] = []
        state, result_outcome, statement = "AVAILABLE", None, ""
        try:
            issue = profile_issue(req.classification.classification, profile)
            if issue:
                raise common.InputFailure("NOT_APPLICABLE", issue)
            if req.profile_id not in definition.applicable_profiles:
                raise common.InputFailure("NOT_APPLICABLE", "PATTERN_NOT_APPLICABLE_TO_PROFILE")
            values = verify_inputs(source, req, definition, resolver, used, mappings, issues)
            for metric, key in prerequisite_roles(definition, req):
                role = f"{metric}:{key}"
                if role not in cache:
                    if req.calculation_mode == "PINNED":
                        pin = next((p for p in req.pinned_calculations if p.role == role), None)
                        if pin is None:
                            raise ValueError("Required pinned calculation absent")
                        candidate = await repository.get_calculation(pin.snapshot_id)
                        if candidate is None:
                            raise ValueError("Pinned calculation snapshot absent")
                        selected_metric = pin.metric_id
                        consumed.add(role)
                    else:
                        if metric == "CFO_TO_NET_PROFIT":
                            candidate = await calculator.calculate_financials(common_request(req))
                        else:
                            candidate = await calculator.calculate_sector_kpis(
                                SectorKPIRequest(
                                    company_id=req.company_id,
                                    extraction_id=req.extraction_id,
                                    classification=req.classification,
                                    profile_id=req.profile_id,
                                    profile_version=req.profile_version,
                                    reporting_scope=req.reporting_scope,
                                    alignment=req.alignment,
                                    current_period=key,
                                    kpis=(metric,),
                                )
                            )
                        created.append(candidate.id)
                        selected_metric = metric
                    reference = validate_calculation(
                        candidate, req, metric, key, selected_metric, source
                    )
                    cache[role] = (candidate, reference)
                candidate, reference = cache[role]
                references.append(reference)
                calculated: CalculationResult | SectorKPIResult
                if isinstance(candidate, CalculationSnapshot):
                    calculated = next(r for r in candidate.results if r.metric_id == metric)
                    issues.extend(calculated.issues)
                else:
                    calculated = next(
                        r for r in candidate.results if r.definition.metric_id == metric
                    )
                    issues.extend(i.code for i in calculated.issues)
                if calculated.status != "AVAILABLE":
                    raise common.InputFailure(calculated.status, "PREREQUISITE_NOT_AVAILABLE")
                assert (
                    calculated.exact_numerator is not None
                    and calculated.exact_denominator is not None
                )
                values.append(
                    exact(
                        role,
                        Fraction(calculated.exact_numerator, calculated.exact_denominator),
                        "RATIO_X"
                        if metric == "CFO_TO_NET_PROFIT"
                        else "PERCENT"
                        if metric == "DEPOSIT_GROWTH"
                        else "BASIS_POINTS",
                        reference.formula.id,
                    )
                )
            if definition.pattern_id == "DEPOSIT_GROWTH_DECELERATION":
                values.append(
                    exact(
                        "DEPOSIT_GROWTH_DECELERATION",
                        common.percentage_point_difference(
                            fraction(values[-2]), fraction(values[-1])
                        ),
                        "PERCENTAGE_POINTS",
                        "PERCENTAGE_POINT_DIFFERENCE",
                    )
                )
            rule_values = values if not references else [values[-1]]
            result_outcome, statement = outcome(definition, rule_values)
        except common.InputFailure as exc:
            state = "INVALID_INPUT" if exc.status == "DIVISION_BY_ZERO" else exc.status
            if "SCOPE" in exc.issue:
                state = "INCOMPATIBLE_SCOPE"
            elif (
                any(k in exc.issue for k in ("PERIOD", "DURATION", "ALIGNMENT"))
                and state == "INVALID_INPUT"
            ):
                state = "INCOMPATIBLE_PERIOD"
            statement = exc.issue
            issues.append(exc.issue)
        except ArithmeticError:
            state = "INVALID_INPUT"
            result_outcome = None
            statement = "NUMERIC_REPRESENTATION_OUT_OF_RANGE"
            issues.append(statement)
        findings.append(
            PatternFinding.model_validate(
                dict(
                    id=uuid4(),
                    snapshot_id=snapshot_id,
                    definition=definition,
                    evaluation_status=state,
                    rule_outcome=result_outcome,
                    observed=values,
                    inputs=used,
                    mappings=mappings,
                    calculations=references,
                    statement=statement,
                    issues=[
                        CalculationIssue(
                            code=i,
                            severity="ERROR"
                            if i == statement and state != "AVAILABLE"
                            else "LIMITATION",
                            message=i,
                        )
                        for i in dict.fromkeys(
                            [*issues, "PINNED_EVIDENCE_NOT_HISTORICAL_AVAILABILITY_ASSERTION"]
                        )
                    ],
                )
            )
        )
    if req.calculation_mode == "PINNED" and consumed != {p.role for p in req.pinned_calculations}:
        raise ValueError("Unused pinned calculation references")
    referenced = tuple(dict.fromkeys(r.snapshot_id for f in findings for r in f.calculations))
    return PatternSnapshot(
        id=snapshot_id,
        request=req,
        profile=profile,
        evaluated_at=now,
        source_payload_sha256=digest(source),
        execution=PatternExecution(
            calculation_mode=req.calculation_mode,
            created_snapshot_ids=tuple(created),
            referenced_snapshot_ids=referenced,
        ),
        findings=tuple(findings),
    )
