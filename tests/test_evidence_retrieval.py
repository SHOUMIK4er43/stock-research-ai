"""DETAILED_RESEARCH_V1 Box 2: evidence retrieval - offline only (MockTransport + fake DNS)."""

import asyncio
import hashlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from test_screener_evidence import catalogue_for
from test_summary_input import INFY

from stock_research_ai.adapters.documents.evidence_fetcher import (
    RawArtifactEvidenceWriter,
    SafeEvidenceFetcher,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.application.evidence_catalogue import assemble_catalogue
from stock_research_ai.application.evidence_retrieval import (
    canonical_manifest_json,
    detect_content_type,
    retrieve_evidence,
    select_documents,
)
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    DocumentRetrievalStatus,
    EvidenceCatalogue,
    EvidenceCompany,
    EvidenceDocumentType,
    RetrievalState,
    SourceDiscovery,
    SourceOutcome,
)
from stock_research_ai.domain.evidence_retrieval import (
    DetectedContentType,
    EvidenceRetrievalManifest,
    FetchResult,
    RetrievalOutcome,
    RetrievalPolicy,
)
from stock_research_ai.domain.financial import ReportingScope

T = EvidenceDocumentType
O = RetrievalOutcome  # noqa: E741
C = DetectedContentType
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
RUN = UUID("11111111-2222-4333-8444-555555555555")
HOST = "https://docs.example.com"
PUBLIC = ["93.184.216.34"]


def pdf(tag: str, size: int = 0) -> bytes:
    return b"%PDF-1.7\n" + tag.encode() + b"\n" + b"0" * size + b"\n%%EOF\n"


LOGIN_PAGE = b"<!DOCTYPE html><html><body>Please sign in</body></html>"
COPY = pdf("same bytes under two urls")
ROUTES: dict[str, object] = {
    "/ar.pdf": (200, pdf("annual report"), "application/pdf"),
    "/deck": (200, pdf("deck without extension"), "application/octet-stream"),
    "/fake.pdf": (200, LOGIN_PAGE, "text/html"),
    "/transcript.html": (200, b"<html><body>Transcript</body></html>", "text/html"),
    "/moved": (302, "/ar2.pdf"),
    "/ar2.pdf": (200, pdf("after redirect"), "application/pdf"),
    "/evil": (302, "https://internal.example/secret.pdf"),
    "/downgrade": (302, "http://docs.example.com/ar.pdf"),
    "/metadata": (302, "https://169.254.169.254/latest/"),
    "/loop1": (302, "/loop2"),
    "/loop2": (302, "/loop1"),
    "/big.pdf": (200, pdf("big", 5000), "application/pdf"),
    "/slow": "TIMEOUT",
    "/missing": (404, b"not found", "text/html"),
    "/forbidden": (403, b"denied", "text/html"),
    "/ratelimited": (429, b"slow down", "text/html"),
    "/server-error": (500, b"oops", "text/html"),
    "/garbage.pdf": (200, b"\x00\x01binary junk", "application/pdf"),
    "/copy-a.pdf": (200, COPY, "application/pdf"),
    "/copy-b.pdf": (200, COPY, "application/pdf"),
    "/sheet": (200, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "application/vnd.ms-excel"),
}


class Server:
    """A MockTransport handler acting as the test HTTP server; records every request."""

    def __init__(self, routes=ROUTES) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route == "TIMEOUT":
            raise httpx.ReadTimeout("slow", request=request)
        if route is None:
            return httpx.Response(404)
        if route[0] in (301, 302, 303, 307, 308):
            return httpx.Response(route[0], headers={"location": route[1]})
        status, body, kind = route
        return httpx.Response(status, content=body, headers={"content-type": kind})

    def calls(self, path: str) -> int:
        return sum(r.url.path == path for r in self.requests)


async def lookup(host: str) -> list[str]:
    return {"internal.example": ["10.0.0.5"], "ipv6-local.example": ["fe80::1"]}.get(host, PUBLIC)


def fetcher(server: Server) -> SafeEvidenceFetcher:
    return SafeEvidenceFetcher(transport=httpx.MockTransport(server), lookup=lookup)


async def fetch(server: Server, url: str, **kw) -> FetchResult:
    options = {"max_bytes": 10_000, "timeout_seconds": 5.0, "max_redirects": 5, **kw}
    return await fetcher(server).fetch(url, **options)


