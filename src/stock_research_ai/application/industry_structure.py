"""DETAILED_RESEARCH_V1 Box 9: industry structure, demand / pricing signals, themes.

* Structure observations: registered topics in management statements and company
  documents, each kept as the explicit sentence with its nature (assertion vs disclosure)
  - no Five-Forces or moat score.
* Demand / pricing signals: a registered subject word ('demand', 'pricing') with an
  explicit registered direction word within a few words; several directions -> MIXED.
  Direction is never inferred from tone. Analyst questions are not signals.
* Themes: Box 8's own registered topics, aggregated across companies.
* Conflicts: INCREASING vs DECREASING signals from different companies for the same
  printed period are preserved, never resolved.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID, uuid5

from stock_research_ai.application.competitive_evidence import TextUnit
from stock_research_ai.application.peer_registry import (
    DEMAND_SUBJECT,
    DIRECTION_WORDS,
    NEGATION,
    NEGATION_WINDOW,
    NS,
    PRICING_SUBJECT,
    SIGNAL_WINDOW,
    STRUCTURE_TOPICS,
)
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain.industry_intelligence import (
    IndustryEvidenceConflict,
    IndustryStructureObservation,
    IndustryTheme,
    MarketSignal,
    Nature,
    SignalDirection,
)

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def structure_observations(units: list[TextUnit]) -> list[IndustryStructureObservation]:
    out = []
    for u in units:
        if u.nature == Nature.ANALYST_QUESTION:
            continue
        for topic, pattern in STRUCTURE_TOPICS:
            if pattern.search(u.text):
                out.append(
                    IndustryStructureObservation(
                        observation_id=uuid5(
                            NS, f"structure|{topic}|{u.layer.value}|{u.record_id}|{u.locator}"
                        ),
                        topic=topic,
                        company_key=u.company_key,
                        statement=u.text,
                        nature=u.nature,
                        authority=u.authority,
                        document_type=u.document_type,
                        event_date=u.event_date,
                        period_text=u.period_text,
                        source=u.ref(),
                    )
                )
    return out


def _direction(text: str, subject: re.Pattern[str]) -> tuple[SignalDirection, list[str], list[str]]:
    words = [(m.group(0), m.start()) for m in _WORD.finditer(text)]
    lowered = [w.lower() for w, _ in words]
    subjects = [i for i, (w, _) in enumerate(words) if subject.fullmatch(w)]
    found: dict[str, list[str]] = defaultdict(list)
    for i in subjects:
        window = range(max(0, i - SIGNAL_WINDOW), min(len(words), i + SIGNAL_WINDOW + 1))
        for j in window:
            if j == i:
                continue
            if any(NEGATION.fullmatch(lowered[n]) for n in range(max(0, j - NEGATION_WINDOW), j)):
                continue
            for name, pattern in DIRECTION_WORDS:
                candidate = lowered[j]
                if j + 1 < len(lowered) and pattern.fullmatch(f"{candidate} {lowered[j + 1]}"):
                    found[name].append(f"{candidate} {lowered[j + 1]}")
                elif pattern.fullmatch(candidate):
                    found[name].append(candidate)
    subject_words = sorted({words[i][0].lower() for i in subjects})
    if not found:
        return SignalDirection.UNKNOWN, [], subject_words
    names = sorted(found)
    direction = SignalDirection(names[0]) if len(names) == 1 else SignalDirection.MIXED
    return direction, sorted({w for n in names for w in found[n]}), subject_words


def market_signals(units: list[TextUnit]) -> tuple[list[MarketSignal], list[MarketSignal]]:
    demand: list[MarketSignal] = []
    pricing: list[MarketSignal] = []
    for u in units:
        if u.nature in (Nature.ANALYST_QUESTION, Nature.AGGREGATOR_DATA):
            continue
        for kind, subject, bucket in (
            ("DEMAND", DEMAND_SUBJECT, demand),
            ("PRICING", PRICING_SUBJECT, pricing),
        ):
            if not subject.search(u.text):
                continue
            direction, direction_words, subject_words = _direction(u.text, subject)
            bucket.append(
                MarketSignal(
                    signal_id=uuid5(NS, f"signal|{kind}|{u.layer.value}|{u.record_id}|{u.locator}"),
                    kind=kind,  # type: ignore[arg-type]
                    company_key=u.company_key,
                    direction=direction,
                    direction_words=tuple(direction_words),
                    subject_words=tuple(subject_words),
                    quote=u.text,
                    nature=u.nature,
                    event_date=u.event_date,
                    period_text=u.period_text,
                    source=u.ref(),
                )
            )
    return demand, pricing


def signal_conflicts(signals: list[MarketSignal]) -> list[IndustryEvidenceConflict]:
    """INCREASING vs DECREASING from different companies for the same printed period."""
    groups: dict[tuple[str, str], list[MarketSignal]] = defaultdict(list)
    for s in signals:
        if s.direction in (SignalDirection.INCREASING, SignalDirection.DECREASING) and (
            s.period_text
        ):
            groups[(s.kind, " ".join(s.period_text.upper().replace("'", "").split()))].append(s)
    out = []
    for (kind, period), items in sorted(groups.items()):
        firsts: dict[tuple[str, SignalDirection], MarketSignal] = {}
        for s in sorted(items, key=lambda s: str(s.signal_id)):
            firsts.setdefault((s.company_key, s.direction), s)
        ups = [s for (c, d), s in sorted(firsts.items()) if d == SignalDirection.INCREASING]
        downs = [s for (c, d), s in sorted(firsts.items()) if d == SignalDirection.DECREASING]
        for a in ups:
            for b in downs:
                if a.company_key == b.company_key:
                    continue
                x, y = sorted([a, b], key=lambda s: str(s.signal_id))
                out.append(
                    IndustryEvidenceConflict(
                        conflict_id=uuid5(NS, f"conflict|{kind}|{x.signal_id}|{y.signal_id}"),
                        topic=kind,
                        record_a=x.signal_id,
                        record_b=y.signal_id,
                        company_a=x.company_key,
                        company_b=y.company_key,
                        claim_a=f"{kind} {x.direction.value} ({period}): {x.quote[:300]}",
                        claim_b=f"{kind} {y.direction.value} ({period}): {y.quote[:300]}",
                        nature_a=x.nature,
                        nature_b=y.nature,
                        date_a=x.event_date,
                        date_b=y.event_date,
                    )
                )
    return out


@dataclass
class _ThemeAcc:
    companies: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    events: set[UUID] = field(default_factory=set)
    mentions: int = 0
    dates: list[date] = field(default_factory=list)
    ids: list[UUID] = field(default_factory=list)


def industry_themes(companies: list[CompanyLayers]) -> list[IndustryTheme]:
    acc: dict[str, _ThemeAcc] = {}
    for c in companies:
        if c.box8 is None:
            continue
        for t in c.box8.themes:
            e = acc.setdefault(t.topic, _ThemeAcc())
            e.companies.add(c.company_key)
            e.sources.add(t.source)
            e.events.update(t.event_ids)
            e.mentions += t.mentions
            e.dates.extend(d for d in (t.first_date, t.last_date) if d)
            e.ids.append(t.theme_id)
    return [
        IndustryTheme(
            theme_id=uuid5(NS, f"theme|{topic}|" + "|".join(sorted(e.companies))),
            topic=topic,
            companies=tuple(sorted(e.companies)),
            sources=tuple(sorted(e.sources)),  # type: ignore[arg-type]
            events=len(e.events),
            mentions=e.mentions,
            first_date=min(e.dates) if e.dates else None,
            last_date=max(e.dates) if e.dates else None,
            evidence_ids=tuple(sorted(e.ids, key=str)),
        )
        for topic, e in sorted(acc.items())
    ]
