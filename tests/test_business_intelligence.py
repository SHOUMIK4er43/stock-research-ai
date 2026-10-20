"""DETAILED_RESEARCH_V1 Box 7: business, segment & operating KPI intelligence (offline).

Fixtures A-O are synthetic filings run through the real Box 1 catalogue, Box 2 retrieval,
Box 3 extraction and Box 4 reconciliation before Box 7 reads them.
"""

import ast
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from test_document_intelligence import make_pdf
from test_screener_dataset import dataset
from test_summary_input import INFY

from stock_research_ai.adapters.documents.document_parsers import LocalDocumentParser
from stock_research_ai.adapters.documents.evidence_fetcher import (
    RawArtifactEvidenceReader,
    RawArtifactEvidenceWriter,
    SafeEvidenceFetcher,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.application import business_registry as registry
from stock_research_ai.application.business_intelligence import (
    build_business_intelligence,
    canonical_business_json,
    verify_business_provenance,
)
from stock_research_ai.application.business_lineage import SegmentAlias
from stock_research_ai.application.business_lines import (
    expand_shorthand,
    rejoin,
    split_matrix_header,
    trailing_values,
)
from stock_research_ai.application.document_intelligence import extract_evidence
from stock_research_ai.application.evidence_catalogue import assemble_catalogue
from stock_research_ai.application.evidence_retrieval import retrieve_evidence
from stock_research_ai.application.financial_reconciliation import (
    build_canonical_dataset,
    detect_classification,
)
from stock_research_ai.domain.business_intelligence import (
    AnalyticStatus,
    Attribution,
    BusinessEvidence,
    BusinessEvidenceKind,
    BusinessIntelligenceDataset,
    BusinessLimits,
    CapacityStatus,
    Comparability,
    Domain,
    GeographyDimension,
    KPIPeriodKind,
    KPIUnit,
    LineageKind,
    NameMatch,
    ProjectStatus,
    ReconOutcome,
    Restated,
    SectorFamily,
    SegmentLevel,
    SegmentMetric,
    SegmentRole,
    SourceKind,
    SourceLocation,
    TotalOutcome,
)
from stock_research_ai.domain.document_intelligence import ExtractionLimits
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    EvidenceCompany,
    EvidenceDocumentType,
    SourceAuthority,
    SourceDiscovery,
    SourceOutcome,
)
from stock_research_ai.domain.evidence_retrieval import RetrievalPolicy
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Currency,
    Industry,
    PeriodType,
    ReportingBasis,
)

T = EvidenceDocumentType
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
FY26, FY25, FY24 = date(2026, 3, 31), date(2025, 3, 31), date(2024, 3, 31)
BSE = "https://www.bseindia.com"
SITE = "https://www.widgetco.com"
COMPANY = EvidenceCompany(
    company_key="screener:1489",
    name="Widget Co Ltd",
    source_company_key="INFY",
    reporting_scope=ReportingScope.CONSOLIDATED,
    website_url=SITE,
)

# --- fixture filings ---------------------------------------------------------------------


def segment_table(
    title="Notes to the Consolidated Financial Statements",
    years=("2026", "2025", "2024"),
    rows=None,
):
    head = [
        title,
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        " ".join(years),
    ]
    return head + (rows or SEGMENT_ROWS)


SEGMENT_ROWS = [
    "Segment revenue",
    "Chemicals 1,200 1,000 900",
    "Pharma 800 700 600",
    "Agri 400 300 250",
    "Total segment revenue 2,400 2,000 1,750",
    "Less: Inter-segment revenue 100 80 50",
    "Revenue from operations 2,300 1,920 1,700",
    "Segment results",
    "Chemicals 240 200 180",
    "Pharma 160 140 120",
    "Agri 40 30 20",
    "Total segment results 440 370 320",
    "Less: Finance costs 40 30 20",
    "Less: Unallocable expenditure 50 40 30",
    "Add: Other income 30 20 10",
    "Profit before tax 380 320 280",
    "Segment assets",
    "Chemicals 2,000 1,800 1,600",
    "Pharma 1,500 1,400 1,300",
    "Agri 700 600 500",
    "Total segment assets 4,200 3,800 3,400",
]

GEOGRAPHY = [
    "Consolidated revenue by geography",
    "(Rs. in crore)",
    "Particulars Year ended March 31,",
    "2026 2025",
    "India 1,000 900",
    "United States 600 500",
    "Europe 500 400",
    "Rest of the world 200 120",
    "Total 2,300 1,920",
    "* Revenue by geography is based on the location of customers.",
]

KPI_IT = [
    "Key operating metrics",
    "Particulars Year ended March 31,",
    "2026 2025",
    "Total employees 3,28,594 3,23,578",
    "Voluntary attrition (%) 13.0 14.1",
    "Utilization excluding trainees (%) 84.9 85.2",
    "Active clients 1,965 1,869",
    "Large deal TCV (US$ bn) 13.0 11.6",
    "Top 5 clients (%) 12.1 13.2",
    "Widget velocity index 12 10",
]

CAPACITY = [
    "Operational highlights",
    "Particulars Year ended March 31,",
    "2026 2025",
    "Installed capacity (MTPA) 12.5 10.0",
    "Capacity under construction (MTPA) 3.0 2.5",
    "Capacity utilisation (%) 78 74",
    "Production (MT) 9.1 7.2",
    "Sales volume (MT) 8.9 7.0",
    "Realisation (Rs. per tonne) 5,450 5,200",
]

ORDER_BOOK = [
    "Order book position",
    "(Rs. in crore)",
    "Particulars As at March 31, 2026 As at March 31, 2025",
    "Order book (executable over 24-30 months) 12,500 10,000",
    "Order inflow 6,000 5,100",
]

PROJECTS = [
    "Project pipeline",
    "Project: Dahej expansion",
    "Location: Gujarat",
    "Segment: Chemicals",
    "Capacity addition: 2.0 MTPA",
    "Capex: Rs. 1,200 crore",
    "Expected completion: Q4 FY2027",
    "Status: Under construction",
]

BANK_KPI = [
    "Key business parameters (Consolidated)",
    "(Rs. in crore)",
    "Particulars As at March 31, 2026 As at March 31, 2025",
    "Deposits 2,84,000 2,66,000",
    "Advances 2,46,000 2,28,000",
    "CASA ratio (%) 34.3 33.1",
    "Gross NPA (%) 1.6 1.7",
    "Net NPA (%) 0.3 0.6",
    "Number of branches 1,250 1,200",
    "Capital adequacy ratio (%) 16.5 15.9",
    "Installed capacity (MW) 100 90",
]