# --- Safe fetcher: URL, redirect and network safety -------------------------------------------


async def test_valid_pdf_and_redirect_chain_is_recorded():
    server = Server()
    direct = await fetch(server, f"{HOST}/ar.pdf")
    assert (direct.outcome, direct.http_status, direct.content) == ("OK", 200, pdf("annual report"))
    moved = await fetch(server, f"{HOST}/moved")
    assert moved.outcome == "OK" and moved.final_url == f"{HOST}/ar2.pdf"
    assert [(h.url, h.http_status) for h in moved.redirects] == [(f"{HOST}/moved", 302)]


@pytest.mark.parametrize(
    "url,code",
    [
        ("file:///etc/passwd", "UNSUPPORTED_SCHEME"),
        ("data:application/pdf;base64,JVBERi0=", "UNSUPPORTED_SCHEME"),
        ("javascript:alert(1)", "UNSUPPORTED_SCHEME"),
        ("http://docs.example.com/ar.pdf", "NOT_HTTPS"),
        ("https://localhost/ar.pdf", "PRIVATE_DESTINATION"),
        ("https://127.0.0.1/ar.pdf", "PRIVATE_DESTINATION"),
        ("https://[::1]/ar.pdf", "PRIVATE_DESTINATION"),
        ("https://10.1.2.3/ar.pdf", "PRIVATE_DESTINATION"),
        ("https://169.254.169.254/latest/", "PRIVATE_DESTINATION"),
        ("https://user:pass@docs.example.com/ar.pdf", "CREDENTIALS_IN_URL"),
        ("https://internal.example/ar.pdf", "PRIVATE_DESTINATION"),  # resolves privately
        ("https://ipv6-local.example/ar.pdf", "PRIVATE_DESTINATION"),  # link-local IPv6
    ],
)
async def test_unsafe_urls_are_never_requested(url, code):
    server = Server()
    result = await fetch(server, url)
    assert (result.outcome, result.code, result.content) == ("UNSUPPORTED", code, None)
    assert server.requests == []


@pytest.mark.parametrize(
    "path,outcome,code",
    [
        ("/evil", "BLOCKED", "PRIVATE_DESTINATION"),  # redirect into a private network
        ("/downgrade", "BLOCKED", "UNSAFE_REDIRECT"),  # https -> http
        ("/metadata", "BLOCKED", "UNSAFE_REDIRECT"),  # cloud metadata address
        ("/loop1", "FAILED", "REDIRECT_LOOP"),
    ],
)
async def test_every_redirect_is_revalidated(path, outcome, code):
    server = Server()
    result = await fetch(server, f"{HOST}{path}")
    assert (result.outcome, result.code, result.content) == (outcome, code, None)
    assert all(r.url.host == "docs.example.com" for r in server.requests)


async def test_redirect_count_is_bounded():
    routes = {f"/r{i}": (302, f"/r{i + 1}") for i in range(10)}
    result = await fetch(Server(routes), f"{HOST}/r0", max_redirects=3)
    assert (result.outcome, result.code, len(result.redirects)) == (
        "FAILED",
        "TOO_MANY_REDIRECTS",
        4,
    )


async def test_cookies_and_credentials_are_never_forwarded_between_hops():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://other.example.org/file.pdf", "set-cookie": "s=1"},
            )
        return httpx.Response(200, content=pdf("x"), headers={"content-type": "application/pdf"})

    result = await SafeEvidenceFetcher(httpx.MockTransport(handler), lookup).fetch(
        f"{HOST}/start", max_bytes=10_000, timeout_seconds=5, max_redirects=5
    )
    assert result.outcome == "OK"
    assert all("authorization" not in r.headers and "cookie" not in r.headers for r in seen)


@pytest.mark.parametrize(
    "path,outcome,code",
    [
        ("/missing", "NOT_FOUND", "HTTP_404"),
        ("/forbidden", "BLOCKED", "HTTP_403"),
        ("/ratelimited", "BLOCKED", "HTTP_429"),
        ("/server-error", "FAILED", "HTTP_500"),
        ("/slow", "TIMEOUT", "READ_TIMEOUT"),
        ("/big.pdf", "TOO_LARGE", "CONTENT_LENGTH_OVER_LIMIT"),
    ],
)
async def test_status_mapping(path, outcome, code):
    result = await fetch(Server(), f"{HOST}{path}", max_bytes=1000)
    assert (result.outcome, result.code, result.content) == (outcome, code, None)


