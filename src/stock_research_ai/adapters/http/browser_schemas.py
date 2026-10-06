"""Browser-facing transport DTOs (Part 6B-A). Intentionally narrow: these are NOT
internal domain models widened for convenience - each field is exactly what the
extension is allowed to send or needs to read. The Bearer token is transport
authentication (an HTTP header) and is never a field on any of these models.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from stock_research_ai import __version__
from stock_research_ai.domain.report import ResearchReportSnapshot

Portal = Literal["SCREENER"]
ResearchPlan = Literal["FULL_RESEARCH_V1"]
ArtifactKind = Literal["INSTITUTIONAL_REPORT", "INVESTOR_BRIEF", "RESEARCH_JSON", "REPORT_PDF"]

API_VERSION = "v1"
_REPORT_SCHEMA_VERSION: str = ResearchReportSnapshot.model_fields["schema_version"].default


class BrowserResearchRequest(BaseModel):
    """The ONLY shape a research launch may take. No HTML, no DOM selectors, no
    document URLs, no sector, no financial values, no valuation assumptions, no
    arbitrary prompt - the server owns every one of those, unconditionally."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    portal: Portal
    source_url: str
    research_plan: ResearchPlan = "FULL_RESEARCH_V1"


class BrowserSummaryRequest(BaseModel):
    """SUMMARY_V1: the ONLY shape a summary request may take - the detected page's portal
    and URL. The server owns extraction, credentials, the prompt and the model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    portal: Portal
    source_url: str


class PairingCompleteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pairing_code: str


class PairingCompleteResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    token: str


class VersionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: str = API_VERSION
    application_version: str = __version__
    report_schema_version: str = _REPORT_SCHEMA_VERSION
    supported_research_plans: tuple[ResearchPlan, ...] = ("FULL_RESEARCH_V1",)
    supported_portals: tuple[Portal, ...] = ("SCREENER",)


class ErrorResponse(BaseModel):
    """A typed error body distinguishable from FastAPI's default {"detail": str}."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