async def filings(root: Path, files: dict[str, tuple[EvidenceDocumentType, list[list[str]], str]]):
    """Synthetic PDFs -> real Box 1 catalogue -> Box 2 retrieval -> Box 3 extraction."""
    # These integration fixtures represent Widget Co filings. Print an explicit issuer on
    # page one so tests exercise the same fail-closed identity contract as production.
    bodies = {
        path: make_pdf(
            [[COMPANY.name, *pages[0]], *pages[1:]]
            if pages
            else [[COMPANY.name]]
        )
        for path, (_kind, pages, _host) in files.items()
    }
    candidates = [
        DocumentCandidate(
            document_type=kind,
            confidence=ClassificationConfidence.HIGH,
            title=path,
            url=f"{host}{path}",
            is_direct_document=True,
            discovery=DocumentDiscovery(
                source_name="test",
                discovered_via_url="https://www.screener.in/company/INFY/",
                locator=path,
                classified_as=kind,
                discovered_at=NOW,
            ),
        )
        for path, (kind, _pages, host) in files.items()
    ]
    catalogue = assemble_catalogue(
        COMPANY,
        [SourceDiscovery(source_name="test", candidates=tuple(candidates))],
        [SourceOutcome(source_name="test", status="SUCCESS", candidates_found=len(candidates))],
        catalogued_at=NOW,
    )

    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=bodies[request.url.path], headers={"content-type": "application/pdf"}
        )

    async def lookup(host: str) -> list[str]:
        return ["93.184.216.34"]

    manifest = await retrieve_evidence(
        catalogue,
        SafeEvidenceFetcher(httpx.MockTransport(serve), lookup),
        RawArtifactEvidenceWriter(FileRawArtifactStore(root)),
        RetrievalPolicy(
            concurrency=1,
            per_type_limit={T.ANNUAL_REPORT: 5, T.QUARTERLY_RESULT: 5, T.INVESTOR_PRESENTATION: 5},
        ),
        run_id=UUID(int=7),
        clock=lambda: NOW,
    )
    bundle = await extract_evidence(
        catalogue,
        manifest,
        RawArtifactEvidenceReader(root),
        LocalDocumentParser(),
        ExtractionLimits(),
        clock=lambda: NOW,
    )
    return bundle, catalogue


async def business(
    root: Path,
    files: dict[str, tuple[EvidenceDocumentType, list[list[str]], str]],
    *,
    industry: Industry | None = None,
    screener=None,
    **kw,
) -> tuple[BusinessIntelligenceDataset, CanonicalFinancialDataset, object]:
    bundle, catalogue = await filings(root, files)
    _CATALOGUES[bundle.bundle_id] = catalogue
    box4 = build_canonical_dataset(
        company_key="screener:1489",
        company_name="Widget Co Ltd",
        screeners=[],
        bundle=bundle,
        catalogue=catalogue,
        generated_at=NOW,
    )
    if industry is None and screener is None:
        # the synthetic filings describe an industrial company; it is declared, never
        # inferred (an unclassified Box 4 is UNKNOWN and gets no industrial treatment)
        industry = Industry.GENERAL
    if industry is not None:
        box4 = box4.model_copy(update={"industry": industry})
    elif screener is not None:  # as production: Box 4 classifies the acquired page itself
        decided = detect_classification([screener])
        box4 = box4.model_copy(update={"industry": decided.industry,
                                       "business_class": decided.business_class})
    data = build_business_intelligence(
        box4=box4, bundle=bundle, catalogue=catalogue, screener=screener, generated_at=NOW, **kw
    )
    return data, box4, bundle


def ar(*pages: list[str], host: str = BSE) -> tuple[EvidenceDocumentType, list[list[str]], str]:
    return (T.ANNUAL_REPORT, list(pages), host)


def ip(*pages: list[str]) -> tuple[EvidenceDocumentType, list[list[str]], str]:
    return (T.INVESTOR_PRESENTATION, list(pages), SITE)


def seg(data, key, metric=SegmentMetric.REVENUE, end=FY26, role=SegmentRole.REPORTABLE):
    found = [
        v
        for v in data.canonical_segment_values
        if v.segment_key == key
        and v.metric == metric
        and v.period.period_end == end
        and v.segment_role == role
    ]
    assert len(found) == 1, (key, metric, end, found)
    return found[0]


def analytic(data, code, key, end=FY26, qualifier=None):
    found = [
        a
        for a in data.segment_analytics
        if a.code == code
        and a.subject_key == key
        and a.period.period_end == end
        and (qualifier is None or a.qualifier == qualifier)
    ]
    assert len(found) == 1, (code, key, end, found)
    return found[0]


def kpi(data, code, end=FY26, qualifier=None):
    found = [
        v
        for v in data.canonical_kpi_values
        if v.canonical_kpi == code
        and v.period.period_end == end
        and (qualifier is None or v.qualifier == qualifier)
    ]
    assert len(found) == 1, (code, end, found)
    return found[0]


@pytest.fixture
async def stable(tmp_path):
    """Fixture A + E + L: a stable 3-segment company, one AR, geography table."""
    return await business(tmp_path, {"/ar26.pdf": ar(segment_table(), GEOGRAPHY)})


# --- domain contracts ----------------------------------------------------------------------


async def test_contracts_are_strict_and_immutable(stable):
    data, _box4, _bundle = stable
    o = data.segment_observations[0]
    with pytest.raises(ValidationError):
        o.metric = SegmentMetric.OTHER  # type: ignore[misc]
    with pytest.raises(ValidationError):
        type(o).model_validate({**o.model_dump(), "surprise": 1})
    with pytest.raises(ValidationError):  # Box 3 provenance must name its blocks
        SourceLocation(
            source_kind=SourceKind.BOX3_BLOCK,
            document_id=UUID(int=1),
            extraction_id=UUID(int=2),
            source_url="x",
            artifact_sha256="0" * 64,
            authority=SourceAuthority.PRIMARY_REGULATORY,
            page_start=1,
            locator="x",
        )


async def test_ids_and_serialization_are_deterministic(tmp_path):
    files = {"/ar26.pdf": ar(segment_table(), GEOGRAPHY, KPI_IT)}
    first, box4, bundle = await business(tmp_path / "a", files)
    second, _b, _x = await business(tmp_path / "b", files)
    assert canonical_business_json(first) == canonical_business_json(second)
    assert first.dataset_id == second.dataset_id
    again = build_business_intelligence(
        box4=box4, bundle=bundle, catalogue=None, screener=None, generated_at=NOW
    )
    assert again.dataset_id != first.dataset_id  # no catalogue -> no Box 3 reading at all
    payload = json.loads(canonical_business_json(first))
    assert payload["schema_version"] == "BUSINESS_INTELLIGENCE_DATASET_V1"


async def test_no_orphan_provenance_and_raw_values_on_their_lines(stable):
    data, box4, bundle = stable
    assert verify_business_provenance(data, bundle, box4) == []
    for o in data.segment_observations:
        assert o.source.document_id and o.source.block_ids and o.source.page_start
        assert o.source.artifact_sha256 in {e.artifact_sha256 for e in bundle.extractions}
    with pytest.raises(ValidationError, match="orphaned"):
        value = data.canonical_segment_values[0]
        BusinessIntelligenceDataset.model_validate(
            {
                **data.model_dump(),
                "canonical_segment_values": [
                    {**value.model_dump(), "selected_observation_id": UUID(int=99)}
                ],
            }
        )