async def test_declared_length_over_limit_is_refused_before_reading():
    def handler(request):
        return httpx.Response(
            200, content=pdf("x"), headers={"content-length": "999999999", "content-type": "x"}
        )

    result = await SafeEvidenceFetcher(httpx.MockTransport(handler), lookup).fetch(
        f"{HOST}/x.pdf", max_bytes=1000, timeout_seconds=5, max_redirects=5
    )
    assert (result.outcome, result.code) == ("TOO_LARGE", "CONTENT_LENGTH_OVER_LIMIT")


async def test_streamed_body_without_length_is_cut_off_at_the_limit():
    async def chunks():
        yield b"%PDF-" + b"0" * 600
        yield b"0" * 600

    def handler(request):
        return httpx.Response(200, content=chunks())  # streamed: no Content-Length

    result = await SafeEvidenceFetcher(httpx.MockTransport(handler), lookup).fetch(
        f"{HOST}/x.pdf", max_bytes=1000, timeout_seconds=5, max_redirects=5
    )
    assert (result.outcome, result.code, result.content) == ("TOO_LARGE", "BODY_OVER_LIMIT", None)


async def test_whole_request_deadline():
    async def slow_dns(host: str) -> list[str]:
        await asyncio.sleep(1)
        return PUBLIC

    result = await SafeEvidenceFetcher(httpx.MockTransport(Server()), slow_dns).fetch(
        f"{HOST}/ar.pdf", max_bytes=1000, timeout_seconds=0.05, max_redirects=5
    )
    assert (result.outcome, result.code) == ("TIMEOUT", "DEADLINE_EXCEEDED")


# --- Content detection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,declared,expected",
    [
        (pdf("x"), "application/octet-stream", C.PDF),  # signature beats header
        (pdf("x"), None, C.PDF),
        (LOGIN_PAGE, "application/pdf", C.HTML),  # a 'PDF' that is really a login page
        (b"\n  <html><body>x</body></html>", None, C.HTML),
        (b"plain transcript text", "text/plain; charset=utf-8", C.TEXT),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1....", "application/vnd.ms-excel", C.SPREADSHEET),
        (
            b"PK\x03\x04....",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            C.SPREADSHEET,
        ),
        (b"PK\x03\x04....", "application/zip", C.UNKNOWN),
        (b"\x00\x01junk", "application/pdf", C.UNKNOWN),
    ],
)
def test_detect_content_type(content, declared, expected):
    assert detect_content_type(content, declared) is expected


# --- Retrieval service --------------------------------------------------------------------------

COMPANY = EvidenceCompany(
    company_key="screener:9999",
    name="Example Ltd",
    source_company_key="9999",
    reporting_scope=ReportingScope.STANDALONE,
)


def candidate(path_or_url, kind, *, ineligible=False, period=None):
    url = path_or_url if path_or_url.startswith("http") else f"{HOST}{path_or_url}"
    return DocumentCandidate(
        document_type=kind,
        confidence=ClassificationConfidence.HIGH,
        title=f"{kind.value} {path_or_url}",
        url=url,
        is_direct_document=False,
        period_label=period,
        retrieval=RetrievalState(status=DocumentRetrievalStatus.INELIGIBLE, status_code="NOT_HTTPS")
        if ineligible
        else RetrievalState(),
        discovery=DocumentDiscovery(
            source_name="test",
            discovered_via_url="https://www.screener.in/company/EX/",
            locator=url,
            classified_as=kind,
            discovered_at=NOW,
        ),
    )


DOCS = [
    ("/ar.pdf", T.ANNUAL_REPORT),
    ("/deck", T.INVESTOR_PRESENTATION),
    ("/fake.pdf", T.QUARTERLY_RESULT),
    ("/transcript.html", T.CONCALL_TRANSCRIPT),
    ("/moved", T.ANNUAL_REPORT),
    ("/evil", T.CREDIT_RATING_REPORT),
    ("/loop1", T.AGM_NOTICE),
    ("/big.pdf", T.BRSR),
    ("/slow", T.SHAREHOLDER_LETTER),
    ("/missing", T.EXCHANGE_ANNOUNCEMENT),
    ("/forbidden", T.REGULATION_30_FILING),
    ("/ratelimited", T.CORPORATE_GOVERNANCE_REPORT),
    ("/garbage.pdf", T.INSIDER_TRADING_DISCLOSURE),
    ("/copy-a.pdf", T.AGM_MINUTES),
    ("/copy-b.pdf", T.OTHER_COMPANY_DOCUMENT),
    ("/sheet", T.SHAREHOLDING_PATTERN),
    ("https://youtu.be/abc", T.CONCALL_AUDIO),
]


