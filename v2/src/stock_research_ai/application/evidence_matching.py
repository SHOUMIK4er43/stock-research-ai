"""Neutral, provider/role-agnostic evidence-matching primitives shared by Part 5B's
AnalystModel validator and Part 5C's independent challenger.

Pure relocation from application/analysis_validation.py - names and bodies are
byte-identical to their prior location; only the module changed. Proof of no behavior
change is that Part 5B's own full test suite passes unchanged after this move.
"""

import re
from decimal import Decimal

from stock_research_ai.application.analysis import Calculation, ResolvedEvidence
from stock_research_ai.domain.analysis import EvidenceReference, SourceEpistemicClass
from stock_research_ai.domain.calculations import CalculationResult, CalculationSnapshot
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.metrics import MetricMappingStatus

FORBIDDEN_RECOMMENDATION = re.compile(
    r"\b(buy|sell|\bhold\b|overweight|underweight|outperform|underperform|accumulate|reduce"
    r"|initiate coverage|target price)\b",
    re.IGNORECASE,
)
FORBIDDEN_CERTAINTY = re.compile(
    r"\b(guaranteed|certain to|will definitely|always beats|never fails|without doubt)\b",
    re.IGNORECASE,
)
FORBIDDEN_HISTORICAL_KNOWLEDGE = re.compile(
    r"\b(was known|was already known|was publicly known|confirmed as of|historically known"
    r"|was known to the market)\b",
    re.IGNORECASE,
)
FORBIDDEN_BASE_LIKELIHOOD = re.compile(
    r"\b(most likely|most probable|highest probability|expected case)\b", re.IGNORECASE
)
BANK_INDUSTRIAL_TERMS = re.compile(
    r"\b(net debt|ev/ebitda|free cash flow yield|fcff)\b", re.IGNORECASE
)

_NUM = r"-?\d+(?:,\d{3})*(?:\.\d+)?"
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PERCENT", re.compile(rf"({_NUM})\s?%")),
    ("BASIS_POINTS", re.compile(rf"({_NUM})\s?(?:bps|basis points)", re.IGNORECASE)),
    ("MULTIPLE_X", re.compile(rf"({_NUM})x\b", re.IGNORECASE)),
    ("CURRENCY", re.compile(rf"(?:₹|Rs\.?|INR)\s?({_NUM})", re.IGNORECASE)),
)
_PLAIN_NUMBER = re.compile(_NUM)

_UNIT_FAMILY_ALIASES = {
    "RATIO_X": "MULTIPLE_X",
    "CURRENCY_PER_SHARE": "CURRENCY",
}


class NumericToken:
    __slots__ = ("value", "family")

    def __init__(self, value: Decimal, family: str) -> None:
        self.value = value
        self.family = family


def extract_numeric_tokens(statement: str) -> tuple[NumericToken, ...]:
    tokens: list[NumericToken] = []
    covered: list[tuple[int, int]] = []
    for family, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(statement):
            covered.append(match.span())
            raw = match.group(1).replace(",", "")
            tokens.append(NumericToken(Decimal(raw), family))

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < c[1] and c[0] < span[1] for c in covered)

    for match in _PLAIN_NUMBER.finditer(statement):
        if overlaps(match.span()):
            continue
        raw = match.group(0).replace(",", "")
        tokens.append(NumericToken(Decimal(raw), "NUMBER"))
    return tuple(tokens)


