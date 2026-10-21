"""DETAILED_RESEARCH_V1 Box 9: industry and competitive intelligence.

A source-backed, rule-based peer and industry evidence layer: which companies are
genuinely comparable (and why, from which classification / evidence), how the target's
trusted Box 4 / 5 / 7 facts compare with theirs on aligned periods, bases and units, what
management statements, analyst questions and company documents explicitly say about
competitors, market position, demand, pricing and industry structure - and which
comparisons are NOT valid. Descriptive only: no score, no 'moat', no recommendation.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import Currency, PeriodType, ReportingBasis
from stock_research_ai.domain.management_intelligence import Provenance

INDUSTRY_SCHEMA: Literal["INDUSTRY_INTELLIGENCE_DATASET_V1"] = "INDUSTRY_INTELLIGENCE_DATASET_V1"
ENGINE_VERSION = "industry-intelligence/1"
REGISTRY_VERSION = "peer-registry/1"
SELECTION_VERSION = "peer-selection/1"

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Excerpt = Annotated[str, StringConstraints(max_length=4000)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- vocabularies ----------------------------------------------------------------------------


class PeerSource(StrEnum):
    SCREENER_PEER_TABLE = "SCREENER_PEER_TABLE"  # Box 1B: Screener's same-industry peer list
    COMPETITOR_MENTION = "COMPETITOR_MENTION"  # an explicit, resolved competitor mention


class Eligibility(StrEnum):
    INCLUDED = "INCLUDED"
    INCLUDED_LIMITED = "INCLUDED_LIMITED"
    EXCLUDED = "EXCLUDED"
    UNKNOWN = "UNKNOWN"


class Comparability(StrEnum):
    COMPARABLE = "COMPARABLE"
    PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class Dimension(StrEnum):
    SECTOR_FAMILY = "SECTOR_FAMILY"
    BUSINESS_MODEL = "BUSINESS_MODEL"  # classification depth and regulated model
    REPORTING_BASIS = "REPORTING_BASIS"
    ACCOUNTING_PERIOD = "ACCOUNTING_PERIOD"
    CURRENCY = "CURRENCY"
    FINANCIAL_METRICS = "FINANCIAL_METRICS"
    OPERATING_KPIS = "OPERATING_KPIS"
    SEGMENTS = "SEGMENTS"


class Alignment(StrEnum):
    ALIGNED = "ALIGNED"  # same period type and the same period end
    NEAR_ALIGNED = "NEAR_ALIGNED"  # same period type, period ends within 3 months
    NOT_ALIGNED = "NOT_ALIGNED"


class ObservationStatus(StrEnum):
    COMPARABLE = "COMPARABLE"
    PERIOD_MISMATCH = "PERIOD_MISMATCH"
    BASIS_MISMATCH = "BASIS_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    DEFINITION_MISMATCH = "DEFINITION_MISMATCH"
    MISSING = "MISSING"
    NOT_CALCULATED = "NOT_CALCULATED"  # upstream status other than CALCULATED
    #: the peer's Box 4 business model is not proven compatible for THIS metric (e.g. a
    #: financial subtype unresolved on either side: business-model-neutral metrics only)
    CLASSIFICATION_MISMATCH = "CLASSIFICATION_MISMATCH"


class Directionality(StrEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    NOT_REGISTERED = "NOT_REGISTERED"  # values shown; never ranked


class KPIEquivalence(StrEnum):
    EXACT = "EXACT"
    PROXY = "PROXY"  # displayed, never ranked
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UNKNOWN = "UNKNOWN"


class SegmentMatch(StrEnum):
    IDENTICAL_NAME = "IDENTICAL_NAME"  # same normalized name: definitions still unverified
    UNMATCHED = "UNMATCHED"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"


class ContextType(StrEnum):
    CUSTOMER_WIN = "CUSTOMER_WIN"
    CUSTOMER_LOSS = "CUSTOMER_LOSS"
    PRICING = "PRICING"
    MARKET_SHARE = "MARKET_SHARE"
    TECHNOLOGY = "TECHNOLOGY"
    CAPACITY = "CAPACITY"
    PRODUCT = "PRODUCT"
    TALENT = "TALENT"
    REGULATION = "REGULATION"
    DEMAND = "DEMAND"
    M_AND_A = "M_AND_A"
    GENERAL_COMPETITION = "GENERAL_COMPETITION"
    OTHER = "OTHER"


class Resolution(StrEnum):
    RESOLVED = "RESOLVED"  # exact name / registered alias of exactly one company
    AMBIGUOUS = "AMBIGUOUS"  # a registered alias shared by several companies
    UNRESOLVED = "UNRESOLVED"  # generic competition language, no company named


class SourceLayer(StrEnum):
    BOX1B = "BOX1B"
    BOX3_EVIDENCE = "BOX3_EVIDENCE"
    BOX4_VALUE = "BOX4_VALUE"
    BOX5_METRIC = "BOX5_METRIC"
    BOX7_KPI = "BOX7_KPI"
    BOX7_EVIDENCE = "BOX7_EVIDENCE"
    BOX8_STATEMENT = "BOX8_STATEMENT"
    BOX8_TURN = "BOX8_TURN"


class Nature(StrEnum):
    """What kind of statement the evidence is - never upgraded."""

    COMPANY_ASSERTION = "COMPANY_ASSERTION"  # management speech / presentation commentary
    ANALYST_QUESTION = "ANALYST_QUESTION"
    COMPANY_DISCLOSURE = "COMPANY_DISCLOSURE"  # filed annual report / regulatory document
    AGGREGATOR_DATA = "AGGREGATOR_DATA"


class ClaimStatus(StrEnum):
    CORROBORATED = "CORROBORATED"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    CONFLICTED = "CONFLICTED"
    UNVERIFIED = "UNVERIFIED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class SignalDirection(StrEnum):
    INCREASING = "INCREASING"
    STABLE = "STABLE"
    DECREASING = "DECREASING"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --- provenance ---------------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    """Which upstream record a Box 9 fact comes from, plus its Box 3 location when it has
    one (Box 8 statements / turns, Box 3 evidence)."""

    model_config = _model()
    company_key: Label
    layer: SourceLayer
    record_id: UUID | None  # Box 4 value / Box 5 metric / Box 7 KPI / Box 8 statement ...
    dataset_id: UUID | None  # the upstream dataset the record belongs to
    location: Provenance | None = None
    source_url: Label | None = None
    locator: Label | None = None


class PeerTableRef(BaseModel):
    """A row of the target's Box 1B Screener peer table."""

    model_config = _model()
    source_url: Label
    content_sha256: Sha256Hex
    retrieved_at: AwareDatetime
    row_index: NonNegativeInt
    displayed_rank: Label
    displayed_name: Label
    screener_company_id: Label | None
    company_url: Label | None


