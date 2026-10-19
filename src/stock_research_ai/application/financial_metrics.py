"""DETAILED_RESEARCH_V1 Box 5: deterministic calculations over Box 4 canonical data.

One Frame = one reporting basis in one currency. A value is looked up by (metric, exact
Box 4 period key), so standalone/consolidated, quarter/YTD, FY/TTM and currencies can never
meet. Every calculation goes through Engine.emit, which:
  * resolves its FormulaDefinition (financial_formulas.FORMULAS) and checks sector and
    period compatibility,
  * records the exact canonical inputs with their Box 4 lineage (provenance), the input
    periods and the calculated metrics it builds on,
  * takes the weakest input confidence (further capped by Box 4 outcome and source
    authority) and propagates reconciliation warnings,
  * returns an explicit status, never a number, when the inputs cannot support one.
Averages use BOTH the opening and the closing balance or nothing; a missing value is never
zero.
"""

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID, uuid5

from stock_research_ai.application.financial_formulas import (
    DUPONT_TOLERANCE_PP,
    FORMULAS,
    Refused,
    Result,
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
from stock_research_ai.application.financial_parsing import period_from
from stock_research_ai.application.financial_reconciliation import extraction_integrity_issue
from stock_research_ai.domain.evidence import AUTHORITY_RANK, SourceAuthority
from stock_research_ai.domain.financial_analytics import (
    DERIVED_QUANTUM,
    AnalyticsUnit,
    CalculatedMetric,
    CalculationStatus,
    MetricInput,
    Sector,
)
from stock_research_ai.domain.financial_business import RESTRICTED_INDUSTRIES
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    Outcome,
    PeriodType,
    ReconciliationRecord,
    ReportingBasis,
    ValueKind,
)

M = CanonicalMetric
S = CalculationStatus
U = AnalyticsUnit
NS = UUID("b6f1d2a4-3c5e-4f7a-8b9c-0d1e2f3a4b5c")
#: A SOURCE_PRECEDENCE input whose losing source differs by more than this share of the
#: selected value is a material conflict: confidence LOW, warning MATERIAL_INPUT_CONFLICT.
MATERIAL_CONFLICT_SHARE = Decimal("0.10")
CONFIDENCE_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
#: When several inputs fail, the reported status is the first in this order.
STATUS_PRIORITY = [
    S.INVALID_INPUT,
    S.BASIS_MISMATCH,
    S.CURRENCY_MISMATCH,
    S.PERIOD_MISMATCH,
    S.INPUT_CONFLICT,
    S.LOW_CONFIDENCE_INPUT,
    S.UNSUPPORTED_SECTOR,
    S.NOT_APPLICABLE,
    S.DIVISION_BY_ZERO,
    S.INPUT_MISSING,
    S.INSUFFICIENT_HISTORY,
]
#: an upstream calculation that produced no number leaves its dependants without an input
UPSTREAM_UNAVAILABLE = {S.NOT_APPLICABLE, S.DIVISION_BY_ZERO, S.UNSUPPORTED_SECTOR, S.INVALID_INPUT}
REVENUE = (M.REVENUE_FROM_OPERATIONS, M.REVENUE)
#: alternative groups that are different accounting definitions of one idea: a frame uses
#: one member for every period, never revenue from operations for some years and a
#: reported / provider 'Revenue' for others
SINGLE_CONCEPT_GROUPS = frozenset({REVENUE})
EPS = (M.EPS, M.DILUTED_EPS, M.BASIC_EPS)
FIXED = (M.PROPERTY_PLANT_EQUIPMENT, M.FIXED_ASSETS)
AGGREGATOR_RANK = AUTHORITY_RANK[SourceAuthority.SECONDARY_AGGREGATOR]


# --- facts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    value: Decimal
    inputs: tuple[MetricInput, ...]
    confidence: Confidence
    warnings: tuple[str, ...] = ()
    derived_from: tuple[UUID, ...] = ()
    formula_id: str | None = None


@dataclass(frozen=True)
class Missing:
    status: CalculationStatus
    what: str
    inputs: tuple[MetricInput, ...] = ()
    derived_from: tuple[UUID, ...] = ()


Got = Fact | Missing
Values = dict[str, Decimal]
Formula = Callable[[Values], Result]


