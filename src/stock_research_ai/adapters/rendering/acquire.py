"""Box 15 acquisition-stage capture: one company -> one ResearchStage JSON.

This is the ONLY networked step of Detailed Research, and it adds no research logic: it
chains the existing, already-audited adapters exactly as the RVNL live validation did —

    Screener consolidated + standalone pages (Box 1, extract_snapshot)
      -> evidence catalogue (Box 1) -> bounded retrieval (Box 2: 1 annual report,
         <= 4 concall transcripts, <= 2 rating reports) -> Box 3 extraction
      -> optional Box 12 exchange acquisition (NSE / BSE / Kite; blocked sources recorded,
         never bypassed)

and captures the outputs. Boxes 4-14 and every renderer then run offline from the stage
(`stock-research-ai detailed-research --stage ...`). Cited analyst inputs for Box 13
(contingent liabilities, auditor reports, ratings ...) are never invented: they come only
from an explicit DueDiligenceRequest file, else Box 13 runs with none and reports the gaps.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid5

from stock_research_ai.adapters.rendering.cli import build_research_stage
from stock_research_ai.adapters.screener.peers_api import PeerFetch
from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.adapters.screener.source import validate_url
from stock_research_ai.application.final_report_jobs import FinalReportGenerationError
from stock_research_ai.application.report_claims import NS
from stock_research_ai.application.report_orchestrator import (
    MAX_PEER_PAGES,
    ResearchStage,
    canonical_identity,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.market_intelligence import MarketIntelligenceRequest, RawPayload
from stock_research_ai.domain.report_rendering import FailureCode, screener_page_key
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset, PeerStatus

Page = tuple[CompanyExtractionResult, bytes]
FetchPage = Callable[[str], Awaitable[Page]]
FetchEvidence = Callable[[Page], Awaitable[tuple[EvidenceCatalogue | None,
                                                 EvidenceBundle | None]]]
FetchExchange = Callable[[MarketIntelligenceRequest], Awaitable[tuple[RawPayload, ...]]]
FetchPeers = Callable[[str], Awaitable[PeerFetch]]  # warehouse ID -> the peer fragment


class StageAcquisitionError(FinalReportGenerationError):
    """An expected acquisition failure with a stable code (never a traceback): no Screener
    page could be acquired, or the declared identity is not the acquired company's."""


class PageUnavailableError(ValueError):
    """One Screener page could not be acquired (retrieval failed / nothing preserved)."""


@dataclass(frozen=True)
class StageRequest:
    source_url: str  # a supported Screener company page (either view)
    as_of: date
    # Declared identifiers are CHECKS only: the acquired Screener page defines the company,
    # and a declared value that disagrees with it fails the run before any exchange fetch.
    nse_symbol: str | None = None
    isin: str | None = None
    bse_code: str | None = None
    diligence: DueDiligenceRequest | None = None  # explicit cited Box 13 inputs, if any


def page_urls(source_url: str) -> tuple[str, str]:
    """(consolidated, standalone) URLs of the page's company; the consolidated page is the
    primary (analytical-basis) page."""
    url = validate_url(source_url)
    path = urlsplit(url).path
    slug = path.split("/")[2]
    base = f"https://www.screener.in/company/{slug}/"
    return base + "consolidated/", base


def _same(declared: str | None, canonical: str | None) -> bool:
    return declared is None or (canonical is not None
                                and declared.strip().upper() == canonical.strip().upper())


async def _pages(urls: tuple[str, ...], fetch_page: FetchPage) -> list[Page]:
    pages: list[Page] = []
    for url in urls:
        try:
            page = await fetch_page(url)
        except Exception:  # noqa: BLE001 - any page failure falls back to the next view
            continue
        if not page[1] or page[0].company is None:
            continue  # nothing preserved / no company identified on the page
        pages.append(page)
    return pages


