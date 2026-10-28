"""DETAILED_RESEARCH_V1 Box 8: management events, participants, turns and Q&A pairs.

Box 3 transcript blocks -> events (one per printed call title: a transcript PDF may hold a
press conference AND an earnings call) -> participant lists exactly as printed -> speaker
turns -> prepared remarks / Q&A -> question/answer pairs.

Box 3's own speaker attribution is NOT trusted for roles: it defaults unknown speakers to
management and attributes list lines ('Business Standard', 'Membership No') as speakers.
Here a role comes only from a printed participant heading, the moderator / operator label
itself, or the moderator's own introduction ('the next question is from X'). Anything else
stays UNKNOWN.
"""

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID, uuid5

from stock_research_ai.application.management_registry import (
    DEFERRAL,
    EVENT_TITLE,
    EVENT_TYPES,
    EXPLICIT_DATE,
    MODERATOR_LABEL,
    NS,
    PARTICIPANT_HEADINGS,
    PERIOD_LABEL,
    QA_START,
    QUESTIONER_INTRO,
    TOPICS,
)
from stock_research_ai.domain.document_intelligence import (
    BlockType,
    DocumentExtraction,
    EvidenceKind,
    TextBlock,
)
from stock_research_ai.domain.evidence import EvidenceDocumentType
from stock_research_ai.domain.financial_canonical import PeriodType, ReportingBasis
from stock_research_ai.domain.management_intelligence import (
    AnswerStatus,
    EventType,
    ManagementEvent,
    ManagementLimits,
    Participant,
    Provenance,
    QAPair,
    ReportingPeriod,
    Role,
    RoleBasis,
    Section,
    Turn,
)

TRANSCRIPT_TYPES = {EvidenceDocumentType.CONCALL_TRANSCRIPT, EvidenceDocumentType.AGM_TRANSCRIPT}
#: Company documents whose Box 3 MANAGEMENT_GUIDANCE / MANAGEMENT_CLAIM evidence is management's
#: own written voice (no speakers, no Q&A).
DOCUMENT_TYPES = {
    EvidenceDocumentType.INVESTOR_PRESENTATION,
    EvidenceDocumentType.ANNUAL_REPORT,
    EvidenceDocumentType.SHAREHOLDER_LETTER,
}
DOCUMENT_EVIDENCE = {EvidenceKind.MANAGEMENT_GUIDANCE, EvidenceKind.MANAGEMENT_CLAIM}
_SKIP_BLOCKS = {BlockType.HEADER, BlockType.FOOTER, BlockType.TABLE_ROW, BlockType.TABLE_CAPTION}
_PAGE_MARKER = re.compile(r"^(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?$", re.I)
_HONORIFIC = re.compile(r"^(?:mr|ms|mrs|dr|shri|smt|prof)\.?\s+", re.I)
_NAME_TOKEN = re.compile(r"^(?:[A-Z][A-Za-z'’\-]*\.?|[A-Z]\.(?:[A-Z]\.)*)$")
_LABEL = re.compile(r"^(?P<label>[^:\n]{1,60}):\s*(?P<rest>.*)$", re.S)
_SEPARATOR = re.compile(r"\s+[–—-]\s+|,\s+")
_NOT_NAMES = {
    "note",
    "disclaimer",
    "page",
    "sub",
    "ref",
    "encl",
    "cin",
    "nse symbol",
    "bse scrip code",
    "membership no",
    "website",
    "email",
    "tel",
    "fax",
    "date",
    "time",
    "subject",
    "venue",
}
_FACILITATION = re.compile(r"\b(?:next|first|last) question\b|\bquestion is from\b", re.I)
_LONG_BLOCK_WORDS = 15
_COURTESY_WORDS = 15
_QUOTES = "\"'“”‘’ "


def normalize_name(raw: str) -> str:
    text = raw.strip().strip(":").strip()
    while True:
        stripped = _HONORIFIC.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"[.,’'`]", " ", text)
    return " ".join(text.lower().split())


def name_like(text: str, *, min_tokens: int = 1) -> bool:
    tokens = text.split()
    return (
        min_tokens <= len(tokens) <= 6
        and all(_NAME_TOKEN.match(t) for t in tokens)
        and not any(ch.isdigit() for ch in text)
        and normalize_name(text) not in _NOT_NAMES
    )


