"""Box 15 command-line glue: build a research stage from acquisition outputs and run the
offline orchestrator into the local final-report store.

`stock-research-ai detailed-research --stage stage.json [--formats json,md,html,pdf]
[--output-root DIR]` prints the structured result (status, failure, manifest) as JSON and
exits 0 (COMPLETE), 3 (PARTIAL_RENDER) or 2 (FAILED).
"""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from stock_research_ai.adapters.rendering.pdf import render_pdf
from stock_research_ai.adapters.rendering.store import FileFinalReportStore, final_report_root
from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application.report_orchestrator import (
    ResearchStage,
    StagePayload,
    run_detailed_research,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.forecasting import ForecastRequest
from stock_research_ai.domain.fundamental_valuation import ValuationRequest
from stock_research_ai.domain.market_intelligence import MarketIntelligenceRequest, RawPayload
from stock_research_ai.private_files import write_private_file

MAX_STAGE_BYTES = 512 * 1024 * 1024

EXIT_CODES = {"COMPLETE": 0, "PARTIAL_RENDER": 3, "FAILED": 2}


def build_research_stage(
    *,
    generated_at: datetime,
    pages: Sequence[tuple[CompanyExtractionResult, bytes]],
    catalogue: EvidenceCatalogue | None,
    bundle: EvidenceBundle | None,
    diligence_request: DueDiligenceRequest,
    report_request: FinalReportRequest,
    market_request: MarketIntelligenceRequest | None = None,
    market_payloads: Sequence[RawPayload] = (),
    forecast_request: ForecastRequest | None = None,
    valuation_request: ValuationRequest | None = None,
) -> ResearchStage:
    """Box 1 extraction + preserved page bytes -> Box 1B (the existing pure parser), plus the
    other acquisition outputs, as one JSON-safe stage. `pages[0]` is the primary page."""
    screeners = tuple(build_screener_dataset(extraction, content, None)
                      for extraction, content in pages)
    return ResearchStage(
        generated_at=generated_at, screeners=screeners, catalogue=catalogue, bundle=bundle,
        market_request=market_request,
        market_payloads=tuple(StagePayload.of(p) for p in market_payloads),
        forecast_request=forecast_request, valuation_request=valuation_request,
        diligence_request=diligence_request, report_request=report_request)


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    cmd = commands.add_parser(
        "detailed-research",
        help="Box 15: run DETAILED_RESEARCH_V1 Boxes 4-14 offline from a captured research "
        "stage and render JSON / Markdown / HTML / PDF into the local final-report store")
    cmd.add_argument("--stage", type=Path, required=True)
    cmd.add_argument("--formats", default="json,md,html,pdf")
    cmd.add_argument("--output-root", type=Path, default=None)


def add_stage_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    cmd = commands.add_parser(
        "detailed-research-stage",
        help="Box 15: capture one company's research stage (Screener pages, filings, exchange "
        "feeds) with the existing acquisition adapters; network. Render it offline with "
        "'detailed-research --stage'")
    cmd.add_argument("--source-url", required=True)
    cmd.add_argument("--as-of", required=True, type=date.fromisoformat)
    cmd.add_argument("--nse-symbol")
    cmd.add_argument("--isin")
    cmd.add_argument("--bse-code")
    cmd.add_argument("--diligence-inputs", type=Path,
                     help="DueDiligenceRequest JSON with cited Box 13 inputs (optional)")
    cmd.add_argument("--work", type=Path, required=True,
                     help="Directory for raw pages / documents captured during acquisition")
    cmd.add_argument("--output", type=Path, required=True)


def run_stage(args: argparse.Namespace) -> int:
    import asyncio

    from stock_research_ai.adapters.rendering.acquire import (
        StageRequest,
        capture_stage,
        live_fetchers,
    )

    output: Path = args.output
    if output.exists():
        raise ValueError("The stage output already exists; choose a new path")
    diligence = None
    if args.diligence_inputs is not None:
        diligence = DueDiligenceRequest.model_validate_json(args.diligence_inputs.read_text())
    now = datetime.now(UTC)
    page, evidence, exchange = live_fetchers(args.work.resolve(), now=now)
    stage = asyncio.run(capture_stage(StageRequest(
        source_url=args.source_url, as_of=args.as_of, nse_symbol=args.nse_symbol,
        isin=args.isin, bse_code=args.bse_code, diligence=diligence), now=now,
        fetch_page=page, fetch_evidence=evidence, fetch_exchange=exchange))
    write_private_file(output, stage.model_dump_json())
    print(json.dumps({"stage": str(output), "company_key": stage.screeners[0].company_key,
                      "pages": len(stage.screeners), "market_payloads":
                      len(stage.market_payloads), "documents": len(
                          stage.bundle.extractions) if stage.bundle else 0}, indent=2))
    return 0


def run(args: argparse.Namespace, settings: Settings) -> int:
    root = args.output_root.resolve() if args.output_root else final_report_root(settings)
    stage_path: Path = args.stage
    if stage_path.stat().st_size > MAX_STAGE_BYTES:
        raise ValueError("The research stage exceeds its size bound")
    result = run_detailed_research(
        stage_path.read_bytes(), store=FileFinalReportStore(root), pdf=render_pdf,
        formats=[f for f in args.formats.split(",") if f.strip()])
    print(json.dumps({"report_root": str(root), **result.model_dump(mode="json")}, indent=2))
    return EXIT_CODES[result.status]
