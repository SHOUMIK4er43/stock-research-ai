"""DETAILED_RESEARCH_V1 Box 1: Screener page as an EvidenceSource (offline fixtures only)."""

from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_summary_input import INFY, YESBANK, replay

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.evidence import (
    ScreenerEvidenceSource,
    classify_title,
    explicit_fiscal_period,
)
from stock_research_ai.application.evidence_catalogue import (
    build_evidence_catalogue,
    canonical_catalogue_json,
)
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentRetrievalStatus,
    EvidenceCatalogue,
    EvidenceDocumentType,
    SourceAuthority,
)
from stock_research_ai.domain.financial import ReportingScope

T = EvidenceDocumentType
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
SYNTHETIC_URL = "https://www.screener.in/company/SYNEV/"
SYNTHETIC = """<!doctype html><html><head><title>Synthetic Evidence Ltd - Screener</title></head>
<body>
<div id="company-info" data-company-id="SYN-EVIDENCE" data-consolidated="false"></div>
<section id="top"><h1>Synthetic Evidence Ltd</h1>
<a href="https://www.nseindia.com/get-quotes/equity?symbol=SYNEV">NSE: SYNEV</a>
<a href="http://www.synthetic-evidence.example">Website</a></section>
<section id="documents">
<h3>Announcements</h3><a href="https://www.bseindia.com/x/corp-announcements/">All</a>
<ul>
<li><a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=aaaa-1111.pdf">
Notice of 12th Annual General Meeting<div>AGM on 14 Aug</div>
<time datetime="2026-07-01T10:00:00+05:30">1 Jul</time></a></li>
<li><a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=bbbb-2222.pdf">
Outcome of Board Meeting - Financial Results Q1 FY27
<time datetime="2026-07-20T18:00:00+05:30">20 Jul</time></a></li>
<li><a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=eeee-5555.pdf">
Intimation of ESG Rating<time datetime="2026-06-01T09:00:00">1 Jun</time></a></li>
<li><a href="javascript:alert(1)">Bad link</a></li>
<li><a href="http://127.0.0.1/secret.pdf">Private network</a></li>
<li><a href="https://www.screener.in/login/">Login to view</a></li>
</ul>
<h3>Annual reports</h3><ul>
<li><a href="https://www.bseindia.com/xml-data/corpfiling/AttachHis/cccc-3333.pdf">
Annual Report 2026<div>from bse</div></a></li>
<li><a href="http://www.synthetic-evidence.example/ar2025.pdf">Annual Report 2025
<div>from company</div></a></li>
</ul>
<h3>Concalls</h3><ul>
<li><div class="nowrap">Jul 2026</div>
<a href="https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=dddd-4444.pdf">Transcript</a>
<button>AI Summary</button>
<a href="https://ir.synthetic-evidence.example/q1fy27.pdf">PPT</a>
<a href="https://youtu.be/xyz">REC</a>
<a href="https://ir.synthetic-evidence.example/notes.pdf">Notes</a></li>
<li><div class="nowrap">Apr 2026</div><div>Transcript</div>
<a href="https://ir.synthetic-evidence.example/q4fy26.pdf">PPT</a></li>
</ul>
<h3>Credit ratings</h3><ul>
<li><a href="https://www.icra.in/Rationale/ShowRationaleReport/?Id=1">Rating update
<div>14 Aug from icra</div></a></li>
<li><a href="https://www.crisil.com/r/Synthetic_June 26_ 2025.html">Rating update
<div>26 Jun 2025 from crisil</div></a></li>
</ul>
<h3>Other filings</h3><ul>
<li><a href="https://www.bseindia.com/xml-data/corpfiling/AttachLive/dddd-4444.pdf">
Earnings call transcript (exchange copy)</a></li>
<li><a href="https://www.synthetic-evidence.example/misc.pdf">Miscellaneous update</a></li>
</ul>
</section></body></html>"""