class ClassificationRef(BaseModel):
    model_config = _model()
    level: Label  # 'Broad Sector' / 'Sector' / 'Broad Industry' / 'Industry'
    name: Label
    url: Label


# --- peers --------------------------------------------------------------------------------------


class PeerCandidate(BaseModel):
    model_config = _model()
    peer_candidate_id: UUID
    target_company_key: Label
    peer_company_key: Label
    peer_company_name: Label
    peer_symbol: Label | None  # from the Screener company URL, as printed
    sources: tuple[PeerSource, ...]
    raw_label: Label
    source_classification: tuple[ClassificationRef, ...]  # the peer's own Box 1B levels
    industry: Label | None  # the peer's Box 4 industry
    sector_family: Code | None
    #: the peer's Box 4 business model (peer_selection.business_model), never a Box 9 guess
    business_model: Label | None = None
    eligibility: Eligibility
    selection_reasons: tuple[Code, ...]
    exclusion_reasons: tuple[Code, ...]
    table_ref: PeerTableRef | None
    mention_ids: tuple[UUID, ...] = ()
    selection_rank: PositiveInt | None = None  # order under the registered criteria
    confidence: Literal["HIGH", "MEDIUM", "LOW"]

    @model_validator(mode="after")
    def reasoned(self) -> Self:
        if self.eligibility == Eligibility.EXCLUDED and not self.exclusion_reasons:
            raise ValueError("an excluded peer keeps its exclusion reasons")
        if self.eligibility in (Eligibility.INCLUDED, Eligibility.INCLUDED_LIMITED) and (
            not self.selection_reasons
        ):
            raise ValueError("an included peer keeps its selection reasons")
        return self


