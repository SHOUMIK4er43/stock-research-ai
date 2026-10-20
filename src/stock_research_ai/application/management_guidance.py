"""DETAILED_RESEARCH_V1 Box 8: structured guidance and its lifecycle.

A GuidanceItem is created only when ONE registered subject and ONE compatible numeric
target (or, without numbers, one explicit direction / an explicit withdrawal) sit in the
same forward-looking management sentence. Two subjects with numbers, two numbers for one
subject, a number with no registered subject, or a subject the company's sector rules out
(industrial EBITDA for a bank) keep the statement and record an UnparsedItem - structured
guidance is never manufactured.

Lifecycle states compare a guidance item only with the previous item on the SAME subject,
qualifier, measure, target period, unit and currency (and no conflicting known basis),
ordered by printed call date.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.management_calls import fiscal_period, month_end
from stock_research_ai.application.management_registry import (
    BPS,
    CONDITION,
    CONSTANT_CURRENCY,
    CURRENCY_CODES,
    CURRENCY_SENSITIVE,
    DIRECTION_AFTER_SUBJECT,
    DIRECTION_DOWN,
    DIRECTION_UP,
    FINANCIAL,
    IMPACT,
    MAXIMUM_WORDS,
    MINIMUM_WORDS,
    MONEY,
    NS,
    PERCENT_POINT,
    PERCENT_RANGE,
    PERIOD_LABEL,
    REPORTED_CURRENCY,
    SCALE_TO_CRORE,
    STATED_CHANGE,
    SUBJECTS,
    TURN_PERIOD,
    UNIVERSAL,
    SubjectRule,
)
from stock_research_ai.application.management_statements import StatementContext
from stock_research_ai.domain.financial_canonical import Currency, PeriodType, ReportingBasis
from stock_research_ai.domain.management_intelligence import (
    Confidence,
    Direction,
    Equivalence,
    GuidanceItem,
    GuidanceTimeline,
    GuidanceUnit,
    Lifecycle,
    ManagementEvent,
    Measure,
    ReportingPeriod,
    StatedChange,
    StatementType,
    SubjectLayer,
    SubjectLink,
    TargetType,
    UnparsedItem,
)

ST = StatementType
_GUIDING = {ST.GUIDANCE, ST.TARGET, ST.EXPECTATION}
_CONSOLIDATED = re.compile(r"\bconsolidated\b", re.I)
_STANDALONE = re.compile(r"\bstand-?alone\b", re.I)
SEGMENT_SUBJECT = "SEGMENT_REVENUE_GROWTH"
_COMPATIBLE_STATED = {
    StatedChange.MAINTAINED: {Lifecycle.REITERATED},
    StatedChange.RAISED: {Lifecycle.RAISED},
    StatedChange.LOWERED: {Lifecycle.LOWERED},
    StatedChange.REVISED: {
        Lifecycle.RAISED,
        Lifecycle.LOWERED,
        Lifecycle.NARROWED,
        Lifecycle.WIDENED,
    },
    StatedChange.WITHDRAWN: {Lifecycle.WITHDRAWN},
}


@dataclass(frozen=True)
class Segment:
    key: str
    names: tuple[str, ...]


@dataclass
class Target:
    unit: GuidanceUnit
    target_type: TargetType
    low: Decimal | None
    high: Decimal | None
    value: Decimal | None
    decimals: int
    span: tuple[int, int]
    raw: str
    currency: Currency | None = None
    scale_to_crore: Decimal | None = None


@dataclass
class GuidanceOutput:
    guidance: list[GuidanceItem] = field(default_factory=list)
    timelines: list[GuidanceTimeline] = field(default_factory=list)
    unparsed: list[UnparsedItem] = field(default_factory=list)


def _decimals(*texts: str | None) -> int:
    return max((len(t.split(".")[1]) if t and "." in t else 0) for t in texts)


def _number(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def numeric_targets(sentence: str) -> list[Target]:
    found: list[Target] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for m in PERCENT_RANGE.finditer(sentence):
        lo_text = m.group("blo") or m.group("lo")
        hi_text = m.group("bhi") or m.group("hi")
        lo, hi = _number(lo_text), _number(hi_text)
        taken.append(m.span())
        if lo > hi:  # '2.4% and 1%' style lists are not a range: both stay as values
            for text in (lo_text, hi_text):
                found.append(
                    Target(
                        GuidanceUnit.PERCENT,
                        TargetType.POINT,
                        None,
                        None,
                        _number(text),
                        _decimals(text),
                        m.span(),
                        m.group(0),
                    )
                )
            continue
        found.append(
            Target(
                GuidanceUnit.PERCENT,
                TargetType.RANGE,
                lo,
                hi,
                None,
                _decimals(lo_text, hi_text),
                m.span(),
                m.group(0),
            )
        )
    for m in PERCENT_POINT.finditer(sentence):
        a, b = m.span("v")[0], m.span()[1]
        if not free(a, b):
            continue
        taken.append((a, b))
        prefix = (m.group("prefix") or "").lower()
        kind = (
            TargetType.MINIMUM
            if prefix in MINIMUM_WORDS
            else TargetType.MAXIMUM
            if prefix in MAXIMUM_WORDS
            else TargetType.POINT
        )
        found.append(
            Target(
                GuidanceUnit.PERCENT,
                kind,
                None,
                None,
                _number(m.group("v")),
                _decimals(m.group("v")),
                m.span(),
                m.group(0).strip(),
            )
        )
    for m in BPS.finditer(sentence):
        if not free(*m.span()):
            continue
        taken.append(m.span())
        lo_text, hi_text = m.group("lo"), m.group("hi")
        if hi_text is not None and _number(lo_text) <= _number(hi_text):
            found.append(
                Target(
                    GuidanceUnit.BASIS_POINTS,
                    TargetType.RANGE,
                    _number(lo_text),
                    _number(hi_text),
                    None,
                    _decimals(lo_text, hi_text),
                    m.span(),
                    m.group(0),
                )
            )
        else:
            found.append(
                Target(
                    GuidanceUnit.BASIS_POINTS,
                    TargetType.POINT,
                    None,
                    None,
                    _number(lo_text),
                    _decimals(lo_text),
                    m.span(),
                    m.group(0),
                )
            )
    for m in MONEY.finditer(sentence):
        if not free(*m.span()):
            continue
        taken.append(m.span())
        code = CURRENCY_CODES.get(m.group("cur").lower(), "OTHER")
        scale_text = (m.group("s2") or m.group("s1") or "").lower()
        scale = Decimal(SCALE_TO_CRORE[scale_text]) if scale_text in SCALE_TO_CRORE else None
        lo_text, hi_text = m.group("lo"), m.group("hi")
        if hi_text is not None and _number(lo_text) <= _number(hi_text):
            target = Target(
                GuidanceUnit.MONEY,
                TargetType.RANGE,
                _number(lo_text),
                _number(hi_text),
                None,
                _decimals(lo_text, hi_text),
                m.span(),
                m.group(0).strip(),
            )
        else:
            target = Target(
                GuidanceUnit.MONEY,
                TargetType.POINT,
                None,
                None,
                _number(lo_text),
                _decimals(lo_text),
                m.span(),
                m.group(0).strip(),
            )
        target.currency = Currency(code)
        target.scale_to_crore = scale
        found.append(target)
    return sorted(found, key=lambda t: t.span)


def _applicable(rule_sectors: str, family: str) -> bool:
    if rule_sectors == UNIVERSAL:
        return True
    if family == "UNKNOWN":
        return False  # an unresolved business gets universal rules only
    return (rule_sectors == FINANCIAL) == (family == FINANCIAL)


def match_subjects(
    sentence: str, family: str, segments: list[Segment]
) -> tuple[list[tuple[SubjectRule | Segment, tuple[int, int]]], list[SubjectRule]]:
    """Registered subjects in the sentence (earlier registry rules win overlaps) and the
    subjects the company's sector family rules out."""
    accepted: list[tuple[SubjectRule | Segment, tuple[int, int]]] = []
    blocked: list[SubjectRule] = []
    for segment in segments:
        for name in segment.names:
            n = re.escape(name)
            m = re.search(
                rf"\b{n}\b(?:\s+[\w-]+){{0,3}}?\s+(?:revenues?\s+)?grow\w*"
                rf"|\bgrow\w*\s+(?:in|for|of)\s+(?:the\s+|our\s+)?{n}\b",
                sentence,
                re.I,
            )
            if m:
                accepted.append((segment, m.span()))
                break
    for rule in SUBJECTS:
        m = rule.pattern.search(sentence)
        if m is None:
            continue
        if any(not (m.end() <= a or m.start() >= b) for _, (a, b) in accepted):
            continue
        if accepted and rule.code == "REVENUE_GROWTH" and isinstance(accepted[0][0], Segment):
            continue
        if not _applicable(rule.sectors, family):
            blocked.append(rule)
            continue
        accepted.append((rule, m.span()))
    return accepted, blocked


