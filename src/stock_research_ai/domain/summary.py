"""SUMMARY_V1 input: a small, bounded, source-labelled projection of one Screener page.

Every value is copied from an existing normalized observation; nothing is calculated.
Bounds are enforced by validation, never by silent truncation.
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
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_research_ai.domain.financial import (
    ExactDecimal,
    ValueStatus,
)
from stock_research_ai.domain.financial import (
    FinancialUnit as FinancialUnit,
)
from stock_research_ai.domain.financial import (
    ReportingScope as ReportingScope,
)
from stock_research_ai.domain.financial_canonical import Industry

SUMMARY_INPUT_MAX_BYTES = 24_576

BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=120)]
BoundedUrl = Annotated[str, StringConstraints(min_length=1, max_length=300)]
MetricId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
SafeCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
SummaryBlockName = Literal[
    "company", "snapshot", "annual", "quarterly", "balance_sheet", "cash_flow"
]


class SummaryInputBudgetExceeded(ValueError):
    """The canonical serialization exceeds the hard byte ceiling; nothing is trimmed."""

    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        super().__init__(f"SummaryInput is {actual_bytes} bytes; maximum is {max_bytes}")
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes


class SummaryInputUnavailable(ValueError):
    """No verified company identity, so no summary input can be produced."""


class SummaryListing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    exchange: BoundedText
    symbol: BoundedText | None = None
    exchange_identifier: BoundedText | None = None


class SummaryCompany(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: BoundedText
    source_company_key: BoundedText
    reporting_scope: ReportingScope
    listings: tuple[SummaryListing, ...] = Field(default=(), max_length=2)
    source_url: BoundedUrl
    retrieved_at: AwareDatetime | None = None
    coverage_overall: Literal["COMPLETE", "PARTIAL", "FAILED"]


class SummaryValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period: BoundedText
    value: ExactDecimal | None
    status: ValueStatus
    #: the column's end date when the source resolved it (names FY / quarter / BS date)
    period_end: date | None = None
    source_observation_id: UUID | None = None
    source_artifact_id: UUID | None = None
    source_location: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == ValueStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("Only available values may be non-null")
        provenance = (
            self.source_observation_id,
            self.source_artifact_id,
            self.source_location,
        )
        if any(item is not None for item in provenance) and not all(
            item is not None for item in provenance
        ):
            raise ValueError("Summary value provenance must be complete or absent")
        return self


class SummaryMetric(BaseModel):
    """One point-in-time source-displayed metric from the page's top ratios."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    label: BoundedText
    metric_id: MetricId
    unit: FinancialUnit
    reporting_scope: ReportingScope
    observation: SummaryValue


class SummaryRow(BaseModel):
    """One source row: completed periods oldest-first, with TTM kept separate."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    label: BoundedText
    metric_id: MetricId
    unit: FinancialUnit
    reporting_scope: ReportingScope
    values: tuple[SummaryValue, ...] = Field(max_length=5)  # 5 quarters: one year-on-year
    ttm: SummaryValue | None = None


class SummaryBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[SummaryRow, ...] = Field(default=(), max_length=16)


class SummaryLimitation(BaseModel):
    """Machine-readable reason something is absent; codes only, never source text."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    block: SummaryBlockName
    code: SafeCode
    metric_ids: tuple[MetricId, ...] = Field(default=(), max_length=4)


class SummaryInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["SUMMARY_INPUT_V1"] = "SUMMARY_INPUT_V1"
    company: SummaryCompany
    business_classification: Industry = Industry.UNKNOWN
    snapshot: tuple[SummaryMetric, ...] = Field(default=(), max_length=16)
    annual: SummaryBlock
    quarterly: SummaryBlock
    balance_sheet: SummaryBlock
    cash_flow: SummaryBlock
    limitations: tuple[SummaryLimitation, ...] = Field(default=(), max_length=48)


# --- SUMMARY_V1 Box 3: model output, result and safe errors --------------------------

