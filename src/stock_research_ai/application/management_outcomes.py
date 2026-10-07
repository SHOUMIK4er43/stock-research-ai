"""DETAILED_RESEARCH_V1 Box 8: guidance outcomes (guidance-outcome-rules/1).

Rules, applied in this order - the first that applies decides:

  1. QUALITATIVE_TARGET     qualitative / withdrawn target            -> NOT_COMPARABLE
  2. SUBJECT_UNLINKED       no trusted upstream actual is registered  -> INSUFFICIENT_DATA
  3. PROXY_DEFINITION       the actual measures a related, not the
                            same, thing (company 'operating margin',
                            constant-currency growth)                 -> NOT_COMPARABLE
  4. TARGET_PERIOD_UNKNOWN  no explicit / resolvable target period    -> NOT_COMPARABLE
  5. CHANGE_TARGET          a bps change target vs a level actual     -> NOT_COMPARABLE
  6. ACTUAL_NOT_AVAILABLE   no actual of the subject (of the target's
                            period type) at all                       -> INSUFFICIENT_DATA
                            - unless a different period type ends on
                            the target date                           -> NOT_COMPARABLE
  7. NOT_YET_DUE            target period ends after the latest actual
                            of the same period type                   -> NOT_YET_DUE
  8. PERIOD_MISMATCH        only a different period type (quarter /
                            YTD vs FY) ends on the target date        -> NOT_COMPARABLE
  9. ACTUAL_PERIOD_MISSING  no actual for the exact target period     -> INSUFFICIENT_DATA
 10. BASIS_MISMATCH /       stated basis has no actual; or basis
     BASIS_UNRESOLVED       unknown and actuals exist in two bases    -> NOT_COMPARABLE
 11. CURRENCY_MISMATCH /    different currency / unit                 -> NOT_COMPARABLE
     UNIT_MISMATCH
 12. STRUCTURE_CHANGED      Box 7 segment structure changed between
                            issue and target without restatement      -> NOT_COMPARABLE
 13. compare:
       RANGE    inside -> MET; beyond the favourable side -> BEAT; beyond the unfavourable
                side -> MISSED, except growth below the range but still positive -> PARTIAL;
                directionality not registered -> AMBIGUOUS outside the range
       POINT    within half a unit of the printed precision -> MET; otherwise as RANGE
       MINIMUM  actual >= value -> MET, else MISSED;  MAXIMUM  actual <= value -> MET, else MISSED
       DIRECTION growth sign, or the change from the previous period of the same type

Counts are descriptive. There is no score, grade or rank.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.management_registry import NS, SUBJECT_INDEX
from stock_research_ai.domain.business_intelligence import (
    AnalyticStatus,
    BusinessIntelligenceDataset,
    Comparability,
)
from stock_research_ai.domain.financial_analytics import (
    CalculationStatus,
    FinancialAnalyticsDataset,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    Currency,
    PeriodType,
    ReportingBasis,
)
from stock_research_ai.domain.management_intelligence import (
    OUTCOME_RULES_VERSION,
    AccuracySummary,
    ActualReference,
    Direction,
    Equivalence,
    GuidanceItem,
    GuidanceOutcome,
    GuidanceUnit,
    ManagementEvent,
    Measure,
    OutcomeResult,
    SubjectLayer,
    TargetType,
)

R = OutcomeResult
_PERCENT_UNITS = {"PERCENT"}
_MONEY_UNITS = {"MONEY"}


@dataclass(frozen=True)
class Actual:
    layer: SubjectLayer
    record_id: UUID
    code: str
    value: Decimal
    unit: str
    period_type: PeriodType
    period_end: date | None
    basis: ReportingBasis
    currency: Currency | None
    upstream: tuple[UUID, ...]
    comparability: Comparability | None = None

    def reference(self) -> ActualReference:
        return ActualReference(
            layer=self.layer,
            record_id=self.record_id,
            code=self.code,
            value=self.value,
            unit=self.unit,
            period_type=self.period_type,
            period_end=self.period_end,
            reporting_basis=self.basis,
            currency=self.currency,
            upstream_ids=self.upstream,
        )


def actual_index(
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset | None,
    box7: BusinessIntelligenceDataset | None,
) -> dict[tuple[SubjectLayer, str], list[Actual]]:
    index: dict[tuple[SubjectLayer, str], list[Actual]] = {}

    def add(a: Actual) -> None:
        index.setdefault((a.layer, a.code), []).append(a)

    for v in box4.canonical_values:
        add(
            Actual(
                SubjectLayer.BOX4_METRIC,
                v.canonical_value_id,
                v.metric.value,
                v.value,
                v.value_kind.value,
                v.period.period_type,
                v.period.period_end,
                v.reporting_basis,
                v.currency,
                (v.selected_observation_id,),
            )
        )
    if box5 is not None:
        for m in box5.all_metrics():
            if m.status != CalculationStatus.CALCULATED or m.value is None:
                continue
            add(
                Actual(
                    SubjectLayer.BOX5_METRIC,
                    m.metric_id,
                    m.code,
                    m.value,
                    m.unit.value,
                    m.period.period_type,
                    m.period.period_end,
                    m.reporting_basis,
                    m.currency,
                    m.input_value_ids,
                )
            )
    if box7 is not None:
        for k in box7.canonical_kpi_values:
            if k.segment_key is not None or k.geography_key is not None:
                continue
            code = k.canonical_kpi + (f":{k.qualifier}" if k.qualifier else "")
            add(
                Actual(
                    SubjectLayer.BOX7_KPI,
                    k.canonical_value_id,
                    code,
                    k.value,
                    k.unit.value,
                    k.period.period_type,
                    k.period.period_end,
                    k.reporting_basis,
                    k.currency,
                    k.supporting_observation_ids,
                )
            )
        for s in box7.segment_analytics:
            if s.status != AnalyticStatus.CALCULATED or s.value is None:
                continue
            add(
                Actual(
                    SubjectLayer.BOX7_SEGMENT,
                    s.analytic_id,
                    f"{s.code}:{s.subject_key}",
                    s.value,
                    s.unit,
                    s.period.period_type,
                    s.period.period_end,
                    s.reporting_basis,
                    s.currency,
                    s.input_canonical_ids,
                    s.comparability,
                )
            )
    for values in index.values():
        values.sort(key=lambda a: (a.period_end or date.min, a.basis.value, str(a.record_id)))
    return index


class OutcomeEngine:
    def __init__(
        self,
        index: dict[tuple[SubjectLayer, str], list[Actual]],
        box7: BusinessIntelligenceDataset | None,
        events: dict[UUID, ManagementEvent],
    ):
        self.index = index
        self.box7 = box7
        self.events = events

    def _outcome(
        self,
        g: GuidanceItem,
        result: OutcomeResult,
        rule: str,
        reason: str,
        actual: Actual | None = None,
        warnings: Iterable[str] = (),
    ) -> GuidanceOutcome:
        return GuidanceOutcome(
            outcome_id=uuid5(NS, f"outcome|{g.guidance_id}|{OUTCOME_RULES_VERSION}"),
            guidance_id=g.guidance_id,
            result=result,
            rule_version=OUTCOME_RULES_VERSION,
            rule=rule,
            actual=actual.reference() if actual else None,
            reason=reason,
            warnings=tuple(sorted(set(warnings))),
        )

    def evaluate(self, g: GuidanceItem) -> GuidanceOutcome:
        link = g.subject
        if g.target_type == TargetType.QUALITATIVE:
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "QUALITATIVE_TARGET",
                "no deterministic outcome rule for a qualitative target",
            )
        if link.equivalence == Equivalence.NONE or link.target_code is None:
            return self._outcome(
                g,
                R.INSUFFICIENT_DATA,
                "SUBJECT_UNLINKED",
                f"no trusted upstream actual: {link.reason}",
                warnings=("ACTUAL_NOT_AVAILABLE",),
            )
        if link.equivalence == Equivalence.PROXY:
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "PROXY_DEFINITION",
                f"{link.target_code} is a proxy, not the guided measure: {link.reason}",
            )
        period = g.target_period
        if period is None or period.period_end is None:
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "TARGET_PERIOD_UNKNOWN",
                "the target period is not explicit / resolvable",
                warnings=("PERIOD_UNKNOWN",),
            )
        if g.unit == GuidanceUnit.BASIS_POINTS:
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "CHANGE_TARGET",
                "a basis-point change target is not a level actual",
                warnings=("UNIT_MISMATCH",),
            )
        if link.layer == SubjectLayer.BOX7_SEGMENT and self._segment_blocked(g):
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "STRUCTURE_CHANGED",
                "Box 7 marks the segment analytic for the target period as not "
                "comparable (segment structure changed without restatement)",
                warnings=("NOT_COMPARABLE_STRUCTURE",),
            )
        actuals = self.index.get((link.layer, link.target_code), [])
        if not actuals:
            return self._outcome(
                g,
                R.INSUFFICIENT_DATA,
                "ACTUAL_NOT_AVAILABLE",
                f"no {link.layer.value} {link.target_code} actual exists",
                warnings=("ACTUAL_NOT_AVAILABLE",),
            )
        same_type = [a for a in actuals if a.period_type == period.period_type and a.period_end]
        latest = max((a.period_end for a in same_type if a.period_end), default=None)
        if latest is None:
            if any(a.period_end == period.period_end for a in actuals):
                return self._outcome(
                    g,
                    R.NOT_COMPARABLE,
                    "PERIOD_MISMATCH",
                    f"no {period.period_type.value} actual exists; only a different period "
                    f"type ends {period.period_end}",
                    warnings=("PERIOD_MISMATCH",),
                )
            return self._outcome(
                g,
                R.INSUFFICIENT_DATA,
                "ACTUAL_NOT_AVAILABLE",
                f"no {period.period_type.value} {link.target_code} actual exists",
                warnings=("ACTUAL_NOT_AVAILABLE",),
            )
        if period.period_end > latest:
            return self._outcome(
                g,
                R.NOT_YET_DUE,
                "NOT_YET_DUE",
                f"target period ends {period.period_end}; latest "
                f"{period.period_type.value} actual ends {latest}",
            )
        exact = [a for a in same_type if a.period_end == period.period_end]
        if not exact:
            if any(a.period_end == period.period_end for a in actuals):
                return self._outcome(
                    g,
                    R.NOT_COMPARABLE,
                    "PERIOD_MISMATCH",
                    f"only a different period type ends {period.period_end}",
                    warnings=("PERIOD_MISMATCH",),
                )
            return self._outcome(
                g,
                R.INSUFFICIENT_DATA,
                "ACTUAL_PERIOD_MISSING",
                f"no {period.period_type.value} actual ends {period.period_end}",
                warnings=("ACTUAL_NOT_AVAILABLE",),
            )
        warnings: list[str] = []
        if g.reporting_basis != ReportingBasis.UNKNOWN:
            exact_basis = [a for a in exact if a.basis == g.reporting_basis]
            if not exact_basis:
                return self._outcome(
                    g,
                    R.NOT_COMPARABLE,
                    "BASIS_MISMATCH",
                    f"no {g.reporting_basis.value} actual for the target period",
                    warnings=("BASIS_MISMATCH",),
                )
        else:
            bases = {a.basis for a in exact}
            if len(bases) != 1:
                return self._outcome(
                    g,
                    R.NOT_COMPARABLE,
                    "BASIS_UNRESOLVED",
                    f"the guidance basis is unknown and actuals exist in {len(bases)} bases",
                    warnings=("BASIS_UNKNOWN",),
                )
            exact_basis = exact
            warnings.append("BASIS_FROM_ONLY_AVAILABLE_ACTUAL")
        if len(exact_basis) != 1:
            return self._outcome(
                g,
                R.AMBIGUOUS,
                "MULTIPLE_ACTUALS",
                f"{len(exact_basis)} actuals match the target period and basis",
            )
        actual = exact_basis[0]
        if (
            g.unit == GuidanceUnit.PERCENT
            and actual.unit not in _PERCENT_UNITS
            or (g.unit == GuidanceUnit.MONEY and actual.unit not in _MONEY_UNITS)
        ):
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "UNIT_MISMATCH",
                f"guidance in {g.unit.value}, actual in {actual.unit}",
                actual,
                ("UNIT_MISMATCH",),
            )
        if g.unit == GuidanceUnit.MONEY and (
            g.currency != actual.currency or g.scale_to_crore is None
        ):
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "CURRENCY_MISMATCH",
                f"guidance {g.currency}, actual {actual.currency} "
                "(or the guidance scale is unknown)",
                actual,
                ("CURRENCY_MISMATCH",),
            )
        if self._structure_changed(g, actual):
            return self._outcome(
                g,
                R.NOT_COMPARABLE,
                "STRUCTURE_CHANGED",
                "Box 7 reports a segment-structure change without restated comparatives",
                actual,
                ("NOT_COMPARABLE_STRUCTURE",),
            )
        return self._compare(g, actual, warnings)

    def _segment_blocked(self, g: GuidanceItem) -> bool:
        if self.box7 is None or g.target_period is None:
            return False
        for s in self.box7.segment_analytics:
            if f"{s.code}:{s.subject_key}" != g.subject.target_code:
                continue
            if s.period.period_end != g.target_period.period_end:
                continue
            if s.status == AnalyticStatus.NOT_COMPARABLE or s.comparability in (
                Comparability.STRUCTURE_CHANGED,
                Comparability.NOT_COMPARABLE,
            ):
                return True
        return False

    def _structure_changed(self, g: GuidanceItem, actual: Actual) -> bool:
        if g.subject.layer != SubjectLayer.BOX7_SEGMENT:
            return False
        if actual.comparability in (Comparability.STRUCTURE_CHANGED, Comparability.NOT_COMPARABLE):
            return True
        if self.box7 is None or g.target_period is None or g.target_period.period_end is None:
            return False
        issued = g.issued_period.period_end if g.issued_period else None
        for a in self.box7.segment_comparability:
            if a.status != Comparability.STRUCTURE_CHANGED or a.reporting_basis != actual.basis:
                continue
            if a.to_period_end <= g.target_period.period_end and (
                issued is None or a.to_period_end > issued
            ):
                return True
        return False

    def _compare(self, g: GuidanceItem, actual: Actual, warnings: list[str]) -> GuidanceOutcome:
        value = actual.value
        scale = g.scale_to_crore if g.unit == GuidanceUnit.MONEY else Decimal(1)
        assert scale is not None
        rule_meta = SUBJECT_INDEX.get(g.subject.subject_code)
        higher = rule_meta.higher_is_better if rule_meta else True
        growth = g.measure == Measure.GROWTH
        if g.target_type == TargetType.DIRECTION:
            return self._direction(g, actual, warnings)
        if g.target_type == TargetType.MINIMUM and g.value is not None:
            ok = value >= g.value * scale
            return self._outcome(
                g,
                R.MET if ok else R.MISSED,
                "MINIMUM",
                f"actual {value} vs minimum {g.value * scale}",
                actual,
                warnings,
            )
        if g.target_type == TargetType.MAXIMUM and g.value is not None:
            ok = value <= g.value * scale
            return self._outcome(
                g,
                R.MET if ok else R.MISSED,
                "MAXIMUM",
                f"actual {value} vs maximum {g.value * scale}",
                actual,
                warnings,
            )
        if g.target_type == TargetType.RANGE and g.low is not None and g.high is not None:
            low, high, rule = g.low * scale, g.high * scale, "RANGE"
        elif g.value is not None:
            tolerance = Decimal(5) / Decimal(10) ** (g.decimals + 1)
            low, high, rule = (g.value - tolerance) * scale, (g.value + tolerance) * scale, "POINT"
        else:
            return self._outcome(g, R.AMBIGUOUS, "NO_TARGET_VALUE", "no target value", actual)
        reason = f"actual {value} vs target {low}..{high}"
        if low <= value <= high:
            return self._outcome(g, R.MET, rule, reason, actual, warnings)
        if higher is None:
            return self._outcome(
                g,
                R.AMBIGUOUS,
                f"{rule}/DIRECTIONALITY_UNREGISTERED",
                reason + f"; directionality is not registered for {g.subject.subject_code}",
                actual,
                warnings,
            )
        above = value > high
        if above == higher:
            return self._outcome(g, R.BEAT, rule, reason, actual, warnings)
        if growth and higher and value > 0:
            return self._outcome(
                g, R.PARTIAL, f"{rule}/POSITIVE_GROWTH_BELOW_RANGE", reason, actual, warnings
            )
        return self._outcome(g, R.MISSED, rule, reason, actual, warnings)

    def _direction(self, g: GuidanceItem, actual: Actual, warnings: list[str]) -> GuidanceOutcome:
        if g.direction not in (Direction.INCREASE, Direction.DECREASE):
            return self._outcome(g, R.AMBIGUOUS, "DIRECTION", "no usable direction", actual)
        if g.measure == Measure.GROWTH:
            change = actual.value
            basis_text = f"growth {change}"
        else:
            earlier = [
                a
                for a in self.index.get((actual.layer, actual.code), [])
                if a.period_type == actual.period_type
                and a.basis == actual.basis
                and a.period_end is not None
                and actual.period_end is not None
                and a.period_end < actual.period_end
            ]
            if not earlier:
                return self._outcome(
                    g,
                    R.INSUFFICIENT_DATA,
                    "DIRECTION/NO_PRIOR_PERIOD",
                    "no earlier actual to measure the direction against",
                    actual,
                )
            prior = max(earlier, key=lambda a: a.period_end or date.min)
            change = actual.value - prior.value
            basis_text = f"change {change} from {prior.period_end}"
        if change == 0:
            return self._outcome(
                g, R.MISSED, "DIRECTION", basis_text + " (no change)", actual, warnings
            )
        ok = (change > 0) == (g.direction == Direction.INCREASE)
        return self._outcome(
            g, R.MET if ok else R.MISSED, "DIRECTION", basis_text, actual, warnings
        )


def summaries(
    guidance: list[GuidanceItem], outcomes: list[GuidanceOutcome]
) -> list[AccuracySummary]:
    by_id = {o.guidance_id: o for o in outcomes}
    scopes: dict[tuple[str, str], list[OutcomeResult]] = {}
    for g in guidance:
        result = by_id[g.guidance_id].result
        keys = [
            ("OVERALL", "ALL"),
            ("EVENT", str(g.event_id)),
            ("SUBJECT", g.subject.subject_code + (f"|{g.qualifier}" if g.qualifier else "")),
            ("TARGET_PERIOD", g.target_period.key() if g.target_period else "UNKNOWN"),
        ]
        for scope_key in keys:
            scopes.setdefault(scope_key, []).append(result)
    out = []
    for (scope, name), results in sorted(scopes.items()):
        c = Counter(results)
        out.append(
            AccuracySummary(
                scope=scope,  # type: ignore[arg-type]
                scope_key=name,
                issued=len(results),
                due=len(results) - c[R.NOT_YET_DUE],
                met=c[R.MET],
                beat=c[R.BEAT],
                missed=c[R.MISSED],
                partial=c[R.PARTIAL],
                not_yet_due=c[R.NOT_YET_DUE],
                not_comparable=c[R.NOT_COMPARABLE],
                insufficient_data=c[R.INSUFFICIENT_DATA],
                ambiguous=c[R.AMBIGUOUS],
            )
        )
    return out