async def test_raw_names_labels_and_units_are_preserved(tmp_path):
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(segment_table(), CAPACITY)})
    chem = next(
        o
        for o in data.segment_observations
        if o.segment_key == "chemicals" and o.metric == SegmentMetric.REVENUE
    )
    assert chem.segment_raw_name == "Chemicals" and chem.raw_value in ("1,200", "1,000", "900")
    assert chem.raw_unit == "Rs. in crore" and chem.currency == Currency.INR
    installed = next(
        o
        for o in data.operating_kpi_observations
        if o.canonical_kpi == "CAPACITY" and o.qualifier == "INSTALLED"
    )
    assert installed.raw_label == "Installed capacity (MTPA)"
    assert installed.raw_unit == "MTPA" and installed.unit == KPIUnit.MTPA


async def test_unknown_items_are_preserved_not_dropped(tmp_path):
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(KPI_IT)})
    unknown = [u for u in data.unmapped_lines if u.raw_label == "Widget velocity index"]
    assert len(unknown) == 1 and unknown[0].reason == "UNKNOWN_KPI"
    assert unknown[0].raw_values == ("12", "10") and unknown[0].domain == Domain.KPI
    assert data.statistics.unmapped_kpis >= 1


# --- segments ------------------------------------------------------------------------------


async def test_segment_revenue_result_assets_extracted(stable):
    data, _b, _x = stable
    assert seg(data, "chemicals").value == Decimal(1200)
    assert seg(data, "pharma", SegmentMetric.SEGMENT_RESULT, FY25).value == Decimal(140)
    assert seg(data, "agri", SegmentMetric.ASSETS, FY24).value == Decimal(500)
    chem = seg(data, "chemicals")
    assert chem.reporting_basis == ReportingBasis.CONSOLIDATED
    assert chem.period.period_type == PeriodType.FY and chem.outcome == ReconOutcome.SINGLE_SOURCE
    names = {d.canonical_name for d in data.business_structure.segments}
    assert names == {"Chemicals", "Pharma", "Agri"}


async def test_segment_total_eliminations_and_unallocated(stable):
    data, _b, _x = stable
    checks = {
        (c.metric, c.reported_total_source, c.period.period_end): c
        for c in data.total_reconciliations
    }
    assert checks[("REVENUE", "TABLE_TOTAL", FY26)].outcome == TotalOutcome.MATCH
    revenue = checks[("REVENUE", "TABLE_COMPANY_LINE", FY26)]
    assert revenue.outcome == TotalOutcome.ELIMINATIONS_EXPLAIN_DIFFERENCE
    assert revenue.difference == Decimal(100)
    result = checks[("SEGMENT_RESULT", "TABLE_COMPANY_LINE", FY26)]
    assert result.outcome == TotalOutcome.UNALLOCATED_EXPLAINS_DIFFERENCE
    assert sorted(a.signed_value for a in result.adjustments) == [-50, -40, 30]
    # values themselves are never mutated to make totals add up
    assert (
        seg(data, "chemicals").value + seg(data, "pharma").value + seg(data, "agri").value == 2400
    )


async def test_segment_hierarchy_only_when_the_table_establishes_it(tmp_path):
    rows = [
        "Segment revenue",
        "Power",
        "Generation 500 450",
        "Transmission 300 250",
        "Total Power 800 700",
        "Cement 400 350",
        "Total segment revenue 1,200 1,050",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar.pdf": ar(segment_table(years=("2026", "2025"), rows=rows))}
    )
    definitions = {d.segment_key: d for d in data.business_structure.segments}
    assert definitions["generation"].parent_key == "power"
    assert definitions["generation"].level == SegmentLevel.SUBSEGMENT
    assert set(definitions["power"].child_keys) == {"generation", "transmission"}
    assert definitions["cement"].parent_key is None
    hierarchy = [c for c in data.total_reconciliations if c.metric == "REVENUE_HIERARCHY"]
    assert hierarchy and all(c.outcome == TotalOutcome.MATCH for c in hierarchy)
    flat, _b, _x = await business(tmp_path / "flat", {"/ar.pdf": ar(segment_table())})
    assert all(d.parent_key is None for d in flat.business_structure.segments)


async def test_exact_alias_normalization_and_configured_alias(tmp_path):
    rows = ["Segment revenue", "Energy & Utilities (1) 100 90", "Total 100 90"]
    other = ["Segment revenue", "Energy and Utilities 100 90", "Total 100 90"]
    data, _b, _x = await business(
        tmp_path,
        {
            "/ar26.pdf": ar(segment_table(years=("2026", "2025"), rows=rows)),
            "/ip26.pdf": ip(segment_table("Consolidated", years=("2026", "2025"), rows=other)),
        },
    )
    value = seg(data, "energy and utilities")
    assert value.outcome == ReconOutcome.EXACT_MATCH and len(value.supporting_observation_ids) == 1
    rows_b = ["Segment revenue", "Utilities 100 90", "Total 100 90"]
    aliased, _b, _x = await business(
        tmp_path / "alias",
        {"/ar.pdf": ar(segment_table(years=("2026", "2025"), rows=rows_b))},
        aliases=[SegmentAlias("Utilities", "Energy and Utilities", "board note 4.2")],
    )
    definition = next(d for d in aliased.business_structure.segments)
    assert definition.segment_key == "energy and utilities"
    assert definition.name_match == NameMatch.EXPLICIT_ALIAS
    raw = next(o for o in aliased.segment_observations if o.segment_role == SegmentRole.REPORTABLE)
    assert raw.segment_key == "utilities" and raw.segment_raw_name == "Utilities"  # raw kept


def two_year(rows):
    return segment_table(years=("2026", "2025"), rows=rows)


def prior_year(rows):
    head = [
        "Notes to the Consolidated Financial Statements",
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2025 2024",
    ]
    return head + rows


async def test_explicit_rename_links_history(tmp_path):
    """Fixture B."""
    old = ["Segment revenue", "Chemicals 1,000 900", "Crop Care 300 250", "Total 1,300 1,150"]
    new = [
        "Segment revenue",
        "Chemicals 1,200 1,000",
        "Agri Solutions 400 300",
        "Total 1,600 1,300",
    ]
    note = [
        "The Crop Care segment has been renamed as Agri Solutions with no change in its "
        "composition of businesses during the year."
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(two_year(new), note)}
    )
    renamed = [r for r in data.segment_name_relations if r.outcome == NameMatch.RENAMED]
    assert [(r.from_key, r.to_key) for r in renamed] == [("crop care", "agri solutions")]
    event = next(e for e in data.segment_lineage if e.kind == LineageKind.RENAMED)
    assert event.old_segments == ("Crop Care",) and event.new_segments == ("Agri Solutions",)
    fy25 = seg(data, "agri solutions", end=FY25)
    assert fy25.outcome == ReconOutcome.EXACT_MATCH  # Crop Care FY25 = Agri Solutions FY25
    growth = analytic(data, "SEGMENT_REVENUE_GROWTH", "agri solutions", FY25)
    assert growth.status == AnalyticStatus.CALCULATED and growth.value == Decimal(20)
    definition = next(
        d for d in data.business_structure.segments if d.segment_key == "agri solutions"
    )
    assert definition.name_match == NameMatch.RENAMED
    assert set(definition.raw_names) == {"Crop Care", "Agri Solutions"}


