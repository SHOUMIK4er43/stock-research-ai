"""DETAILED_RESEARCH_V1 Box 8: management statements.

Each sentence a MANAGEMENT speaker (or a company document) says is classified by the
versioned statement rules (management-registry/1). A sentence becomes a statement when it
matches at least one statement type or carries an explicit forward-looking marker;
pleasantries ('Thank you, Rishi.') do not. The quote is the sentence exactly as the
whitespace-normalised Box 3 text has it, and its provenance names the Box 3 blocks the
sentence spans.
"""

import re
from dataclasses import dataclass
from uuid import UUID, uuid5

from stock_research_ai.application.management_calls import TurnText, topics_of
from stock_research_ai.application.management_registry import (
    ABBREVIATIONS,
    EXPECTATION,
    FORWARD,
    HISTORICAL_GUIDANCE,
    NS,
    PERIOD_LABEL,
    SENTENCE_END,
    STATEMENT_RULES,
)
from stock_research_ai.application.semantic_guards import mask
from stock_research_ai.domain.management_intelligence import (
    Confidence,
    ManagementEvent,
    ManagementStatement,
    Role,
    StatementType,
)

ST = StatementType
_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|per ?cent|basis points|bps|crores?|lakhs?|bn|million)"
    r"|(?:₹|rs\.?|inr|us\$|\$|usd)\s*\d",
    re.I,
)
# Matched against the REVERSED text: anchored and linear. The equivalent forward search
# `(\w+)\W*$` retries every start position and is quadratic on long words.
_LAST_WORD_REVERSED = re.compile(r"\W*(\w+)")
_MIN_WORDS = 5
#: call facilitation, never evidence about the company: hand-overs, Q&A logistics and
#: sign-offs ('With that, I will hand it over to Rohit', 'I will take them one by one',
#: 'Look forward to meeting you again next quarter'). Kept in the transcript turns only.
_FILLER = re.compile(
    r"\bhand(?:ing)? (?:it |this |the (?:call|floor|line) )?(?:back )?over to\b"
    r"|\btake (?:them|these|those|your questions) one by one\b"
    r"|\blook(?:ing)? forward to (?:meeting|speaking|talking|seeing|interacting)\b"
    r"|\b(?:begin|start|open|move to|now take) (?:the )?(?:q ?& ?a|question[- ]and[- ]answer"
    r"|questions)\b"
    r"|^\W*(?:thank you|thanks)(?: (?:all|everyone|so much|very much))?\W*$",
    re.I,
)


def _last_word(text: str) -> str | None:
    match = _LAST_WORD_REVERSED.match(text[::-1])
    return match.group(1)[::-1] if match else None


def sentences(text: str) -> list[tuple[int, int]]:
    """Sentence spans; a period after a registered abbreviation or an initial ('M. B.')
    does not end a sentence."""
    spans = []
    start = 0
    for m in SENTENCE_END.finditer(text):
        end = m.start() + len(m.group().rstrip())
        word = _last_word(text[start : m.start()])
        if word and (word.lower() in ABBREVIATIONS or (len(word) == 1 and word.isupper())):
            continue
        spans.append((start, end))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def classify(sentence: str) -> tuple[list[StatementType], bool, bool]:
    # a keyword in its non-financial sense ('technical debt', 'chip prices', 'Target Group')
    # is never a statement type
    meaning = mask(sentence)
    types = [kind for kind, pattern in STATEMENT_RULES if pattern.search(meaning)]
    if EXPECTATION.search(sentence):
        lead = [t for t in types if t in (ST.GUIDANCE, ST.TARGET)]
        types = [*lead, ST.EXPECTATION, *[t for t in types if t not in lead]]
    forward = bool(FORWARD.search(sentence))
    historical = bool(HISTORICAL_GUIDANCE.search(sentence))
    return types, forward, historical


@dataclass
class StatementContext:
    statement: ManagementStatement
    sentence: str
    turn: TurnText


def names_later_year(sentence: str, current: int | None) -> bool:
    """Tense rule: a sentence naming a fiscal year after the call's own fiscal year speaks
    about the future ('Our EBIT margin band for FY27 is ...' on a Q4 FY26 call)."""
    if current is None:
        return False
    for m in PERIOD_LABEL.finditer(sentence):
        text = m.group("qy") or m.group("fy2") or m.group("fy")
        year = int(text) + (2000 if len(text) == 2 else 0)
        if year > current:
            return True
    return False


def extract_statements(
    turns: list[TurnText],
    max_chars: int,
    events: dict[UUID, ManagementEvent] | None = None,
) -> list[StatementContext]:
    out: list[StatementContext] = []
    for t in turns:
        if t.role != Role.MANAGEMENT:
            continue
        event = (events or {}).get(t.turn.event_id)
        current = (
            event.reporting_period.fiscal_year
            if event is not None and event.reporting_period is not None
            else None
        )
        for index, (start, end) in enumerate(sentences(t.text)):
            sentence = t.text[start:end].strip()
            if len(sentence.split()) < _MIN_WORDS:
                continue
            if _FILLER.search(sentence) and not re.search(r"\d", sentence):
                continue
            types, forward, historical = classify(sentence)
            forward = forward or names_later_year(sentence, current)
            if not types and not forward:
                continue
            types = types or [ST.OTHER]
            numeric = bool(_NUMBER.search(sentence))
            if types[0] in (ST.GUIDANCE, ST.TARGET) and numeric:
                confidence = Confidence.HIGH
            elif {ST.GUIDANCE, ST.TARGET, ST.EXPECTATION} & set(types) or (forward and numeric):
                confidence = Confidence.MEDIUM
            else:
                confidence = Confidence.LOW
            spanned = {b.block_id: b for a, z, b in t.spans if a < end and z > start}
            blocks = list(spanned.values())
            if not blocks and t.spans:
                blocks = [t.spans[0][2]]
            provenance = (
                t.source.provenance(blocks, sentence_index=index)
                if blocks
                else t.turn.provenance.model_copy(
                    update={
                        "sentence_index": index,
                        "locator": f"{t.turn.provenance.locator}/s{index}",
                    }
                )
            )
            statement = ManagementStatement(
                statement_id=uuid5(NS, f"statement|{t.turn.turn_id}|{index}"),
                event_id=t.turn.event_id,
                turn_id=t.turn.turn_id,
                speaker_id=t.turn.participant_id,
                speaker_role=t.role,
                quote=sentence[:max_chars],
                normalized_text=" ".join(sentence.lower().split())[:max_chars],
                statement_types=tuple(types),
                topics=topics_of(sentence),
                forward_looking=forward,
                historical_reference=historical,
                confidence=confidence,
                provenance=provenance,
            )
            out.append(StatementContext(statement, sentence, t))
    return out
