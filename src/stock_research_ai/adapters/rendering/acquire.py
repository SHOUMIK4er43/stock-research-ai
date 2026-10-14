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
from stock_research_ai.adapters.screener.source import validate_url
from stock_research_ai.application.report_claims import NS
from stock_research_ai.application.report_orchestrator import ResearchStage
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.market_intelligence import MarketIntelligenceRequest, RawPayload

Page = tuple[CompanyExtractionResult, bytes]
FetchPage = Callable[[str], Awaitable[Page]]
FetchEvidence = Callable[[Page], Awaitable[tuple[EvidenceCatalogue | None,
                                                 EvidenceBundle | None]]]
FetchExchange = Callable[[MarketIntelligenceRequest], Awaitable[tuple[RawPayload, ...]]]


@dataclass(frozen=True)
class StageRequest:
    source_url: str  # a supported Screener company page (either view)
    as_of: date
    nse_symbol: str | None = None  # declared; Box 12 verifies it, never trusted blindly
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


async def capture_stage(request: StageRequest, *, now: datetime, fetch_page: FetchPage,
                        fetch_evidence: FetchEvidence,
                        fetch_exchange: FetchExchange | None) -> ResearchStage:
    pages: list[Page] = []
    failures: list[str] = []
    for url in page_urls(request.source_url):
        try:
            pages.append(await fetch_page(url))
        except (ValueError, OSError) as exc:
            failures.append(f"{url}: {type(exc).__name__}")
    if not pages:
        raise ValueError("No Screener page could be acquired: " + "; ".join(failures))
    catalogue, bundle = await fetch_evidence(pages[0])
    company = pages[0][0].company
    market_request = None
    payloads: tuple[RawPayload, ...] = ()
    if fetch_exchange is not None and (request.nse_symbol or request.bse_code or request.isin):
        market_request = MarketIntelligenceRequest(
            nse_symbol=request.nse_symbol, bse_code=request.bse_code, isin=request.isin,
            company_name=company.name if company is not None else None, as_of=now)
        payloads = await fetch_exchange(market_request)
    stage = build_research_stage(
        generated_at=now, pages=pages, catalogue=catalogue, bundle=bundle,
        market_request=market_request, market_payloads=payloads,
        diligence_request=DueDiligenceRequest(as_of=request.as_of, company_key="pending"),
        report_request=FinalReportRequest(as_of=request.as_of, company_key="pending"))
    key = stage.screeners[0].company_key
    diligence = request.diligence or DueDiligenceRequest(
        as_of=request.as_of, company_key=key, nse_symbol=request.nse_symbol, isin=request.isin)
    if diligence.company_key != key:
        raise ValueError("The cited Box 13 inputs belong to another company key")
    return stage.model_copy(update={
        "diligence_request": diligence,
        "report_request": FinalReportRequest(as_of=request.as_of, company_key=key,
                                             nse_symbol=request.nse_symbol,
                                             isin=request.isin)})


# --- live wiring (the existing adapters; network) ---------------------------------------------


def live_fetchers(work: Path, *, now: datetime) -> tuple[FetchPage, FetchEvidence,
                                                         FetchExchange]:
    """The production steps, each one an existing adapter call. `work` holds the raw
    artifacts (pages, documents) and throwaway SQLite databases; it is never the report
    store."""
    from stock_research_ai.adapters.documents.document_parsers import LocalDocumentParser
    from stock_research_ai.adapters.documents.evidence_fetcher import (
        RawArtifactEvidenceReader,
        RawArtifactEvidenceWriter,
        SafeEvidenceFetcher,
    )
    from stock_research_ai.adapters.exchange.source import ExchangeFetcher, acquire
    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
    from stock_research_ai.adapters.screener.evidence import ScreenerEvidenceSource
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

    async def fetch_page(url: str) -> Page:
        name = "consolidated" if url.endswith("/consolidated/") else "standalone"
        out = work / name
        out.mkdir(parents=True, exist_ok=True)
        settings = Settings(_env_file=None, database_path=out / "db.sqlite3",  # type: ignore[call-arg]
                            artifact_path=out / "page")
        await initialize_database(settings)
        extraction = await extract_snapshot(settings, ExtractionRequest(
            provider="screener", url=url), out / "export")
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

    return fetch_page, fetch_evidence, fetch_exchange