async def test_ambiguous_rename_is_not_merged(tmp_path):
    old = ["Segment revenue", "Chemicals 1,000 900", "Crop Care 300 250", "Total 1,300 1,150"]
    new = ["Segment revenue", "Chemicals 1,200", "Agri Solutions 400", "Total 1,600"]
    head = [
        "Notes to the Consolidated Financial Statements",
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(head + new)}
    )
    possible = [r for r in data.segment_name_relations if r.outcome == NameMatch.POSSIBLE_RENAME]
    assert [(r.from_key, r.to_key) for r in possible] == [("crop care", "agri solutions")]
    assert {d.segment_key for d in data.business_structure.segments} >= {
        "crop care",
        "agri solutions",
    }
    assert any(w.code == "SEGMENT_ALIAS_AMBIGUOUS" for w in data.warnings)
    agri = analytic(data, "SEGMENT_REVENUE_GROWTH", "agri solutions")
    assert agri.status == AnalyticStatus.NOT_COMPARABLE and agri.value is None


async def test_structure_change_without_restatement_blocks_history(tmp_path):
    """Fixture C: 2 segments -> 3 segments, comparatives not restated."""
    old = ["Segment revenue", "Chemicals 1,000 900", "Agri 300 250", "Total 1,300 1,150"]
    new_head = [
        "Notes to the Consolidated Financial Statements",
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026",
    ]
    new = [
        "Segment revenue",
        "Specialty Chemicals 700",
        "Bulk Chemicals 500",
        "Agri 400",
        "Total 1,600",
    ]
    note = [
        "During the year the Company reorganised its reportable segments. Comparative "
        "segment figures have not been restated as it is impracticable to do so."
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(new_head + new, note)}
    )
    event = next(e for e in data.segment_lineage if e.kind == LineageKind.REORGANIZED)
    assert event.restated_comparatives == Restated.NO and event.effective_period_end == FY26
    change = next(a for a in data.segment_comparability if a.to_period_end == FY26)
    assert change.status == Comparability.STRUCTURE_CHANGED
    assert set(change.added_segments) == {"specialty chemicals", "bulk chemicals"}
    agri = analytic(data, "SEGMENT_REVENUE_GROWTH", "agri")  # same name, new definition
    assert agri.status == AnalyticStatus.NOT_COMPARABLE and agri.value is None
    assert data.statistics.segment_structure_changes == 1
    earlier = analytic(data, "SEGMENT_REVENUE_GROWTH", "agri", FY25)  # within the old structure
    assert earlier.status == AnalyticStatus.CALCULATED and earlier.value == Decimal(20)


async def test_restated_comparatives_are_comparable(tmp_path):
    """Fixture D."""
    old = ["Segment revenue", "Chemicals 1,000 900", "Agri 300 250", "Total 1,300 1,150"]
    new = [
        "Segment revenue",
        "Specialty Chemicals 700 560",
        "Bulk Chemicals 500 440",
        "Agri 400 280",
        "Total 1,600 1,280",
    ]
    note = [
        "During the year the Company reorganised its reportable segments. Comparative "
        "segment information for the previous year has been restated accordingly."
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(two_year(new), note)}
    )
    assert next(e for e in data.segment_lineage).restated_comparatives == Restated.YES
    latest = next(a for a in data.segment_comparability if a.to_period_end == FY26)
    assert latest.status == Comparability.RESTATED_COMPARABLE
    earlier = next(a for a in data.segment_comparability if a.to_period_end == FY25)
    assert earlier.status == Comparability.STRUCTURE_CHANGED
    agri25 = seg(data, "agri", end=FY25)
    assert agri25.outcome == ReconOutcome.RESTATEMENT and agri25.value == Decimal(280)
    assert len(agri25.conflicting_observation_ids) == 1  # the original 300 stays visible
    growth = analytic(data, "SEGMENT_REVENUE_GROWTH", "specialty chemicals")
    assert growth.status == AnalyticStatus.CALCULATED and growth.value == Decimal(25)


async def test_basis_period_and_currency_isolation(tmp_path):
    standalone = [
        "Notes to the Standalone Financial Statements",
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,100 950",
        "Pharma 700 650",
        "Total 1,800 1,600",
    ]
    usd = [
        "Consolidated segment information",
        "(US$ in million)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 144 120",
        "Pharma 96 84",
        "Total 240 204",
    ]
    quarter = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Quarter ended June 30, 2026 Quarter ended June 30, 2025",
        "Segment revenue",
        "Chemicals 310 260",
        "Pharma 210 180",
        "Total 520 440",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar.pdf": ar(segment_table(), standalone, usd), "/qr.pdf": ar(quarter)}
    )
    chem = [
        v
        for v in data.canonical_segment_values
        if v.segment_key == "chemicals" and v.metric == SegmentMetric.REVENUE
    ]
    keys = {
        (v.reporting_basis, v.currency, v.period.period_type, v.period.period_end) for v in chem
    }
    assert (ReportingBasis.STANDALONE, Currency.INR, PeriodType.FY, FY26) in keys
    assert (ReportingBasis.CONSOLIDATED, Currency.INR, PeriodType.FY, FY26) in keys
    assert (ReportingBasis.CONSOLIDATED, Currency.USD, PeriodType.FY, FY26) in keys
    assert (
        ReportingBasis.CONSOLIDATED,
        Currency.INR,
        PeriodType.QUARTER,
        date(2026, 6, 30),
    ) in keys
    assert all(v.outcome == ReconOutcome.SINGLE_SOURCE for v in chem)  # nothing merged
    usd_check = [
        c
        for c in data.total_reconciliations
        if c.currency == Currency.USD and c.reported_total_source in ("NONE",)
    ]
    assert all(
        c.outcome in (TotalOutcome.NOT_COMPARABLE, TotalOutcome.INSUFFICIENT_DATA)
        for c in usd_check
    )


async def test_matrix_layout_with_company_vocabulary(tmp_path):
    """INFY-style: segments as columns, wrapped header, one line per period."""
    rows_layout = [
        "Segment information",
        "Segment revenue",
        "Financial Services (1) 10 9",
        "Hi-Tech 5 4",
        "All other segments 1 1",
        "Total 16 14",
    ]
    matrix = [
        "Business segments",
        "Year ended March 31, 2026 and March 31, 2025:",
        "(Rs. in crore)",
        "Particulars Financial",
        "Services (1)",
        "Hi-Tech All other",
        "segments (2)",
        "Total",
        "Revenue from",
        "operations 1,000 500 100 1,600",
        "900 400 100 1,400",
        "Segment Profit 200 100 10 310",
        "180 80 20 280",
        "Unallocable expenses 40",
        "30",
        "Profit before tax 270",
        "250",
        "(1) Financial Services include enterprises in banking and insurance",
    ]
    data, _b, _x = await business(
        tmp_path,
        {
            "/qr.pdf": (T.QUARTERLY_RESULT, [rows_layout], BSE),
            "/ar.pdf": ar(["Notes to the Consolidated Financial Statements", *matrix]),
        },
    )
    assert seg(data, "hi-tech").value == Decimal(500)
    assert seg(data, "financial services", end=FY25).value == Decimal(900)
    assert seg(data, "total", SegmentMetric.SEGMENT_RESULT, role=SegmentRole.TOTAL).value == 310
    result = next(
        c
        for c in data.total_reconciliations
        if c.metric == "SEGMENT_RESULT"
        and c.reported_total_source == "TABLE_COMPANY_LINE"
        and c.period.period_end == FY26
    )
    assert result.outcome == TotalOutcome.UNALLOCATED_EXPLAINS_DIFFERENCE
    obs = next(o for o in data.segment_observations if o.segment_key == "hi-tech")
    assert "MATRIX_HEADER_FROM_COMPANY_VOCABULARY" in obs.warnings