async def source_for(tmp_path: Path, fixture_dir: Path, case, html: str | None = None):
    extraction = await replay(tmp_path, fixture_dir, case, html)
    content = await FileRawArtifactStore(tmp_path / "evidence").read(extraction.raw_artifacts[0])
    return extraction, ScreenerEvidenceSource(extraction, content)


async def catalogue_for(tmp_path, fixture_dir, case, html=None) -> EvidenceCatalogue:
    _extraction, source = await source_for(tmp_path, fixture_dir, case, html)
    return await build_evidence_catalogue(source.company(), [source], catalogued_at=NOW)


# --- Deterministic title classification ------------------------------------------------


@pytest.mark.parametrize(
    "title,kind",
    [
        ("Annual Report 2024", T.ANNUAL_REPORT),
        (
            "Outcome of Board Meeting - Financial Results for quarter ended June 30",
            T.QUARTERLY_RESULT,
        ),
        ("Investor Presentation Q1 FY27", T.INVESTOR_PRESENTATION),
        ("Transcript of Earnings Conference Call", T.CONCALL_TRANSCRIPT),
        ("Audio Recording of Earnings Call", T.CONCALL_AUDIO),
        ("Intimation of Credit Rating", T.CREDIT_RATING_REPORT),
        ("Notice of 45th Annual General Meeting", T.AGM_NOTICE),
        ("Transcript of the AGM held on 24 June", T.AGM_TRANSCRIPT),
        ("Proceedings of the Annual General Meeting", T.AGM_MINUTES),
        ("Shareholding Pattern for quarter ended June", T.SHAREHOLDING_PATTERN),
        ("Disclosure under SEBI (PIT) Regulations", T.INSIDER_TRADING_DISCLOSURE),
        ("Business Responsibility and Sustainability Report", T.BRSR),
        ("Report on Corporate Governance", T.CORPORATE_GOVERNANCE_REPORT),
        ("Letter to Shareholders", T.SHAREHOLDER_LETTER),
        ("Announcement under Regulation 30 (LODR)-Newspaper Publication", T.REGULATION_30_FILING),
        # Explicit keywords only: these must NOT be guessed.
        ("Intimation of ESG Rating", None),
        ("Change in Directors", None),
        ("Diagram of management structure", None),  # contains 'agm' only inside a word
        ("Results", None),
    ],
)
def test_classify_title(title, kind):
    assert classify_title(title) is kind


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Investor Presentation Q1 FY27", (2027, 1)),
        ("Q3 FY'26 Earnings", (2026, 3)),
        ("Annual Report FY2026", (2026, None)),
        ("Annual Report 2026", (None, None)),  # a bare year is not a fiscal year
        ("Q2 results", (None, None)),
    ],
)
def test_explicit_fiscal_period(title, expected):
    assert explicit_fiscal_period(title) == expected


# --- Synthetic page: every rule and safety path --------------------------------------------