async def _peer_screeners(primary: DetailedScreenerDataset,
                          fetch_page: FetchPage) -> tuple[DetailedScreenerDataset, ...]:
    """The Box 1B peer table's own rows only (never a guessed peer), at most MAX_PEER_PAGES
    fetches (failed ones included), each with the same page adapter; a failed peer is
    simply absent and Box 9 records it."""
    if primary.peers is None:
        return ()
    out: list[DetailedScreenerDataset] = []
    seen = {primary.company_key}
    attempted = 0
    for row in primary.peers.peers:
        if attempted >= MAX_PEER_PAGES:
            break
        if row.is_subject_company or row.company_url is None or row.screener_company_id is None:
            continue
        key = f"screener:{row.screener_company_id}"
        if key in seen:
            continue
        seen.add(key)
        attempted += 1
        try:
            extraction, content = await fetch_page(validate_url(str(row.company_url)))
            if not content or extraction.company is None:
                continue
            peer = build_screener_dataset(extraction, content, None)
        except Exception:  # noqa: BLE001 - a peer page failure never stops the run
            continue
        if peer.company_key == key:  # the page must be the row's own company
            out.append(peer)
    return tuple(out)


async def capture_stage(request: StageRequest, *, now: datetime, fetch_page: FetchPage,
                        fetch_evidence: FetchEvidence,
                        fetch_exchange: FetchExchange | None,
                        fetch_peers: FetchPeers | None = None) -> ResearchStage:
    """Acquire one company. The primary page (consolidated, else standalone) BINDS the run:
    its Screener company key, NSE symbol and BSE code are the identity; every declared
    identifier is checked against it before any evidence or exchange acquisition."""
    pages = await _pages(page_urls(request.source_url), fetch_page)
    if not pages:
        raise StageAcquisitionError(FailureCode.SOURCE_ACQUISITION_FAILED,
                                    "No Screener company page could be acquired")
    primary = build_screener_dataset(*pages[0], None)
    requested_page = screener_page_key(request.source_url)
    acquired_page = screener_page_key(primary.profile.page_url)
    if requested_page is None or acquired_page != requested_page:
        raise StageAcquisitionError(
            FailureCode.COMPANY_IDENTITY_MISMATCH,
            "The acquired Screener page does not match the requested company page")
    pages = [p for p in pages if build_screener_dataset(*p, None).company_key
             == primary.company_key]
    nse, bse = canonical_identity(primary)
    diligence = request.diligence
    if not (_same(request.nse_symbol, nse) and _same(request.bse_code, bse)) or (
            diligence is not None and not _same(diligence.nse_symbol, nse)):
        raise StageAcquisitionError(
            FailureCode.COMPANY_IDENTITY_MISMATCH,
            "The declared exchange identity is not the acquired Screener company's")
    if diligence is not None and diligence.company_key != primary.company_key:
        raise StageAcquisitionError(FailureCode.COMPANY_IDENTITY_MISMATCH,
                                    "The cited Box 13 inputs belong to another company key")
    peer_fetch: PeerFetch | None = None
    if fetch_peers is not None and primary.profile.warehouse_id is not None:
        try:
            peer_fetch = await fetch_peers(primary.profile.warehouse_id)
        except Exception:  # noqa: BLE001 - the peer fragment is optional
            peer_fetch = None
    catalogue, bundle = await fetch_evidence(pages[0])
    market_request = None
    payloads: tuple[RawPayload, ...] = ()
    if fetch_exchange is not None and (nse or bse):
        # canonical identifiers only: a declared ISIN is never an acquisition key (it is
        # verified against Box 12's resolved ISIN by the orchestrator)
        market_request = MarketIntelligenceRequest(
            nse_symbol=nse, bse_code=bse, isin=None, company_name=primary.profile.name,
            as_of=now)
        payloads = await fetch_exchange(market_request)
    stage = build_research_stage(
        generated_at=now, pages=pages, catalogue=catalogue, bundle=bundle,
        market_request=market_request, market_payloads=payloads,
        diligence_request=DueDiligenceRequest(as_of=request.as_of, company_key="pending"),
        report_request=FinalReportRequest(as_of=request.as_of, company_key="pending"),
        peer_fetch=peer_fetch)
    if fetch_peers is not None and stage.screeners[0].peer_status.status == "NOT_REQUESTED":
        # the fragment was attempted but nothing could be fetched (no warehouse ID / error)
        unavailable = PeerStatus(status="UNAVAILABLE", code="PEER_FETCH_FAILED")
        stage = stage.model_copy(update={"screeners": (
            stage.screeners[0].model_copy(update={"peer_status": unavailable}),
            *stage.screeners[1:])})
    peers = await _peer_screeners(stage.screeners[0], fetch_page)
    key = primary.company_key
    diligence = diligence or DueDiligenceRequest(
        as_of=request.as_of, company_key=key, nse_symbol=nse, isin=request.isin)
    return stage.model_copy(update={
        "peer_screeners": peers,
        "diligence_request": diligence,
        "report_request": FinalReportRequest(as_of=request.as_of, company_key=key,
                                             nse_symbol=nse, isin=request.isin)})