def catalogue(items=DOCS, extra=()) -> EvidenceCatalogue:
    candidates = [candidate(p, k) for p, k in items] + list(extra)
    return assemble_catalogue(
        COMPANY,
        [SourceDiscovery(source_name="test", candidates=tuple(candidates))],
        [SourceOutcome(source_name="test", status="SUCCESS", candidates_found=len(candidates))],
        catalogued_at=NOW,
    )


async def run(server, root: Path, policy=None, cat=None) -> EvidenceRetrievalManifest:
    return await retrieve_evidence(
        cat or catalogue(),
        fetcher(server),
        RawArtifactEvidenceWriter(FileRawArtifactStore(root)),
        policy or RetrievalPolicy(max_file_bytes=2000, concurrency=1),
        run_id=RUN,
        clock=lambda: NOW,
    )


def by_path(manifest):
    return {a.requested_url.removeprefix(HOST): a for a in manifest.artifacts}


async def test_retrieval_outcomes_for_every_document_type(tmp_path):
    server = Server()
    manifest = await run(server, tmp_path / "store")
    records = by_path(manifest)
    expected = {
        "/ar.pdf": (O.RETRIEVED, None, C.PDF),
        "/deck": (O.RETRIEVED, None, C.PDF),  # a PDF behind an extension-less URL
        "/fake.pdf": (O.INVALID_CONTENT, "UNEXPECTED_HTML", C.HTML),
        "/transcript.html": (O.RETRIEVED, None, C.HTML),
        "/moved": (O.RETRIEVED, None, C.PDF),
        "/evil": (O.BLOCKED, "PRIVATE_DESTINATION", None),
        "/loop1": (O.FAILED, "REDIRECT_LOOP", None),
        "/big.pdf": (O.TOO_LARGE, "FILE_SIZE_LIMIT", None),
        "/slow": (O.TIMEOUT, "READ_TIMEOUT", None),
        "/missing": (O.NOT_FOUND, "HTTP_404", None),
        "/forbidden": (O.BLOCKED, "HTTP_403", None),
        "/ratelimited": (O.BLOCKED, "HTTP_429", None),
        "/garbage.pdf": (O.INVALID_CONTENT, "UNEXPECTED_UNKNOWN", C.UNKNOWN),
        "/copy-a.pdf": (O.RETRIEVED, None, C.PDF),
        "/copy-b.pdf": (O.RETRIEVED, None, C.PDF),
        "/sheet": (O.RETRIEVED, None, C.SPREADSHEET),
        "https://youtu.be/abc": (O.NOT_ATTEMPTED, "AUDIO_REFERENCE_ONLY", C.AUDIO_REFERENCE),
    }
    assert {p: (r.outcome, r.code, r.detected_content_type) for p, r in records.items()} == expected
    assert server.calls("/abc") == 0  # audio is referenced, never downloaded

    # Verified bytes are stored content-addressed; rejected bytes are hashed but never stored.
    ar = records["/ar.pdf"]
    assert ar.sha256 == hashlib.sha256(pdf("annual report")).hexdigest()
    assert ar.artifact is not None and ar.artifact.reference.endswith(f"{ar.sha256}.pdf")
    assert (tmp_path / "store" / ar.artifact.reference).read_bytes() == pdf("annual report")
    assert records["/sheet"].file_extension == ".xls"
    fake = records["/fake.pdf"]
    assert fake.artifact is None and fake.sha256 == hashlib.sha256(LOGIN_PAGE).hexdigest()
    stored = sorted(p.name for p in (tmp_path / "store").rglob("*") if p.is_file())
    unique = {r.sha256 for r in manifest.artifacts if r.outcome is O.RETRIEVED}
    assert len(stored) == len(unique) == 6  # copy-a/copy-b share one stored file

    # Identical bytes under two URLs: one canonical, one linked duplicate.
    assert records["/copy-b.pdf"].duplicate_of == records["/copy-a.pdf"].document_id
    assert records["/copy-a.pdf"].artifact == records["/copy-b.pdf"].artifact
    assert records["/moved"].final_url == f"{HOST}/ar2.pdf"
    assert len(records["/moved"].redirects) == 1

    assert (
        manifest.documents_discovered,
        manifest.documents_selected,
        manifest.documents_retrieved,
        manifest.documents_failed,
        manifest.documents_blocked,
        manifest.unique_content_hashes,
        manifest.duplicate_content_documents,
    ) == (17, 16, 7, 6, 3, 6, 1)
    assert manifest.total_bytes_retrieved == sum(
        r.size_bytes for r in manifest.artifacts if r.outcome is O.RETRIEVED
    )