async def test_synthetic_page_catalogue(tmp_path, fixture_dir):
    result = await catalogue_for(tmp_path, fixture_dir, (INFY[0], SYNTHETIC_URL), SYNTHETIC)
    assert result.company.company_key == "screener:SYN-EVIDENCE"
    assert result.company.website_url == "http://www.synthetic-evidence.example"
    by_label = {d.discovered_from[0].source_label: d for d in result.documents}

    agm = by_label["Notice of 12th Annual General Meeting"]
    assert agm.document_type is T.AGM_NOTICE
    assert agm.published_date == date(2026, 7, 1)
    assert agm.source_authority is SourceAuthority.PRIMARY_REGULATORY

    results = by_label["Outcome of Board Meeting - Financial Results Q1 FY27"]
    assert results.document_type is T.QUARTERLY_RESULT
    assert (results.fiscal_year, results.fiscal_quarter) == (2027, 1)

    esg = by_label["Intimation of ESG Rating"]
    assert esg.document_type is T.EXCHANGE_ANNOUNCEMENT  # never guessed as a credit rating
    assert esg.published_date is None  # timezone-less source timestamp is not interpreted
    assert esg.published_date_text == "2026-06-01T09:00:00"

    annual = [d for d in result.documents if d.document_type is T.ANNUAL_REPORT]
    assert sorted(d.period_label for d in annual) == ["2025", "2026"]
    assert all(d.published_date is None and d.fiscal_year is None for d in annual)
    company_copy = next(d for d in annual if d.period_label == "2025")
    assert company_copy.source_authority is SourceAuthority.PRIMARY_COMPANY
    assert company_copy.retrieval.status is DocumentRetrievalStatus.INELIGIBLE
    assert company_copy.retrieval.status_code == "NOT_HTTPS"

    transcript = next(d for d in result.documents if d.document_type is T.CONCALL_TRANSCRIPT)
    assert transcript.period_label == "Jul 2026"
    assert transcript.confidence is ClassificationConfidence.HIGH
    # The concall link and the exchange copy are the same BSE attachment: one document.
    assert len(transcript.discovered_from) == 2
    assert transcript.source_url == (
        "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=dddd-4444.pdf"
    )
    assert transcript.alternate_urls == (
        "https://www.bseindia.com/xml-data/corpfiling/AttachLive/dddd-4444.pdf",
    )

    kinds = {(d.document_type, d.period_label) for d in result.documents}
    assert (T.INVESTOR_PRESENTATION, "Jul 2026") in kinds
    assert (T.INVESTOR_PRESENTATION, "Apr 2026") in kinds  # row without a transcript link
    assert (T.CONCALL_AUDIO, "Jul 2026") in kinds
    notes = next(d for d in result.documents if d.metadata.get("link_kind") == "Notes")
    assert notes.document_type is T.OTHER_COMPANY_DOCUMENT
    assert notes.confidence is ClassificationConfidence.LOW
    misc = by_label["Miscellaneous update"]
    assert misc.document_type is T.OTHER_COMPANY_DOCUMENT

    ratings = sorted(
        (d for d in result.documents if d.document_type is T.CREDIT_RATING_REPORT),
        key=lambda d: d.metadata["rating_agency_label"],
    )
    assert [d.metadata["rating_agency_label"] for d in ratings] == ["crisil", "icra"]
    assert ratings[0].published_date == date(2025, 6, 26)
    assert ratings[1].published_date is None  # '14 Aug' has no year: never assumed
    assert ratings[1].published_date_text == "14 Aug"
    assert all(d.source_authority is SourceAuthority.THIRD_PARTY for d in ratings)

    assert sorted((i.code, i.url) for i in result.issues) == [
        ("AGGREGATOR_LINK_SKIPPED", None),
        ("INDEX_LINK_SKIPPED", "https://www.bseindia.com/x/corp-announcements/"),
        ("UNSAFE_URL", None),
        ("UNSAFE_URL", None),
    ]
    urls = {d.source_url for d in result.documents} | {
        u for d in result.documents for u in d.alternate_urls
    }
    assert not any("127.0.0.1" in u or u.startswith("javascript") for u in urls)
    assert all(d.document_type is not T.SCREENER_EXPORT for d in result.documents)


async def test_screener_page_itself_is_retrieved_evidence(tmp_path, fixture_dir):
    extraction, source = await source_for(tmp_path, fixture_dir, INFY)
    result = await build_evidence_catalogue(source.company(), [source], catalogued_at=NOW)
    (page,) = [d for d in result.documents if d.document_type is T.SCREENER_PAGE]
    artifact = extraction.raw_artifacts[0]
    assert page.retrieval.status is DocumentRetrievalStatus.RETRIEVED
    assert page.retrieval.content_hash == artifact.content_sha256
    assert page.retrieval.file_size_bytes == artifact.size_bytes
    assert page.retrieval.local_artifact_reference == artifact.content_reference
    assert page.source_authority is SourceAuthority.SECONDARY_AGGREGATOR
    assert page.reporting_basis is ReportingScope.STANDALONE
    assert page.discovered_from[0].raw_artifact_id == artifact.id


async def test_content_must_match_the_preserved_artifact(tmp_path, fixture_dir):
    extraction = await replay(tmp_path, fixture_dir, INFY)
    with pytest.raises(ValueError, match="does not match"):
        ScreenerEvidenceSource(extraction, b"<html>tampered</html>")