class PeerSet(BaseModel):
    model_config = _model()
    peer_set_id: UUID
    target_company_key: Label
    methodology_version: Label
    max_peers: PositiveInt
    candidate_ids: tuple[UUID, ...]
    included_ids: tuple[UUID, ...]
    limited_ids: tuple[UUID, ...]
    excluded_ids: tuple[UUID, ...]
    unknown_ids: tuple[UUID, ...]
    beyond_limit_ids: tuple[UUID, ...]  # eligible, but past the peer-count bound
    ranking_criteria: tuple[Label, ...]


class DimensionAssessment(BaseModel):
    model_config = _model()
    dimension: Dimension
    status: Comparability
    detail: Label


class PeerComparabilityAssessment(BaseModel):
    """Dimension by dimension - never compressed into one number."""

    model_config = _model()
    assessment_id: UUID
    peer_company_key: Label
    dimensions: tuple[DimensionAssessment, ...]
    overall: Comparability
    comparable_metrics: tuple[Code, ...]
    not_comparable_metrics: tuple[Code, ...]


# --- benchmarks ---------------------------------------------------------------------------------


class MetricObservation(BaseModel):
    model_config = _model()
    company_key: Label
    is_target: bool
    status: ObservationStatus
    value: ExactDecimal | None
    unit: Label | None
    period_type: PeriodType | None
    period_end: date | None
    alignment: Alignment | None
    reporting_basis: ReportingBasis | None
    currency: Currency | None
    source: EvidenceRef | None
    upstream_ids: tuple[UUID, ...] = ()
    detail: Label | None = None
    #: the exact metric definition (formula, building-block variants, canonical inputs)
    definition: Label | None = None
    #: |peer period end - target period end| in days (0 when ALIGNED)
    alignment_days: NonNegativeInt | None = None


class BenchmarkStatistics(BaseModel):
    """Descriptive statistics over COMPARABLE peer observations (the target excluded)."""

    model_config = _model()
    peer_count: NonNegativeInt
    minimum: ExactDecimal | None
    maximum: ExactDecimal | None
    median: ExactDecimal | None
    mean: ExactDecimal | None


class Ranking(BaseModel):
    """Only with registered directionality and at least the registered minimum peers."""

    model_config = _model()
    directionality: Directionality
    rank: PositiveInt  # competition ranking (1, 2, 2, 4); 1 = most favourable
    of: PositiveInt  # target + comparable peers
    percentile: ExactDecimal | None  # share of comparable peers the target is ahead of
    ties: NonNegativeInt


class PeerFinancialBenchmark(BaseModel):
    model_config = _model()
    benchmark_id: UUID
    metric_code: Code
    layer: Literal["BOX4", "BOX5"]
    unit: Label
    period_type: PeriodType
    period_end: date
    reporting_basis: ReportingBasis
    directionality: Directionality
    target: MetricObservation
    peers: tuple[MetricObservation, ...]
    statistics: BenchmarkStatistics
    ranking: Ranking | None
    rank_withheld_reason: Label | None
    missing_peers: tuple[Label, ...]
    #: the target's exact metric definition; only identical peer definitions are COMPARABLE
    definition: Label | None = None
    #: every peer observation not COMPARABLE, by status: none of them alters a statistic
    excluded_peers: NonNegativeInt = 0
    exclusion_counts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    #: UNAVAILABLE when no peer is eligible (statistics.peer_count == 0)
    availability: Literal["AVAILABLE", "UNAVAILABLE"] = "AVAILABLE"
    unavailable_reason: Label | None = None

    @model_validator(mode="after")
    def counted(self) -> Self:
        eligible = sum(1 for o in self.peers if o.status == ObservationStatus.COMPARABLE)
        if self.statistics.peer_count != eligible:
            raise ValueError("benchmark statistics are over the COMPARABLE peers only")
        if self.excluded_peers != len(self.peers) - eligible or sum(
            self.exclusion_counts.values()
        ) != self.excluded_peers:
            raise ValueError("excluded peer counts must match the peer observations")
        if (self.availability == "AVAILABLE") != (eligible > 0):
            raise ValueError("a benchmark is AVAILABLE exactly when a peer is eligible")
        return self