async def test_one_failure_never_stops_the_run_and_nothing_is_retried(tmp_path):
    server = Server()
    manifest = await run(server, tmp_path / "s")
    assert manifest.documents_retrieved == 7
    assert server.calls("/slow") == 1 and server.calls("/missing") == 1


async def test_retries_only_when_configured_and_only_for_transient_failures(tmp_path):
    server = Server()
    await run(server, tmp_path / "s", RetrievalPolicy(max_attempts=2, max_file_bytes=2000))
    assert server.calls("/slow") == 2  # TIMEOUT retried once
    assert server.calls("/missing") == 1  # 404 never retried
    assert server.calls("/forbidden") == 1  # blocked never retried


async def test_manifest_is_deterministic_and_round_trips(tmp_path):
    first = await run(Server(), tmp_path / "a")
    second = await run(Server(), tmp_path / "b")
    encoded = canonical_manifest_json(first)
    assert encoded == canonical_manifest_json(second)
    assert EvidenceRetrievalManifest.model_validate_json(encoded) == first
    by_type = Counter({(c.document_type, c.outcome): c.count for c in first.outcomes_by_type})
    assert by_type[(T.ANNUAL_REPORT, O.RETRIEVED)] == 2
    assert by_type[(T.CONCALL_AUDIO, O.NOT_ATTEMPTED)] == 1


async def test_selection_order_tiers_and_limits(tmp_path):
    policy = RetrievalPolicy(
        max_tier=2,
        per_type_limit={T.ANNUAL_REPORT: 1},
        max_documents=5,
        concurrency=1,
        max_file_bytes=2000,
    )
    selected, skipped = select_documents(catalogue(), policy)
    assert [d.document_type for d in selected] == [
        T.ANNUAL_REPORT,
        T.QUARTERLY_RESULT,
        T.INVESTOR_PRESENTATION,
        T.CONCALL_TRANSCRIPT,
        T.CREDIT_RATING_REPORT,
    ]
    reasons = Counter(skipped.values())
    assert reasons["TYPE_LIMIT"] == 1  # the second annual report
    assert reasons["BELOW_POLICY_TIER"] == 2  # tier-3 announcement + other document
    assert reasons["AUDIO_REFERENCE_ONLY"] == 1
    assert reasons["DOCUMENT_LIMIT"] == 8
    manifest = await run(Server(), tmp_path / "s", policy)
    assert sorted(a.selection_rank for a in manifest.artifacts if a.selection_rank) == [
        1,
        2,
        3,
        4,
        5,
    ]


async def test_ineligible_catalogue_urls_are_not_fetched(tmp_path):
    server = Server()
    cat = catalogue(items=[], extra=[candidate(f"{HOST}/ar.pdf", T.ANNUAL_REPORT, ineligible=True)])
    manifest = await run(server, tmp_path / "s", cat=cat)
    (record,) = manifest.artifacts
    assert (record.outcome, record.code) == (O.NOT_ATTEMPTED, "INELIGIBLE_URL")
    assert server.requests == []


async def test_total_byte_budget(tmp_path):
    items = [(f"/doc{i}.pdf", T.ANNUAL_REPORT) for i in range(5)]
    routes = {f"/doc{i}.pdf": (200, pdf(f"d{i}", 400), "application/pdf") for i in range(5)}
    size = len(pdf("d0", 400))
    policy = RetrievalPolicy(max_file_bytes=1000, max_total_bytes=2 * size + 100, concurrency=1)
    manifest = await run(Server(routes), tmp_path / "s", policy, catalogue(items))
    outcomes = [(a.outcome, a.code) for a in manifest.artifacts]
    assert outcomes[:2] == [(O.RETRIEVED, None)] * 2
    assert outcomes[2] == (O.TOO_LARGE, "TOTAL_BYTE_BUDGET")  # only 100 bytes were left
    assert manifest.total_bytes_retrieved == 2 * size <= policy.max_total_bytes


