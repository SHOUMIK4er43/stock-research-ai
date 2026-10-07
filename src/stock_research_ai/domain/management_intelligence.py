"""DETAILED_RESEARCH_V1 Box 8: management and concall intelligence.

Structured, deterministic management evidence: who spoke (as printed), what was said
(bounded quotes with exact Box 3 page/block provenance), which statements are guidance,
how guidance on the SAME subject / measure / period changed between calls, and - only
where the definitions are the same - how it compared with the trusted Box 4 / Box 5 /
Box 7 actual. Language metrics are descriptive counts. There is no management score,
no credibility or honesty label, no evasiveness judgement.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import Currency, PeriodType, ReportingBasis

MANAGEMENT_SCHEMA: Literal["MANAGEMENT_INTELLIGENCE_DATASET_V1"] = (
    "MANAGEMENT_INTELLIGENCE_DATASET_V1"
)
ENGINE_VERSION = "management-intelligence/1"
REGISTRY_VERSION = "management-registry/1"
OUTCOME_RULES_VERSION = "guidance-outcome-rules/1"
LANGUAGE_LEXICON_VERSION = "management-language/1"

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Excerpt = Annotated[str, StringConstraints(max_length=4000)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- vocabularies ----------------------------------------------------------------------------


class EventType(StrEnum):
    EARNINGS_CALL = "EARNINGS_CALL"
    ANALYST_MEET = "ANALYST_MEET"
    MEDIA_CONFERENCE = "MEDIA_CONFERENCE"  # press / media call: questioners are journalists
    AGM = "AGM"
    INVESTOR_DAY = "INVESTOR_DAY"
    COMPANY_DOCUMENT = "COMPANY_DOCUMENT"  # presentation / AR letters: no speakers
    OTHER = "OTHER"


class Role(StrEnum):
    MANAGEMENT = "MANAGEMENT"
    ANALYST = "ANALYST"
    MEDIA = "MEDIA"  # journalists listed as such on a press call
    OPERATOR = "OPERATOR"
    MODERATOR = "MODERATOR"
    UNKNOWN = "UNKNOWN"


class RoleBasis(StrEnum):
    PARTICIPANT_LIST = "PARTICIPANT_LIST"  # printed under a participants heading
    SPEAKER_LABEL = "SPEAKER_LABEL"  # 'Moderator' / 'Operator' label itself
    MODERATOR_INTRODUCTION = "MODERATOR_INTRODUCTION"  # 'next question is from X'
    DOCUMENT_AUTHOR = "DOCUMENT_AUTHOR"  # the company's own document
    NONE = "NONE"


class Section(StrEnum):
    PREPARED = "PREPARED"
    QA = "QA"
    UNKNOWN = "UNKNOWN"


class AnswerStatus(StrEnum):
    ANSWERED = "ANSWERED"
    DEFERRED = "DEFERRED"  # registered deferral phrase in the answer
    NO_ANSWER_TURN = "NO_ANSWER_TURN"
    UNKNOWN = "UNKNOWN"


class StatementType(StrEnum):
    GUIDANCE = "GUIDANCE"
    TARGET = "TARGET"
    EXPECTATION = "EXPECTATION"
    CAPITAL_ALLOCATION = "CAPITAL_ALLOCATION"
    CAPEX = "CAPEX"
    CAPACITY = "CAPACITY"
    ORDER_BOOK = "ORDER_BOOK"
    MARGIN = "MARGIN"
    GROWTH = "GROWTH"
    DEMAND = "DEMAND"
    PRICING = "PRICING"
    COST = "COST"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    DEBT = "DEBT"
    DIVIDEND_BUYBACK = "DIVIDEND_BUYBACK"
    M_AND_A = "M_AND_A"
    RISK = "RISK"
    EXPLANATION = "EXPLANATION"
    STRATEGY = "STRATEGY"
    OPERATIONAL_CLAIM = "OPERATIONAL_CLAIM"
    GOVERNANCE = "GOVERNANCE"
    OTHER = "OTHER"


class Confidence(StrEnum):
    """Extraction certainty only - never a judgement of management."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SubjectLayer(StrEnum):
    BOX4_METRIC = "BOX4_METRIC"
    BOX5_METRIC = "BOX5_METRIC"
    BOX7_KPI = "BOX7_KPI"
    BOX7_SEGMENT = "BOX7_SEGMENT"
    QUALITATIVE = "QUALITATIVE"
    UNLINKED = "UNLINKED"


