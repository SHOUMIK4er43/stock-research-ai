"""Small deterministic formula registry; no provider imports or source acquisition."""

import calendar
import hashlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from uuid import uuid4

from stock_research_ai.domain.calculations import (
    CalculationRequest,
    CalculationResult,
    CalculationSnapshot,
    CalculationSpec,
    FormulaDefinition,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.financial import FinancialObservation

FORMULAS = {
    "OPERATING_MARGIN_CHANGE_BPS": FormulaDefinition(
        id="OPERATING_MARGIN_CHANGE_BPS",
        expression=(
            "(current_unrounded_operating_margin_percent - "
            "prior_unrounded_operating_margin_percent) * 100"
        ),
    ),
    "SOURCE_METRIC_GROWTH": FormulaDefinition(
        id="SOURCE_METRIC_GROWTH", expression="(current - prior) / abs(prior) * 100"
    ),
    "OPERATING_MARGIN": FormulaDefinition(
        id="OPERATING_MARGIN", expression="operating_profit / sales * 100"
    ),
    "AVERAGE_ASSETS_RETURN": FormulaDefinition(
        id="AVERAGE_ASSETS_RETURN",
        expression=(
            "annual_net_profit / ((prior_fiscal_end_assets + current_fiscal_end_assets) / 2) * 100"
        ),
    ),
}


class InputFailure(Exception):
    def __init__(self, status: str, issue: str) -> None:
        self.status = status
        self.issue = issue


def period_key(o: FinancialObservation) -> str | None:
    """Read source year/month; never create start dates or fiscal labels."""
    pieces = o.period.fiscal_label.split()
    label = None
    if len(pieces) == 2 and pieces[0] in list(calendar.month_abbr) and pieces[1].isdigit():
        label = f"{int(pieces[1]):04d}-{list(calendar.month_abbr).index(pieces[0]):02d}"
    end = o.period.end_date.strftime("%Y-%m") if o.period.end_date else None
    if label and end and label != end:
        raise InputFailure("INVALID_INPUT", "CONFLICTING_PERIOD_LABEL_AND_DATE")
    return end or label


def previous(key: str, months: int) -> str:
    year, month = map(int, key.split("-"))
    index = year * 12 + month - 1 - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def calculate(source: CompanyExtractionResult, request: CalculationRequest) -> CalculationSnapshot:
    if (
        not source.company
        or source.company.company_id != request.company_id
        or source.metadata.id != request.extraction_id
    ):
        raise ValueError("Request must identify the exact source company and extraction")
    artifacts = {a.id for a in source.raw_artifacts}
    if request.sector.provenance and request.sector.provenance.raw_artifact_id not in artifacts:
        raise ValueError("Sector mapping must reference this extraction's evidence")
    snapshot_id, now = uuid4(), datetime.now(UTC)
    results = []
    for spec in request.calculations:
        used: list[FinancialObservation] = []
        issues = ["PINNED_EXTRACTION_NOT_A_LATEST_OR_HISTORICAL_AVAILABILITY_ASSERTION"]
        status = "AVAILABLE"
        value = display = None
        numerator = denominator = None
        label: str = spec.formula_id
        try:
            ratio = evaluate(source, request, spec, used, issues)
            numerator, denominator = ratio.numerator, ratio.denominator
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                value = Decimal(numerator) / Decimal(denominator)
                display = value.quantize(Decimal("0.01"))
            if spec.formula_id == "SOURCE_METRIC_GROWTH":
                label = f"Growth of source-reported {used[0].original_label or used[0].metric}"
        except InputFailure as exc:
            status = exc.status
            issues.append(exc.issue)
        except ArithmeticError:
            status = "INVALID_INPUT"
            issues.append("DECIMAL_REPRESENTATION_OUT_OF_RANGE")
            value = display = None
            numerator = denominator = None
        results.append(
            CalculationResult.model_validate(
                dict(
                    snapshot_id=snapshot_id,
                    extraction_id=request.extraction_id,
                    metric_id=spec.metric_id,
                    label=label,
                    formula=FORMULAS[spec.formula_id],
                    alignment=request.alignment,
                    sector=request.sector,
                    period=spec.current_period,
                    comparison=spec.comparison,
                    reporting_scope=request.reporting_scope,
                    unit="BASIS_POINTS"
                    if spec.formula_id == "OPERATING_MARGIN_CHANGE_BPS"
                    else "PERCENT",
                    calculated_at=now,
                    status=status,
                    value=value,
                    display_value=display,
                    exact_numerator=numerator,
                    exact_denominator=denominator,
                    inputs=tuple(used),
                    issues=tuple(dict.fromkeys(issues)),
                )
            )
        )
    return CalculationSnapshot(
        id=snapshot_id,
        request=request,
        source_payload_sha256=hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
        calculated_at=now,
        results=tuple(results),
    )


def evaluate(
    source: CompanyExtractionResult,
    request: CalculationRequest,
    spec: CalculationSpec,
    used: list[FinancialObservation],
    issues: list[str],
) -> Fraction:
    sector = request.sector.classification
    if sector in {"UNKNOWN", "OTHER_FINANCIAL"}:
        raise InputFailure("NOT_APPLICABLE", "SECTOR_CONVENTION_NOT_SUPPORTED")
    if request.reporting_scope == "unknown":
        raise InputFailure("INVALID_INPUT", "UNKNOWN_SCOPE")
    if source.company and source.company.reporting_scope != request.reporting_scope:
        raise InputFailure("INVALID_INPUT", "INCOMPATIBLE_SCOPE")
    if spec.formula_id == "OPERATING_MARGIN" and sector != "NON_FINANCIAL":
        raise InputFailure("NOT_APPLICABLE", "INDUSTRIAL_MARGIN_NOT_APPLICABLE_TO_BANK")
    if spec.formula_id == "SOURCE_METRIC_GROWTH" and (
        (spec.source_metric in {"deposits", "revenue"} and sector != "BANK")
        or (spec.source_metric == "sales" and sector != "NON_FINANCIAL")
    ):
        raise InputFailure("NOT_APPLICABLE", "METRIC_SECTOR_MISMATCH")
    if spec.formula_id == "OPERATING_MARGIN_CHANGE_BPS":
        margin = spec.model_copy(update={"formula_id": "OPERATING_MARGIN"})
        current = evaluate(source, request, margin, used, issues)
        prior = evaluate(
            source,
            request,
            margin.model_copy(
                update={
                    "current_period": previous(
                        spec.current_period, 3 if spec.comparison == "QUARTERLY_SEQUENTIAL" else 12
                    )
                }
            ),
            used,
            issues,
        )
        return (current - prior) * 100
    annual = spec.comparison == "ANNUAL_YOY"
    month = int(spec.current_period[5:])
    fiscal_end = request.alignment.fiscal_year_end_month
    if (annual and month != fiscal_end) or (not annual and (month - fiscal_end) % 3):
        raise InputFailure("INVALID_INPUT", "PERIOD_OUTSIDE_DECLARED_FISCAL_ALIGNMENT")
    if spec.formula_id == "AVERAGE_ASSETS_RETURN" and not annual:
        raise InputFailure("NOT_APPLICABLE", "AVERAGE_ASSETS_RETURN_REQUIRES_ANNUAL_DURATION")
    financials = source.profit_loss if annual else source.quarterly
    frequency = "annual" if annual else "quarterly"

    def take(metric: str, key: str, instant: bool = False) -> Fraction:
        rows = source.balance_sheet if instant else financials
        candidates = [o for o in rows if o.metric_id == metric and period_key(o) == key]
        if not candidates:
            raise InputFailure("INSUFFICIENT_INPUT", f"MISSING_OBSERVATION:{metric}:{key}")
        if len(candidates) != 1:
            raise InputFailure("INVALID_INPUT", f"DUPLICATE_OBSERVATION:{metric}:{key}")
        o = candidates[0]
        if any(prior.id == o.id for prior in used):
            raise InputFailure("INVALID_INPUT", "DUPLICATE_INPUT_OBSERVATION_ID")
        used.append(o)
        artifact = next(a for a in source.raw_artifacts if a.id == o.provenance.raw_artifact_id)
        retrieval = next(r for r in source.retrievals if r.id == artifact.retrieval_id)
        if not artifact.complete or retrieval.status != "success":
            raise InputFailure("INVALID_INPUT", "INPUT_EVIDENCE_INCOMPLETE_OR_UNAVAILABLE")
        if o.mapping_status != "mapped":
            raise InputFailure("INVALID_INPUT", "UNVERIFIED_METRIC_MAPPING")
        if o.reporting_scope != request.reporting_scope:
            raise InputFailure("INVALID_INPUT", "INCOMPATIBLE_SCOPE")
        if o.period.kind != ("instant" if instant else "duration") or (
            o.period.frequency != ("point_in_time" if instant else frequency)
        ):
            raise InputFailure("INVALID_INPUT", "INCOMPATIBLE_PERIOD_KIND_OR_FREQUENCY")
        if instant and o.period.end_date is None:
            raise InputFailure("INVALID_INPUT", "INSTANT_DATE_UNKNOWN")
        if (
            o.period.end_date
            and o.period.end_date.day
            != calendar.monthrange(o.period.end_date.year, o.period.end_date.month)[1]
        ):
            raise InputFailure("INVALID_INPUT", "PERIOD_END_NOT_DECLARED_MONTH_END")
        if not instant and o.period.start_date is not None:
            expected_start = previous(key, 11 if annual else 2) + "-01"
            if o.period.start_date.isoformat() != expected_start:
                raise InputFailure("INVALID_INPUT", "DURATION_CONFLICTS_WITH_ALIGNMENT_POLICY")
        if not instant and o.period.start_date is None:
            issues.append("DURATION_START_UNKNOWN_USING_DECLARED_PERIOD_ALIGNMENT_V1")
        if o.value_status != "available":
            state = {"not_applicable": "NOT_APPLICABLE", "parse_error": "INVALID_INPUT"}.get(
                o.value_status, "INSUFFICIENT_INPUT"
            )
            raise InputFailure(state, f"INPUT_STATUS:{o.value_status}")
        if o.value is None or not o.value.is_finite():
            raise InputFailure("INVALID_INPUT", "MALFORMED_VALUE")
        if o.unit.kind == "unknown":
            raise InputFailure("INVALID_INPUT", "UNKNOWN_UNIT")
        if o.unit.kind != "money":
            raise InputFailure("INVALID_INPUT", "FINANCIAL_FORMULA_REQUIRES_MONETARY_INPUT")
        if used[0].unit.kind != o.unit.kind or used[0].unit.currency != o.unit.currency:
            raise InputFailure("INVALID_INPUT", "INCOMPATIBLE_UNIT_OR_CURRENCY")
        return Fraction(o.value) * Fraction(o.unit.scale)

    if spec.formula_id == "SOURCE_METRIC_GROWTH":
        if spec.source_metric == "deposits" and not annual:
            raise InputFailure("NOT_APPLICABLE", "DEPOSIT_PROOF_REQUIRES_ANNUAL_END_POINTS")
        current = take(spec.source_metric, spec.current_period, spec.source_metric == "deposits")
        prior_key = previous(
            spec.current_period, 3 if spec.comparison == "QUARTERLY_SEQUENTIAL" else 12
        )
        prior = take(spec.source_metric, prior_key, spec.source_metric == "deposits")
        if used[0].original_label != used[1].original_label:
            raise InputFailure("INVALID_INPUT", "SOURCE_METRIC_DEFINITION_CHANGED")
        if prior == 0:
            raise InputFailure("DIVISION_BY_ZERO", "ZERO_PRIOR_VALUE")
        if prior < 0:
            issues.append("NEGATIVE_BASE_ABSOLUTE_DENOMINATOR_CONVENTION")
        return (current - prior) / abs(prior) * 100
    if spec.formula_id == "OPERATING_MARGIN":
        profit = take("operating_profit", spec.current_period)
        base = take("sales", spec.current_period)
    else:
        profit = take("net_profit", spec.current_period)
        opening = take("total_assets", previous(spec.current_period, 12), True)
        closing = take("total_assets", spec.current_period, True)
        if opening < 0 or closing < 0:
            raise InputFailure("INVALID_INPUT", "NEGATIVE_ASSETS")
        base = (opening + closing) / 2
        issues.append("AVERAGE_ASSETS_RETURN_NOT_A_VALIDATED_BANK_ROA_CONVENTION")
    if base == 0:
        raise InputFailure("DIVISION_BY_ZERO", "ZERO_DENOMINATOR")
    if base < 0:
        raise InputFailure("INVALID_INPUT", "NEGATIVE_RATIO_DENOMINATOR")
    return profit / base * 100