async def test_page_without_documents_section_is_reported(tmp_path, fixture_dir):
    html = (fixture_dir / INFY[0]).read_text()
    start = html.index('<section id="documents"')
    end = html.index("</section>", start) + len("</section>")
    result = await catalogue_for(tmp_path, fixture_dir, INFY, html[:start] + html[end:])
    assert [d.document_type for d in result.documents] == [T.SCREENER_PAGE]
    assert [i.code for i in result.issues] == ["DOCUMENTS_SECTION_MISSING"]


# --- Real fixtures: a useful, deterministic catalogue -------------------------------------


@pytest.mark.parametrize(
    "case,name,expected",
    [
        (
            INFY,
            "Infosys Ltd",
            {
                T.SCREENER_PAGE: 1,
                T.ANNUAL_REPORT: 15,
                T.CONCALL_TRANSCRIPT: 37,
                T.INVESTOR_PRESENTATION: 8,
                T.CONCALL_AUDIO: 4,
                T.CREDIT_RATING_REPORT: 6,
                T.REGULATION_30_FILING: 2,
                T.EXCHANGE_ANNOUNCEMENT: 3,
            },
        ),
        (
            YESBANK,
            "Yes Bank Ltd",
            {
                T.SCREENER_PAGE: 1,
                T.ANNUAL_REPORT: 15,
                T.CONCALL_TRANSCRIPT: 17,
                T.INVESTOR_PRESENTATION: 35,
                T.CONCALL_AUDIO: 7,
                T.CREDIT_RATING_REPORT: 6,
                T.REGULATION_30_FILING: 5,
            },
        ),
    ],
)
async def test_fixture_catalogue(tmp_path, fixture_dir, case, name, expected):
    result = await catalogue_for(tmp_path, fixture_dir, case)
    assert result.company.name == name
    assert Counter(d.document_type for d in result.documents) == expected
    assert [i.code for i in result.issues] == ["INDEX_LINK_SKIPPED"]
    assert result.sources[0].status == "SUCCESS"

    for document in result.documents:
        assert document.company_key == result.company.company_key
        # Every sighting comes from the one page fetch it was read from.
        assert document.discovered_at == result.documents[0].discovered_at
        assert all(d.source_name == "screener" for d in document.discovered_from)
        assert document.fiscal_year is None  # no explicit FY token on these labels
        if document.source_domain.endswith("bseindia.com"):
            assert document.source_authority is SourceAuthority.PRIMARY_REGULATORY

    transcripts = [d for d in result.documents if d.document_type is T.CONCALL_TRANSCRIPT]
    assert all(d.period_label and d.period_label[-4:].isdigit() for d in transcripts)
    annual = [d for d in result.documents if d.document_type is T.ANNUAL_REPORT]
    assert all(d.published_date is None for d in annual)
    ratings = [d for d in result.documents if d.document_type is T.CREDIT_RATING_REPORT]
    assert all(
        (d.published_date is None) == (not d.published_date_text[-4:].isdigit()) for d in ratings
    )
    dated = [d.published_date for d in result.documents if d.published_date]
    assert dated == sorted(dated, reverse=True)
    assert all(d.published_date is None for d in result.documents[len(dated) :])


async def test_fixture_catalogue_is_deterministic(tmp_path, fixture_dir):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = await catalogue_for(tmp_path / "a", fixture_dir, YESBANK)
    second = await catalogue_for(tmp_path / "b", fixture_dir, YESBANK)
    # Separate extractions differ only in retrieval time/artifact; identities are stable.
    assert [d.document_id for d in first.documents] == [d.document_id for d in second.documents]
    assert [d.document_type for d in first.documents] == [d.document_type for d in second.documents]
    encoded = canonical_catalogue_json(first)
    assert EvidenceCatalogue.model_validate_json(encoded) == first
    assert canonical_catalogue_json(EvidenceCatalogue.model_validate_json(encoded)) == encoded