class Equivalence(StrEnum):
    EXACT = "EXACT"  # the registered actual measures the same thing
    PROXY = "PROXY"  # related, but definitions may differ: never scored
    NONE = "NONE"


class Measure(StrEnum):
    LEVEL = "LEVEL"
    GROWTH = "GROWTH"
    MARGIN = "MARGIN"
    RATIO = "RATIO"
    DIRECTION = "DIRECTION"
    QUALITATIVE = "QUALITATIVE"


class TargetType(StrEnum):
    POINT = "POINT"
    RANGE = "RANGE"
    MINIMUM = "MINIMUM"
    MAXIMUM = "MAXIMUM"
    DIRECTION = "DIRECTION"
    QUALITATIVE = "QUALITATIVE"


class Direction(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    STABLE = "STABLE"
    UNKNOWN = "UNKNOWN"


class GuidanceUnit(StrEnum):
    PERCENT = "PERCENT"
    MONEY = "MONEY"
    COUNT = "COUNT"
    BASIS_POINTS = "BASIS_POINTS"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class Lifecycle(StrEnum):
    ISSUED = "ISSUED"
    REITERATED = "REITERATED"
    RAISED = "RAISED"
    LOWERED = "LOWERED"
    NARROWED = "NARROWED"
    WIDENED = "WIDENED"
    WITHDRAWN = "WITHDRAWN"


class StatedChange(StrEnum):
    """What the speaker's own words say about the guidance (not computed)."""

    MAINTAINED = "MAINTAINED"
    REVISED = "REVISED"
    RAISED = "RAISED"
    LOWERED = "LOWERED"
    WITHDRAWN = "WITHDRAWN"
    NONE = "NONE"


class OutcomeResult(StrEnum):
    MET = "MET"
    BEAT = "BEAT"
    MISSED = "MISSED"
    PARTIAL = "PARTIAL"
    NOT_YET_DUE = "NOT_YET_DUE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    AMBIGUOUS = "AMBIGUOUS"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --- provenance -------------------------------------------------------------------------------


class Provenance(BaseModel):
    """Box 3 location: extraction -> artifact -> page -> block(s) (-> sentence)."""

    model_config = _model()
    document_id: UUID
    extraction_id: UUID
    document_type: EvidenceDocumentType
    artifact_sha256: Sha256Hex
    source_url: Label
    authority: SourceAuthority
    page_start: PositiveInt
    page_end: PositiveInt
    block_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...] = ()
    sentence_index: NonNegativeInt | None = None
    locator: Label

    @model_validator(mode="after")
    def located(self) -> Self:
        if not self.block_ids and not self.evidence_ids:
            raise ValueError("provenance names its Box 3 blocks or evidence items")
        if self.page_end < self.page_start:
            raise ValueError("a source cannot end before it starts")
        return self


class ReportingPeriod(BaseModel):
    """An explicit fiscal period ('Q1 FY27', 'FY27'): never inferred from dates."""

    model_config = _model()
    period_type: PeriodType  # FY / QUARTER / HALF_YEAR / NINE_MONTH / POINT_IN_TIME
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_end: date | None  # resolved only with the company's fiscal year-end month
    raw_text: Label
    method: Code  # EXPLICIT_LABEL / EVENT_CONTEXT / TURN_CONTEXT / EXPLICIT_DATE

    def key(self) -> str:
        return (
            f"{self.period_type.value}|{self.fiscal_year}|{self.fiscal_quarter}|{self.period_end}"
        )


# --- events, participants, turns, Q&A ---------------------------------------------------------


class ManagementEvent(BaseModel):
    model_config = _model()
    event_id: UUID
    company_key: Label
    document_id: UUID
    document_type: EvidenceDocumentType
    artifact_sha256: Sha256Hex
    event_type: EventType
    title: Label | None
    call_date: date | None  # only when printed in the transcript
    call_date_text: Label | None
    reporting_period: ReportingPeriod | None  # only when printed
    reporting_basis: ReportingBasis  # only when the event's own management text names it
    basis_method: Code
    authority: SourceAuthority
    source_url: Label
    page_start: PositiveInt
    page_end: PositiveInt
    prepared_block_ids: tuple[UUID, ...] = ()  # first and last block of the section
    qa_block_ids: tuple[UUID, ...] = ()
    warnings: tuple[Code, ...] = ()