def weakest(confidences: Sequence[Confidence]) -> Confidence:
    return max(confidences, key=lambda c: CONFIDENCE_RANK[c], default=Confidence.HIGH)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def shift(end: date, months: int) -> date:
    """The month-end `months` months away (quarter and fiscal year ends are month-ends)."""
    total = end.year * 12 + end.month - 1 + months
    return month_end(total // 12, total % 12 + 1)


def compare_periods(
    current: FinancialPeriod, prior: FinancialPeriod, months: int
) -> CalculationStatus | None:
    """None when `prior` is the same kind of period exactly `months` earlier."""
    if (
        current.period_type != prior.period_type
        or current.duration_months != prior.duration_months
        or current.period_type in (PeriodType.TTM, PeriodType.UNKNOWN, PeriodType.OTHER)
        or current.period_end is None
        or prior.period_end is None
        or shift(current.period_end, -months) != prior.period_end
    ):
        return S.PERIOD_MISMATCH
    return None


def quantize(value: Decimal) -> Decimal:
    return value.quantize(DERIVED_QUANTUM, rounding=ROUND_HALF_EVEN)


def first_available(*gots: Got) -> Got:
    for got in gots:
        if isinstance(got, Fact):
            return got
    missing = [g for g in gots if isinstance(g, Missing)]
    return min(missing, key=lambda m: STATUS_PRIORITY.index(m.status))


class Periods:
    """Normalised analytical periods; their keys equal the Box 4 period keys."""

    def __init__(self, fy_end_month: int | None) -> None:
        self.fy_end = fy_end_month

    def fiscal_year(self, end: date) -> int:
        if self.fy_end is None:
            return end.year
        return end.year if end.month <= self.fy_end else end.year + 1

    def fy(self, end: date) -> FinancialPeriod:
        return period_from(PeriodType.FY, 12, end, f"FY{self.fiscal_year(end)}", self.fy_end)

    def quarter(self, end: date) -> FinancialPeriod:
        period = period_from(PeriodType.QUARTER, 3, end, "quarter", self.fy_end)
        label = (
            f"Q{period.fiscal_quarter} FY{period.fiscal_year}"
            if period.fiscal_quarter
            else f"Quarter ended {end.isoformat()}"
        )
        return period.model_copy(update={"label": label})

    def pit(self, end: date) -> FinancialPeriod:
        return period_from(
            PeriodType.POINT_IN_TIME, None, end, f"As at {end.isoformat()}", self.fy_end
        )

    @staticmethod
    def ttm() -> FinancialPeriod:
        return FinancialPeriod(period_type=PeriodType.TTM, label="TTM", duration_months=12)


class Frame:
    """Box 4 canonical values for one basis in one currency, indexed by exact period."""

    def __init__(
        self,
        data: CanonicalFinancialDataset,
        basis: ReportingBasis,
        currency: Currency,
        max_values: int,
    ) -> None:
        self.basis = basis
        self.currency = currency
        self.truncated = False
        self.observations = {
            o.observation_id: (o.source.source_type, o.authority, o.source.locator)
            for o in data.observations
        }
        self.records: dict[UUID, ReconciliationRecord] = {
            r.record_id: r for r in data.reconciliation
        }
        self.values: dict[tuple[CanonicalMetric, str], CanonicalFinancialValue] = {}
        rejected = {o.observation_id: issue for o in data.observations
                    if (issue := extraction_integrity_issue(o)) is not None}
        self.invalid_values: dict[tuple[CanonicalMetric, str], str] = {}
        self.other_currency: set[tuple[CanonicalMetric, str]] = set()
        consumed = 0
        for value in sorted(data.canonical_values, key=lambda v: str(v.canonical_value_id)):
            if value.reporting_basis != basis or value.value_kind not in (
                ValueKind.MONEY,
                ValueKind.PER_SHARE,
            ):
                continue
            key = (value.metric, value.period.key())
            if value.selected_observation_id in rejected:
                self.invalid_values[key] = rejected[value.selected_observation_id]
                continue
            if value.currency != currency:
                self.other_currency.add(key)
                continue
            if consumed >= max_values:
                self.truncated = True
                continue
            consumed += 1
            self.values[key] = value
        self.consumed = consumed
        self.period_keys = {key for _, key in self.values}
        self._concepts: dict[tuple[CanonicalMetric, ...], tuple[CanonicalMetric, ...]] = {}
        self.unresolved: dict[tuple[CanonicalMetric, str], ReconciliationRecord] = {
            (r.metric, r.period.key()): r
            for r in data.reconciliation
            if r.reporting_basis == basis
            and r.selected_observation_id is None
            and r.currency in (currency, Currency.UNKNOWN)
        }

    def periods(self, period_type: PeriodType) -> list[date]:
        ends = {
            v.period.period_end
            for v in self.values.values()
            if v.period.period_type == period_type and v.period.period_end is not None
        }
        return sorted(ends)

    @property
    def has_ttm(self) -> bool:
        return any(v.period.period_type == PeriodType.TTM for v in self.values.values())

    def concept(self, metrics: Sequence[CanonicalMetric]) -> tuple[CanonicalMetric, ...]:
        """The ONE top-line definition this frame uses (revenue from operations and a
        reported / provider 'Revenue' are different concepts): the alternative with the
        most periods here, ties in preference order - Box 6's one-metric-per-series rule.
        Other alternative groups are looked up as given."""
        group = tuple(metrics)
        if group not in SINGLE_CONCEPT_GROUPS:
            return group
        if group not in self._concepts:
            counts = [(sum(1 for (m, _k) in self.values if m == metric), -i, metric)
                      for i, metric in enumerate(group)]
            best = max(counts)
            self._concepts[group] = (best[2],) if best[0] else group
        return self._concepts[group]

    def get(self, metrics: Sequence[CanonicalMetric], period: FinancialPeriod) -> Got:
        metrics = self.concept(metrics)
        key = period.key()
        for metric in metrics:
            value = self.values.get((metric, key))
            if value is not None:
                return self._fact(value)
        names = "/".join(m.value for m in metrics)
        what = f"{names} @ {period.label}"
        for metric in metrics:
            issue = self.invalid_values.get((metric, key))
            if issue is not None:
                return Missing(S.INVALID_INPUT, f"{what}: extraction integrity rejected ({issue})")
        for metric in metrics:
            record = self.unresolved.get((metric, key))
            if record is not None and record.outcome in (Outcome.CONFLICT, Outcome.AMBIGUOUS):
                return Missing(S.INPUT_CONFLICT, f"{what} (Box 4 {record.outcome.value})")
            if record is not None:
                return Missing(S.LOW_CONFIDENCE_INPUT, f"{what} (Box 4 {record.outcome.value})")
        if any((metric, key) in self.other_currency for metric in metrics):
            return Missing(S.CURRENCY_MISMATCH, f"{what} (only in another currency)")
        if key not in self.period_keys:
            return Missing(S.INSUFFICIENT_HISTORY, f"{what} (period not in the data)")
        return Missing(S.INPUT_MISSING, f"{what} (not reported)")

    def _fact(self, value: CanonicalFinancialValue) -> Got:
        if value.confidence == Confidence.LOW:
            return Missing(S.LOW_CONFIDENCE_INPUT, f"{value.metric.value} @ {value.period.label}")
        warnings: list[str] = []
        confidence: Confidence = value.confidence
        source, authority, locator = self.observations[value.selected_observation_id]
        if value.outcome in (Outcome.SOURCE_PRECEDENCE, Outcome.RESTATEMENT):
            warnings.append(f"INPUT_{value.outcome.value}")
            confidence = weakest([confidence, Confidence.MEDIUM])
            record = self.records.get(value.reconciliation_record_id)
            if (
                record is not None
                and record.max_difference is not None
                and value.value != 0
                and record.max_difference / abs(value.value) > MATERIAL_CONFLICT_SHARE
            ):
                warnings.append("MATERIAL_INPUT_CONFLICT")
                confidence = Confidence.LOW
        elif (
            value.outcome == Outcome.SINGLE_SOURCE and AUTHORITY_RANK[authority] >= AGGREGATOR_RANK
        ):
            warnings.append("INPUT_AGGREGATOR_ONLY")  # not confirmed by a primary source
            confidence = weakest([confidence, Confidence.MEDIUM])
        return Fact(
            value=value.value,
            inputs=(
                MetricInput(
                    role="",
                    canonical_value_id=value.canonical_value_id,
                    metric=value.metric,
                    value=value.value,
                    value_kind=value.value_kind,
                    currency=value.currency,
                    period=value.period,
                    reporting_basis=value.reporting_basis,
                    outcome=value.outcome,
                    confidence=value.confidence,
                    reconciliation_record_id=value.reconciliation_record_id,
                    selected_observation_id=value.selected_observation_id,
                    selected_source=source,
                    selected_authority=authority,
                    source_locator=locator,
                    supporting_observation_ids=value.supporting_observation_ids,
                    conflicting_observation_ids=value.conflicting_observation_ids,
                ),
            ),
            confidence=confidence,
            warnings=tuple(warnings),
        )


# --- the engine --------------------------------------------------------------------------------


@dataclass
class Engine:
    frame: Frame
    periods: Periods
    industry: Industry
    analytics_key: str
    max_provenance_refs: int = 64
    latest_fy: FinancialPeriod | None = None
    metrics: dict[UUID, CalculatedMetric] = field(default_factory=dict)
    index: dict[tuple[str, str], CalculatedMetric] = field(default_factory=dict)
    provenance_overflows: int = 0

    @property
    def sector(self) -> Sector:
        return Sector.FINANCIAL if self.industry in RESTRICTED_INDUSTRIES else Sector.GENERAL

    @property
    def financial(self) -> bool:
        return self.sector == Sector.FINANCIAL

    def find(self, code: str, period: FinancialPeriod) -> CalculatedMetric | None:
        return self.index.get((code, period.key()))

    def got(self, code: str, period: FinancialPeriod) -> Got:
        metric = self.find(code, period)
        if metric is None:
            if period.key() not in self.frame.period_keys:
                return Missing(
                    S.INSUFFICIENT_HISTORY, f"{code} @ {period.label} (period not in the data)"
                )
            return Missing(S.INPUT_MISSING, f"{code} @ {period.label} (not derived)")
        if metric.value is None or metric.confidence is None:
            status = S.INPUT_MISSING if metric.status in UPSTREAM_UNAVAILABLE else metric.status
            return Missing(
                status,
                f"{code} @ {period.label} ({metric.status.value})",
                metric.provenance,
                (metric.metric_id,),
            )
        return Fact(
            metric.value,
            metric.provenance,
            metric.confidence,
            metric.warnings,
            (metric.metric_id,),
            metric.formula_id,
        )

    def canonical(self, metrics: Sequence[CanonicalMetric], period: FinancialPeriod) -> Got:
        return self.frame.get(metrics, period)

    def emit(
        self,
        code: str,
        period: FinancialPeriod,
        parts: Sequence[tuple[str, Got]],
        compute: Formula,
        *,
        comparison: FinancialPeriod | None = None,
        variant: str | None = None,
        warnings: Sequence[str] = (),
        cap: Confidence | None = None,
        missing_note: str | None = None,
    ) -> CalculatedMetric:
        definition = FORMULAS[code]
        expression = definition.expression
        if variant is not None:
            match = next((v for v in definition.variants if v.name == variant), None)
            if match is None:
                raise ValueError(f"{code} has no registered variant {variant}")
            expression = f"{definition.expression} [{variant}: {match.expression}]"
        formula_id = definition.formula_id + (f"/{variant}" if variant else "")
        metric_id = uuid5(
            NS,
            f"{self.analytics_key}|{self.frame.basis.value}|{self.frame.currency.value}|{code}|"
            f"{period.key()}|{comparison.key() if comparison else '-'}",
        )
        currency = self.frame.currency if definition.unit in (U.MONEY, U.PER_SHARE) else None
        common: dict[str, Any] = {
            "metric_id": metric_id,
            "code": code,
            "label": definition.name,
            "category": definition.category,
            "unit": definition.unit,
            "currency": currency,
            "reporting_basis": self.frame.basis,
            "period": period,
            "comparison_period": comparison,
            "formula_id": formula_id,
            "formula_text": expression,
        }
        if self.sector not in definition.sectors or (
            definition.sectors == (Sector.FINANCIAL,) and self.industry != Industry.BANK
        ):
            metric = CalculatedMetric(
                **common,
                status=S.UNSUPPORTED_SECTOR,
                value=None,
                input_value_ids=(),
                provenance=(),
                confidence=None,
                interpretation="SECTOR_NOT_APPLICABLE",
                missing_inputs=(f"not defined for {self.industry.value} ({self.sector.value})",),
            )
            if self.latest_fy is None or period.key() == self.latest_fy.key():
                self._store(metric)
            return metric
        if period.period_type not in definition.periods:
            metric = CalculatedMetric(
                **common,
                status=S.PERIOD_MISMATCH,
                value=None,
                input_value_ids=(),
                provenance=(),
                confidence=None,
                missing_inputs=(
                    f"{code} is defined for {[p.value for p in definition.periods]}, "
                    f"not {period.period_type.value}",
                ),
            )
            self._store(metric)
            return metric
        facts: dict[str, Fact] = {}
        missing: list[tuple[str, Missing]] = []
        for role, got in parts:
            if isinstance(got, Fact):
                facts[role] = got
            else:
                missing.append((role, got))
        inputs: list[MetricInput] = []
        derived: list[UUID] = []
        items: list[tuple[str, Fact | Missing]] = [*facts.items(), *missing]
        for role, item in items:
            for i in item.inputs:
                inputs.append(i.model_copy(update={"role": f"{role}.{i.role}" if i.role else role}))
            derived.extend(d for d in item.derived_from if d in self.metrics)
        all_warnings = {w for f in facts.values() for w in f.warnings} | set(warnings)
        status = S.CALCULATED
        value: Decimal | None = None
        interpretation: str | None = None
        notes = [f"{role}: {m.what}" for role, m in missing]
        if missing_note:
            notes.append(missing_note)
        unique_inputs = {i.canonical_value_id for i in inputs}
        if missing:
            status = min((m.status for _, m in missing), key=STATUS_PRIORITY.index)
            if status in UPSTREAM_UNAVAILABLE:
                status = S.INPUT_MISSING
        elif len(unique_inputs) > self.max_provenance_refs:
            status = S.INVALID_INPUT
            self.provenance_overflows += 1
            notes.append(
                f"{len(unique_inputs)} canonical inputs exceed the provenance bound "
                f"of {self.max_provenance_refs}"
            )
            inputs = []
        else:
            mixed = self._mixing(inputs)
            if mixed is not None:
                status = mixed
            else:
                result = compute({role: fact.value for role, fact in facts.items()})
                if isinstance(result, Refused):
                    status, interpretation = result.status, result.interpretation
                else:
                    value = result if definition.unit == U.MONEY else quantize(result)
        by_code: dict[str, set[str]] = {}
        for f in facts.values():
            if f.formula_id and f.formula_id.count("/") >= 2:
                code_part, _, variant_part = f.formula_id.rpartition("/")
                by_code.setdefault(code_part, set()).add(variant_part)
        if any(len(found) > 1 for found in by_code.values()):
            all_warnings.add("MIXED_FORMULA_VARIANTS")  # one block built two ways at two dates
        confidence: Confidence | None = None
        if status == S.CALCULATED:
            confidence = weakest([f.confidence for f in facts.values()] + ([cap] if cap else []))
        metric = CalculatedMetric(
            **common,
            status=status,
            value=value,
            input_value_ids=tuple(sorted({i.canonical_value_id for i in inputs}, key=str)),
            input_metric_ids=tuple(sorted(set(derived), key=str)),
            input_periods=tuple(sorted({i.period.key() for i in inputs})),
            provenance=tuple(inputs),
            missing_inputs=tuple(notes),
            confidence=confidence,
            interpretation=interpretation,
            warnings=tuple(sorted(all_warnings)),
        )
        self._store(metric)
        return metric

    def _store(self, metric: CalculatedMetric) -> None:
        self.metrics[metric.metric_id] = metric
        self.index[(metric.code, metric.period.key())] = metric

    def annotate(self, metric: CalculatedMetric, interpretation: str) -> CalculatedMetric:
        updated = metric.model_copy(update={"interpretation": interpretation})
        self._store(updated)
        return updated

    @staticmethod
    def _mixing(inputs: Sequence[MetricInput]) -> CalculationStatus | None:
        if len({i.reporting_basis for i in inputs}) > 1:
            return S.BASIS_MISMATCH
        if len({i.currency for i in inputs}) > 1:
            return S.CURRENCY_MISMATCH
        return None

    # --- generic shapes ----------------------------------------------------------------------

    def ratio_metric(
        self,
        code: str,
        period: FinancialPeriod,
        numerator: tuple[str, Got],
        denominator: tuple[str, Got],
        *,
        scale: int = 100,
        positive_denominator: bool = False,
        absolute_numerator: bool = False,
    ) -> CalculatedMetric:
        n_role, d_role = numerator[0], denominator[0]

        def compute(v: Values) -> Result:
            top = abs(v[n_role]) if absolute_numerator else v[n_role]
            if positive_denominator:
                return positive_ratio(top, v[d_role], scale, d_role.upper())
            return ratio(top, v[d_role], scale)

        return self.emit(code, period, [numerator, denominator], compute)

    def averaged_ratio(
        self,
        code: str,
        period: FinancialPeriod,
        numerator: tuple[str, Got],
        balance: str,
        closing: Got,
        opening: Got,
        *,
        scale: int = 100,
    ) -> CalculatedMetric:
        """numerator / average(opening, closing) - both balances are required."""

        def compute(v: Values) -> Result:
            mean = average(v[f"{balance}_open"], v[f"{balance}_close"])
            return positive_ratio(v[numerator[0]], mean, scale, balance.upper())

        return self.emit(
            code,
            period,
            [numerator, (f"{balance}_open", opening), (f"{balance}_close", closing)],
            compute,
            comparison=self._prior(period),
        )

    def days_metric(
        self,
        code: str,
        period: FinancialPeriod,
        flow: tuple[str, Got],
        balance: str,
        closing: Got,
        opening: Got,
    ) -> CalculatedMetric:
        """average(opening, closing) / annual flow x 365 - both balances are required."""
        return self.emit(
            code,
            period,
            [flow, (f"{balance}_open", opening), (f"{balance}_close", closing)],
            lambda v: days(average(v[f"{balance}_open"], v[f"{balance}_close"]), v[flow[0]]),
            comparison=self._prior(period),
        )

    def _prior(self, period: FinancialPeriod) -> FinancialPeriod | None:
        if period.period_end is None:
            return None
        return self.periods.fy(shift(period.period_end, -12))

    def yoy(
        self,
        code: str,
        current: Got,
        prior: Got,
        period: FinancialPeriod,
        prior_period: FinancialPeriod,
        months: int = 12,
    ) -> CalculatedMetric:
        mismatch = compare_periods(period, prior_period, months)

        def compute(v: Values) -> Result:
            if mismatch is not None:
                return Refused(mismatch)
            return growth(v["current"], v["prior"])

        return self.emit(
            code, period, [("current", current), ("prior", prior)], compute, comparison=prior_period
        )

    def difference(
        self,
        code: str,
        current: Got,
        prior: Got,
        period: FinancialPeriod,
        prior_period: FinancialPeriod | None,
        scale: int = 1,
    ) -> CalculatedMetric:
        return self.emit(
            code,
            period,
            [("current", current), ("prior", prior)],
            lambda v: (v["current"] - v["prior"]) * scale,
            comparison=prior_period,
        )

    def magnitude(self, code: str, period: FinancialPeriod, got: Got) -> CalculatedMetric:
        return self.emit(code, period, [("reported", got)], lambda v: abs(v["reported"]))


# --- building blocks ---------------------------------------------------------------------------


def base_flow_metrics(e: Engine, p: FinancialPeriod) -> None:
    """EBITDA, EBIT, tax rate, NOPAT, capex, FCF and COGS for one flow period."""
    g = e.canonical
    reported = g([M.EBITDA], p)
    if isinstance(reported, Fact):
        e.emit("EBITDA", p, [("ebitda", reported)], lambda v: v["ebitda"], variant="REPORTED")
    else:
        e.emit(
            "EBITDA",
            p,
            [
                ("pbt", g([M.PBT], p)),
                ("finance_cost", g([M.FINANCE_COST], p)),
                ("depreciation", g([M.DEPRECIATION_AMORTIZATION], p)),
            ],
            lambda v: v["pbt"] + v["finance_cost"] + v["depreciation"],
            variant="DERIVED",
        )
    reported = g([M.EBIT], p)
    if isinstance(reported, Fact):
        e.emit("EBIT", p, [("ebit", reported)], lambda v: v["ebit"], variant="REPORTED")
    else:
        e.emit(
            "EBIT",
            p,
            [("pbt", g([M.PBT], p)), ("finance_cost", g([M.FINANCE_COST], p))],
            lambda v: v["pbt"] + v["finance_cost"],
            variant="DERIVED",
        )
    pbt = g([M.PBT], p)
    total_tax = g([M.TOTAL_TAX], p)
    current_tax, deferred_tax = g([M.CURRENT_TAX], p), g([M.DEFERRED_TAX], p)
    if isinstance(total_tax, Fact):
        e.emit(
            "TAX_RATE",
            p,
            [("tax", total_tax), ("pbt", pbt)],
            lambda v: positive_ratio(v["tax"], v["pbt"], 100, "PBT"),
            variant="TOTAL_TAX",
        )
    elif isinstance(current_tax, Fact) and isinstance(deferred_tax, Fact):
        e.emit(
            "TAX_RATE",
            p,
            [("current_tax", current_tax), ("deferred_tax", deferred_tax), ("pbt", pbt)],
            lambda v: positive_ratio(v["current_tax"] + v["deferred_tax"], v["pbt"], 100, "PBT"),
            variant="COMPONENTS",
        )
    else:
        e.emit(
            "TAX_RATE",
            p,
            [("pbt", pbt), ("pat", g([M.PAT], p))],
            lambda v: positive_ratio(v["pbt"] - v["pat"], v["pbt"], 100, "PBT"),
            variant="IMPLIED",
            warnings=["IMPLIED_FROM_PBT_AND_PAT"],
            cap=Confidence.MEDIUM,
        )
    e.emit(
        "NOPAT",
        p,
        [("ebit", e.got("EBIT", p)), ("tax_rate", e.got("TAX_RATE", p))],
        lambda v: quantize(nopat(v["ebit"], v["tax_rate"])),
    )
    capex = g([M.CAPEX], p)
    if isinstance(capex, Fact):
        e.emit("CAPEX", p, [("capex", capex)], lambda v: abs(v["capex"]), variant="CAPEX")
    else:
        e.emit(
            "CAPEX",
            p,
            [("purchase_of_ppe", g([M.PURCHASE_OF_PPE], p))],
            lambda v: abs(v["purchase_of_ppe"]),
            variant="PURCHASE_OF_PPE",
        )
    e.emit(
        "FCF",
        p,
        [("cfo", g([M.CFO], p)), ("capex", e.got("CAPEX", p))],
        lambda v: free_cash_flow(v["cfo"], v["capex"]),
    )
    e.emit(
        "COGS",
        p,
        [
            ("raw_materials", g([M.RAW_MATERIAL_COST], p)),
            ("purchases", g([M.PURCHASES_OF_STOCK_IN_TRADE], p)),
            ("inventory_change", g([M.CHANGE_IN_INVENTORIES], p)),
        ],
        lambda v: v["raw_materials"] + v["purchases"] + v["inventory_change"],
    )


def base_balance_metrics(e: Engine, b: FinancialPeriod) -> None:
    g = e.canonical
    share_capital, reserves = g([M.EQUITY_SHARE_CAPITAL], b), g([M.RESERVES, M.OTHER_EQUITY], b)
    if isinstance(share_capital, Fact) and isinstance(reserves, Fact):
        e.emit(
            "SHAREHOLDERS_EQUITY",
            b,
            [("share_capital", share_capital), ("reserves", reserves)],
            lambda v: v["share_capital"] + v["reserves"],
            variant="CAPITAL_PLUS_RESERVES",
        )
    else:
        e.emit(
            "SHAREHOLDERS_EQUITY",
            b,
            [("total_equity", g([M.TOTAL_EQUITY], b))],
            lambda v: v["total_equity"],
            variant="TOTAL_EQUITY",
        )
    debt = first_available(g([M.TOTAL_DEBT], b), g([M.TOTAL_BORROWINGS], b))
    if isinstance(debt, Fact):
        e.emit("GROSS_DEBT", b, [("debt", debt)], lambda v: v["debt"], variant="REPORTED")
    else:
        e.emit(
            "GROSS_DEBT",
            b,
            [
                ("long_term", g([M.LONG_TERM_BORROWINGS], b)),
                ("short_term", g([M.SHORT_TERM_BORROWINGS], b)),
            ],
            lambda v: v["long_term"] + v["short_term"],
            variant="COMPONENTS",
        )
    gross = e.got("GROSS_DEBT", b)
    e.emit(
        "LEASE_ADJUSTED_DEBT",
        b,
        [("debt", gross), ("leases", g([M.LEASE_LIABILITIES], b))],
        lambda v: v["debt"] + v["leases"],
    )
    cash = g([M.CASH_AND_CASH_EQUIVALENTS], b)
    net = e.emit("NET_DEBT", b, [("debt", gross), ("cash", cash)], lambda v: v["debt"] - v["cash"])
    if net.value is not None and net.value < 0:
        e.annotate(net, "NET_CASH")
    equity = e.got("SHAREHOLDERS_EQUITY", b)
    e.emit(
        "CAPITAL_EMPLOYED",
        b,
        [("equity", equity), ("debt", gross)],
        lambda v: v["equity"] + v["debt"],
    )
    e.emit(
        "INVESTED_CAPITAL",
        b,
        [("equity", equity), ("debt", gross), ("cash", cash)],
        lambda v: v["equity"] + v["debt"] - v["cash"],
    )
    e.emit(
        "NET_WORKING_CAPITAL",
        b,
        [
            ("current_assets", g([M.TOTAL_CURRENT_ASSETS], b)),
            ("current_liabilities", g([M.TOTAL_CURRENT_LIABILITIES], b)),
        ],
        lambda v: v["current_assets"] - v["current_liabilities"],
    )
    reported = g([M.TOTAL_LIABILITIES], b)
    current_l = g([M.TOTAL_CURRENT_LIABILITIES], b)
    non_current_l = g([M.TOTAL_NON_CURRENT_LIABILITIES], b)
    if isinstance(reported, Fact):
        e.emit(
            "TOTAL_LIABILITIES",
            b,
            [("liabilities", reported)],
            lambda v: v["liabilities"],
            variant="REPORTED",
        )
    elif isinstance(current_l, Fact) and isinstance(non_current_l, Fact):
        e.emit(
            "TOTAL_LIABILITIES",
            b,
            [("current", current_l), ("non_current", non_current_l)],
            lambda v: v["current"] + v["non_current"],
            variant="CURRENT_PLUS_NON_CURRENT",
        )
    else:
        e.emit(
            "TOTAL_LIABILITIES",
            b,
            [
                ("equity_and_liabilities", g([M.TOTAL_EQUITY_AND_LIABILITIES], b)),
                ("total_equity", g([M.TOTAL_EQUITY], b)),
            ],
            lambda v: v["equity_and_liabilities"] - v["total_equity"],
            variant="TOTAL_LESS_EQUITY",
        )


# --- annual analytics --------------------------------------------------------------------------

Source = tuple[CanonicalMetric, ...] | str
GROWTH_FLOWS: dict[str, Source] = {
    "REVENUE": REVENUE,
    "OPERATING_PROFIT": (M.OPERATING_PROFIT,),
    "EBITDA": "EBITDA",
    "EBIT": "EBIT",
    "PBT": (M.PBT,),
    "PAT": (M.PAT,),
    "EPS": EPS,
    "CFO": (M.CFO,),
    "FCF": "FCF",
    "CAPEX": "CAPEX",
    "COGS": "COGS",
}
GROWTH_BALANCES: dict[str, Source] = {
    "TOTAL_ASSETS": (M.TOTAL_ASSETS,),
    "EQUITY": "SHAREHOLDERS_EQUITY",
    "GROSS_DEBT": "GROSS_DEBT",
    "RECEIVABLES": (M.TRADE_RECEIVABLES,),
    "INVENTORY": (M.INVENTORY,),
    "PAYABLES": (M.TRADE_PAYABLES,),
    "SHARE_CAPITAL": (M.EQUITY_SHARE_CAPITAL,),
    "DEPOSITS": (M.DEPOSITS,),
    "ADVANCES": (M.ADVANCES,),
}


def pair(
    e: Engine, source: Source, current: FinancialPeriod, prior: FinancialPeriod
) -> tuple[Got, Got]:
    """Both endpoints of a comparison, always the SAME canonical metric."""
    if isinstance(source, str):
        return e.got(source, current), e.got(source, prior)
    now = e.canonical(source, current)
    if isinstance(now, Fact):
        return now, e.canonical((now.inputs[0].metric,), prior)
    return now, e.canonical(source, prior)


def annual_metrics(e: Engine, end: date) -> None:
    P = e.periods
    p, pp = P.fy(end), P.fy(shift(end, -12))
    b, bp = P.pit(end), P.pit(shift(end, -12))
    g = e.canonical
    revenue = g(REVENUE, p)

    for name, source in GROWTH_FLOWS.items():
        current, prior = pair(e, source, p, pp)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, p, pp)
    for name, source in GROWTH_BALANCES.items():
        current, prior = pair(e, source, b, bp)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, p, pp)

    # profitability
    margins: dict[str, Got] = {
        "EBITDA_MARGIN": e.got("EBITDA", p),
        "OPERATING_MARGIN": g([M.OPERATING_PROFIT], p),
        "EBIT_MARGIN": e.got("EBIT", p),
        "PBT_MARGIN": g([M.PBT], p),
        "PAT_MARGIN": g([M.PAT], p),
        "CFO_MARGIN": g([M.CFO], p),
        "FCF_MARGIN": e.got("FCF", p),
        "EMPLOYEE_COST_TO_REVENUE": g([M.EMPLOYEE_COST], p),
        "RAW_MATERIAL_COST_TO_REVENUE": g([M.RAW_MATERIAL_COST], p),
        "POWER_AND_FUEL_TO_REVENUE": g([M.POWER_AND_FUEL], p),
        "OTHER_OPERATING_COST_TO_REVENUE": g([M.OTHER_OPERATING_EXPENSE], p),
        "OTHER_EXPENSES_TO_REVENUE": g([M.OTHER_EXPENSES], p),
        "DEPRECIATION_TO_REVENUE": g([M.DEPRECIATION_AMORTIZATION], p),
        "FINANCE_COST_TO_REVENUE": g([M.FINANCE_COST], p),
        "CAPEX_TO_REVENUE": e.got("CAPEX", p),
    }
    for code, numerator in margins.items():
        e.ratio_metric(
            code, p, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
    e.emit(
        "GROSS_MARGIN",
        p,
        [("revenue", revenue), ("cogs", e.got("COGS", p))],
        lambda v: positive_ratio(v["revenue"] - v["cogs"], v["revenue"], 100, "REVENUE"),
    )
    pbt = g([M.PBT], p)
    e.ratio_metric(
        "OTHER_INCOME_TO_PBT",
        p,
        ("other_income", g([M.OTHER_INCOME], p)),
        ("pbt", pbt),
        positive_denominator=True,
    )
    e.ratio_metric(
        "EXCEPTIONAL_TO_PBT",
        p,
        ("exceptional", g([M.EXCEPTIONAL_ITEMS], p)),
        ("pbt", pbt),
        positive_denominator=True,
        absolute_numerator=True,
    )
    for code in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, p), e.got(code, pp), p, pp, scale=100)
    e.difference("TAX_RATE_CHANGE_YOY", e.got("TAX_RATE", p), e.got("TAX_RATE", pp), p, pp)
    e.difference(
        "OTHER_INCOME_TO_PBT_CHANGE_YOY",
        e.got("OTHER_INCOME_TO_PBT", p),
        e.got("OTHER_INCOME_TO_PBT", pp),
        p,
        pp,
    )

    # returns (averages of opening and closing balances)
    pat = g([M.PAT], p)
    assets, assets_open = g([M.TOTAL_ASSETS], b), g([M.TOTAL_ASSETS], bp)
    equity, equity_open = e.got("SHAREHOLDERS_EQUITY", b), e.got("SHAREHOLDERS_EQUITY", bp)
    e.averaged_ratio("ROE", p, ("pat", pat), "equity", equity, equity_open)
    e.averaged_ratio("ROA", p, ("pat", pat), "assets", assets, assets_open)
    e.averaged_ratio(
        "ROCE",
        p,
        ("ebit", e.got("EBIT", p)),
        "capital_employed",
        e.got("CAPITAL_EMPLOYED", b),
        e.got("CAPITAL_EMPLOYED", bp),
    )
    e.averaged_ratio(
        "ROIC",
        p,
        ("nopat", e.got("NOPAT", p)),
        "invested_capital",
        e.got("INVESTED_CAPITAL", b),
        e.got("INVESTED_CAPITAL", bp),
    )
    e.emit(
        "RETURN_ON_TANGIBLE_CAPITAL",
        p,
        [
            ("ebit", e.got("EBIT", p)),
            ("capital_open", e.got("CAPITAL_EMPLOYED", bp)),
            ("goodwill_open", g([M.GOODWILL], bp)),
            ("intangibles_open", g([M.INTANGIBLE_ASSETS], bp)),
            ("capital_close", e.got("CAPITAL_EMPLOYED", b)),
            ("goodwill_close", g([M.GOODWILL], b)),
            ("intangibles_close", g([M.INTANGIBLE_ASSETS], b)),
        ],
        lambda v: positive_ratio(
            v["ebit"],
            average(
                v["capital_open"] - v["goodwill_open"] - v["intangibles_open"],
                v["capital_close"] - v["goodwill_close"] - v["intangibles_close"],
            ),
            100,
            "TANGIBLE_CAPITAL",
        ),
        comparison=pp,
    )
    e.averaged_ratio(
        "ASSET_TURNOVER", p, ("revenue", revenue), "assets", assets, assets_open, scale=1
    )
    fixed, fixed_open = pair(e, FIXED, b, bp)
    e.averaged_ratio(
        "FIXED_ASSET_TURNOVER", p, ("revenue", revenue), "fixed_assets", fixed, fixed_open, scale=1
    )
    e.difference("ROE_CHANGE_YOY", e.got("ROE", p), e.got("ROE", pp), p, pp)
    e.difference("ROCE_CHANGE_YOY", e.got("ROCE", p), e.got("ROCE", pp), p, pp)

    # DuPont: implied vs direct ROE
    e.ratio_metric(
        "DUPONT_NET_MARGIN", p, ("pat", pat), ("revenue", revenue), positive_denominator=True
    )
    e.averaged_ratio(
        "DUPONT_ASSET_TURNOVER", p, ("revenue", revenue), "assets", assets, assets_open, scale=1
    )
    e.emit(
        "DUPONT_EQUITY_MULTIPLIER",
        p,
        [
            ("assets_open", assets_open),
            ("assets_close", assets),
            ("equity_open", equity_open),
            ("equity_close", equity),
        ],
        lambda v: positive_ratio(
            average(v["assets_open"], v["assets_close"]),
            average(v["equity_open"], v["equity_close"]),
            1,
            "EQUITY",
        ),
        comparison=pp,
    )
    e.emit(
        "DUPONT_IMPLIED_ROE",
        p,
        [
            ("net_margin", e.got("DUPONT_NET_MARGIN", p)),
            ("asset_turnover", e.got("DUPONT_ASSET_TURNOVER", p)),
            ("equity_multiplier", e.got("DUPONT_EQUITY_MULTIPLIER", p)),
        ],
        lambda v: dupont_roe(v["net_margin"], v["asset_turnover"], v["equity_multiplier"]),
    )
    implied, direct = e.got("DUPONT_IMPLIED_ROE", p), e.got("ROE", p)
    gap = (
        implied.value - direct.value
        if isinstance(implied, Fact) and isinstance(direct, Fact)
        else Decimal(0)
    )
    e.emit(
        "DUPONT_ROE_DIFFERENCE",
        p,
        [("implied", implied), ("direct", direct)],
        lambda v: v["implied"] - v["direct"],
        warnings=["DUPONT_TOLERANCE_EXCEEDED"] if abs(gap) > DUPONT_TOLERANCE_PP else [],
    )
    for code in ("DUPONT_NET_MARGIN", "DUPONT_ASSET_TURNOVER", "DUPONT_EQUITY_MULTIPLIER"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, p), e.got(code, pp), p, pp)

    # leverage & solvency
    debt, net_debt = e.got("GROSS_DEBT", b), e.got("NET_DEBT", b)
    ebitda, cfo = e.got("EBITDA", p), g([M.CFO], p)
    cash = g([M.CASH_AND_CASH_EQUIVALENTS], b)
    e.ratio_metric(
        "DEBT_TO_EQUITY", p, ("debt", debt), ("equity", equity), scale=1, positive_denominator=True
    )
    e.ratio_metric(
        "LEASE_ADJUSTED_DEBT_TO_EQUITY",
        p,
        ("debt", e.got("LEASE_ADJUSTED_DEBT", b)),
        ("equity", equity),
        scale=1,
        positive_denominator=True,
    )
    e.ratio_metric(
        "DEBT_TO_ASSETS", p, ("debt", debt), ("assets", assets), scale=1, positive_denominator=True
    )
    e.emit(
        "DEBT_TO_CAPITAL",
        p,
        [("debt", debt), ("equity", equity)],
        lambda v: positive_ratio(v["debt"], v["debt"] + v["equity"], 1, "CAPITAL"),
    )
    e.ratio_metric(
        "LIABILITIES_TO_ASSETS",
        p,
        ("liabilities", e.got("TOTAL_LIABILITIES", b)),
        ("assets", assets),
        scale=1,
        positive_denominator=True,
    )
    nde = e.ratio_metric(
        "NET_DEBT_TO_EQUITY",
        p,
        ("net_debt", net_debt),
        ("equity", equity),
        scale=1,
        positive_denominator=True,
    )
    if nde.value is not None and nde.value < 0:
        e.annotate(nde, "NET_CASH")
    e.ratio_metric(
        "DEBT_TO_EBITDA", p, ("debt", debt), ("ebitda", ebitda), scale=1, positive_denominator=True
    )

    def net_debt_to_ebitda(v: Values) -> Result:
        if v["net_debt"] < 0:
            return Refused(S.NOT_APPLICABLE, "NET_CASH")
        return positive_ratio(v["net_debt"], v["ebitda"], 1, "EBITDA")

    e.emit(
        "NET_DEBT_TO_EBITDA", p, [("net_debt", net_debt), ("ebitda", ebitda)], net_debt_to_ebitda
    )
    finance_cost = g([M.FINANCE_COST], p)
    e.ratio_metric(
        "INTEREST_COVERAGE", p, ("ebit", e.got("EBIT", p)), ("finance_cost", finance_cost), scale=1
    )
    e.ratio_metric(
        "FINANCE_COST_COVERAGE", p, ("ebitda", ebitda), ("finance_cost", finance_cost), scale=1
    )
    e.yoy(
        "INTEREST_COVERAGE_CHANGE_YOY",
        e.got("INTEREST_COVERAGE", p),
        e.got("INTEREST_COVERAGE", pp),
        p,
        pp,
    )
    e.ratio_metric(
        "DEBT_TO_CFO", p, ("debt", debt), ("cfo", cfo), scale=1, positive_denominator=True
    )
    e.ratio_metric("CFO_TO_DEBT", p, ("cfo", cfo), ("debt", debt), scale=1)
    e.ratio_metric("CASH_TO_DEBT", p, ("cash", cash), ("debt", debt), scale=1)
    e.ratio_metric(
        "EQUITY_TO_ASSETS", p, ("equity", equity), ("assets", assets), positive_denominator=True
    )

    # liquidity (as at the fiscal year end)
    current_assets = g([M.TOTAL_CURRENT_ASSETS], b)
    current_liabilities = g([M.TOTAL_CURRENT_LIABILITIES], b)
    e.ratio_metric(
        "CURRENT_RATIO",
        p,
        ("current_assets", current_assets),
        ("current_liabilities", current_liabilities),
        scale=1,
    )
    e.emit(
        "QUICK_RATIO",
        p,
        [
            ("current_assets", current_assets),
            ("inventory", g([M.INVENTORY], b)),
            ("current_liabilities", current_liabilities),
        ],
        lambda v: ratio(v["current_assets"] - v["inventory"], v["current_liabilities"]),
    )
    e.ratio_metric(
        "CASH_RATIO", p, ("cash", cash), ("current_liabilities", current_liabilities), scale=1
    )

    # working capital
    receivables = g([M.TRADE_RECEIVABLES], b)
    cogs = e.got("COGS", p)
    e.days_metric(
        "RECEIVABLE_DAYS",
        p,
        ("revenue", revenue),
        "receivables",
        receivables,
        g([M.TRADE_RECEIVABLES], bp),
    )
    e.days_metric(
        "INVENTORY_DAYS", p, ("cogs", cogs), "inventory", g([M.INVENTORY], b), g([M.INVENTORY], bp)
    )
    e.days_metric(
        "PAYABLE_DAYS",
        p,
        ("cogs", cogs),
        "payables",
        g([M.TRADE_PAYABLES], b),
        g([M.TRADE_PAYABLES], bp),
    )
    e.emit(
        "CASH_CONVERSION_CYCLE",
        p,
        [
            ("receivable_days", e.got("RECEIVABLE_DAYS", p)),
            ("inventory_days", e.got("INVENTORY_DAYS", p)),
            ("payable_days", e.got("PAYABLE_DAYS", p)),
        ],
        lambda v: cash_conversion_cycle(
            v["receivable_days"], v["inventory_days"], v["payable_days"]
        ),
    )
    for code in ("RECEIVABLE_DAYS", "INVENTORY_DAYS"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, p), e.got(code, pp), p, pp)
    nwc = e.got("NET_WORKING_CAPITAL", b)
    e.ratio_metric(
        "NWC_TO_REVENUE", p, ("nwc", nwc), ("revenue", revenue), positive_denominator=True
    )
    e.emit(
        "NWC_CHANGE",
        p,
        [("current", nwc), ("prior", e.got("NET_WORKING_CAPITAL", bp))],
        lambda v: v["current"] - v["prior"],
        comparison=pp,
    )
    e.ratio_metric(
        "RECEIVABLES_TO_REVENUE",
        p,
        ("receivables", receivables),
        ("revenue", revenue),
        positive_denominator=True,
    )
    e.ratio_metric(
        "INVENTORY_TO_REVENUE",
        p,
        ("inventory", g([M.INVENTORY], b)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    for code, a, z in (
        ("RECEIVABLES_VS_REVENUE_GROWTH", "RECEIVABLES", "REVENUE"),
        ("INVENTORY_VS_REVENUE_GROWTH", "INVENTORY", "REVENUE"),
        ("PAYABLES_VS_COGS_GROWTH", "PAYABLES", "COGS"),
    ):
        e.difference(code, e.got(f"{a}_GROWTH_YOY", p), e.got(f"{z}_GROWTH_YOY", p), p, pp)

    # cash-flow quality
    fcf = e.got("FCF", p)
    for code, top, bottom in (
        ("CFO_TO_PAT", ("cfo", cfo), ("pat", pat)),
        ("CFO_TO_EBITDA", ("cfo", cfo), ("ebitda", ebitda)),
        ("CFO_TO_PBT", ("cfo", cfo), ("pbt", pbt)),
        ("FCF_TO_PAT", ("fcf", fcf), ("pat", pat)),
        ("FCF_TO_EBITDA", ("fcf", fcf), ("ebitda", ebitda)),
    ):
        e.ratio_metric(code, p, top, bottom, scale=1, positive_denominator=True)
    e.ratio_metric(
        "CASH_TAX_RATE",
        p,
        ("tax_paid", g([M.TAX_PAID], p)),
        ("pbt", pbt),
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.emit(
        "CASH_TAX_VS_BOOK_TAX_GAP",
        p,
        [("cash_tax_rate", e.got("CASH_TAX_RATE", p)), ("tax_rate", e.got("TAX_RATE", p))],
        lambda v: v["cash_tax_rate"] - v["tax_rate"],
    )
    e.emit(
        "CASH_INTEREST_COVERAGE",
        p,
        [("cfo", cfo), ("interest_paid", g([M.INTEREST_PAID], p))],
        lambda v: ratio(v["cfo"] + abs(v["interest_paid"]), abs(v["interest_paid"])),
    )
    e.difference("CFO_TO_PAT_CHANGE_YOY", e.got("CFO_TO_PAT", p), e.got("CFO_TO_PAT", pp), p, pp)
    e.difference(
        "PAT_VS_CFO_GROWTH_GAP", e.got("PAT_GROWTH_YOY", p), e.got("CFO_GROWTH_YOY", p), p, pp
    )
    e.difference(
        "PAT_VS_EPS_GROWTH_GAP", e.got("PAT_GROWTH_YOY", p), e.got("EPS_GROWTH_YOY", p), p, pp
    )

    # capital allocation
    capex = e.got("CAPEX", p)
    dividends, buyback = g([M.DIVIDENDS_PAID], p), g([M.BUYBACK], p)
    depreciation = g([M.DEPRECIATION_AMORTIZATION], p)
    e.ratio_metric(
        "CAPEX_TO_CFO", p, ("capex", capex), ("cfo", cfo), scale=1, positive_denominator=True
    )
    e.ratio_metric(
        "CAPEX_TO_DEPRECIATION",
        p,
        ("capex", capex),
        ("depreciation", depreciation),
        scale=1,
        positive_denominator=True,
    )
    for code, metric in (
        ("ACQUISITION_SPEND", M.ACQUISITIONS),
        ("DIVIDENDS_PAID", M.DIVIDENDS_PAID),
        ("BUYBACK_SPEND", M.BUYBACK),
        ("DEBT_RAISED", M.DEBT_RAISED),
        ("DEBT_REPAID", M.DEBT_REPAID),
    ):
        e.magnitude(code, p, g([metric], p))
    e.ratio_metric(
        "DIVIDEND_PAYOUT",
        p,
        ("dividends", dividends),
        ("pat", pat),
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.ratio_metric(
        "DIVIDENDS_TO_FCF",
        p,
        ("dividends", dividends),
        ("fcf", fcf),
        scale=1,
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.emit(
        "SHAREHOLDER_DISTRIBUTION_TO_FCF",
        p,
        [("dividends", dividends), ("buyback", buyback), ("fcf", fcf)],
        lambda v: positive_ratio(abs(v["dividends"]) + abs(v["buyback"]), v["fcf"], 1, "FCF"),
    )
    e.ratio_metric(
        "DIVIDENDS_TO_CFO",
        p,
        ("dividends", dividends),
        ("cfo", cfo),
        scale=1,
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.ratio_metric(
        "BUYBACK_TO_CFO",
        p,
        ("buyback", buyback),
        ("cfo", cfo),
        scale=1,
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.emit(
        "REINVESTMENT_RATE",
        p,
        [
            ("capex", capex),
            ("depreciation", depreciation),
            ("nwc_change", e.got("NWC_CHANGE", p)),
            ("nopat", e.got("NOPAT", p)),
        ],
        lambda v: positive_ratio(
            v["capex"] - v["depreciation"] + v["nwc_change"], v["nopat"], 100, "NOPAT"
        ),
    )
    e.emit(
        "NET_DEBT_CHANGE",
        p,
        [("current", net_debt), ("prior", e.got("NET_DEBT", bp))],
        lambda v: v["current"] - v["prior"],
        comparison=pp,
    )
    e.emit(
        "CASH_CHANGE",
        p,
        [("current", cash), ("prior", g([M.CASH_AND_CASH_EQUIVALENTS], bp))],
        lambda v: v["current"] - v["prior"],
        comparison=pp,
    )

    # accruals
    e.emit("TOTAL_ACCRUALS", p, [("pat", pat), ("cfo", cfo)], lambda v: v["pat"] - v["cfo"])
    e.averaged_ratio(
        "CASH_FLOW_ACCRUAL_RATIO",
        p,
        ("accruals", e.got("TOTAL_ACCRUALS", p)),
        "assets",
        assets,
        assets_open,
    )
    e.emit(
        "BALANCE_SHEET_ACCRUAL_RATIO",
        p,
        [
            ("ca", current_assets),
            ("ca_prior", g([M.TOTAL_CURRENT_ASSETS], bp)),
            ("cash", cash),
            ("cash_prior", g([M.CASH_AND_CASH_EQUIVALENTS], bp)),
            ("cl", current_liabilities),
            ("cl_prior", g([M.TOTAL_CURRENT_LIABILITIES], bp)),
            ("depreciation", depreciation),
            ("assets_close", assets),
            ("assets_open", assets_open),
        ],
        lambda v: positive_ratio(
            (v["ca"] - v["ca_prior"])
            - (v["cash"] - v["cash_prior"])
            - (v["cl"] - v["cl_prior"])
            - v["depreciation"],
            average(v["assets_open"], v["assets_close"]),
            100,
            "ASSETS",
        ),
        comparison=pp,
    )
    e.difference(
        "CASH_FLOW_ACCRUAL_RATIO_CHANGE_YOY",
        e.got("CASH_FLOW_ACCRUAL_RATIO", p),
        e.got("CASH_FLOW_ACCRUAL_RATIO", pp),
        p,
        pp,
    )
    e.emit(
        "NON_CASH_EARNINGS_SHARE",
        p,
        [("pat", pat), ("cfo", cfo)],
        lambda v: positive_ratio(v["pat"] - v["cfo"], v["pat"], 100, "PAT"),
    )
    e.averaged_ratio(
        "DEPRECIATION_RATE",
        p,
        ("depreciation", depreciation),
        "fixed_assets",
        fixed,
        fixed_open,
    )
    e.difference(
        "DEPRECIATION_RATE_CHANGE_YOY",
        e.got("DEPRECIATION_RATE", p),
        e.got("DEPRECIATION_RATE", pp),
        p,
        pp,
    )
    e.ratio_metric(
        "CWIP_TO_FIXED_ASSETS",
        p,
        ("cwip", g([M.CWIP], b)),
        ("fixed_assets", fixed),
        positive_denominator=True,
    )
    _asset_mix(e, p, pp, b, bp)

    # banking
    e.emit(
        "COST_TO_INCOME",
        p,
        [
            ("operating_expenses", g([M.BANK_OPERATING_EXPENSES], p)),
            ("net_interest_income", g([M.NET_INTEREST_INCOME], p)),
            ("other_income", g([M.OTHER_INCOME], p)),
        ],
        lambda v: positive_ratio(
            v["operating_expenses"], v["net_interest_income"] + v["other_income"], 100, "INCOME"
        ),
    )
    e.ratio_metric(
        "FINANCING_PROFIT_MARGIN",
        p,
        ("financing_profit", g([M.BANK_FINANCING_PROFIT], p)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    e.ratio_metric(
        "PROVISIONS_TO_REVENUE",
        p,
        ("provisions", g([M.PROVISIONS_AND_CONTINGENCIES], p)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    for code in ("GROSS_NPA_RATIO", "NET_NPA_RATIO"):
        e.emit(
            code,
            p,
            [],
            lambda v: Refused(S.INPUT_MISSING),
            missing_note="NPA % is preserved by Box 4 as an unmapped percentage line, not as "
            "a canonical value; Box 5 does not reinterpret raw lines",
        )


def _asset_mix(
    e: Engine, p: FinancialPeriod, pp: FinancialPeriod, b: FinancialPeriod, bp: FinancialPeriod
) -> None:
    g = e.canonical
    components: dict[str, tuple[CanonicalMetric, ...]] = {
        "receivables": (M.TRADE_RECEIVABLES,),
        "inventory": (M.INVENTORY,),
        "cash": (M.CASH_AND_CASH_EQUIVALENTS,),
        "investments": (M.TOTAL_INVESTMENTS,),
        "fixed_assets": FIXED,
    }
    parts: list[tuple[str, Got]] = [
        ("assets", g([M.TOTAL_ASSETS], b)),
        ("assets_prior", g([M.TOTAL_ASSETS], bp)),
    ]
    present = []
    for name, metrics in components.items():
        now, before = pair(e, metrics, b, bp)
        if isinstance(now, Fact) and isinstance(before, Fact):
            parts += [(name, now), (f"{name}_prior", before)]
            present.append(name)

    def compute(v: Values) -> Result:
        if not present:
            return Refused(S.INPUT_MISSING)
        if v["assets"] <= 0 or v["assets_prior"] <= 0:
            return Refused(S.DIVISION_BY_ZERO)
        return max(
            abs(v[n] / v["assets"] - v[f"{n}_prior"] / v["assets_prior"]) * 100 for n in present
        )

    e.emit("ASSET_MIX_SHIFT", p, parts, compute, comparison=pp)


def _cagr_formula(years: int) -> Formula:
    def compute(v: Values) -> Result:
        return cagr(v["end"], v["start"], years)

    return compute


def _cumulative_ratio(years: int) -> Formula:
    def compute(v: Values) -> Result:
        cfo = sum((v[f"cfo_{k}"] for k in range(years)), Decimal(0))
        pat = sum((v[f"pat_{k}"] for k in range(years)), Decimal(0))
        return positive_ratio(cfo, pat, 1, "CUMULATIVE_PAT")

    return compute


def _cumulative_sum(name: str, years: int) -> Formula:
    def compute(v: Values) -> Result:
        return sum((v[f"{name}_{k}"] for k in range(years)), Decimal(0))

    return compute


def window_metrics(e: Engine, latest: date) -> None:
    """CAGR, multi-year margin change, cumulative cash flows, incremental ROIC (latest FY)."""
    P = e.periods
    p = P.fy(latest)
    cagr_sources: dict[str, Source] = {
        "REVENUE": REVENUE,
        "OPERATING_PROFIT": (M.OPERATING_PROFIT,),
        "EBITDA": "EBITDA",
        "EBIT": "EBIT",
        "PBT": (M.PBT,),
        "PAT": (M.PAT,),
        "EPS": EPS,
        "CFO": (M.CFO,),
        "FCF": "FCF",
        "TOTAL_ASSETS": (M.TOTAL_ASSETS,),
        "EQUITY": "SHAREHOLDERS_EQUITY",
        "DEPOSITS": (M.DEPOSITS,),
        "ADVANCES": (M.ADVANCES,),
    }
    balance = {"TOTAL_ASSETS", "EQUITY", "DEPOSITS", "ADVANCES"}
    for years in (3, 5, 10):
        start_end = shift(latest, -12 * years)
        sp = P.fy(start_end)
        for name, source in cagr_sources.items():
            now_p, then_p = (P.pit(latest), P.pit(start_end)) if name in balance else (p, sp)
            current, start = pair(e, source, now_p, then_p)
            e.emit(
                f"{name}_CAGR_{years}Y",
                p,
                [("end", current), ("start", start)],
                _cagr_formula(years),
                comparison=sp,
            )
        if years in (3, 5):
            for code in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN"):
                e.difference(
                    f"{code}_CHANGE_{years}Y", e.got(code, p), e.got(code, sp), p, sp, scale=100
                )
            years_back = [P.fy(shift(latest, -12 * k)) for k in range(years)]
            cfo_parts = [(f"cfo_{k}", e.canonical([M.CFO], q)) for k, q in enumerate(years_back)]
            pat_parts = [(f"pat_{k}", e.canonical([M.PAT], q)) for k, q in enumerate(years_back)]
            e.emit(
                f"CUMULATIVE_CFO_TO_PAT_{years}Y",
                p,
                [*cfo_parts, *pat_parts],
                _cumulative_ratio(years),
                comparison=years_back[-1],
            )
            fcf_parts = [(f"fcf_{k}", e.got("FCF", q)) for k, q in enumerate(years_back)]
            e.emit(
                f"CUMULATIVE_FCF_{years}Y",
                p,
                fcf_parts,
                _cumulative_sum("fcf", years),
                comparison=years_back[-1],
            )
    sp3 = P.fy(shift(latest, -36))
    e.emit(
        "INCREMENTAL_ROIC_3Y",
        p,
        [
            ("nopat", e.got("NOPAT", p)),
            ("nopat_start", e.got("NOPAT", sp3)),
            ("invested_capital", e.got("INVESTED_CAPITAL", P.pit(latest))),
            ("invested_capital_start", e.got("INVESTED_CAPITAL", P.pit(shift(latest, -36)))),
        ],
        lambda v: (
            Refused(S.NOT_APPLICABLE, "NO_CAPITAL_INCREASE")
            if v["invested_capital"] - v["invested_capital_start"] <= 0
            else (v["nopat"] - v["nopat_start"])
            * 100
            / (v["invested_capital"] - v["invested_capital_start"])
        ),
        comparison=sp3,
    )
    tax_points = [e.got("TAX_RATE", P.fy(shift(latest, -12 * k))) for k in range(5)]
    available = [t for t in tax_points if isinstance(t, Fact)]
    parts: list[tuple[str, Got]] = [
        (f"tax_rate_{k}", t) for k, t in enumerate(tax_points) if isinstance(t, Fact)
    ]
    if len(available) < 3:
        parts.append(
            (
                "tax_rate_history",
                Missing(
                    S.INSUFFICIENT_HISTORY,
                    f"{len(available)} of 5 fiscal years have a tax rate (3 needed)",
                ),
            )
        )
    e.emit(
        "TAX_RATE_RANGE_5Y",
        p,
        parts,
        lambda v: max(v.values()) - min(v.values()),
        comparison=P.fy(shift(latest, -48)),
    )


def quarterly_metrics(e: Engine, end: date) -> None:
    P = e.periods
    q, yq, sq = P.quarter(end), P.quarter(shift(end, -12)), P.quarter(shift(end, -3))
    g = e.canonical
    base_flow_metrics(e, q)
    quarterly: dict[str, Source] = {
        "REVENUE": REVENUE,
        "OPERATING_PROFIT": (M.OPERATING_PROFIT,),
        "EBITDA": "EBITDA",
        "PBT": (M.PBT,),
        "PAT": (M.PAT,),
        "EPS": EPS,
    }
    for name, source in quarterly.items():
        current, prior = pair(e, source, q, yq)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, q, yq)
        if f"{name}_GROWTH_QOQ" in FORMULAS:
            current, prior = pair(e, source, q, sq)
            e.yoy(f"{name}_GROWTH_QOQ", current, prior, q, sq, months=3)
    revenue = g(REVENUE, q)
    for code, numerator in (
        ("EBITDA_MARGIN", e.got("EBITDA", q)),
        ("OPERATING_MARGIN", g([M.OPERATING_PROFIT], q)),
        ("PBT_MARGIN", g([M.PBT], q)),
        ("PAT_MARGIN", g([M.PAT], q)),
    ):
        e.ratio_metric(
            code, q, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
    for code in ("OPERATING_MARGIN", "PAT_MARGIN"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, q), e.got(code, yq), q, yq, scale=100)


def ttm_metrics(e: Engine) -> None:
    t = e.periods.ttm()
    g = e.canonical
    base_flow_metrics(e, t)
    revenue = g(REVENUE, t)
    for code, numerator in (
        ("EBITDA_MARGIN", e.got("EBITDA", t)),
        ("OPERATING_MARGIN", g([M.OPERATING_PROFIT], t)),
        ("PBT_MARGIN", g([M.PBT], t)),
        ("PAT_MARGIN", g([M.PAT], t)),
    ):
        e.ratio_metric(
            code, t, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