def resolve_period(
    sentence: str, turn_text: str, event: ManagementEvent, fy_end: int | None
) -> tuple[ReportingPeriod | None, str | None]:
    explicit: dict[tuple[PeriodType, int | None, int | None], ReportingPeriod] = {}
    for m in PERIOD_LABEL.finditer(sentence):
        period = fiscal_period(m, fy_end, "EXPLICIT_LABEL")
        if period is not None:
            explicit.setdefault(
                (period.period_type, period.fiscal_year, period.fiscal_quarter), period
            )
    if len(explicit) == 1:
        return next(iter(explicit.values())), None
    if len(explicit) > 1:
        return None, "PERIOD_AMBIGUOUS"
    current = event.reporting_period
    phrase = TURN_PERIOD.search(sentence)
    if (
        phrase is not None
        and current is not None
        and current.fiscal_year is not None
        and current.period_type == PeriodType.QUARTER
        and current.fiscal_quarter in (1, 2, 3)
    ):
        # 'for the year' on a Q1-Q3 call: the fiscal year in progress, as printed in the title
        return (
            ReportingPeriod(
                period_type=PeriodType.FY,
                fiscal_year=current.fiscal_year,
                fiscal_quarter=None,
                period_end=month_end(current.fiscal_year, fy_end) if fy_end else None,
                raw_text=phrase.group(0),
                method="EVENT_CONTEXT",
            ),
            None,
        )
    years: dict[int | None, ReportingPeriod] = {}
    for m in PERIOD_LABEL.finditer(turn_text):
        period = fiscal_period(m, fy_end, "TURN_CONTEXT")
        if period is not None and period.period_type == PeriodType.FY:
            years.setdefault(period.fiscal_year, period)
    if len(years) == 1:
        return next(iter(years.values())), None
    return None, "PERIOD_UNKNOWN"