class Participant(BaseModel):
    model_config = _model()
    participant_id: UUID
    event_id: UUID
    raw_name: Label
    normalized_name: Label
    person_key: Label  # exact normalized name: the only cross-call identity used
    role: Role
    role_basis: RoleBasis
    designation: Label | None  # only as printed
    organisation: Label | None  # only as printed
    provenance: Provenance | None
    warnings: tuple[Code, ...] = ()


class Turn(BaseModel):
    model_config = _model()
    turn_id: UUID
    event_id: UUID
    participant_id: UUID
    sequence: NonNegativeInt
    section: Section
    text: Excerpt  # bounded; the blocks below hold the full text
    truncated: bool
    word_count: NonNegativeInt
    provenance: Provenance


class QAPair(BaseModel):
    model_config = _model()
    qa_pair_id: UUID
    event_id: UUID
    question_turn_ids: tuple[UUID, ...]
    answer_turn_ids: tuple[UUID, ...]
    questioner_id: UUID
    questioner_role: Role
    responder_ids: tuple[UUID, ...]
    topics: tuple[Code, ...]
    answer_status: AnswerStatus
    deferral_phrases: tuple[Label, ...] = ()


# --- statements, guidance, outcomes -----------------------------------------------------------


class ManagementStatement(BaseModel):
    model_config = _model()
    statement_id: UUID
    event_id: UUID
    turn_id: UUID
    speaker_id: UUID
    speaker_role: Role
    quote: Excerpt
    normalized_text: Excerpt
    statement_types: tuple[StatementType, ...]  # first is the primary type
    topics: tuple[Code, ...]
    forward_looking: bool
    historical_reference: bool  # speaks of earlier guidance ('we had said ...')
    confidence: Confidence
    provenance: Provenance


class SubjectLink(BaseModel):
    model_config = _model()
    subject_code: Code  # registry subject, e.g. REVENUE_GROWTH
    layer: SubjectLayer
    target_code: Label | None  # Box 4 metric / Box 5 code / Box 7 KPI or segment key
    target_qualifier: Code | None = None
    equivalence: Equivalence
    reason: Label


class GuidanceItem(BaseModel):
    model_config = _model()
    guidance_id: UUID
    statement_ids: tuple[UUID, ...]
    event_id: UUID
    issued_date: date | None
    issued_period: ReportingPeriod | None
    subject: SubjectLink
    measure: Measure
    qualifier: Code | None  # e.g. CONSTANT_CURRENCY
    target_type: TargetType
    low: ExactDecimal | None = None
    high: ExactDecimal | None = None
    value: ExactDecimal | None = None
    decimals: NonNegativeInt = 0
    direction: Direction
    unit: GuidanceUnit
    currency: Currency | None = None
    scale_to_crore: ExactDecimal | None = None  # MONEY targets, as Box 4 normalizes
    target_period: ReportingPeriod | None
    reporting_basis: ReportingBasis
    basis_method: Code
    raw_target_text: Label
    condition_text: Label | None = None
    stated_change: StatedChange
    lifecycle: Lifecycle
    previous_guidance_id: UUID | None = None
    confidence: Confidence
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def shaped(self) -> Self:
        if self.target_type == TargetType.RANGE and (self.low is None or self.high is None):
            raise ValueError("a range target has both bounds")
        if self.target_type in (TargetType.POINT, TargetType.MINIMUM, TargetType.MAXIMUM) and (
            self.value is None
        ):
            raise ValueError("a point / minimum / maximum target has a value")
        return self


class ActualReference(BaseModel):
    model_config = _model()
    layer: SubjectLayer
    record_id: UUID  # Box 4 canonical value / Box 5 metric / Box 7 canonical value or analytic
    code: Label
    value: ExactDecimal
    unit: Label
    period_type: PeriodType
    period_end: date | None
    reporting_basis: ReportingBasis
    currency: Currency | None = None
    upstream_ids: tuple[UUID, ...] = ()  # the canonical values / observations behind it