class NotApplicableMetric(BaseModel):
    model_config = _model()
    metric_code: Code
    sector_family: Code
    reason: Label


class KPIObservation(BaseModel):
    model_config = _model()
    company_key: Label
    is_target: bool
    canonical_kpi: Code
    qualifier: Code | None
    unit: Label
    period_kind: Label
    period_end: date | None
    reporting_basis: ReportingBasis
    currency: Currency | None
    value: ExactDecimal
    equivalence: KPIEquivalence
    status: ObservationStatus
    source: EvidenceRef
    upstream_ids: tuple[UUID, ...] = ()
    detail: Label | None = None


class PeerKPIBenchmark(BaseModel):
    model_config = _model()
    benchmark_id: UUID
    kpi_code: Code
    qualifier: Code | None
    unit: Label
    period_kind: Label
    period_end: date | None
    directionality: Directionality
    target: KPIObservation
    peers: tuple[KPIObservation, ...]  # EXACT / PROXY / NOT_COMPARABLE, all shown
    statistics: BenchmarkStatistics  # EXACT + COMPARABLE peers only
    ranking: Ranking | None
    rank_withheld_reason: Label | None
    warnings: tuple[Code, ...] = ()


class PeerSegmentComparison(BaseModel):
    model_config = _model()
    comparison_id: UUID
    peer_company_key: Label
    domain: Literal["SEGMENT", "GEOGRAPHY"]
    identical_names: tuple[Label, ...]
    target_only: tuple[Label, ...]
    peer_only: tuple[Label, ...]
    status: SegmentMatch
    note: Label


class PositionObservation(BaseModel):
    """'INFY FY2026 ROE = X %, peer median = Y %, rank = 2/5' - a fact, never a label."""

    model_config = _model()
    observation_id: UUID
    dimension: Code  # GROWTH / PROFITABILITY / CAPITAL_EFFICIENCY / ...
    metric_code: Code
    benchmark_id: UUID
    text: Label


# --- evidence ------------------------------------------------------------------------------------


class CompetitiveMention(BaseModel):
    model_config = _model()
    mention_id: UUID
    source_company_key: Label
    mentioned_company_key: Label | None
    raw_mention: Label | None
    candidates: tuple[Label, ...] = ()  # an ambiguous alias's possible companies
    resolution: Resolution
    context_type: ContextType
    topics: tuple[Code, ...]
    nature: Nature
    event_date: date | None
    speaker_role: Code | None
    quote: Excerpt
    source: EvidenceRef
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class MarketPositionClaim(BaseModel):
    model_config = _model()
    claim_id: UUID
    company_key: Label  # the company the claim is about
    source_company_key: Label  # who said it
    category: Label  # the market / category as written, else 'UNSPECIFIED'
    stated_share: ExactDecimal | None  # only an explicit number
    unit: Literal["PERCENT"] | None
    qualitative: bool  # 'we are a leader' - never a numeric share
    period_text: Label | None
    event_date: date | None
    nature: Nature
    authority: SourceAuthority | None
    status: ClaimStatus
    corroborating_ids: tuple[UUID, ...] = ()
    conflicting_ids: tuple[UUID, ...] = ()
    quote: Excerpt
    source: EvidenceRef
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class IndustryStructureObservation(BaseModel):
    model_config = _model()
    observation_id: UUID
    topic: Code
    company_key: Label
    statement: Excerpt
    nature: Nature
    authority: SourceAuthority | None
    document_type: EvidenceDocumentType | None
    event_date: date | None
    period_text: Label | None
    source: EvidenceRef


class MarketSignal(BaseModel):
    model_config = _model()
    signal_id: UUID
    kind: Literal["DEMAND", "PRICING"]
    company_key: Label
    direction: SignalDirection
    direction_words: tuple[Label, ...]
    subject_words: tuple[Label, ...]
    quote: Excerpt
    nature: Nature
    event_date: date | None
    period_text: Label | None
    source: EvidenceRef


