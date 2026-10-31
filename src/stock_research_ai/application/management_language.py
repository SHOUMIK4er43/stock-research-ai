"""DETAILED_RESEARCH_V1 Box 8: descriptive language metrics and themes.

Language metrics count the registered hedging / certainty phrases (management-language/1)
in management speech per event and section, the rate per 1,000 words, and the change from
the previous dated event of the same type. Themes count the registered topics in
management statements and in analyst / journalist questions across events. Both are
evidence: neither labels management.
"""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.management_calls import TurnText
from stock_research_ai.application.management_registry import (
    CERTAINTY_PATTERNS,
    HEDGING_PATTERNS,
    NS,
)
from stock_research_ai.domain.management_intelligence import (
    LANGUAGE_LEXICON_VERSION,
    LanguageMetrics,
    ManagementEvent,
    ManagementStatement,
    QAPair,
    Role,
    Section,
    Theme,
)

_RATE = Decimal("0.0001")


def _rate(count: int, words: int) -> Decimal | None:
    if words == 0:
        return None
    return (Decimal(count) * 1000 / Decimal(words)).quantize(_RATE, rounding=ROUND_HALF_EVEN)


def chronology(events: Sequence[ManagementEvent]) -> dict[UUID, tuple[date, int]]:
    """Event order: printed call date, then document / sequence order; undated last."""
    return {e.event_id: (e.call_date or date.max, i) for i, e in enumerate(events)}


def language_metrics(
    events: Sequence[ManagementEvent], turns: Sequence[TurnText]
) -> list[LanguageMetrics]:
    order = chronology(events)
    texts: dict[tuple[UUID, Section], list[str]] = {}
    for t in turns:
        if t.role == Role.MANAGEMENT:
            texts.setdefault((t.turn.event_id, t.turn.section), []).append(t.text)
    by_event = {e.event_id: e for e in events}
    rows: list[LanguageMetrics] = []
    for (event_id, section), parts in sorted(
        texts.items(), key=lambda kv: (order[kv[0][0]], kv[0][1].value)
    ):
        text = " ".join(parts)
        counts: dict[str, int] = {}
        hedging = certainty = 0
        for phrase, pattern in HEDGING_PATTERNS.items():
            n = len(pattern.findall(text))
            if n:
                counts[f"hedging:{phrase}"] = n
                hedging += n
        for phrase, pattern in CERTAINTY_PATTERNS.items():
            n = len(pattern.findall(text))
            if n:
                counts[f"certainty:{phrase}"] = n
                certainty += n
        n_words = len(text.split())
        rows.append(
            LanguageMetrics(
                metrics_id=uuid5(
                    NS, f"language|{event_id}|{section.value}|{LANGUAGE_LEXICON_VERSION}"
                ),
                event_id=event_id,
                section=section,
                lexicon_version=LANGUAGE_LEXICON_VERSION,
                management_words=n_words,
                hedging_count=hedging,
                certainty_count=certainty,
                hedging_per_1000_words=_rate(hedging, n_words),
                certainty_per_1000_words=_rate(certainty, n_words),
                phrase_counts=dict(sorted(counts.items())),
            )
        )
    # call-over-call change: the previous dated event of the same type, same section
    out = []
    for row in rows:
        event = by_event[row.event_id]
        previous = None
        if event.call_date is not None:
            earlier = [
                r
                for r in rows
                if r.section == row.section
                and by_event[r.event_id].event_type == event.event_type
                and by_event[r.event_id].call_date is not None
                and order[r.event_id] < order[row.event_id]
            ]
            previous = max(earlier, key=lambda r: order[r.event_id]) if earlier else None
        if previous is None:
            out.append(row)
            continue

        def delta(a: Decimal | None, b: Decimal | None) -> Decimal | None:
            return a - b if a is not None and b is not None else None

        out.append(
            row.model_copy(
                update={
                    "previous_event_id": previous.event_id,
                    "hedging_rate_change": delta(
                        row.hedging_per_1000_words, previous.hedging_per_1000_words
                    ),
                    "certainty_rate_change": delta(
                        row.certainty_per_1000_words, previous.certainty_per_1000_words
                    ),
                }
            )
        )
    return out


def themes(
    events: Sequence[ManagementEvent],
    statements: Sequence[ManagementStatement],
    qa_pairs: Sequence[QAPair],
) -> list[Theme]:
    order = chronology(events)
    by_event = {e.event_id: e for e in events}
    mentions: dict[tuple[str, str], dict[UUID, int]] = {}
    for s in statements:
        for topic in s.topics:
            bucket = mentions.setdefault((topic, "MANAGEMENT"), {})
            bucket[s.event_id] = bucket.get(s.event_id, 0) + 1
    for q in qa_pairs:
        for topic in q.topics:
            bucket = mentions.setdefault((topic, "QUESTIONS"), {})
            bucket[q.event_id] = bucket.get(q.event_id, 0) + 1
    out = []
    for (topic, source), per_event in sorted(mentions.items()):
        ids = sorted(per_event, key=lambda e: order[e])
        first, last = by_event[ids[0]], by_event[ids[-1]]
        out.append(
            Theme(
                theme_id=uuid5(NS, f"theme|{topic}|{source}"),
                topic=topic,
                source=source,  # type: ignore[arg-type]
                event_ids=tuple(ids),
                mentions=sum(per_event.values()),
                first_event_id=first.event_id,
                last_event_id=last.event_id,
                first_date=first.call_date,
                last_date=last.call_date,
            )
        )
    return out