def test_matrix_header_split_is_unique_or_refused():
    words = "Financial Services (1) Manufacturing Energy, Utilities and Services Total".split()
    vocab = {"financial services", "manufacturing", "energy, utilities and services"}
    assert split_matrix_header(words, 4, vocab) == [
        "Financial Services",
        "Manufacturing",
        "Energy, Utilities and Services",
        "Total",
    ]
    assert split_matrix_header(words, 4, vocab - {"manufacturing"}) is None
    ambiguous = {
        "financial services",
        "manufacturing",
        "energy, utilities and services",
        "manufacturing energy,",
        "utilities and services",
    }
    assert split_matrix_header(words, 4, ambiguous) is None


def test_split_numbers_rejoin_only_when_unique():
    assert rejoin(["57,", "45", "4", "48,595"], 2) == ["57,454", "48,595"]
    assert rejoin(["27,", "877", "17,797"], 2) == ["27,877", "17,797"]
    assert rejoin(["1", "2", "3"], 2) is None  # '12 3' or '1 23': ambiguous
    assert trailing_values("Financial Services (1) 13,463 7,668") == (
        "Financial Services (1)",
        ["13,463", "7,668"],
    )


# --- geography -----------------------------------------------------------------------------


async def test_geography_extraction_dimension_and_attribution(stable):
    data, _b, _x = stable
    rows = {
        v.geography_key: v for v in data.canonical_geography_values if v.period.period_end == FY26
    }
    assert rows["india"].dimension == GeographyDimension.COUNTRY
    assert rows["united states"].dimension == GeographyDimension.COUNTRY
    assert rows["europe"].dimension == GeographyDimension.REGION
    assert rows["rest of the world"].dimension == GeographyDimension.MARKET_GROUP
    assert rows["total"].is_total
    assert {v.attribution for v in rows.values()} == {Attribution.CUSTOMER_LOCATION}
    assert rows["europe"].value == Decimal(500)


async def test_geography_mix_growth_and_total(stable):
    data, _b, _x = stable
    mix = analytic(data, "GEOGRAPHIC_REVENUE_MIX", "india")
    assert mix.value == Decimal(1000) / Decimal(2300) * 100
    growth = analytic(data, "GEOGRAPHIC_REVENUE_GROWTH", "rest of the world")
    assert growth.value == (Decimal(200) / Decimal(120) - 1) * 100
    table = next(
        c
        for c in data.total_reconciliations
        if c.domain == Domain.GEOGRAPHY
        and c.reported_total_source == "TABLE_TOTAL"
        and c.period.period_end == FY26
    )
    assert table.outcome == TotalOutcome.MATCH


async def test_geography_attribution_methods_never_reconcile(tmp_path):
    by_entity = [
        "Consolidated revenue by geography",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "India 1,200 1,000",
        "United States 500 500",
        "Europe 400 300",
        "Rest of the world 200 120",
        "Total 2,300 1,920",
        "* Revenue by geography is based on the location of the entity making the sale.",
    ]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(GEOGRAPHY, by_entity)})
    india = [
        v
        for v in data.canonical_geography_values
        if v.geography_key == "india" and v.period.period_end == FY26
    ]
    assert {(v.attribution, v.value) for v in india} == {
        (Attribution.CUSTOMER_LOCATION, Decimal(1000)),
        (Attribution.ENTITY_LOCATION, Decimal(1200)),
    }
    assert all(v.outcome == ReconOutcome.SINGLE_SOURCE for v in india)


async def test_geography_aliases_only_when_explicit(tmp_path):
    usa = [line.replace("United States", "USA") for line in GEOGRAPHY]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(GEOGRAPHY), "/ip.pdf": ip(usa)})
    keys = {v.geography_key for v in data.canonical_geography_values}
    assert {"united states", "usa"} <= keys  # never assumed identical


async def test_unknown_geography_and_attribution_warned(tmp_path):
    lines = [
        "Revenue by geography",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "India 1,000 900",
        "Narnia 300 200",
        "Total 1,300 1,100",
    ]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(lines)})
    narnia = next(o for o in data.geography_observations if o.geography_key == "narnia")
    assert narnia.dimension == GeographyDimension.UNKNOWN and "UNKNOWN_GEOGRAPHY" in narnia.warnings
    codes = {w.code for w in data.warnings}
    assert {"UNKNOWN_GEOGRAPHY", "GEOGRAPHY_ATTRIBUTION_UNKNOWN"} <= codes


# --- KPIs ----------------------------------------------------------------------------------


@pytest.fixture
async def it_company(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    return await business(tmp_path / "f", {"/ar.pdf": ar(KPI_IT)}, screener=screener)


async def test_it_kpis_typed_units_and_periods(it_company):
    data, _b, _x = it_company
    assert data.sector == SectorFamily.IT_SERVICES
    employees = kpi(data, "EMPLOYEES")
    assert employees.value == Decimal(328594) and employees.unit == KPIUnit.PEOPLE
    assert employees.period_kind == KPIPeriodKind.POINT_IN_TIME
    attrition = kpi(data, "ATTRITION")
    assert attrition.unit == KPIUnit.PERCENT and attrition.qualifier == "VOLUNTARY"
    assert attrition.period_kind == KPIPeriodKind.FY
    assert kpi(data, "IT_UTILIZATION").qualifier == "EXCLUDING_TRAINEES"
    tcv = kpi(data, "LARGE_DEAL_TCV")
    assert tcv.unit == KPIUnit.MONEY and tcv.currency == Currency.USD
    assert not [v for v in data.canonical_kpi_values if v.canonical_kpi == "TCV"]
    assert kpi(data, "TOP_5_CLIENTS_SHARE").value == Decimal("12.1")
    assert data.business_structure.sector_reason.startswith("Box 1B classification")


async def test_capacity_utilization_volume_realization(tmp_path):
    """Fixture F."""
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(CAPACITY)})
    statuses = {f.capacity_status: f for f in data.capacity_facts if f.as_of == FY26}
    assert statuses[CapacityStatus.INSTALLED].value == Decimal("12.5")
    assert statuses[CapacityStatus.UNDER_CONSTRUCTION].value == Decimal("3.0")
    assert {f.unit for f in data.capacity_facts} == {KPIUnit.MTPA}
    assert kpi(data, "CAPACITY_UTILIZATION").value == Decimal(78)
    assert kpi(data, "PRODUCTION").unit == KPIUnit.MT
    assert kpi(data, "SALES_VOLUME").value == Decimal("8.9")  # never = production
    realization = kpi(data, "REALIZATION")
    assert realization.unit == KPIUnit.MONEY_PER_UNIT and realization.currency == Currency.INR
    growth = analytic(data, "CAPACITY_GROWTH", "CAPACITY||", qualifier="INSTALLED")
    assert growth.value == Decimal(25)
    assert analytic(data, "VOLUME_GROWTH", "SALES_VOLUME||").status == AnalyticStatus.CALCULATED
    assert analytic(data, "REALIZATION_GROWTH", "REALIZATION||").status == (
        AnalyticStatus.CALCULATED
    )