SUMMARY_TEXT_MAX_CHARACTERS = 8_000
SUMMARY_REQUEST_MAX_BYTES = 24_576

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# Line breaks and tabs are the only control characters a plain-text summary needs.
# Bidirectional overrides/isolates can make displayed text misrepresent its content.
_ALLOWED_CONTROLS = frozenset("\n\r\t")
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")


def unsafe_text_characters(text: str) -> bool:
    """True when text holds control or bidi-override characters unsafe to display."""
    return any(
        (ord(ch) < 32 or 127 <= ord(ch) < 160)
        and ch not in _ALLOWED_CONTROLS
        or ch in _BIDI_CONTROLS
        for ch in text
    )


class SummaryErrorCode(StrEnum):
    SUMMARY_INPUT_TOO_LARGE = "SUMMARY_INPUT_TOO_LARGE"
    SUMMARY_REQUEST_TOO_LARGE = "SUMMARY_REQUEST_TOO_LARGE"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    SUMMARY_TRUNCATED = "SUMMARY_TRUNCATED"
    SUMMARY_EMPTY = "SUMMARY_EMPTY"
    SUMMARY_TOO_LARGE = "SUMMARY_TOO_LARGE"
    MODEL_MISMATCH = "MODEL_MISMATCH"
    UNSAFE_CONTENT = "UNSAFE_CONTENT"
    # Provider/transport failures raised by the SummaryModel adapter.
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    UNSAFE_REQUEST = "UNSAFE_REQUEST"
    PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_CONNECTION_LOST = "PROVIDER_CONNECTION_LOST"


# code -> (fixed message, retryable_by_user, ambiguous). Messages never carry provider
# bodies, prompts, summary text, credentials or exception text.
_ERRORS: dict[SummaryErrorCode, tuple[str, bool, bool]] = {
    SummaryErrorCode.SUMMARY_INPUT_TOO_LARGE: (
        "Company data exceeds the summary input budget.",
        False,
        False,
    ),
    SummaryErrorCode.SUMMARY_REQUEST_TOO_LARGE: (
        "Summary request exceeds the request size ceiling.",
        False,
        False,
    ),
    SummaryErrorCode.INVALID_PROVIDER_RESPONSE: (
        "The model returned a response in an unexpected shape.",
        True,
        False,
    ),
    SummaryErrorCode.SUMMARY_TRUNCATED: ("The model did not finish the summary.", True, False),
    SummaryErrorCode.SUMMARY_EMPTY: ("The model returned an empty summary.", True, False),
    SummaryErrorCode.SUMMARY_TOO_LARGE: (
        "The model returned a summary longer than allowed.",
        True,
        False,
    ),
    SummaryErrorCode.MODEL_MISMATCH: (
        "The responding model differs from the requested model.",
        False,
        False,
    ),
    SummaryErrorCode.UNSAFE_CONTENT: (
        "The model returned text containing disallowed characters.",
        True,
        False,
    ),
    SummaryErrorCode.PROVIDER_NOT_CONFIGURED: (
        "The summary provider is not configured.",
        False,
        False,
    ),
    SummaryErrorCode.CREDENTIAL_UNAVAILABLE: (
        "No usable summary provider credential is available.",
        False,
        False,
    ),
    SummaryErrorCode.UNSAFE_REQUEST: ("The summary request failed a safety check.", False, False),
    SummaryErrorCode.PROVIDER_HTTP_ERROR: (
        "The summary provider rejected the request.",
        True,
        False,
    ),
    SummaryErrorCode.PROVIDER_UNAVAILABLE: (
        "The summary provider could not be reached; no request was sent.",
        True,
        False,
    ),
    # Ambiguous: the request may have reached the provider (and been billed).
    SummaryErrorCode.PROVIDER_TIMEOUT: (
        "The summary provider did not respond in time; the request may have run.",
        True,
        True,
    ),
    SummaryErrorCode.PROVIDER_CONNECTION_LOST: (
        "The connection to the summary provider was lost; the request may have run.",
        True,
        True,
    ),
}