async def test_budget_cap_holds_under_concurrency(tmp_path):
    items = [(f"/doc{i}.pdf", T.ANNUAL_REPORT) for i in range(8)]
    routes = {f"/doc{i}.pdf": (200, pdf(f"d{i}", 300), "application/pdf") for i in range(8)}
    policy = RetrievalPolicy(max_file_bytes=1000, max_total_bytes=1500, concurrency=4)
    manifest = await run(Server(routes), tmp_path / "s", policy, catalogue(items))
    assert manifest.total_bytes_retrieved <= policy.max_total_bytes
    assert manifest.documents_retrieved >= 1


async def test_concurrency_is_bounded(tmp_path):
    in_flight = 0
    peak = 0

    class SlowFetcher:
        async def fetch(self, url, *, max_bytes, timeout_seconds, max_redirects):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return FetchResult(outcome="OK", final_url=url, http_status=200, content=pdf(url))

    items = [(f"/doc{i}.pdf", T.ANNUAL_REPORT) for i in range(10)]
    await retrieve_evidence(
        catalogue(items),
        SlowFetcher(),
        RawArtifactEvidenceWriter(FileRawArtifactStore(tmp_path / "s")),
        RetrievalPolicy(concurrency=3),
        run_id=RUN,
        clock=lambda: NOW,
    )
    assert peak == 3


async def test_stored_artifacts_are_immutable(tmp_path):
    store = tmp_path / "s"
    manifest = await run(Server(), store)
    record = by_path(manifest)["/ar.pdf"]
    path = store / record.artifact.reference
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        await run(Server(), store)  # never overwritten with different bytes, never trusted


# --- Real Screener catalogue end to end (quarterly Raw PDF links) --------------------------------


async def test_screener_catalogue_retrieval_follows_quarterly_resolver_links(tmp_path, fixture_dir):
    cat = await catalogue_for(tmp_path / "cat", fixture_dir, INFY)
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        if request.url.path.startswith("/company/source/quarter/"):
            month, year = request.url.path.strip("/").split("/")[-2:]
            target = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/q-{year}-{month}.pdf"
            return httpx.Response(302, headers={"location": target})
        if request.url.host == "www.bseindia.com":
            return httpx.Response(
                200, content=pdf(request.url.path), headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)

    policy = RetrievalPolicy(
        max_tier=1,
        per_type_limit={T.QUARTERLY_RESULT: 2, T.ANNUAL_REPORT: 1},
        max_documents=3,
        concurrency=1,
    )
    manifest = await retrieve_evidence(
        cat,
        SafeEvidenceFetcher(httpx.MockTransport(handler), lookup),
        RawArtifactEvidenceWriter(FileRawArtifactStore(tmp_path / "store")),
        policy,
        run_id=RUN,
        clock=lambda: NOW,
    )
    selected = sorted(
        (a for a in manifest.artifacts if a.selection_rank), key=lambda a: a.selection_rank
    )
    assert [a.document_type for a in selected] == [
        T.ANNUAL_REPORT,
        T.QUARTERLY_RESULT,
        T.QUARTERLY_RESULT,
    ]
    quarterly = selected[1]
    assert quarterly.outcome is O.RETRIEVED
    assert quarterly.requested_url.startswith(
        "https://www.screener.in/company/source/quarter/1489/"
    )
    assert quarterly.final_url.startswith("https://www.bseindia.com/xml-data/corpfiling/AttachHis/")
    assert [h.http_status for h in quarterly.redirects] == [302]
    page = next(a for a in manifest.artifacts if a.document_type is T.SCREENER_PAGE)
    assert (page.outcome, page.code, page.selection_rank) == (
        O.RETRIEVED,
        "PRESERVED_BY_EXTRACTION",
        None,
    )
    assert manifest.documents_discovered == len(cat.documents)
    assert manifest.documents_selected == 3 and len(served) == 5  # 1 AR + 2x(redirect + PDF)