class GuidanceOutcome(BaseModel):
    model_config = _model()
    outcome_id: UUID
    guidance_id: UUID
    result: OutcomeResult
    rule_version: Label
    rule: Label  # the exact rule applied
    actual: ActualReference | None
    reason: Label
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def evidenced(self) -> Self:
        scored = {
            OutcomeResult.MET,
            OutcomeResult.BEAT,
            OutcomeResult.MISSED,
            OutcomeResult.PARTIAL,
        }
        if self.result in scored and self.actual is None:
            raise ValueError("a scored outcome must name its actual")
        return self


class AccuracySummary(BaseModel):
    """Descriptive counts only. No score, grade, rank or rating."""

    model_config = _model()
    scope: Literal["OVERALL", "EVENT", "SUBJECT", "TARGET_PERIOD"]
    scope_key: Label
    issued: NonNegativeInt
    due: NonNegativeInt
    met: NonNegativeInt
    beat: NonNegativeInt
    missed: NonNegativeInt
    partial: NonNegativeInt
    not_yet_due: NonNegativeInt
    not_comparable: NonNegativeInt
    insufficient_data: NonNegativeInt
    ambiguous: NonNegativeInt


# --- language, themes ------------------------------------------------------------------------


class LanguageMetrics(BaseModel):
    """Descriptive phrase counts for management speech - evidence, not a judgement."""

    model_config = _model()
    metrics_id: UUID
    event_id: UUID
    section: Section
    lexicon_version: Label
    management_words: NonNegativeInt
    hedging_count: NonNegativeInt
    certainty_count: NonNegativeInt
    hedging_per_1000_words: ExactDecimal | None
    certainty_per_1000_words: ExactDecimal | None
    phrase_counts: dict[str, int]
    previous_event_id: UUID | None = None
    hedging_rate_change: ExactDecimal | None = None
    certainty_rate_change: ExactDecimal | None = None


class Theme(BaseModel):
    model_config = _model()
    theme_id: UUID
    topic: Code
    source: Literal["MANAGEMENT", "QUESTIONS"]
    event_ids: tuple[UUID, ...]
    mentions: NonNegativeInt
    first_event_id: UUID
    last_event_id: UUID
    first_date: date | None
    last_date: date | None


class GuidanceTimeline(BaseModel):
    model_config = _model()
    timeline_id: UUID
    subject_code: Code
    qualifier: Code | None
    target_period_key: Label
    guidance_ids: tuple[UUID, ...]  # in call order
    lifecycles: tuple[Lifecycle, ...]


class UnparsedItem(BaseModel):
    model_config = _model()
    item_id: UUID
    kind: Code  # GUIDANCE_AMBIGUOUS / SUBJECT_UNLINKED / SPEAKER_UNKNOWN / ...
    text: Excerpt
    reason: Label
    statement_id: UUID | None
    provenance: Provenance


class ManagementWarning(BaseModel):
    model_config = _model()
    code: Code
    severity: Severity
    detail: Label | None = None


class ManagementLimits(BaseModel):
    model_config = _model()
    max_documents: PositiveInt = 60
    max_events: PositiveInt = 120
    max_participants: PositiveInt = 5_000
    max_turns: PositiveInt = 50_000
    max_qa_pairs: PositiveInt = 10_000
    max_statements: PositiveInt = 50_000
    max_guidance: PositiveInt = 5_000
    max_outcomes: PositiveInt = 5_000
    max_unparsed: PositiveInt = 10_000
    max_excerpt_chars: PositiveInt = 1_500


class UpstreamReference(BaseModel):
    model_config = _model()
    layer: Literal["BOX3", "BOX4", "BOX5", "BOX7"]
    dataset_id: UUID
    sha256: Sha256Hex
    schema_version: Label


class ManagementStatistics(BaseModel):
    model_config = _model()
    documents_consumed: NonNegativeInt
    events: NonNegativeInt
    participants: NonNegativeInt
    participants_by_role: dict[str, int]
    turns: NonNegativeInt
    qa_pairs: NonNegativeInt
    answer_status_counts: dict[str, int]
    statements: NonNegativeInt
    statements_by_type: dict[str, int]
    guidance: NonNegativeInt
    guidance_linked_exact: NonNegativeInt
    guidance_linked_proxy: NonNegativeInt
    guidance_unlinked: NonNegativeInt
    lifecycle_counts: dict[str, int]
    outcome_counts: dict[str, int]
    unparsed: NonNegativeInt
    warnings_by_code: dict[str, int]


class ManagementIntelligenceDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["MANAGEMENT_INTELLIGENCE_DATASET_V1"] = MANAGEMENT_SCHEMA
    engine_version: Label = ENGINE_VERSION
    registry_version: Label = REGISTRY_VERSION
    outcome_rules_version: Label = OUTCOME_RULES_VERSION
    lexicon_version: Label = LANGUAGE_LEXICON_VERSION
    dataset_id: UUID
    company_key: Label
    company_name: Label
    sector: Code
    generated_at: AwareDatetime
    source_box3: UpstreamReference
    source_box4: UpstreamReference
    source_box5: UpstreamReference | None
    source_box7: UpstreamReference | None
    limits: ManagementLimits
    status: Literal["COMPLETE", "PARTIAL"]
    events: tuple[ManagementEvent, ...]
    participants: tuple[Participant, ...]
    turns: tuple[Turn, ...]
    qa_pairs: tuple[QAPair, ...]
    statements: tuple[ManagementStatement, ...]
    guidance: tuple[GuidanceItem, ...]
    timelines: tuple[GuidanceTimeline, ...]
    outcomes: tuple[GuidanceOutcome, ...]
    accuracy: tuple[AccuracySummary, ...]
    language: tuple[LanguageMetrics, ...]
    themes: tuple[Theme, ...]
    unparsed: tuple[UnparsedItem, ...]
    warnings: tuple[ManagementWarning, ...]
    statistics: ManagementStatistics

    @model_validator(mode="after")
    def no_orphans(self) -> Self:
        def unique(ids: list[UUID], what: str) -> set[UUID]:
            if len(ids) != len(set(ids)):
                raise ValueError(f"{what} IDs must be unique")
            return set(ids)

        events = unique([e.event_id for e in self.events], "event")
        people = unique([p.participant_id for p in self.participants], "participant")
        turns = unique([t.turn_id for t in self.turns], "turn")
        statements = unique([s.statement_id for s in self.statements], "statement")
        guidance = unique([g.guidance_id for g in self.guidance], "guidance")
        unique([o.outcome_id for o in self.outcomes], "outcome")
        for p in self.participants:
            if p.event_id not in events:
                raise ValueError(f"participant {p.participant_id} has no event")
        for t in self.turns:
            if t.event_id not in events or t.participant_id not in people:
                raise ValueError(f"turn {t.turn_id} is orphaned")
        for q in self.qa_pairs:
            if q.event_id not in events or not {*q.question_turn_ids, *q.answer_turn_ids} <= turns:
                raise ValueError(f"Q&A pair {q.qa_pair_id} is orphaned")
            if q.questioner_id not in people or not set(q.responder_ids) <= people:
                raise ValueError(f"Q&A pair {q.qa_pair_id} names an unknown participant")
        for s in self.statements:
            if s.event_id not in events or s.turn_id not in turns or s.speaker_id not in people:
                raise ValueError(f"statement {s.statement_id} is orphaned")
        for g in self.guidance:
            if g.event_id not in events or not set(g.statement_ids) <= statements:
                raise ValueError(f"guidance {g.guidance_id} is orphaned")
            if g.previous_guidance_id is not None and g.previous_guidance_id not in guidance:
                raise ValueError(f"guidance {g.guidance_id} names an unknown predecessor")
        for o in self.outcomes:
            if o.guidance_id not in guidance:
                raise ValueError(f"outcome {o.outcome_id} has no guidance")
        for line in self.timelines:
            if not set(line.guidance_ids) <= guidance:
                raise ValueError(f"timeline {line.timeline_id} is orphaned")
        for m in self.language:
            if m.event_id not in events:
                raise ValueError(f"language metrics {m.metrics_id} have no event")
        for theme in self.themes:
            if not set(theme.event_ids) <= events:
                raise ValueError(f"theme {theme.theme_id} names an unknown event")
        for u in self.unparsed:
            if u.statement_id is not None and u.statement_id not in statements:
                raise ValueError(f"unparsed item {u.item_id} names an unknown statement")
        return self
