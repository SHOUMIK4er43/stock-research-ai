"""Deterministic, Part-6B0-owned sector-profile resolution from an already-acquired
Screener artifact.

This module decides a VALUE only. It reuses the existing, frozen sector-profile
vocabulary (application.sector_kpis.BANK/IT, domain.sector_kpis.ProfileClassification,
domain.calculations.SectorDeclaration) unchanged - Part 4C/5A/5B/5C continue to receive
exactly the same shape they already accept today, whether it was hand-authored by a test
or produced here. No second sector taxonomy. No LLM classification. No fuzzy matching.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.ports import BroadIndustrySource
from stock_research_ai.application.sector_kpis import BANK, IT
from stock_research_ai.domain.provenance import ObservationProvenance, RawArtifact
from stock_research_ai.domain.sector_kpis import ProfileClassification

MAPPING_VERSION = "SCREENER_INDUSTRY_PROFILE_MAP_V1"

#: Exact, closed, versioned mapping from the observed Screener "Broad Industry" label to
#: (coarse SectorDeclaration classification, sector-KPI profile_id). Only the two profiles
#: genuinely implemented today (application.sector_kpis.PROFILES) are mapped; every other
#: label - including ones this table simply doesn't know yet - resolves to UNKNOWN. A
#: future label is added as a new entry, never a silent reinterpretation of an old one.
SCREENER_INDUSTRY_PROFILE_MAP_V1: dict[
    str, tuple[Literal["BANK", "NON_FINANCIAL"], str]
] = {
    "Banks": ("BANK", BANK),
    "IT - Software": ("NON_FINANCIAL", IT),
}

ResolutionStatus = Literal["MAPPED", "UNMAPPED", "NO_SOURCE_LABEL"]


class SectorResolution(BaseModel):
    """Part 6B0's own richer resolution record - NOT a frozen domain contract. Only
    `.declaration` (a real, existing ProfileClassification) ever reaches Part 4C/5A/5B/5C;
    everything else here is diagnostic/orchestration metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution_status: ResolutionStatus
    profile_id: str | None
    source_label: str | None
    mapping_version: str
    declaration: ProfileClassification


def _unknown(reason: str) -> ProfileClassification:
    return ProfileClassification(
        classification="UNKNOWN", basis="CALLER_DECLARED", declaration=reason
    )


def resolve_sector(
    source: BroadIndustrySource, artifact: RawArtifact, content: bytes
) -> SectorResolution:
    """Tier A (verified/existing profile) is not yet implemented - there is no persisted
    company->profile registry to consult. Tier B: deterministic mapping through
    SCREENER_INDUSTRY_PROFILE_MAP_V1 against the exact label observed on the SAME
    already-acquired artifact `extract_company` already retrieved (read via the injected
    `source`, never a second web fetch). Tier C: UNKNOWN."""
    found = source.extract(content, artifact.encoding)
    if found is None:
        return SectorResolution(
            resolution_status="NO_SOURCE_LABEL",
            profile_id=None,
            source_label=None,
            mapping_version=MAPPING_VERSION,
            declaration=_unknown(
                "No Screener Broad Industry label was found on the acquired page"
            ),
        )
    label, source_location = found
    mapped = SCREENER_INDUSTRY_PROFILE_MAP_V1.get(label)
    if mapped is None:
        return SectorResolution(
            resolution_status="UNMAPPED",
            profile_id=None,
            source_label=label,
            mapping_version=MAPPING_VERSION,
            declaration=_unknown(
                f"Screener Broad Industry label {label!r} has no {MAPPING_VERSION} entry"
            ),
        )
    classification, profile_id = mapped
    return SectorResolution(
        resolution_status="MAPPED",
        profile_id=profile_id,
        source_label=label,
        mapping_version=MAPPING_VERSION,
        declaration=ProfileClassification(
            classification=classification,
            basis="VERIFIED_MAPPING",
            declaration=(
                f"Mapped from Screener Broad Industry label {label!r} via {MAPPING_VERSION}"
            ),
            provenance=ObservationProvenance(
                raw_artifact_id=artifact.id,
                source_location=source_location,
                normalizer_version="1",
            ),
        ),
    )