async def test_order_book_as_of_definition_and_growth(tmp_path):
    """Fixture G."""
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(ORDER_BOOK)})
    book = next(f for f in data.order_book_facts if f.kind == "ORDER_BOOK" and f.as_of == FY26)
    assert book.value == Decimal(12500) and book.currency == Currency.INR
    assert book.executable_period == "executable over 24-30 months"
    assert book.scope == "COMPANY"
    growth = analytic(data, "ORDER_BOOK_GROWTH", "ORDER_BOOK||")
    assert growth.value == Decimal(25)
    inflow = next(o for o in data.operating_kpi_observations if o.canonical_kpi == "ORDER_INFLOW")
    assert "UNKNOWN_KPI_PERIOD" in inflow.warnings  # a flow under an 'as at' header
    assert not [v for v in data.canonical_kpi_values if v.canonical_kpi == "ORDER_INFLOW"]


async def test_project_facts_from_labelled_blocks(tmp_path):
    """Fixture H."""
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(PROJECTS)})
    (project,) = data.project_facts
    assert project.name == "Dahej expansion" and project.location == "Gujarat"
    assert project.capacity_addition == Decimal("2.0") and project.capacity_unit == KPIUnit.MTPA
    assert project.capex_value == Decimal(1200) and project.capex_currency == Currency.INR
    assert project.expected_completion_raw == "Q4 FY2027"
    assert project.status == ProjectStatus.UNDER_CONSTRUCTION
    assert ("Status", "Under construction") in project.fields


async def test_bank_kpis_and_sector_safety(tmp_path):
    """Fixture M."""
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(BANK_KPI)}, industry=Industry.BANK)
    assert data.sector == SectorFamily.FINANCIAL
    assert kpi(data, "DEPOSITS").value == Decimal(284000)
    assert kpi(data, "CASA_RATIO").unit == KPIUnit.PERCENT
    assert kpi(data, "GROSS_NPA").unit == KPIUnit.PERCENT
    assert kpi(data, "BRANCHES").value == Decimal(1250)
    assert kpi(data, "CAPITAL_ADEQUACY").value == Decimal("16.5")
    refused = next(u for u in data.unmapped_lines if u.raw_label.startswith("Installed capacity"))
    assert refused.reason == "SECTOR_NOT_APPLICABLE"
    assert not data.capacity_facts and not data.order_book_facts
    assert not [a for a in data.segment_analytics if "MARGIN" in a.code]


async def test_banking_kpis_never_applied_to_an_it_company(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    data, _b, _x = await business(tmp_path / "f", {"/ar.pdf": ar(BANK_KPI)}, screener=screener)
    assert not [
        v for v in data.canonical_kpi_values if v.canonical_kpi in ("DEPOSITS", "CASA_RATIO")
    ]
    reasons = {u.raw_label.split(" (")[0]: u.reason for u in data.unmapped_lines}
    assert reasons["CASA ratio"] == "SECTOR_NOT_APPLICABLE"


async def test_kpi_referencing_box4_instead_of_copying(tmp_path):
    from stock_research_ai.domain.financial_canonical import (
        CanonicalFinancialValue,
        Confidence,
        FinancialPeriod,
        Outcome,
        Scale,
        StatementType,
        ValueKind,
    )

    data, box4, bundle = await business(tmp_path, {"/ar.pdf": ar(BANK_KPI)}, industry=Industry.BANK)
    deposits = kpi(data, "DEPOSITS")
    assert deposits.box4_canonical_value_id is None  # Box 4 holds no deposits here

    def box4_deposits(amount: str) -> CanonicalFinancialValue:
        return CanonicalFinancialValue(
            canonical_value_id=UUID(int=41),
            metric=CanonicalMetric.DEPOSITS,
            value=Decimal(amount),
            value_kind=ValueKind.MONEY,
            currency=Currency.INR,
            scale=Scale.CRORES,
            period=FinancialPeriod(
                period_type=PeriodType.POINT_IN_TIME, label="Mar 2026", period_end=FY26
            ),
            reporting_basis=ReportingBasis.CONSOLIDATED,
            statement_type=StatementType.BALANCE_SHEET,
            selected_observation_id=UUID(int=42),
            supporting_observation_ids=(),
            conflicting_observation_ids=(),
            reconciliation_record_id=UUID(int=43),
            outcome=Outcome.SINGLE_SOURCE,
            selection_reason="test",
            confidence=Confidence.HIGH,
        )

    for amount, comparison in (("284000", "EXACT_MATCH"), ("290000", "CONFLICT")):
        linked = box4.model_copy(update={"canonical_values": (box4_deposits(amount),)})
        again = build_business_intelligence(
            box4=linked,
            bundle=bundle,
            catalogue=_catalogue(bundle),
            screener=None,
            generated_at=NOW,
        )
        value = kpi(again, "DEPOSITS")
        assert value.box4_canonical_value_id == UUID(int=41)
        assert value.box4_comparison == comparison
        assert value.value == Decimal(284000)  # Box 7 keeps its own reported number
        conflicts = [w for w in again.warnings if w.code == "KPI_RECONCILIATION_CONFLICT"]
        assert bool(conflicts) == (comparison == "CONFLICT")


_CATALOGUES: dict[UUID, object] = {}


def _catalogue(bundle):
    return _CATALOGUES[bundle.bundle_id]


async def test_segment_and_geography_scope_on_kpis(tmp_path):
    lines = [
        "Operational highlights",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Sales volume - Domestic (MT) 6.0 5.0",
        "Sales volume - Exports (MT) 2.9 2.0",
    ]
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(lines)})
    scopes = {
        (v.geography_key, v.value) for v in data.canonical_kpi_values if v.period.period_end == FY26
    }
    assert scopes == {("domestic", Decimal(6)), ("exports", Decimal("2.9"))}


async def test_missing_period_and_unit_stay_low_confidence(tmp_path):
    """Fixture N."""
    lines = ["Operational highlights", "Installed capacity 12.5", "Plant utilisation 80"]
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(lines)})
    observations = data.operating_kpi_observations
    assert {o.canonical_kpi for o in observations} == {"CAPACITY", "CAPACITY_UTILIZATION"}
    for o in observations:
        assert o.confidence.value == "LOW" and "UNKNOWN_KPI_PERIOD" in o.warnings
        assert o.period is None and o.period_kind == KPIPeriodKind.UNKNOWN
    assert (
        "UNKNOWN_KPI_UNIT"
        in next(o for o in observations if o.canonical_kpi == "CAPACITY").warnings
    )
    assert data.canonical_kpi_values == ()