#: transcript labels for an unnamed questioner: never a company speaker
_GENERIC_QUESTIONERS = frozenset({
    "participant", "unidentified participant", "unknown participant", "analyst",
    "unidentified analyst", "questioner", "unidentified speaker", "audience member",
})
#: 'This is Sudhir from Kotak AMC' / 'I am X with Y Securities' at the start of a turn
_SELF_INTRO = re.compile(
    r"^(?:\W*(?i:hi|hello|good (?:morning|afternoon|evening)|thanks?(?: you)?)\b[^.]{0,40}[.,!]?)*"
    r"\s*(?i:this is|i am|i'm|my name is)\s+[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3}\s+"
    r"(?:from|with|of)\s+[A-Z]",
)


def _self_introduced(draft: "_Draft") -> bool:
    text = " ".join(piece for piece, _ in draft.pieces[:2])
    return _SELF_INTRO.search(clean(text)) is not None


def clean(text: str) -> str:
    return " ".join(text.split())


def words(text: str) -> int:
    return len(text.split())


def month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def fiscal_period(
    match: re.Match[str], fy_end_month: int | None, method: str
) -> ReportingPeriod | None:
    """'Q1 FY27' / 'FY27' / "fiscal '27" -> an explicit period; the end date only with the
    company's fiscal year-end month (Box 4)."""
    year_text = match.group("qy") or match.group("fy2") or match.group("fy")
    if year_text is None:
        return None
    year = int(year_text)
    year = year + 2000 if year < 100 else year
    if not 1990 <= year <= 2100:
        return None
    quarter = int(match.group("q")) if match.group("q") else None
    end: date | None = None
    if fy_end_month is not None:
        back = 3 * (4 - quarter) if quarter else 0
        index = year * 12 + (fy_end_month - 1) - back
        end = month_end(index // 12, index % 12 + 1)
    return ReportingPeriod(
        period_type=PeriodType.QUARTER if quarter else PeriodType.FY,
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_end=end,
        raw_text=match.group(0),
        method=method,
    )


def parse_date(text: str) -> date | None:
    m = EXPLICIT_DATE.search(text)
    if m is None:
        return None
    day = m.group("d1") or m.group("d2")
    month = m.group("m1") or m.group("m2")
    year = m.group("y1") or m.group("y2")
    try:
        return datetime.strptime(f"{int(day)} {month[:3]} {year}", "%d %b %Y").date()
    except ValueError:
        return None


def topics_of(text: str) -> tuple[str, ...]:
    return tuple(code for code, pattern in TOPICS if pattern.search(text))


@dataclass
class Source:
    """One Box 3 extraction plus its catalogue URL."""

    extraction: DocumentExtraction
    source_url: str
    title: str | None = None
    evidence_by_block: dict[UUID, list[UUID]] = field(default_factory=dict)

    def provenance(
        self,
        blocks: list[TextBlock],
        *,
        evidence_ids: tuple[UUID, ...] = (),
        sentence_index: int | None = None,
        pages: tuple[int, int] | None = None,
    ) -> Provenance:
        ex = self.extraction
        if blocks:
            start = min(b.page_number for b in blocks)
            end = max(b.page_number for b in blocks)
            first = min(blocks, key=lambda b: b.order)
            locator = f"{ex.artifact_sha256[:12]}/p{first.page_number}/block{first.order}"
        else:
            start, end = pages or (1, 1)
            locator = f"{ex.artifact_sha256[:12]}/p{start}/evidence"
        if not evidence_ids:
            seen: dict[UUID, None] = {}
            for b in blocks:
                for e in self.evidence_by_block.get(b.block_id, ()):
                    seen[e] = None
            evidence_ids = tuple(seen)
        return Provenance(
            document_id=ex.document_id,
            extraction_id=ex.extraction_id,
            document_type=ex.document_type,
            artifact_sha256=ex.artifact_sha256,
            source_url=self.source_url,
            authority=ex.authority,
            page_start=start,
            page_end=end,
            block_ids=tuple(b.block_id for b in blocks),
            evidence_ids=evidence_ids,
            sentence_index=sentence_index,
            locator=locator + (f"/s{sentence_index}" if sentence_index is not None else ""),
        )


@dataclass
class TurnText:
    """Full (unbounded) turn text with the Box 3 block behind every character span."""

    turn: Turn
    text: str
    spans: list[tuple[int, int, TextBlock]]
    role: Role
    source: Source


@dataclass
class CallOutput:
    events: list[ManagementEvent] = field(default_factory=list)
    participants: list[Participant] = field(default_factory=list)
    turns: list[TurnText] = field(default_factory=list)
    qa_pairs: list[QAPair] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (code, detail)


@dataclass
class _Entry:
    raw_name: str
    role: Role
    detail: list[str]
    blocks: list[TextBlock]


@dataclass
class _Draft:
    key: str
    raw_label: str
    blocks: list[TextBlock]
    pieces: list[tuple[str, TextBlock]]


def _content(blocks: tuple[TextBlock, ...]) -> list[TextBlock]:
    out = []
    for b in sorted(blocks, key=lambda b: b.order):
        text = b.normalized_text.strip()
        if (
            b.block_type in _SKIP_BLOCKS
            or b.is_repeated_header
            or b.is_repeated_footer
            or not text
            or _PAGE_MARKER.match(text)
        ):
            continue
        out.append(b)
    return out


def _title(text: str) -> str | None:
    t = clean(text).strip(_QUOTES).rstrip(":").strip(_QUOTES)
    if words(t) > 12:
        return None
    m = EVENT_TITLE.search(t)
    if m is None or m.end() != len(t):
        return None
    return t


def _heading(text: str) -> tuple[Role, str] | None:
    """A participant heading ('CORPORATE PARTICIPANTS:', 'MANAGEMENT: MR. X - CEO')."""
    m = _LABEL.match(text)
    label, rest = (m.group("label"), m.group("rest")) if m else (text, "")
    for pattern, role in PARTICIPANT_HEADINGS:
        if pattern.match(clean(label)) and (m is not None or words(text) <= 4):
            return Role(role), rest.strip()
    return None


def _moderator(label: str) -> Role | None:
    m = MODERATOR_LABEL.match(clean(label))
    if m is None or words(label) > 3:
        return None
    return Role.OPERATOR if m.group(1).lower() == "operator" else Role.MODERATOR


class CallParser:
    def __init__(self, company_key: str, fy_end_month: int | None, limits: ManagementLimits):
        self.company_key = company_key
        self.fy_end = fy_end_month
        self.limits = limits
        self.out = CallOutput()

    # --- transcripts ---------------------------------------------------------------------------

    def transcript(self, source: Source) -> None:
        ex = source.extraction
        blocks = _content(ex.blocks)
        starts = [i for i, b in enumerate(blocks) if _title(b.normalized_text)]
        segments: list[tuple[int, int]] = []
        for i in starts:
            # a repeated title with no speech since the previous one belongs to that event
            if segments and not any(
                words(b.normalized_text) > _LONG_BLOCK_WORDS for b in blocks[segments[-1][0] : i]
            ):
                continue
            if segments:
                segments[-1] = (segments[-1][0], i)
            segments.append((i, len(blocks)))
        if not segments:
            self.out.warnings.append(("EVENT_TITLE_MISSING", f"{ex.artifact_sha256[:12]}"))
            segments = [(-1, len(blocks))]
        for sequence, (start, end) in enumerate(segments):
            title = _title(blocks[start].normalized_text) if start >= 0 else None
            body = blocks[start + 1 : end] if start >= 0 else blocks
            self._event(source, sequence, title, blocks[start] if start >= 0 else None, body)

    def _event(
        self,
        source: Source,
        sequence: int,
        title: str | None,
        title_block: TextBlock | None,
        body: list[TextBlock],
    ) -> None:
        ex = source.extraction
        warnings: list[str] = []
        event_type = EventType.OTHER
        if ex.document_type == EvidenceDocumentType.AGM_TRANSCRIPT:
            event_type = EventType.AGM
        if title:
            for pattern, kind in EVENT_TYPES:
                if pattern.search(title):
                    event_type = kind
                    break
        else:
            warnings.append("EVENT_TITLE_MISSING")
        event_id = uuid5(NS, f"event|{self.company_key}|{ex.artifact_sha256}|{sequence}")

        # the printed call date: a date-only line right after the title
        call_date: date | None = None
        call_date_text: str | None = None
        for b in body[:4]:
            text = clean(b.normalized_text)
            m = EXPLICIT_DATE.search(text)
            if m and m.start() == 0 and words(text) <= 5:
                call_date, call_date_text = parse_date(text), m.group(0)
                break
        if call_date is None:
            warnings.append("CALL_DATE_UNKNOWN")

        period: ReportingPeriod | None = None
        if title:
            m = PERIOD_LABEL.search(title)
            if m:
                period = fiscal_period(m, self.fy_end, "EXPLICIT_LABEL")
        if period is None:
            warnings.append("PERIOD_UNKNOWN")
        elif period.period_end is None:
            warnings.append("FISCAL_YEAR_END_UNKNOWN")

        speech_at = self._speech_start(body)
        labels_later = {
            normalize_name(clean(b.normalized_text))
            for b in body[speech_at:]
            if name_like(clean(b.normalized_text).rstrip(":"))
        }
        entries = self._participant_list(body[:speech_at], labels_later)
        if not entries:
            warnings.append("PARTICIPANT_LIST_MISSING")
        media = event_type == EventType.MEDIA_CONFERENCE
        people: dict[str, Participant] = {}
        for entry in entries:
            key = normalize_name(entry.raw_name)
            if not key or key in people:
                continue
            detail = clean(" ".join(entry.detail)) or None
            people[key] = Participant(
                participant_id=uuid5(NS, f"participant|{event_id}|{key}"),
                event_id=event_id,
                raw_name=entry.raw_name,
                normalized_name=key,
                person_key=key,
                role=entry.role,
                role_basis=RoleBasis.PARTICIPANT_LIST,
                designation=detail if entry.role == Role.MANAGEMENT else None,
                organisation=detail if entry.role != Role.MANAGEMENT else None,
                provenance=source.provenance(entry.blocks),
            )

        drafts = self._turns(body[speech_at:], people)
        introduced: dict[str, Role] = {}
        turn_texts: list[TurnText] = []
        unknown: set[str] = set()
        for index, draft in enumerate(drafts):
            if draft.key not in people:
                moderator = _moderator(draft.raw_label)
                if moderator is not None:
                    role, basis = moderator, RoleBasis.SPEAKER_LABEL
                elif draft.key in introduced:
                    role, basis = introduced[draft.key], RoleBasis.MODERATOR_INTRODUCTION
                elif draft.key in _GENERIC_QUESTIONERS:
                    role, basis = Role.ANALYST, RoleBasis.SPEAKER_LABEL
                elif _self_introduced(draft):
                    role, basis = Role.ANALYST, RoleBasis.SELF_INTRODUCTION
                else:
                    role, basis = Role.UNKNOWN, RoleBasis.NONE
                    unknown.add(draft.key)
                people[draft.key] = Participant(
                    participant_id=uuid5(NS, f"participant|{event_id}|{draft.key}"),
                    event_id=event_id,
                    raw_name=draft.raw_label,
                    normalized_name=draft.key,
                    person_key=draft.key,
                    role=role,
                    role_basis=basis,
                    designation=None,
                    organisation=None,
                    provenance=source.provenance(draft.blocks[:1]),
                    warnings=("ROLE_UNKNOWN",) if role == Role.UNKNOWN else (),
                )
            speaker = people[draft.key]
            text_parts: list[str] = []
            spans: list[tuple[int, int, TextBlock]] = []
            cursor = 0
            for piece, block in draft.pieces:
                piece = clean(piece)
                if not piece:
                    continue
                if text_parts:
                    cursor += 1
                spans.append((cursor, cursor + len(piece), block))
                text_parts.append(piece)
                cursor += len(piece)
            full = " ".join(text_parts)
            if speaker.role in (Role.MODERATOR, Role.OPERATOR, Role.MANAGEMENT):
                for m in QUESTIONER_INTRO.finditer(full):
                    name = m.group("name").strip()
                    if name_like(name, min_tokens=1):
                        introduced.setdefault(
                            normalize_name(name), Role.MEDIA if media else Role.ANALYST
                        )
            bound = self.limits.max_excerpt_chars
            turn = Turn(
                turn_id=uuid5(NS, f"turn|{event_id}|{index}"),
                event_id=event_id,
                participant_id=speaker.participant_id,
                sequence=index,
                section=Section.UNKNOWN,
                text=full[:bound],
                truncated=len(full) > bound,
                word_count=words(full),
                provenance=source.provenance(draft.blocks),
            )
            turn_texts.append(TurnText(turn, full, spans, speaker.role, source))
        for key in sorted(unknown):
            self.out.warnings.append(("ROLE_UNKNOWN", f"{title or event_id}: {key}"))

        # prepared remarks / Q&A: the first moderator / host turn that opens questions,
        # after management has spoken
        qa_at: int | None = None
        management_spoke = False
        for i, t in enumerate(turn_texts):
            if (
                management_spoke
                and t.role in (Role.MODERATOR, Role.OPERATOR, Role.MANAGEMENT)
                and QA_START.search(t.text)
            ):
                qa_at = i
                break
            if t.role == Role.MANAGEMENT and t.turn.word_count >= 20:
                management_spoke = True
        if qa_at is None:
            for i, t in enumerate(turn_texts):
                if management_spoke and t.role in (Role.ANALYST, Role.MEDIA):
                    qa_at = i
                    warnings.append("QA_BOUNDARY_FROM_FIRST_QUESTIONER")
                    break
        if qa_at is None and turn_texts:
            warnings.append("QA_BOUNDARY_UNKNOWN")
        for i, t in enumerate(turn_texts):
            section = (
                Section.UNKNOWN
                if qa_at is None
                else (Section.QA if i >= qa_at else Section.PREPARED)
            )
            t.turn = t.turn.model_copy(update={"section": section})

        management_text = " ".join(t.text for t in turn_texts if t.role == Role.MANAGEMENT)
        consolidated = bool(re.search(r"\bconsolidated\b", management_text, re.I))
        standalone = bool(re.search(r"\bstand-?alone\b", management_text, re.I))
        event_basis, basis_method = ReportingBasis.UNKNOWN, "NOT_STATED"
        if consolidated != standalone:
            event_basis = ReportingBasis.CONSOLIDATED if consolidated else ReportingBasis.STANDALONE
            basis_method = "EVENT_MANAGEMENT_TEXT"
        elif consolidated:
            basis_method = "BOTH_BASES_MENTIONED"

        all_blocks = ([title_block] if title_block else []) + body
        prepared = [
            b for t in turn_texts if t.turn.section == Section.PREPARED for _, _, b in t.spans
        ]
        qa = [b for t in turn_texts if t.turn.section == Section.QA for _, _, b in t.spans]
        event = ManagementEvent(
            event_id=event_id,
            company_key=self.company_key,
            document_id=ex.document_id,
            document_type=ex.document_type,
            artifact_sha256=ex.artifact_sha256,
            event_type=event_type,
            title=title,
            call_date=call_date,
            call_date_text=call_date_text,
            reporting_period=period,
            reporting_basis=event_basis,
            basis_method=basis_method,
            authority=ex.authority,
            source_url=source.source_url,
            page_start=min((b.page_number for b in all_blocks), default=1),
            page_end=max((b.page_number for b in all_blocks), default=1),
            prepared_block_ids=(prepared[0].block_id, prepared[-1].block_id) if prepared else (),
            qa_block_ids=(qa[0].block_id, qa[-1].block_id) if qa else (),
            warnings=tuple(sorted(set(warnings))),
        )
        self.out.events.append(event)
        self.out.participants.extend(sorted(people.values(), key=lambda p: p.person_key))
        self.out.turns.extend(turn_texts)
        self.out.qa_pairs.extend(self._pairs(event, turn_texts, people))

    def _speech_start(self, body: list[TextBlock]) -> int:
        """The first speaker label: a moderator label, a 'Name: words...' line, or the line
        before the first long block."""
        for i, b in enumerate(body):
            text = clean(b.normalized_text)
            m = _LABEL.match(text)
            label = m.group("label") if m else text
            if _moderator(label) is not None:
                return i
            if (
                m
                and _heading(text) is None
                and name_like(label, min_tokens=2)
                and words(m.group("rest")) >= 4
            ):
                return i
            if words(text) > _LONG_BLOCK_WORDS and _heading(text) is None:
                if i > 0 and name_like(clean(body[i - 1].normalized_text).rstrip(":")):
                    return i - 1
                return i
        return len(body)

    def _participant_list(self, region: list[TextBlock], later: set[str]) -> list[_Entry]:
        entries: list[_Entry] = []
        role: Role | None = None
        separated = False
        for b in region:
            text = clean(b.normalized_text)
            heading = _heading(text)
            if heading is not None:
                role, rest = heading
                separated = bool(rest)
                if rest:
                    entries.append(self._separated(rest, role, b))
                continue
            if role is None or _title(text) or EXPLICIT_DATE.match(text):
                continue
            last = entries[-1] if entries and entries[-1].role == role else None
            if separated:
                if _SEPARATOR.search(text) and name_like(_SEPARATOR.split(text, 1)[0]):
                    entries.append(self._separated(text, role, b))
                elif last is not None:
                    last.detail.append(text)
                    last.blocks.append(b)
                continue
            key = normalize_name(text)
            if any(normalize_name(e.raw_name) == key for e in entries):
                continue
            if name_like(text) and (last is None or last.detail or key in later):
                entries.append(_Entry(text, role, [], [b]))
            elif last is not None and not last.detail:
                last.detail.append(text)
                last.blocks.append(b)
        return entries

    @staticmethod
    def _separated(text: str, role: Role, block: TextBlock) -> _Entry:
        parts = _SEPARATOR.split(text, maxsplit=1)
        return _Entry(parts[0].strip(), role, parts[1:] if len(parts) > 1 else [], [block])

    def _turns(self, region: list[TextBlock], people: dict[str, Participant]) -> list[_Draft]:
        drafts: list[_Draft] = []
        pending: set[str] = set()
        for b in region:
            text = clean(b.normalized_text)
            label: str | None = None
            rest = ""
            m = _LABEL.match(text)
            if m:
                candidate = m.group("label").strip()
                key = normalize_name(candidate)
                if (
                    _moderator(candidate)
                    or key in people
                    or key in pending
                    or name_like(candidate, min_tokens=2)
                    # a one-word label that opens its own line ('Sudhir: Hi. This is ...',
                    # 'Participant: Thank you') is a new speaker, never more of the last one
                    or (name_like(candidate) and m.group("rest").strip() != "")
                ):
                    label, rest = candidate, m.group("rest")
            if label is None and words(text) <= 6:
                bare = text.rstrip(":").strip()
                key = normalize_name(bare)
                if _moderator(bare) or (key and (key in people or key in pending)):
                    label = bare
            if label is not None:
                drafts.append(_Draft(normalize_name(label), label, [b], []))
                if rest.strip():
                    drafts[-1].pieces.append((rest, b))
                continue
            if not drafts:
                continue
            drafts[-1].blocks.append(b)
            drafts[-1].pieces.append((text, b))
            for intro in QUESTIONER_INTRO.finditer(" ".join(p for p, _ in drafts[-1].pieces)):
                name = intro.group("name").strip()
                if name_like(name):
                    pending.add(normalize_name(name))
        return drafts

    # --- Q&A --------------------------------------------------------------------------------

    def _pairs(
        self,
        event: ManagementEvent,
        turns: list[TurnText],
        people: dict[str, Participant],
    ) -> list[QAPair]:
        by_id = {p.participant_id: p for p in people.values()}
        groups: list[tuple[list[TurnText], list[TurnText]]] = []
        for t in turns:
            if t.turn.section != Section.QA:
                continue
            if t.role in (Role.ANALYST, Role.MEDIA, Role.UNKNOWN):
                same = bool(groups) and groups[-1][0][0].turn.participant_id == (
                    t.turn.participant_id
                )
                if same and not groups[-1][1]:
                    groups[-1][0].append(t)
                elif same and t.turn.word_count <= _COURTESY_WORDS and "?" not in t.text:
                    continue  # 'Thank you.' after the answer: no new question
                else:
                    groups.append(([t], []))
            elif t.role == Role.MANAGEMENT and groups:
                if _FACILITATION.search(t.text) and t.turn.word_count < 40:
                    continue  # the host passing to the next questioner is not an answer
                groups[-1][1].append(t)
        pairs = []
        for questions, answers in groups:
            questioner = by_id[questions[0].turn.participant_id]
            phrases = sorted(
                {m.group(0).lower() for a in answers for m in DEFERRAL.finditer(a.text)}
            )
            if not answers:
                status = AnswerStatus.NO_ANSWER_TURN
            elif phrases:
                status = AnswerStatus.DEFERRED
            else:
                status = AnswerStatus.ANSWERED
            responders = list(dict.fromkeys(a.turn.participant_id for a in answers))
            pairs.append(
                QAPair(
                    qa_pair_id=uuid5(NS, f"qa|{event.event_id}|{questions[0].turn.sequence}"),
                    event_id=event.event_id,
                    question_turn_ids=tuple(q.turn.turn_id for q in questions),
                    answer_turn_ids=tuple(a.turn.turn_id for a in answers),
                    questioner_id=questioner.participant_id,
                    questioner_role=questioner.role,
                    responder_ids=tuple(responders),
                    topics=topics_of(" ".join(q.text for q in questions)),
                    answer_status=status,
                    deferral_phrases=tuple(phrases),
                )
            )
        return pairs

    # --- company documents -----------------------------------------------------------------

    def document(self, source: Source, published: date | None) -> None:
        """Presentation / annual-report / letter MANAGEMENT_GUIDANCE and MANAGEMENT_CLAIM
        evidence: management's written voice, one event per document."""
        ex = source.extraction
        items = [e for e in ex.evidence if e.kind in DOCUMENT_EVIDENCE]
        if not items:
            return
        blocks = {b.block_id: b for b in ex.blocks}
        event_id = uuid5(NS, f"event|{self.company_key}|{ex.artifact_sha256}|document")
        author_key = "company document"
        author = Participant(
            participant_id=uuid5(NS, f"participant|{event_id}|{author_key}"),
            event_id=event_id,
            raw_name=source.title or ex.document_type.value,
            normalized_name=author_key,
            person_key=author_key,
            role=Role.MANAGEMENT,
            role_basis=RoleBasis.DOCUMENT_AUTHOR,
            designation=None,
            organisation=None,
            provenance=None,
        )
        turn_texts = []
        bound = self.limits.max_excerpt_chars
        for index, item in enumerate(
            sorted(items, key=lambda e: (e.page_start, str(e.evidence_id)))
        ):
            item_blocks = [blocks[i] for i in item.block_ids if i in blocks]
            text = clean(item.normalized_text)
            spans = [(0, len(text), item_blocks[0])] if item_blocks else []
            turn = Turn(
                turn_id=uuid5(NS, f"turn|{event_id}|{index}"),
                event_id=event_id,
                participant_id=author.participant_id,
                sequence=index,
                section=Section.UNKNOWN,
                text=text[:bound],
                truncated=len(text) > bound,
                word_count=words(text),
                provenance=source.provenance(
                    item_blocks,
                    evidence_ids=(item.evidence_id,),
                    pages=(item.page_start, item.page_end),
                ),
            )
            turn_texts.append(TurnText(turn, text, spans, Role.MANAGEMENT, source))
        self.out.events.append(
            ManagementEvent(
                event_id=event_id,
                company_key=self.company_key,
                document_id=ex.document_id,
                document_type=ex.document_type,
                artifact_sha256=ex.artifact_sha256,
                event_type=EventType.COMPANY_DOCUMENT,
                title=source.title,
                call_date=None,
                call_date_text=None,
                reporting_period=None,
                reporting_basis=ReportingBasis.UNKNOWN,
                basis_method="NOT_STATED",
                authority=ex.authority,
                source_url=source.source_url,
                page_start=min(i.page_start for i in items),
                page_end=max(i.page_end for i in items),
                warnings=("CALL_DATE_NOT_APPLICABLE",)
                + (("PUBLISHED_DATE_UNKNOWN",) if published is None else ()),
            )
        )
        self.out.participants.append(author)
        self.out.turns.extend(turn_texts)
