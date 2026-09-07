"""Company identity as supplied by a caller; no source verification is implied."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExchangeListing(BaseModel):
    """An exchange listing; an unknown exchange stays unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    exchange: str | None = Field(default=None, min_length=1)
    symbol: str = Field(min_length=1)


class ProviderIdentifier(BaseModel):
    """A source-specific identifier, separate from exchange listings."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1)
    identifier: str = Field(min_length=1)


class CompanyRegistration(BaseModel):
    """The minimal user-supplied information needed to register a company."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    listings: tuple[ExchangeListing, ...] = ()
    provider_identifiers: tuple[ProviderIdentifier, ...] = ()


class Company(CompanyRegistration):
    """A registered company with its internal, source-independent identity."""

    id: UUID