# --- live wiring (the existing adapters; network) ---------------------------------------------


def live_fetchers(work: Path, *, now: datetime) -> tuple[FetchPage, FetchEvidence,
                                                         FetchExchange, FetchPeers]:
    """The production steps, each one an existing adapter call. `work` holds the raw
    artifacts (pages, documents) and throwaway SQLite databases; it is never the report
    store. Every network call keeps its adapter's own timeout and size bound."""
    from stock_research_ai.adapters.documents.document_parsers import LocalDocumentParser
    from stock_research_ai.adapters.documents.evidence_fetcher import (
        RawArtifactEvidenceReader,
        RawArtifactEvidenceWriter,
        SafeEvidenceFetcher,
    )
    from stock_research_ai.adapters.exchange.source import ExchangeFetcher, acquire
    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
    from stock_research_ai.adapters.screener.evidence import ScreenerEvidenceSource
    from stock_research_ai.adapters.screener.peers_api import ScreenerPeerClient
    from stock_research_ai.adapters.secret_store import resolve_kite_credentials
    from stock_research_ai.application.document_intelligence import extract_evidence
    from stock_research_ai.application.evidence_catalogue import build_evidence_catalogue
    from stock_research_ai.application.evidence_retrieval import retrieve_evidence
    from stock_research_ai.bootstrap import extract_snapshot, initialize_database
    from stock_research_ai.config import Settings
    from stock_research_ai.domain.document_intelligence import ExtractionLimits
    from stock_research_ai.domain.evidence import EvidenceDocumentType as T
    from stock_research_ai.domain.evidence_retrieval import RetrievalPolicy
    from stock_research_ai.domain.extraction import ExtractionRequest

    counter = iter(range(1_000_000))

    async def fetch_page(url: str) -> Page:
        out = work / f"page-{next(counter):03d}"  # one directory per fetch, never reused
        out.mkdir(parents=True, exist_ok=False)
        settings = Settings(_env_file=None, database_path=out / "db.sqlite3",  # type: ignore[call-arg]
                            artifact_path=out / "page")
        await initialize_database(settings)
        extraction = await extract_snapshot(settings, ExtractionRequest(
            provider="screener", url=url), out / "export")
        if not extraction.raw_artifacts:
            # the retrieval failed (network / HTTP / blocked): nothing was preserved
            raise PageUnavailableError("The Screener page could not be retrieved")
        content = await FileRawArtifactStore(out / "page").read(extraction.raw_artifacts[0])
        return extraction, content

    async def fetch_evidence(page: Page) -> tuple[EvidenceCatalogue | None,
                                                  EvidenceBundle | None]:
        src = ScreenerEvidenceSource(*page)
        catalogue = await build_evidence_catalogue(src.company(), [src], catalogued_at=now)
        policy = RetrievalPolicy(
            max_tier=3, max_documents=7, concurrency=2, max_file_bytes=60_000_000,
            timeout_seconds=180, per_type_limit={t: 0 for t in T} | {
                T.ANNUAL_REPORT: 1, T.CONCALL_TRANSCRIPT: 4, T.CREDIT_RATING_REPORT: 2})
        docs = work / "docs"
        manifest = await retrieve_evidence(
            catalogue, SafeEvidenceFetcher(), RawArtifactEvidenceWriter(
                FileRawArtifactStore(docs)), policy,
            run_id=uuid5(NS, f"stage-retrieval|{page[0].metadata.id}|{now.isoformat()}"),
            clock=lambda: now)
        bundle = await extract_evidence(catalogue, manifest, RawArtifactEvidenceReader(docs),
                                        LocalDocumentParser(), ExtractionLimits(max_seconds=900),
                                        clock=lambda: now)
        return catalogue, bundle

    async def fetch_exchange(request: MarketIntelligenceRequest) -> tuple[RawPayload, ...]:
        return await acquire(request, ExchangeFetcher(), resolve_kite_credentials())

    async def fetch_peers(warehouse_id: str) -> PeerFetch:
        return await ScreenerPeerClient().fetch(warehouse_id)

    return fetch_page, fetch_evidence, fetch_exchange, fetch_peers