def _currency(qualifier: str | None, exact_reason: str) -> tuple[Equivalence, str]:
    """Growth actuals are in reported currency: only explicitly reported-currency guidance
    measures the same thing."""
    if qualifier == "REPORTED_CURRENCY":
        return Equivalence.EXACT, exact_reason
    if qualifier == "CONSTANT_CURRENCY":
        return Equivalence.PROXY, "constant-currency guidance vs reported-currency actual"
    return (
        Equivalence.PROXY,
        "the sentence does not say whether growth is in constant or reported currency",
    )


def _link(subject: SubjectRule | Segment, qualifier: str | None) -> tuple[SubjectLink, str]:
    if isinstance(subject, Segment):
        equivalence, reason = _currency(
            qualifier, f"Box 7 segment {subject.key!r} revenue, year on year"
        )
        return (
            SubjectLink(
                subject_code=SEGMENT_SUBJECT,
                layer=SubjectLayer.BOX7_SEGMENT,
                target_code=f"{SEGMENT_SUBJECT}:{subject.key}",
                equivalence=equivalence,
                reason=reason,
            ),
            SEGMENT_SUBJECT,
        )
    equivalence, reason = subject.equivalence, subject.note
    if subject.code in CURRENCY_SENSITIVE and subject.equivalence == Equivalence.EXACT:
        equivalence, reason = _currency(qualifier, subject.note)
    return (
        SubjectLink(
            subject_code=subject.code,
            layer=subject.layer,
            target_code=subject.target_code,
            equivalence=equivalence,
            reason=reason,
        ),
        subject.code,
    )


def _stated(sentence: str) -> StatedChange:
    for pattern, change in STATED_CHANGE:
        if pattern.search(sentence):
            return change
    return StatedChange.NONE


def _direction(text: str) -> Direction:
    up, down = bool(DIRECTION_UP.search(text)), bool(DIRECTION_DOWN.search(text))
    if up and not down:
        return Direction.INCREASE
    if down and not up:
        return Direction.DECREASE
    return Direction.UNKNOWN