class SummaryGenerationError(Exception):
    """A typed summary failure; its message is fixed by its code alone."""

    def __init__(self, code: SummaryErrorCode) -> None:
        message, retryable, ambiguous = _ERRORS[code]
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.retryable_by_user = retryable
        self.ambiguous = ambiguous


class SummaryUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    prompt_tokens: NonNegativeInt
    completion_tokens: NonNegativeInt


class SummaryModelOutput(BaseModel):
    """What a SummaryModel adapter hands back. Text and finish reason stay unvalidated
    here so the application can reject them with a typed SummaryGenerationError."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    summary_text: str
    requested_model: BoundedText
    reported_model: BoundedText | None
    finish_reason: BoundedText | None
    usage: SummaryUsage | None = None


ClaimId = Annotated[str, StringConstraints(pattern=r"^c_[0-9a-f]{20}$")]


class SummaryClaimSelection(BaseModel):
    """The model may select catalogued claims, but may not author displayed prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claim_ids: tuple[ClaimId, ...] = Field(max_length=256)

    @model_validator(mode="after")
    def unique_claims(self) -> Self:
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("Claim selections must be unique")
        return self


class SummaryGroundingClaim(BaseModel):
    """A deterministic displayed statement linked to observations and raw evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    claim_id: ClaimId
    #: observation: one value; trend: the same metric in two comparable periods; ratio: a
    #: calculated ratio of two cited values of the same period (numerator, denominator)
    kind: Literal["observation", "trend", "ratio"]
    block: SummaryBlockName
    metric_id: MetricId
    label: BoundedText
    text: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    periods: tuple[BoundedText, ...] = Field(min_length=1, max_length=2)
    values: tuple[ExactDecimal, ...] = Field(min_length=1, max_length=2)
    unit: FinancialUnit
    reporting_scope: ReportingScope
    source_observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=2)
    source_artifact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=2)
    source_locations: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=500)], ...
    ] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def linked_inputs(self) -> Self:
        count = 1 if self.kind == "observation" else 2
        if not all(
            len(items) == count
            for items in (
                self.periods,
                self.values,
                self.source_observation_ids,
                self.source_artifact_ids,
                self.source_locations,
            )
        ):
            raise ValueError("A summary claim must link each displayed value to its source")
        return self


class SummaryCompanyRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: BoundedText
    source_company_key: BoundedText
    reporting_scope: ReportingScope


class SummaryResult(BaseModel):
    """One validated SUMMARY_V1 report. Exists only on success; no raw request,
    response, prompt, credential or provider error body is retained."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["SUMMARY_RESULT_V1"] = "SUMMARY_RESULT_V1"
    summary_id: UUID
    company: SummaryCompanyRef
    source_url: BoundedUrl
    data_retrieved_at: AwareDatetime | None
    generated_at: AwareDatetime
    input_sha256: Sha256Hex
    prompt_version: Literal["SUMMARY_PROMPT_V2"] = "SUMMARY_PROMPT_V2"
    summary_text: Annotated[
        str,
        StringConstraints(min_length=1, max_length=SUMMARY_TEXT_MAX_CHARACTERS),
    ]
    grounding_claims: tuple[SummaryGroundingClaim, ...] = Field(default=(), max_length=256)
    limitations: tuple[SummaryLimitation, ...] = Field(default=(), max_length=48)
    provider: Literal["aicredits"] = "aicredits"
    requested_model: BoundedText
    reported_model: BoundedText | None
    finish_reason: Literal["stop"]
    usage: SummaryUsage | None = None

    @field_validator("summary_text")
    @classmethod
    def displayable(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Summary text must not be blank")
        if unsafe_text_characters(value):
            raise ValueError("Summary text contains disallowed characters")
        return value

    @model_validator(mode="after")
    def displayed_claims_are_linked(self) -> Self:
        if len({claim.claim_id for claim in self.grounding_claims}) != len(self.grounding_claims):
            raise ValueError("Summary grounding claims must be unique")
        visible_lines = set(self.summary_text.splitlines())
        if any(claim.text not in visible_lines for claim in self.grounding_claims):
            raise ValueError("Every grounding claim must be an exact displayed sentence")
        return self
