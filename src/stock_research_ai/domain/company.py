"""Company identity as supplied by a caller; no source verification is implied."""

from datetime import date
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.provenance import ObservationProvenance, SourceLink

# Caller-supplied identity bounds: far above real company names, exchange codes, tickers and
# provider identifiers, but small enough that a request cannot persist unbounded text.
MAX_COMPANY_NAME_LENGTH = 300
MAX_EXCHANGE_LENGTH = 32
MAX_SYMBOL_LENGTH = 64
MAX_PROVIDER_LENGTH = 64
MAX_IDENTIFIER_LENGTH = 256
MAX_REGISTRATION_ITEMS = 32


class ExchangeListing(BaseModel):
    """An exchange listing; an unknown exchange stays unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    exchange: str | None = Field(default=None, min_length=1, max_length=MAX_EXCHANGE_LENGTH)
    symbol: str = Field(min_length=1, max_length=MAX_SYMBOL_LENGTH)


class ProviderIdentifier(BaseModel):
    """A source-specific identifier, separate from exchange listings."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1, max_length=MAX_PROVIDER_LENGTH)
    identifier: str = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)


class CompanyRegistration(BaseModel):
    """The minimal user-supplied information needed to register a company."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_COMPANY_NAME_LENGTH)
    listings: tuple[ExchangeListing, ...] = Field(default=(), max_length=MAX_REGISTRATION_ITEMS)
    provider_identifiers: tuple[ProviderIdentifier, ...] = Field(
        default=(), max_length=MAX_REGISTRATION_ITEMS
    )


class Company(CompanyRegistration):
    """A registered company with its internal, source-independent identity."""

    id: UUID


class ListingStatus(StrEnum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    INACTIVE = "inactive"


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    company_id: UUID
    security_id: UUID | None = None
    exchange: str
    symbol: str | None = None
    exchange_identifier: str | None = None
    currency: str | None = None
    status: ListingStatus = ListingStatus.UNKNOWN
    valid_from: date | None = None
    valid_to: date | None = None
    provenance: ObservationProvenance | None = None
    link: SourceLink | None = None

    @model_validator(mode="after")
    def identified(self) -> Self:
        if not self.symbol and not self.exchange_identifier:
            raise ValueError("Listing requires an observed symbol or exchange identifier")
        return self


class SourceCompanyIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    company_id: UUID
    name: str = Field(min_length=1)
    provider: str
    source_company_key: str = Field(min_length=1)
    requested_url: str
    final_url: str
    reporting_scope: ReportingScope
    listings: tuple[Listing, ...] = ()
    provenance: ObservationProvenance