async def test_narrative_only_evidence_never_becomes_a_number(tmp_path):
    """Fixture O."""
    text = [
        "Our plants operated at healthy capacity utilisation levels during the year and the "
        "Company derives revenues from products such as specialty polymers and additives."
    ]
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(text)})
    assert data.operating_kpi_observations == () and data.capacity_facts == ()
    kinds = {e.evidence_kind for e in data.business_evidence}
    assert BusinessEvidenceKind.PRODUCT_SERVICE in kinds
    product = next(
        e for e in data.business_evidence if e.evidence_kind == BusinessEvidenceKind.PRODUCT_SERVICE
    )
    assert product.source.page_start == 1 and product.source.block_ids
    assert product.evidence_id in data.business_structure.product_service_evidence_ids


# --- reconciliation ------------------------------------------------------------------------


async def test_filing_beats_presentation_source_precedence(tmp_path):
    """Fixture K."""
    presentation = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,250 1,000",
        "Pharma 800 700",
        "Agri 400 300",
        "Total 2,450 2,000",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar.pdf": ar(segment_table()), "/ip.pdf": ip(presentation)}
    )
    chem = seg(data, "chemicals")
    assert chem.outcome == ReconOutcome.SOURCE_PRECEDENCE and chem.value == Decimal(1200)
    assert len(chem.conflicting_observation_ids) == 1
    selected = next(
        o for o in data.segment_observations if o.observation_id == chem.selected_observation_id
    )
    assert selected.source.authority == SourceAuthority.PRIMARY_REGULATORY
    assert seg(data, "pharma").outcome == ReconOutcome.EXACT_MATCH
    assert any(w.code == "SEGMENT_RECONCILIATION_CONFLICT" for w in data.warnings)


async def test_equal_authority_conflict_has_no_canonical_value(tmp_path):
    other = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,300 1,000",
        "Pharma 800 700",
        "Total 2,100 1,700",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar.pdf": ar(two_year(SEGMENT_ROWS[:5])), "/qr.pdf": ar(other)}
    )
    record = next(
        r
        for r in data.reconciliation_records
        if r.key.startswith("SEGMENT|chemicals|")
        and "|FY|2026-03-31|" in r.key
        and "|REVENUE|" in r.key
    )
    assert record.outcome == ReconOutcome.CONFLICT and record.selected_observation_id is None
    assert not [
        v
        for v in data.canonical_segment_values
        if v.segment_key == "chemicals" and v.period.period_end == FY26
    ]


async def test_tolerance_match(tmp_path):
    rounded = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,200.4 1,000",
        "Total 1,200.4 1,000",
    ]
    plain = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,200 1,000",
        "Total 1,200 1,000",
    ]
    data, _b, _x = await business(tmp_path, {"/a.pdf": ar(rounded), "/b.pdf": ar(plain)})
    assert seg(data, "chemicals").outcome == ReconOutcome.MATCH_WITHIN_TOLERANCE


async def test_ambiguous_duplicate_in_one_table(tmp_path):
    lines = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 1,200 1,000",
        "Chemicals 1,300 1,000",
        "Total 2,500 2,000",
    ]
    data, _b, _x = await business(tmp_path, {"/a.pdf": ar(lines)})
    record = next(
        r
        for r in data.reconciliation_records
        if r.key.startswith("SEGMENT|chemicals|") and "2026-03-31" in r.key
    )
    assert record.outcome == ReconOutcome.AMBIGUOUS


async def test_different_kpi_definitions_do_not_reconcile(tmp_path):
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(CAPACITY)})
    capacity = [
        v
        for v in data.canonical_kpi_values
        if v.canonical_kpi == "CAPACITY" and v.period.period_end == FY26
    ]
    assert {v.qualifier for v in capacity} == {"INSTALLED", "UNDER_CONSTRUCTION"}
    assert all(v.outcome == ReconOutcome.SINGLE_SOURCE for v in capacity)


# --- analytics -----------------------------------------------------------------------------


async def test_segment_growth_margin_and_mix(stable):
    data, _b, _x = stable
    growth = analytic(data, "SEGMENT_REVENUE_GROWTH", "chemicals")
    assert growth.value == Decimal(20) and growth.comparability == Comparability.COMPARABLE
    assert len(growth.input_observation_ids) == 2 and growth.input_canonical_ids
    result = analytic(data, "SEGMENT_RESULT_GROWTH", "agri")
    assert result.value == (Decimal(40) / Decimal(30) - 1) * 100
    margin = analytic(data, "SEGMENT_RESULT_MARGIN", "pharma")
    assert margin.value == Decimal(20) and margin.formula_id.endswith("SEGMENT_RESULT_MARGIN")
    mix = analytic(data, "SEGMENT_REVENUE_MIX", "chemicals")
    assert mix.value == Decimal(50)  # 1,200 / 2,400 (before eliminations, stated)
    assert mix.metadata["denominator"] == "SUM_OF_REPORTABLE_SEGMENTS"
    assert mix.metadata["company_total_check"] == "ELIMINATIONS_EXPLAIN_DIFFERENCE"
    oldest = analytic(data, "SEGMENT_REVENUE_GROWTH", "chemicals", FY24)
    assert oldest.status == AnalyticStatus.INSUFFICIENT_HISTORY and oldest.value is None
    for a in data.segment_analytics:
        assert set(a.input_observation_ids) <= {
            *(o.observation_id for o in data.segment_observations),
            *(o.observation_id for o in data.geography_observations),
            *(o.kpi_observation_id for o in data.operating_kpi_observations),
        }


async def test_missing_period_is_not_interpolated(tmp_path):
    gap = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2024",
        "Segment revenue",
        "Chemicals 1,200 900",
        "Pharma 800 600",
        "Total 2,000 1,500",
    ]
    data, _b, _x = await business(tmp_path, {"/a.pdf": ar(gap)})
    growth = analytic(data, "SEGMENT_REVENUE_GROWTH", "chemicals")
    assert growth.status == AnalyticStatus.INSUFFICIENT_HISTORY  # FY24 is not FY25


def test_formula_registry_documented():
    from stock_research_ai.application.business_analytics import FORMULAS

    codes = {f.code for f in FORMULAS}
    assert {
        "SEGMENT_REVENUE_GROWTH",
        "SEGMENT_RESULT_MARGIN",
        "SEGMENT_REVENUE_MIX",
        "GEOGRAPHIC_REVENUE_MIX",
        "GEOGRAPHIC_REVENUE_GROWTH",
        "ORDER_BOOK_GROWTH",
        "CAPACITY_GROWTH",
        "VOLUME_GROWTH",
        "REALIZATION_GROWTH",
    } <= codes
    assert all(f.formula_id.startswith("business-formulas/1:") for f in FORMULAS)


# --- registry / limits / boundaries -------------------------------------------------------


