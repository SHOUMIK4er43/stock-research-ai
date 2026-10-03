"""SUMMARY_V1 input: a small, bounded, source-labelled projection of one Screener page.

Every value is copied from an existing normalized observation; nothing is calculated.
Bounds are enforced by validation, never by silent truncation.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from stock_research_ai.domain.financial import (
    ExactDecimal,
    FinancialUnit,
    ReportingScope,
    ValueStatus,
)

SUMMARY_INPUT_MAX_BYTES = 16_384

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

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == ValueStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("Only available values may be non-null")
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
    values: tuple[SummaryValue, ...] = Field(max_length=4)
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
    snapshot: tuple[SummaryMetric, ...] = Field(default=(), max_length=16)
    annual: SummaryBlock
    quarterly: SummaryBlock
    balance_sheet: SummaryBlock
    cash_flow: SummaryBlock
    limitations: tuple[SummaryLimitation, ...] = Field(default=(), max_length=48)