def canonical_values(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> tuple[tuple[Decimal, str], ...]:
    """(value, unit_family) pairs a citation legitimately supports, from already-canonical
    display values only - never recomputed."""
    values: list[tuple[Decimal, str]] = []
    if ref.kind in ("FINANCIAL", "SHAREHOLDING"):
        f = evidence.financial
        assert f is not None
        for section in (
            f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow, f.ratios,
        ):
            for o in section:
                if str(o.id) == ref.item_key and o.value is not None:
                    family = {"money": "CURRENCY", "percent": "PERCENT"}.get(o.unit.kind, "NUMBER")
                    values.append((o.value * o.unit.scale, family))
        for holding in f.shareholding.holdings:
            if (
                str(holding.observation.id) == ref.item_key
                and holding.observation.value is not None
            ):
                o = holding.observation
                family = {"money": "CURRENCY", "percent": "PERCENT"}.get(o.unit.kind, "NUMBER")
                values.append((o.value * o.unit.scale, family))  # type: ignore[operator]
        for count in f.shareholding.shareholder_counts:
            if str(count.id) == ref.item_key and count.value is not None:
                values.append((count.value * count.unit.scale, "NUMBER"))
    elif ref.kind == "MARKET":
        m = evidence.markets[ref.owning_snapshot_id]
        for market_observation in m.observations:
            if str(market_observation.id) == ref.item_key:
                values.append((market_observation.close, "CURRENCY"))
    elif ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c: Calculation = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key and calc_result.display_value is not None:
                unit = calc_result.unit if isinstance(calc_result, CalculationResult) else (
                    calc_result.definition.unit
                )
                values.append((calc_result.display_value, _UNIT_FAMILY_ALIASES.get(unit, unit)))
    elif ref.kind == "MARKET_CALCULATION":
        mc = evidence.market_calculations[ref.owning_snapshot_id]
        for mc_result in mc.results:
            if str(mc_result.metric_id) == ref.item_key and mc_result.display_value is not None:
                mc_family = _UNIT_FAMILY_ALIASES.get(mc_result.unit, mc_result.unit)
                values.append((mc_result.display_value, mc_family))
    elif ref.kind == "TECHNICAL":
        t = evidence.technicals[ref.owning_snapshot_id]
        for series in t.series:
            for point in series.points:
                if str(point.id) == ref.item_key and point.display_value is not None:
                    values.append(
                        (point.display_value, _UNIT_FAMILY_ALIASES.get(series.unit, series.unit))
                    )
    elif ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                for value in val_result.values:
                    values.append(
                        (value.display_value, _UNIT_FAMILY_ALIASES.get(value.unit, value.unit))
                    )
    elif ref.kind == "MACRO":
        macro_snapshot = evidence.macros[ref.owning_snapshot_id]
        for observation in macro_snapshot.observations:
            if observation.reference_period == ref.item_key:
                values.append((observation.value, "NUMBER"))
    return tuple(values)


def is_unverified_semantic(ref: EvidenceReference, evidence: ResolvedEvidence) -> bool:
    if ref.kind not in ("FINANCIAL", "SHAREHOLDING"):
        return False
    f = evidence.financial
    assert f is not None
    for section in (f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow,
                    f.ratios):
        for o in section:
            if str(o.id) == ref.item_key:
                return o.mapping_status != MetricMappingStatus.MAPPED
    for holding in f.shareholding.holdings:
        if str(holding.observation.id) == ref.item_key:
            return holding.observation.mapping_status != MetricMappingStatus.MAPPED
    for count in f.shareholding.shareholder_counts:
        if str(count.id) == ref.item_key:
            return count.mapping_status != MetricMappingStatus.MAPPED
    return False


def source_epistemic_class(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> SourceEpistemicClass:
    if ref.kind in ("COMPANY", "FINANCIAL", "SHAREHOLDING", "MARKET", "DOCUMENT_CHUNK",
                    "PUBLICATION", "MACRO"):
        return "FACT"
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                return "SCENARIO" if val_result.definition.classification == "SCENARIO" else (
                    "CALCULATION"
                )
    return "CALCULATION"


def limitation_codes(ref: EvidenceReference, evidence: ResolvedEvidence) -> tuple[str, ...]:
    """Issue codes propagated from a citation's own upstream result, generically per kind."""
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key:
                if isinstance(calc_result, CalculationResult):
                    return tuple(sorted(calc_result.issues))
                return tuple(sorted(i.code for i in calc_result.issues))
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return tuple(sorted(i.code for i in finding.issues))
    if ref.kind == "MARKET_CALCULATION":
        mc = evidence.market_calculations[ref.owning_snapshot_id]
        for mc_result in mc.results:
            if str(mc_result.metric_id) == ref.item_key:
                return tuple(sorted(i.code for i in mc_result.issues))
    if ref.kind == "TECHNICAL":
        t = evidence.technicals[ref.owning_snapshot_id]
        for series in t.series:
            for point in series.points:
                if str(point.id) == ref.item_key and point.issue:
                    return (point.issue,)
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                return tuple(sorted(i.code for i in val_result.issues))
    return ()


def citation_period_scope(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> tuple[str | None, ReportingScope | None]:
    if ref.kind == "FINANCIAL_CALCULATION":
        c = evidence.calculations[ref.owning_snapshot_id]
        assert isinstance(c, CalculationSnapshot)
        for spec in c.request.calculations:
            if spec.metric_id == ref.item_key:
                return spec.current_period, c.request.reporting_scope
        return None, c.request.reporting_scope
    if ref.kind == "SECTOR_KPI":
        kpi = evidence.calculations[ref.owning_snapshot_id]
        assert not isinstance(kpi, CalculationSnapshot)
        return kpi.request.current_period, kpi.request.reporting_scope
    if ref.kind == "FINANCIAL":
        f = evidence.financial
        assert f is not None
        for section in (f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow,
                        f.ratios):
            for o in section:
                if str(o.id) == ref.item_key:
                    return o.period.fiscal_label, o.reporting_scope
    return None, None


def _semantic_key(ref: EvidenceReference) -> str | None:
    """A comparability key for 'same semantic metric' - only kinds with a stable metric_id
    style item_key are ever compared; everything else can never be called conflicting."""
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI", "MARKET_CALCULATION"):
        return ref.item_key
    return None


def _has_conflicting_evidence(
    refs: list[EvidenceReference], evidence: ResolvedEvidence
) -> bool:
    """Strict comparability per architecture: same semantic metric, same period, same
    reporting scope, compatible unit - only then may disagreement be called a conflict."""
    grouped: dict[tuple[str, str | None, ReportingScope | None], set[tuple[Decimal, str]]] = {}
    for ref in refs:
        key_metric = _semantic_key(ref)
        if key_metric is None:
            continue
        period, scope = citation_period_scope(ref, evidence)
        group_key = (key_metric, period, scope)
        for value, family in canonical_values(ref, evidence):
            grouped.setdefault(group_key, set()).add((value, family))
    return any(
        len({family for _, family in values}) == 1 and len({v for v, _ in values}) > 1
        for values in grouped.values()
    )


def find_reference(
    kind: str,
    owning_snapshot_id: object,
    item_key: str | None,
    sources: tuple[EvidenceReference, ...],
) -> EvidenceReference | None:
    for ref in sources:
        if ref.kind == kind and ref.owning_snapshot_id == owning_snapshot_id and (
            ref.item_key == item_key
        ):
            return ref
    return None


__all__ = [
    "BANK_INDUSTRIAL_TERMS",
    "FORBIDDEN_BASE_LIKELIHOOD",
    "FORBIDDEN_CERTAINTY",
    "FORBIDDEN_HISTORICAL_KNOWLEDGE",
    "FORBIDDEN_RECOMMENDATION",
    "NumericToken",
    "_has_conflicting_evidence",
    "_semantic_key",
    "canonical_values",
    "citation_period_scope",
    "extract_numeric_tokens",
    "find_reference",
    "is_unverified_semantic",
    "limitation_codes",
    "source_epistemic_class",
]