def test_registry_aliases_are_explicit_and_sector_aware():
    it, bank = SectorFamily.IT_SERVICES, SectorFamily.FINANCIAL
    assert registry.match_kpi("utilization", it, None).definition.code == "IT_UTILIZATION"
    assert registry.match_kpi("utilization", SectorFamily.MANUFACTURING, None).reason == (
        "SECTOR_NOT_APPLICABLE"
    )
    assert registry.match_kpi("installed capacity", bank, None).reason == "SECTOR_NOT_APPLICABLE"
    assert registry.match_kpi("order backlog", SectorFamily.GENERAL, None).definition.code == (
        "ORDER_BOOK"
    )
    assert registry.match_kpi("large deal tcv", it, None).definition.code == "LARGE_DEAL_TCV"
    assert registry.match_kpi("casa", bank, KPIUnit.PERCENT).definition.code == "CASA_RATIO"
    assert registry.match_kpi("casa", bank, KPIUnit.MONEY).definition.code == "CASA_DEPOSITS"
    assert registry.match_kpi("widget velocity index", it, None).reason == "UNKNOWN_KPI"
    assert registry.read_unit("(Dollars in millions)").currency == Currency.USD
    assert registry.read_unit("MTPA").unit == KPIUnit.MTPA
    assert registry.name_key("Energy & Utilities (2)") == "energy and utilities"
    assert (
        expand_shorthand("FY2026 FY25", 3) == "year ended March 31, 2026 year ended March 31, 2025"
    )
    assert expand_shorthand("Q1 FY27", 3) == "quarter ended June 30, 2026"


async def test_resource_limits_mark_partial(tmp_path):
    data, _b, _x = await business(
        tmp_path,
        {"/ar.pdf": ar(segment_table())},
        limits=BusinessLimits(max_segment_observations=5),
    )
    assert data.status == "PARTIAL" and len(data.segment_observations) == 5
    assert any(w.code == "RESOURCE_LIMIT_REACHED" for w in data.warnings)


async def test_box1b_profile_becomes_source_backed_evidence(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    data, _b, _x = await business(
        tmp_path / "f", {"/ar.pdf": ar(segment_table())}, screener=screener
    )
    about = next(
        e
        for e in data.business_evidence
        if e.source.source_kind == SourceKind.BOX1B_PROFILE
        and e.evidence_kind == BusinessEvidenceKind.BUSINESS_DESCRIPTION
    )
    assert about.raw_text == screener.profile.about
    assert about.source.artifact_sha256 == screener.source.content_sha256
    assert about.source.authority == SourceAuthority.SECONDARY_AGGREGATOR
    assert about.evidence_id in data.business_structure.description_evidence_ids


def test_box7_modules_stay_deterministic_and_offline():
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    modules = [
        root / "domain" / "business_intelligence.py",
        *sorted((root / "application").glob("business_*.py")),
    ]
    banned = {"httpx", "requests", "anthropic", "openai", "random", "socket", "urllib"}
    for path in modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                for name in names:
                    assert name.split(".")[0] not in banned, (path.name, name)
                    assert "adapters" not in name, (path.name, name)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("uuid4", "now"):
                pytest.fail(f"{path.name} uses non-deterministic {node.func.attr}")  # type: ignore[union-attr]


def test_no_box8_concepts_in_box7():
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    text = " ".join(
        p.read_text().lower()
        for p in [
            root / "domain" / "business_intelligence.py",
            *(root / "application").glob("business_*.py"),
        ]
    )
    for word in ("credibility", "promise", "guidance accuracy", "target price", "forecast("):
        assert word not in text, word


def test_business_evidence_contract_fields():
    fields = set(BusinessEvidence.model_fields)
    assert {
        "evidence_id",
        "company_key",
        "evidence_kind",
        "raw_text",
        "normalized_text",
        "section",
        "source",
        "confidence",
        "tags",
        "metadata",
    } <= fields
    source = set(SourceLocation.model_fields)
    assert {
        "document_id",
        "artifact_sha256",
        "page_start",
        "page_end",
        "block_ids",
        "table_id",
        "locator",
        "authority",
        "source_url",
    } <= source


async def test_bank_metric_row_matrix_definitions_and_sub_segment(tmp_path):
    """YES BANK style: segments named in 'X: Includes ...' bullets, one line per metric
    across segment columns, a single period stated in a sentence, a sub-segment table."""
    page = [
        "Notes to the Consolidated Financial Statements",
        "Segment Results",
        "- Treasury: Includes investments and financial markets activities.",
        "- Corporate / Wholesale Banking: Includes lending to corporate customers.",
        "- Retail Banking: Includes lending to retail customers.",
        "Segmental results for the year ended March 31, 2026 are set out below",
        "(Rs. in crore)",
        "Business Segments Treasury Corporate /",
        "Wholesale",
        "Banking",
        "Retail",
        "Banking# Total",
        "Segment Revenue 700 1,200 1,500 3,400",
        "Add/ (Less): Inter-segment 50",
        "Revenue net of inter- segment 3,450",
        "Result 250 300 (150) 400",
        "Unallocated Expenses (40)",
        "Operating Profit 360",
        "Income Taxes 90",
        "Segment assets 15,000 12,000 13,000 40,000",
        "Unallocated assets 900",
        "Total assets 40,900",
        "(Rs. in crore)",
        "Digital Banking (a sub-segment of Retail banking segment) For the year ended",
        "March 31, 2026",
        "Segment Revenue 6",
        "Result (2.50)",
    ]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(page)}, industry=Industry.BANK)
    assert seg(data, "corporate / wholesale banking").value == Decimal(1200)
    assert seg(data, "retail banking", SegmentMetric.SEGMENT_RESULT).value == Decimal(-150)
    checks = {(c.metric, c.reported_total_source): c for c in data.total_reconciliations}
    revenue = checks[("REVENUE", "TABLE_COMPANY_LINE")]
    assert revenue.outcome == TotalOutcome.ELIMINATIONS_EXPLAIN_DIFFERENCE
    assets = checks[("ASSETS", "TABLE_COMPANY_LINE")]
    assert assets.outcome == TotalOutcome.UNALLOCATED_EXPLAINS_DIFFERENCE
    digital = next(
        d for d in data.business_structure.segments if d.segment_key == "digital banking"
    )
    assert digital.parent_key == "retail banking" and digital.level == SegmentLevel.SUBSEGMENT
    assert all(c.outcome != TotalOutcome.CONFLICT for c in data.total_reconciliations)


async def test_infographic_value_label_order_is_not_guessed(tmp_path):
    page = [
        "Key highlights FY 2025-26",
        "35.1%",
        "CASA Ratio",
        "0.2%",
        "Net NPA",
        "15.3%",
        "Capital Adequacy Ratio",
    ]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(page)}, industry=Industry.BANK)
    assert not data.operating_kpi_observations  # never paired by position


async def test_range_and_stacked_headers_name_their_periods(tmp_path):
    page = [
        "Key business parameters (Standalone)",
        "(Rs. in crore)",
        "April 01, 2025 to",
        "March 31, 2026",
        "April 01, 2024 to",
        "March 31, 2025",
        "Change",
        "Deposits 3,190 2,845 345",
        "Advances 2,734 2,462 272",
    ]
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(page)}, industry=Industry.BANK)
    deposits = {v.period.period_end: v.value for v in data.canonical_kpi_values
                if v.canonical_kpi == "DEPOSITS"}  # fmt: skip
    assert deposits == {FY26: Decimal(3190), FY25: Decimal(2845)}
    extra = [u for u in data.unmapped_lines if u.reason == "EXTRA_COLUMN_WITHOUT_PERIOD"]
    assert {u.raw_values for u in extra} == {("345",), ("272",)}  # the change column is kept