class IndustryTheme(BaseModel):
    model_config = _model()
    theme_id: UUID
    topic: Code
    companies: tuple[Label, ...]
    sources: tuple[Literal["MANAGEMENT", "QUESTIONS"], ...]
    events: NonNegativeInt
    mentions: NonNegativeInt
    first_date: date | None
    last_date: date | None
    evidence_ids: tuple[UUID, ...]  # upstream Box 8 theme IDs


class IndustryEvidenceConflict(BaseModel):
    model_config = _model()
    conflict_id: UUID
    topic: Code
    record_a: UUID
    record_b: UUID
    company_a: Label
    company_b: Label
    claim_a: Label
    claim_b: Label
    nature_a: Nature
    nature_b: Nature
    date_a: date | None
    date_b: date | None
    resolution: Literal["UNRESOLVED"] = "UNRESOLVED"  # never silently chosen


class UnresolvedItem(BaseModel):
    model_config = _model()
    item_id: UUID
    kind: Code
    text: Excerpt
    reason: Label
    source: EvidenceRef | None


class IndustryWarning(BaseModel):
    model_config = _model()
    code: Code
    severity: Severity
    detail: Label | None = None


class IndustryLimits(BaseModel):
    model_config = _model()
    max_candidates: PositiveInt = 50
    max_peers: PositiveInt = 10
    min_peers_for_rank: PositiveInt = 3
    max_benchmarks: PositiveInt = 500
    max_kpi_benchmarks: PositiveInt = 500
    max_segment_comparisons: PositiveInt = 100
    max_mentions: PositiveInt = 2_000
    max_claims: PositiveInt = 1_000
    max_structure_observations: PositiveInt = 2_000
    max_signals: PositiveInt = 2_000
    max_themes: PositiveInt = 500
    max_conflicts: PositiveInt = 500
    max_unresolved: PositiveInt = 5_000
    max_excerpt_chars: PositiveInt = 1_500


class UpstreamReference(BaseModel):
    model_config = _model()
    company_key: Label
    layer: Literal["BOX1B", "BOX4", "BOX5", "BOX7", "BOX8"]
    dataset_id: UUID | None
    sha256: Sha256Hex
    schema_version: Label


class IndustryStatistics(BaseModel):
    model_config = _model()
    candidates: NonNegativeInt
    eligibility_counts: dict[str, int]
    canonical_peers: NonNegativeInt
    financial_benchmarks: NonNegativeInt
    ranked_benchmarks: NonNegativeInt
    kpi_benchmarks: NonNegativeInt
    kpi_equivalence_counts: dict[str, int]
    segment_comparisons: NonNegativeInt
    competitive_mentions: NonNegativeInt
    mention_resolution_counts: dict[str, int]
    market_claims: NonNegativeInt
    claim_status_counts: dict[str, int]
    structure_observations: NonNegativeInt
    demand_signals: NonNegativeInt
    pricing_signals: NonNegativeInt
    themes: NonNegativeInt
    conflicts: NonNegativeInt
    unresolved: NonNegativeInt
    not_applicable_metrics: NonNegativeInt
    warnings_by_code: dict[str, int]


class IndustryIntelligenceDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["INDUSTRY_INTELLIGENCE_DATASET_V1"] = INDUSTRY_SCHEMA
    engine_version: Label = ENGINE_VERSION
    registry_version: Label = REGISTRY_VERSION
    dataset_id: UUID
    company_key: Label
    company_name: Label
    industry: Label
    sector_family: Code
    classification: tuple[ClassificationRef, ...]
    generated_at: AwareDatetime
    business_model: Label = "UNKNOWN"  # the target's Box 4 business model
    sources: tuple[UpstreamReference, ...]
    limits: IndustryLimits
    status: Literal["COMPLETE", "PARTIAL"]
    candidates: tuple[PeerCandidate, ...]
    peer_set: PeerSet
    comparability: tuple[PeerComparabilityAssessment, ...]
    financial_benchmarks: tuple[PeerFinancialBenchmark, ...]
    not_applicable_metrics: tuple[NotApplicableMetric, ...]
    kpi_benchmarks: tuple[PeerKPIBenchmark, ...]
    segment_comparisons: tuple[PeerSegmentComparison, ...]
    positions: tuple[PositionObservation, ...]
    competitive_mentions: tuple[CompetitiveMention, ...]
    market_claims: tuple[MarketPositionClaim, ...]
    structure_observations: tuple[IndustryStructureObservation, ...]
    demand_signals: tuple[MarketSignal, ...]
    pricing_signals: tuple[MarketSignal, ...]
    themes: tuple[IndustryTheme, ...]
    conflicts: tuple[IndustryEvidenceConflict, ...]
    unresolved: tuple[UnresolvedItem, ...]
    warnings: tuple[IndustryWarning, ...]
    statistics: IndustryStatistics

    @model_validator(mode="after")
    def no_orphans(self) -> Self:
        def unique(ids: list[UUID], what: str) -> set[UUID]:
            if len(ids) != len(set(ids)):
                raise ValueError(f"{what} IDs must be unique")
            return set(ids)

        candidates = unique([c.peer_candidate_id for c in self.candidates], "candidate")
        mentions = unique([m.mention_id for m in self.competitive_mentions], "mention")
        claims = unique([c.claim_id for c in self.market_claims], "claim")
        benchmarks = unique(
            [f.benchmark_id for f in self.financial_benchmarks]
            + [k.benchmark_id for k in self.kpi_benchmarks],
            "benchmark",
        )
        structure = unique([o.observation_id for o in self.structure_observations], "observation")
        signals = unique(
            [s.signal_id for s in (*self.demand_signals, *self.pricing_signals)], "signal"
        )
        unique([c.conflict_id for c in self.conflicts], "conflict")
        unique([t.theme_id for t in self.themes], "theme")
        s = self.peer_set
        listed = [
            *s.included_ids,
            *s.limited_ids,
            *s.excluded_ids,
            *s.unknown_ids,
            *s.beyond_limit_ids,
        ]
        if (
            set(s.candidate_ids) != candidates
            or len(listed) != len(set(listed))
            or (set(listed) != candidates)
        ):
            raise ValueError("the peer set must partition exactly the candidates")
        canonical = {*s.included_ids, *s.limited_ids}
        by_id = {c.peer_candidate_id: c for c in self.candidates}
        for cid in s.included_ids:
            if by_id[cid].eligibility != Eligibility.INCLUDED:
                raise ValueError("included_ids hold INCLUDED candidates only")
        for cid in s.limited_ids:
            if by_id[cid].eligibility != Eligibility.INCLUDED_LIMITED:
                raise ValueError("limited_ids hold INCLUDED_LIMITED candidates only")
        peers = {by_id[c].peer_company_key for c in canonical}
        for a in self.comparability:
            if a.peer_company_key not in peers:
                raise ValueError(f"comparability for a non-canonical peer {a.peer_company_key}")
        for c in self.candidates:
            if not set(c.mention_ids) <= mentions:
                raise ValueError(f"candidate {c.peer_candidate_id} names an unknown mention")
        for bench in self.financial_benchmarks:
            for o in bench.peers:
                if o.company_key not in peers:
                    raise ValueError(f"benchmark {bench.benchmark_id} uses a non-canonical peer")
        for kpi in self.kpi_benchmarks:
            for k in kpi.peers:
                if k.company_key not in peers:
                    raise ValueError(f"KPI benchmark {kpi.benchmark_id} uses a non-canonical peer")
        for p in self.positions:
            if p.benchmark_id not in benchmarks:
                raise ValueError(f"position {p.observation_id} names an unknown benchmark")
        for claim in self.market_claims:
            if not {*claim.corroborating_ids, *claim.conflicting_ids} <= claims:
                raise ValueError(f"claim {claim.claim_id} names an unknown claim")
        known = claims | signals | structure | mentions
        for conflict in self.conflicts:
            if not {conflict.record_a, conflict.record_b} <= known:
                raise ValueError(f"conflict {conflict.conflict_id} names an unknown record")
        return self