class GuidanceBuilder:
    def __init__(
        self,
        events: dict[UUID, ManagementEvent],
        family: str,
        segments: list[Segment],
        fy_end: int | None,
        max_chars: int,
    ):
        self.events = events
        self.family = family
        self.segments = segments
        self.fy_end = fy_end
        self.max_chars = max_chars
        self.out = GuidanceOutput()

    def _unparsed(self, ctx: StatementContext, kind: str, reason: str) -> None:
        s = ctx.statement
        self.out.unparsed.append(
            UnparsedItem(
                item_id=uuid5(NS, f"unparsed|{s.statement_id}|{kind}"),
                kind=kind,
                text=ctx.sentence[: self.max_chars],
                reason=reason,
                statement_id=s.statement_id,
                provenance=s.provenance,
            )
        )

    def run(self, statements: list[StatementContext]) -> GuidanceOutput:
        for ctx in statements:
            s = ctx.statement
            if not s.forward_looking or s.historical_reference:
                continue
            types = set(s.statement_types)
            sentence = ctx.sentence
            targets = numeric_targets(sentence)
            subjects, blocked = match_subjects(sentence, self.family, self.segments)
            stated = _stated(sentence)
            guiding = bool(types & _GUIDING)
            if not subjects:
                if blocked and (targets or guiding):
                    self._unparsed(
                        ctx,
                        "SUBJECT_SECTOR_BLOCKED",
                        f"{', '.join(r.code for r in blocked)} is not registered for "
                        f"{self.family} companies",
                    )
                elif targets and guiding:
                    self._unparsed(
                        ctx,
                        "SUBJECT_UNLINKED",
                        "a numeric target with no registered subject in the sentence",
                    )
                continue
            if len(subjects) > 1:
                if targets or guiding:
                    names = ", ".join(
                        x.key if isinstance(x, Segment) else x.code for x, _ in subjects
                    )
                    self._unparsed(
                        ctx, "GUIDANCE_AMBIGUOUS", f"several subjects in one sentence: {names}"
                    )
                continue
            subject, span = subjects[0]
            unit = GuidanceUnit.PERCENT if isinstance(subject, Segment) else subject.unit
            accepts = {
                GuidanceUnit.PERCENT: {GuidanceUnit.PERCENT, GuidanceUnit.BASIS_POINTS},
                GuidanceUnit.MONEY: {GuidanceUnit.MONEY},
            }.get(unit, set())
            compatible = [t for t in targets if t.unit in accepts]
            if len(compatible) > 1 or (compatible and len(targets) > 1):
                self._unparsed(
                    ctx,
                    "GUIDANCE_VALUE_AMBIGUOUS",
                    f"{len(targets)} numeric values in a sentence with one subject",
                )
                continue
            if compatible and IMPACT.search(sentence):
                self._unparsed(
                    ctx,
                    "GUIDANCE_VALUE_AMBIGUOUS",
                    "the number describes an impact / contribution, not the guided level",
                )
                continue
            if not compatible and targets:
                if guiding:
                    self._unparsed(
                        ctx,
                        "UNIT_UNKNOWN",
                        "the sentence's numbers do not carry the subject's unit",
                    )
                continue
            target = compatible[0] if compatible else None
            rest = sentence[: span[0]] + " " + sentence[span[1] :]
            direction = _direction(rest)
            if target is None:
                if stated == StatedChange.WITHDRAWN and types & {ST.GUIDANCE}:
                    self._item(
                        ctx, subject, None, TargetType.QUALITATIVE, Direction.UNKNOWN, unit, stated
                    )
                elif guiding and (verb := DIRECTION_AFTER_SUBJECT.match(sentence[span[1] :])):
                    word = verb.group("verb").lower()
                    stated_direction = (
                        Direction.STABLE
                        if word.startswith(("remain", "stabil"))
                        else _direction(word)
                    )
                    if stated_direction != Direction.UNKNOWN:
                        self._item(
                            ctx, subject, None, TargetType.DIRECTION, stated_direction, unit, stated
                        )
                continue
            self._item(ctx, subject, target, target.target_type, direction, target.unit, stated)
        self._lifecycle()
        return self.out

    def _item(
        self,
        ctx: StatementContext,
        subject: SubjectRule | Segment,
        target: Target | None,
        target_type: TargetType,
        direction: Direction,
        unit: GuidanceUnit,
        stated: StatedChange,
    ) -> None:
        s = ctx.statement
        event = self.events[s.event_id]
        sentence = ctx.sentence
        constant, reported = CONSTANT_CURRENCY.search(sentence), REPORTED_CURRENCY.search(sentence)
        qualifier = (
            "CONSTANT_CURRENCY"
            if constant and not reported
            else "REPORTED_CURRENCY"
            if reported and not constant
            else None
        )
        link, code = _link(subject, qualifier)
        measure = Measure.GROWTH if isinstance(subject, Segment) else subject.measure
        period, period_warning = resolve_period(sentence, ctx.turn.text, event, self.fy_end)
        warnings = [period_warning] if period_warning else []
        if period is not None and period.period_end is None:
            warnings.append("FISCAL_YEAR_END_UNKNOWN")
        consolidated, standalone = _CONSOLIDATED.search(sentence), _STANDALONE.search(sentence)
        if bool(consolidated) != bool(standalone):
            basis = ReportingBasis.CONSOLIDATED if consolidated else ReportingBasis.STANDALONE
            basis_method = "STATEMENT_TEXT"
        else:
            basis, basis_method = event.reporting_basis, event.basis_method
        if link.layer == SubjectLayer.UNLINKED:
            warnings.append("SUBJECT_UNLINKED")
        if target is not None and target.unit == GuidanceUnit.BASIS_POINTS:
            warnings.append("CHANGE_TARGET")
        if (
            target is not None
            and target.unit == GuidanceUnit.MONEY
            and target.scale_to_crore is None
        ):
            warnings.append("SCALE_UNKNOWN")
        condition = CONDITION.search(sentence)
        if target is not None and target.target_type != TargetType.RANGE and target.raw:
            if any(w in target.raw.lower() for w in ("around", "about", "approximately", "~")):
                warnings.append("APPROXIMATE_TARGET")
        if target is not None:
            confidence = (
                Confidence.HIGH
                if period and period.method == "EXPLICIT_LABEL"
                else (Confidence.MEDIUM)
            )
        else:
            confidence = (
                Confidence.MEDIUM if target_type == TargetType.QUALITATIVE else (Confidence.LOW)
            )
        self.out.guidance.append(
            GuidanceItem(
                guidance_id=uuid5(NS, f"guidance|{s.statement_id}|{code}"),
                statement_ids=(s.statement_id,),
                event_id=s.event_id,
                issued_date=event.call_date,
                issued_period=event.reporting_period,
                subject=link,
                measure=measure,
                qualifier=qualifier,
                target_type=target_type,
                low=target.low if target else None,
                high=target.high if target else None,
                value=target.value if target else None,
                decimals=target.decimals if target else 0,
                direction=direction,
                unit=unit,
                currency=target.currency if target else None,
                scale_to_crore=target.scale_to_crore if target else None,
                target_period=period,
                reporting_basis=basis,
                basis_method=basis_method,
                raw_target_text=(target.raw if target else sentence)[: self.max_chars],
                condition_text=condition.group(0)[:300] if condition else None,
                stated_change=stated,
                lifecycle=Lifecycle.ISSUED,
                confidence=confidence,
                warnings=tuple(sorted(set(warnings))),
            )
        )

    # --- lifecycle --------------------------------------------------------------------------

    def _lifecycle(self) -> None:
        order = {e.event_id: i for i, e in enumerate(self.events.values())}
        statement_rank = {
            g.guidance_id: (
                g.issued_date or date.max,
                order.get(g.event_id, 0),
                index,
            )
            for index, g in enumerate(self.out.guidance)
        }
        groups: dict[tuple[object, ...], list[GuidanceItem]] = {}
        updated: dict[UUID, GuidanceItem] = {}
        for g in self.out.guidance:
            if g.target_period is None or g.issued_date is None:
                extra = (
                    ["LIFECYCLE_PERIOD_UNKNOWN"]
                    if g.target_period is None
                    else ["LIFECYCLE_ORDER_UNKNOWN"]
                )
                updated[g.guidance_id] = g.model_copy(
                    update={"warnings": tuple(sorted({*g.warnings, *extra}))}
                )
                continue
            key = (
                g.subject.subject_code,
                g.subject.target_code,
                g.measure,
                g.target_period.key(),
                g.unit,
                g.currency,
            )
            groups.setdefault(key, []).append(g)
        for group, items in groups.items():
            items.sort(key=lambda g: statement_rank[g.guidance_id])
            chain: list[GuidanceItem] = []
            for g in items:
                # compare with the latest item from an EARLIER event; a repeat within one call
                # is compared with the same call's first mention only when no earlier call exists
                earlier = [c for c in chain if c.event_id != g.event_id]
                previous = earlier[-1] if earlier else (chain[-1] if chain else None)
                warnings = set(g.warnings)
                state: Lifecycle = Lifecycle.ISSUED
                link: UUID | None = None
                if previous is not None and (
                    ReportingBasis.UNKNOWN not in (previous.reporting_basis, g.reporting_basis)
                    and previous.reporting_basis != g.reporting_basis
                ):
                    warnings.add("BASIS_CHANGED")
                elif previous is not None and (
                    None not in (previous.qualifier, g.qualifier)
                    and previous.qualifier != g.qualifier
                ):
                    warnings.add("QUALIFIER_CHANGED")
                elif previous is not None:
                    compared = _compare(previous, g)
                    if compared is None:
                        warnings.add("LIFECYCLE_FORM_CHANGED")
                    else:
                        state, link = compared, previous.guidance_id
                elif g.stated_change == StatedChange.WITHDRAWN:
                    state = Lifecycle.WITHDRAWN
                if (
                    g.stated_change in _COMPATIBLE_STATED
                    and state != Lifecycle.ISSUED
                    and state not in _COMPATIBLE_STATED[g.stated_change]
                ):
                    warnings.add("STATED_CHANGE_MISMATCH")
                if state == Lifecycle.ISSUED and g.stated_change not in (
                    StatedChange.NONE,
                    StatedChange.MAINTAINED,
                ):
                    warnings.add("PREVIOUS_GUIDANCE_NOT_FOUND")
                item = g.model_copy(
                    update={
                        "lifecycle": state,
                        "previous_guidance_id": link,
                        "warnings": tuple(sorted(warnings)),
                    }
                )
                updated[g.guidance_id] = item
                chain.append(item)
            subject, target_code, _, period_key, unit, currency = group
            qualifiers = [i.qualifier for i in chain if i.qualifier is not None]
            self.out.timelines.append(
                GuidanceTimeline(
                    timeline_id=uuid5(NS, "timeline|" + "|".join(str(k) for k in group)),
                    subject_code=str(subject),
                    qualifier=qualifiers[0] if qualifiers else None,
                    target_period_key=f"{period_key}|{unit}|{currency}|{target_code}",
                    guidance_ids=tuple(i.guidance_id for i in chain),
                    lifecycles=tuple(i.lifecycle for i in chain),
                )
            )
        self.out.guidance = [updated[g.guidance_id] for g in self.out.guidance]
        self.out.timelines.sort(key=lambda t: str(t.timeline_id))


