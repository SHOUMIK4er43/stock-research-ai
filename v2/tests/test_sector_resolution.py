"""Part 6B0: deterministic sector-profile resolution. No LLM classification, no fuzzy
matching, no second taxonomy - BANK/IT_SERVICES_PROFILE come from application.sector_kpis
unchanged; only the VALUE is decided here, from the exact Screener Broad Industry label
already present on the SAME already-acquired artifact `extract_company` retrieved.
"""

from pathlib import Path
from uuid import uuid4

from test_calculations import evidence as evidence  # noqa: F401

from stock_research_ai.adapters.screener.sector import (
    ScreenerBroadIndustrySource,
    extract_broad_industry,
)
from stock_research_ai.application.sector_kpis import BANK, IT
from stock_research_ai.application.sector_resolution import (
    MAPPING_VERSION,
    SCREENER_INDUSTRY_PROFILE_MAP_V1,
    resolve_sector,
)
from stock_research_ai.domain.provenance import RawArtifact

FIX = Path(__file__).parent / "fixtures"


def _artifact_for(source, company_id_unused=None) -> RawArtifact:
    artifact_id = source.company.provenance.raw_artifact_id
    return next(a for a in source.raw_artifacts if a.id == artifact_id)


def test_extract_broad_industry_yesbank_is_banks():
    content = (FIX / "screener/yesbank_consolidated.html").read_bytes()
    found = extract_broad_industry(content, "utf-8")
    assert found is not None
    label, location = found
    assert label == "Banks"
    assert location.section == "SECTOR_CLASSIFICATION"


def test_extract_broad_industry_infy_is_it_software():
    content = (FIX / "screener/infy_standalone.html").read_bytes()
    found = extract_broad_industry(content, "utf-8")
    assert found is not None
    label, _location = found
    assert label == "IT - Software"


def test_extract_broad_industry_missing_peers_section_returns_none():
    assert extract_broad_industry(b"<html><body>no peers here</body></html>", "utf-8") is None


def test_mapping_covers_exactly_the_two_implemented_profiles():
    assert SCREENER_INDUSTRY_PROFILE_MAP_V1["Banks"] == ("BANK", BANK)
    assert SCREENER_INDUSTRY_PROFILE_MAP_V1["IT - Software"] == ("NON_FINANCIAL", IT)


def test_no_fuzzy_matching_case_and_substring_variants_are_unmapped():
    assert "banks" not in SCREENER_INDUSTRY_PROFILE_MAP_V1  # case-sensitive exact match only
    assert "Bank" not in SCREENER_INDUSTRY_PROFILE_MAP_V1  # substring is not a match
    assert "IT Services" not in SCREENER_INDUSTRY_PROFILE_MAP_V1  # a plausible near-miss


async def test_resolve_sector_maps_bank_from_real_yesbank_artifact(evidence):
    _settings, sources = evidence
    source = sources["YESBANK"]
    artifact = _artifact_for(source)
    content = (FIX / "screener/yesbank_consolidated.html").read_bytes()

    resolution = resolve_sector(ScreenerBroadIndustrySource(), artifact, content)

    assert resolution.resolution_status == "MAPPED"
    assert resolution.profile_id == BANK
    assert resolution.source_label == "Banks"
    assert resolution.mapping_version == MAPPING_VERSION
    assert resolution.declaration.classification == "BANK"
    assert resolution.declaration.basis == "VERIFIED_MAPPING"
    assert resolution.declaration.provenance is not None
    assert resolution.declaration.provenance.raw_artifact_id == artifact.id


async def test_resolve_sector_maps_it_services_from_real_infy_artifact(evidence):
    _settings, sources = evidence
    source = sources["INFY"]
    artifact = _artifact_for(source)
    content = (FIX / "screener/infy_standalone.html").read_bytes()

    resolution = resolve_sector(ScreenerBroadIndustrySource(), artifact, content)

    assert resolution.resolution_status == "MAPPED"
    assert resolution.profile_id == IT
    assert resolution.declaration.classification == "NON_FINANCIAL"
    assert resolution.declaration.basis == "VERIFIED_MAPPING"


class _FakeBroadIndustrySource:
    def __init__(self, result):
        self._result = result

    def extract(self, content, encoding):
        return self._result


def _fake_artifact() -> RawArtifact:
    return RawArtifact(
        id=uuid4(),
        source_url="https://www.screener.in/company/TEST/",
        source_type="text/html",
        retrieved_at="2025-01-01T00:00:00Z",
        content_reference="ref",
        content_sha256="0" * 64,
        collector_version="1",
    )


def test_resolve_sector_unknown_when_label_unmapped():
    resolution = resolve_sector(
        _FakeBroadIndustrySource(("Pharmaceuticals", None)), _fake_artifact(), b""
    )
    assert resolution.resolution_status == "UNMAPPED"
    assert resolution.source_label == "Pharmaceuticals"
    assert resolution.profile_id is None
    assert resolution.declaration.classification == "UNKNOWN"
    assert resolution.declaration.basis == "CALLER_DECLARED"


def test_resolve_sector_unknown_when_no_source_label():
    resolution = resolve_sector(_FakeBroadIndustrySource(None), _fake_artifact(), b"")
    assert resolution.resolution_status == "NO_SOURCE_LABEL"
    assert resolution.source_label is None
    assert resolution.profile_id is None
    assert resolution.declaration.classification == "UNKNOWN"


def test_resolution_never_forces_a_generic_default_profile():
    """An unmapped/absent label must resolve to UNKNOWN, never to BANK or IT_SERVICES_PROFILE
    merely because those are the only two implemented profiles."""
    for result in (("Something Else Entirely", None), None):
        resolution = resolve_sector(_FakeBroadIndustrySource(result), _fake_artifact(), b"")
        assert resolution.declaration.classification == "UNKNOWN"
        assert resolution.profile_id is None
