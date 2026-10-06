"""DETAILED_RESEARCH_V1 Box 1: evidence catalogue contracts, identity, dedupe, ordering."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from stock_research_ai.application.evidence_catalogue import (
    assemble_catalogue,
    build_evidence_catalogue,
    canonical_catalogue_json,
    canonical_url,
    document_id,
    identity_key,
    source_authority,
)
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    DocumentRetrievalStatus,
    EvidenceCatalogue,
    EvidenceCompany,
    EvidenceDocumentType,
    EvidenceSourceError,
    RetrievalState,
    SourceAuthority,
    SourceDiscovery,
    SourceOutcome,
)
from stock_research_ai.domain.financial import ReportingScope

T = EvidenceDocumentType
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
COMPANY = EvidenceCompany(
    company_key="screener:9999",
    name="Example Industries Ltd",
    source_company_key="9999",
    reporting_scope=ReportingScope.CONSOLIDATED,
    website_url="http://www.example-industries.com",
)
BSE_A = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/0a1b2c3d-1111.pdf"
BSE_A_VIEWER = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=0a1b2c3d-1111.pdf"


def candidate(
    url: str,
    kind: EvidenceDocumentType = T.ANNUAL_REPORT,
    *,
    title: str = "Annual Report 2026",
    confidence: ClassificationConfidence = ClassificationConfidence.HIGH,
    source: str = "test",
    locator: str = "#documents/a[1]",
    published: date | None = None,
    period: str | None = None,
    retrieval: RetrievalState | None = None,
) -> DocumentCandidate:
    return DocumentCandidate(
        document_type=kind,
        confidence=confidence,
        title=title,
        url=url,
        is_direct_document=url.endswith(".pdf"),
        published_date=published,
        period_label=period,
        retrieval=retrieval or RetrievalState(),
        discovery=DocumentDiscovery(
            source_name=source,
            discovered_via_url="https://www.screener.in/company/EXAMPLE/consolidated/",
            locator=locator,
            source_label=title,
            classified_as=kind,
            discovered_at=NOW,
        ),
    )


def catalogue(*candidates: DocumentCandidate, source: str = "test") -> EvidenceCatalogue:
    return assemble_catalogue(
        COMPANY,
        [SourceDiscovery(source_name=source, candidates=candidates)],
        [SourceOutcome(source_name=source, status="SUCCESS", candidates_found=len(candidates))],
        catalogued_at=NOW,
    )


# --- URL normalization and identity -------------------------------------------------


def test_canonical_url_normalizes_without_changing_meaning():
    assert (
        canonical_url("HTTPS://WWW.Example.COM:443/Docs/Annual Report.pdf?b=2&utm_source=x&a=1#p=4")
        == "https://www.example.com/Docs/Annual%20Report.pdf?a=1&b=2"
    )
    assert canonical_url("https://example.com") == "https://example.com/"
    assert canonical_url("https://example.com/a%20b.pdf") == canonical_url(
        "https://example.com/a b.pdf"
    )
    # Path case and non-default ports are significant and preserved.
    assert canonical_url("https://example.com/A.pdf") != canonical_url("https://example.com/a.pdf")
    assert canonical_url("https://example.com:8443/a").startswith("https://example.com:8443/")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "ftp://example.com/a.pdf",
        "/relative/a.pdf",
        "https://user:pass@example.com/a.pdf",
    ],
)
def test_unsupported_urls_are_rejected(url):
    with pytest.raises(ValueError):
        canonical_url(url)


def test_bse_viewer_and_attachment_urls_share_one_identity():
    assert identity_key(BSE_A) == identity_key(BSE_A_VIEWER) == "bse-attachment:0a1b2c3d-1111.pdf"
    live = BSE_A.replace("AttachHis", "AttachLive")
    assert identity_key(live) == identity_key(BSE_A)
    other = BSE_A.replace("1111", "2222")
    assert identity_key(other) != identity_key(BSE_A)
    assert identity_key("https://www.example.com/a.pdf") == "url:https://www.example.com/a.pdf"


def test_document_id_is_deterministic_and_company_scoped():
    key = identity_key(BSE_A)
    assert document_id(COMPANY, key) == document_id(COMPANY, key)
    other = COMPANY.model_copy(update={"company_key": "screener:1111"})
    assert document_id(other, key) != document_id(COMPANY, key)


def test_title_changes_do_not_change_identity():
    first = catalogue(candidate(BSE_A, title="Annual Report 2026"))
    second = catalogue(candidate(BSE_A, title="Annual Report FY2026 (revised title)"))
    assert first.documents[0].document_id == second.documents[0].document_id


# --- Source authority ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,authority",
    [
        (BSE_A, SourceAuthority.PRIMARY_REGULATORY),
        ("https://www.nseindia.com/a.pdf", SourceAuthority.PRIMARY_REGULATORY),
        ("https://example-industries.com/ar.pdf", SourceAuthority.PRIMARY_COMPANY),
        ("https://ir.example-industries.com/ar.pdf", SourceAuthority.PRIMARY_COMPANY),
        ("https://www.screener.in/company/X/", SourceAuthority.SECONDARY_AGGREGATOR),
        ("https://www.crisil.com/r.html", SourceAuthority.THIRD_PARTY),
        ("https://www.icra.in/Rationale/?Id=1", SourceAuthority.THIRD_PARTY),
        # Look-alikes are never the company.
        ("https://example-industries.com.evil.test/ar.pdf", SourceAuthority.UNVERIFIED),
        ("https://notexample-industries.com/ar.pdf", SourceAuthority.UNVERIFIED),
        ("https://youtu.be/abc", SourceAuthority.UNVERIFIED),
    ],
)
def test_source_authority(url, authority):
    assert source_authority(url, COMPANY) is authority


def test_authority_is_metadata_never_a_filter():
    result = catalogue(candidate("https://unknown-host.test/x.pdf", T.OTHER_COMPANY_DOCUMENT))
    assert len(result.documents) == 1
    assert result.documents[0].source_authority is SourceAuthority.UNVERIFIED


# --- Deduplication -----------------------------------------------------------------------


def test_same_url_in_different_forms_is_one_document_with_all_sightings():
    result = catalogue(
        candidate("https://www.example.com/ar.pdf", locator="a[1]"),
        candidate("https://WWW.EXAMPLE.com/ar.pdf#page=3", locator="a[2]"),
        candidate("https://www.example.com/ar.pdf?utm_campaign=x", locator="a[3]"),
    )
    (document,) = result.documents
    assert [d.locator for d in document.discovered_from] == ["a[1]", "a[2]", "a[3]"]
    assert document.alternate_urls == ()


def test_bse_cross_path_duplicate_merges_and_keeps_alternate_url():
    result = catalogue(candidate(BSE_A, locator="a[1]"), candidate(BSE_A_VIEWER, locator="a[2]"))
    (document,) = result.documents
    assert document.source_url == BSE_A
    assert document.alternate_urls == (BSE_A_VIEWER,)
    assert document.download_url == BSE_A


def test_similar_titles_at_different_urls_are_never_merged():
    result = catalogue(
        candidate("https://www.example.com/ar-2026.pdf", title="Annual Report 2026"),
        candidate("https://www.example.com/ar-2026-v2.pdf", title="Annual Report 2026"),
        candidate(BSE_A, title="Annual Report 2026"),
    )
    assert len(result.documents) == 3
    assert len({d.document_id for d in result.documents}) == 3


def test_content_type_beats_channel_type_when_one_document_is_seen_twice():
    result = catalogue(
        candidate(
            BSE_A_VIEWER,
            T.REGULATION_30_FILING,
            title="Regulation 30 - Transcript of earnings call",
            confidence=ClassificationConfidence.MEDIUM,
            locator="a[1]",
        ),
        candidate(
            BSE_A, T.CONCALL_TRANSCRIPT, title="Concall Transcript - Jul 2026", locator="a[2]"
        ),
    )
    (document,) = result.documents
    assert document.document_type is T.CONCALL_TRANSCRIPT
    assert document.metadata["observed_types"] == "CONCALL_TRANSCRIPT,REGULATION_30_FILING"
    assert {d.classified_as for d in document.discovered_from} == {
        T.CONCALL_TRANSCRIPT,
        T.REGULATION_30_FILING,
    }


def test_conflicting_dates_stay_missing_and_are_reported():
    result = catalogue(
        candidate(BSE_A, published=date(2026, 7, 1), locator="a[1]"),
        candidate(BSE_A_VIEWER, published=date(2026, 7, 2), locator="a[2]"),
    )
    assert result.documents[0].published_date is None
    assert [i.code for i in result.issues] == ["CONFLICTING_PUBLISHED_DATE"]


def test_one_known_date_is_kept_when_other_sightings_lack_it():
    result = catalogue(
        candidate(BSE_A, published=date(2026, 7, 1), locator="a[1]"),
        candidate(BSE_A_VIEWER, locator="a[2]"),
    )
    assert result.documents[0].published_date == date(2026, 7, 1)
    assert result.issues == ()


def retrieved(content_hash: str) -> RetrievalState:
    return RetrievalState(
        status=DocumentRetrievalStatus.RETRIEVED,
        retrieved_at=NOW,
        content_hash=content_hash,
        file_size_bytes=10,
        mime_type="application/pdf",
        local_artifact_reference="artifacts/x",
    )


def test_identical_content_at_different_urls_is_linked_not_merged():
    company_copy = candidate(
        "https://www.example-industries.com/ar.pdf", retrieval=retrieved("a" * 64), locator="a[1]"
    )
    filing = candidate(BSE_A, retrieval=retrieved("a" * 64), locator="a[2]")
    unrelated = candidate("https://www.example.com/x.pdf", retrieval=retrieved("b" * 64))
    result = catalogue(company_copy, filing, unrelated)
    assert len(result.documents) == 3
    by_url = {d.source_url: d for d in result.documents}
    head = by_url[BSE_A]  # the regulatory filing outranks the company's copy
    assert head.duplicate_of is None
    assert by_url["https://www.example-industries.com/ar.pdf"].duplicate_of == head.document_id
    assert by_url["https://www.example.com/x.pdf"].duplicate_of is None
    # Linking never changes identity.
    assert head.document_id == catalogue(filing).documents[0].document_id


# --- Missing data, ordering, serialization ------------------------------------------------


def test_missing_date_and_period_stay_missing():
    (document,) = catalogue(candidate("https://www.example.com/x.pdf")).documents
    assert document.published_date is None
    assert document.period_label is None
    assert document.fiscal_year is None and document.fiscal_quarter is None


def test_ordering_is_newest_first_then_authority_then_type_then_id():
    docs = catalogue(
        candidate("https://www.example.com/old.pdf", published=date(2024, 1, 1)),
        candidate("https://www.example.com/undated.pdf"),
        candidate(BSE_A, T.QUARTERLY_RESULT, published=date(2026, 7, 1)),
        candidate("https://www.example.com/new.pdf", T.ANNUAL_REPORT, published=date(2026, 7, 1)),
        candidate("https://www.example.com/new2.pdf", T.ANNUAL_REPORT, published=date(2026, 7, 1)),
    ).documents
    assert [d.source_url.rsplit("/", 1)[-1] for d in docs[:1]] == ["0a1b2c3d-1111.pdf"]
    same_day = docs[1:3]
    assert all(d.published_date == date(2026, 7, 1) for d in same_day)
    assert [str(d.document_id) for d in same_day] == sorted(str(d.document_id) for d in same_day)
    assert docs[3].published_date == date(2024, 1, 1)
    assert docs[4].published_date is None


def test_ordering_and_json_are_independent_of_input_order():
    items = [
        candidate("https://www.example.com/a.pdf", published=date(2025, 1, 1), locator="1"),
        candidate("https://www.example.com/b.pdf", T.QUARTERLY_RESULT, locator="2"),
        candidate(BSE_A, published=date(2026, 1, 1), locator="3"),
    ]
    forward = canonical_catalogue_json(catalogue(*items))
    backward = canonical_catalogue_json(catalogue(*reversed(items)))
    assert forward == backward


def test_canonical_json_round_trip():
    result = catalogue(
        candidate(BSE_A, published=date(2026, 7, 1), period="2026"),
        candidate("https://www.example.com/x.pdf", retrieval=retrieved("c" * 64)),
    )
    encoded = canonical_catalogue_json(result)
    restored = EvidenceCatalogue.model_validate_json(encoded)
    assert restored == result
    assert canonical_catalogue_json(restored) == encoded
    assert encoded.startswith('{"catalogued_at":')  # keys sorted


# --- Contract strictness -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://example.com/a.pdf", "https://u:p@example.com/a.pdf"]
)
def test_candidates_reject_unsafe_urls(url):
    with pytest.raises(ValidationError):
        candidate(url)


def test_retrieval_state_must_be_consistent():
    with pytest.raises(ValidationError):
        RetrievalState(status=DocumentRetrievalStatus.RETRIEVED)
    with pytest.raises(ValidationError):
        RetrievalState(content_hash="a" * 64)
    with pytest.raises(ValidationError):
        RetrievalState(status=DocumentRetrievalStatus.INELIGIBLE, file_size_bytes=1)


def test_fiscal_quarter_requires_explicit_year():
    base = candidate("https://www.example.com/a.pdf").model_dump()
    with pytest.raises(ValidationError):
        DocumentCandidate.model_validate({**base, "fiscal_quarter": 2})
    assert (
        DocumentCandidate.model_validate(
            {**base, "fiscal_year": 2027, "fiscal_quarter": 2}
        ).fiscal_quarter
        == 2
    )


def test_catalogue_rejects_foreign_documents():
    result = catalogue(candidate(BSE_A))
    foreign = result.documents[0].model_copy(update={"company_key": "screener:1"})
    with pytest.raises(ValidationError):
        EvidenceCatalogue.model_validate(
            {**result.model_dump(), "documents": [foreign.model_dump()]}
        )


# --- Multi-source orchestration ----------------------------------------------------------------


class StaticSource:
    def __init__(self, name: str, *candidates: DocumentCandidate) -> None:
        self.name = name
        self.candidates = candidates
        self.calls = 0

    async def discover(self, company: EvidenceCompany) -> SourceDiscovery:
        self.calls += 1
        return SourceDiscovery(source_name=self.name, candidates=self.candidates)


class FailingSource:
    name = "broken"

    async def discover(self, company: EvidenceCompany) -> SourceDiscovery:
        raise EvidenceSourceError("SOURCE_UNREACHABLE")


async def test_sources_combine_into_one_catalogue_and_failures_are_recorded():
    screener = StaticSource("screener", candidate(BSE_A_VIEWER, source="screener"))
    exchange = StaticSource("bse", candidate(BSE_A, source="bse"))
    result = await build_evidence_catalogue(
        COMPANY, [screener, FailingSource(), exchange], catalogued_at=NOW
    )
    assert screener.calls == exchange.calls == 1
    (document,) = result.documents
    assert {d.source_name for d in document.discovered_from} == {"bse", "screener"}
    assert [(o.source_name, o.status, o.failure_code) for o in result.sources] == [
        ("broken", "FAILED", "SOURCE_UNREACHABLE"),
        ("bse", "SUCCESS", None),
        ("screener", "SUCCESS", None),
    ]


def test_catalogue_scales_to_hundreds_of_documents():
    items = [
        candidate(
            f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{i:08x}-0000.pdf",
            T.EXCHANGE_ANNOUNCEMENT,
            locator=str(i),
        )
        for i in range(600)
    ]
    result = catalogue(*items)
    assert len(result.documents) == 600
    assert isinstance(result.documents[0].document_id, UUID)