def _bounds(g: GuidanceItem) -> tuple[Decimal, Decimal] | None:
    if g.target_type == TargetType.RANGE and g.low is not None and g.high is not None:
        return g.low, g.high
    if g.target_type in (TargetType.POINT, TargetType.MINIMUM, TargetType.MAXIMUM) and (
        g.value is not None
    ):
        return g.value, g.value
    return None


_DIRECTION_RANK = {Direction.DECREASE: 0, Direction.STABLE: 1, Direction.INCREASE: 2}


def _compare(previous: GuidanceItem, current: GuidanceItem) -> Lifecycle | None:
    if current.stated_change == StatedChange.WITHDRAWN:
        return Lifecycle.WITHDRAWN
    if previous.lifecycle == Lifecycle.WITHDRAWN:
        return None
    old, new = _bounds(previous), _bounds(current)
    if old is not None and new is not None:
        if previous.target_type != current.target_type and TargetType.RANGE not in (
            previous.target_type,
            current.target_type,
        ):
            return None
        (plo, phi), (lo, hi) = old, new
        if (lo, hi) == (plo, phi):
            return Lifecycle.REITERATED
        if lo >= plo and hi <= phi:
            return Lifecycle.NARROWED
        if lo <= plo and hi >= phi:
            return Lifecycle.WIDENED
        return Lifecycle.RAISED if lo + hi > plo + phi else Lifecycle.LOWERED
    if previous.target_type == current.target_type == TargetType.DIRECTION:
        a, b = _DIRECTION_RANK.get(previous.direction), _DIRECTION_RANK.get(current.direction)
        if a is None or b is None:
            return None
        if a == b:
            return Lifecycle.REITERATED
        return Lifecycle.RAISED if b > a else Lifecycle.LOWERED
    return None
