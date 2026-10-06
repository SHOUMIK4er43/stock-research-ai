"""Source-displayed holdings; shareholder counts are separate observations."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from stock_research_ai.domain.financial import FinancialObservation


class HolderCategory(StrEnum):
    PROMOTERS = "promoters"
    FII = "fii"
    DII = "dii"
    GOVERNMENT = "government"
    PUBLIC = "public"
    OTHER = "other"


class ShareholdingObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: HolderCategory
    observation: FinancialObservation


class ShareholdingSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    holdings: tuple[ShareholdingObservation, ...] = ()
    shareholder_counts: tuple[FinancialObservation, ...] = ()
