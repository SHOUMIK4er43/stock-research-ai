"""Minimal command-line entry points for the approved Part 1 interfaces."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from stock_research_ai import __version__
from stock_research_ai.config import Settings


def main(argv: Sequence[str] | None = None) -> None:
    """Run a selected interface or explicitly initialize its SQLite database."""
    parser = argparse.ArgumentParser(prog="stock-research-ai")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--database", type=Path, help="Override the SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create the company registry if it does not exist")
    commands.add_parser("serve-http", help="Serve the local FastAPI interface")
    commands.add_parser("serve-mcp", help="Serve MCP over stdin/stdout")
    commands.add_parser(
        "pair-code",
        help="Issue a one-time browser-extension pairing code (Part 6B-A); printed once, "
        "used with POST /v1/pair",
    )
    commands.add_parser(
        "revoke-pairing",
        help="Revoke the paired browser extension and any pending pairing code; its token "
        "stops working immediately (pair again with `pair-code`)",
    )
    configure_analyst_cmd = commands.add_parser(
        "configure-analyst",
        help="Store an analyst API key in the macOS Keychain (Part 6C-A); prompted "
        "securely, never echoed, never written to a plaintext config file",
    )
    configure_analyst_cmd.add_argument(
        "--provider", choices=("anthropic", "aicredits"), default="anthropic"
    )
    commands.add_parser(
        "diagnostics",
        help="Print safe, non-secret production state for troubleshooting (Part 6C-A)",
    )
    backup_cmd = commands.add_parser(
        "backup", help="Back up the research database and artifacts (Part 6C-A)"
    )
    backup_cmd.add_argument(
        "--output", type=Path, required=True, help="Empty destination directory"
    )
    restore_cmd = commands.add_parser(
        "restore", help="Restore a backup created by `backup` (Part 6C-A)"
    )
    restore_cmd.add_argument(
        "--input", type=Path, required=True, help="Backup directory to restore"
    )
    restore_cmd.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing database (a safety backup is taken first)",
    )
    launchagent_cmd = commands.add_parser(
        "render-launchagent",
        help="Write a macOS LaunchAgent plist for this installed binary (Part 6C-B "
        "installer plumbing); never embeds a secret",
    )
    launchagent_cmd.add_argument("--output", type=Path, required=True)
    launchagent_cmd.add_argument(
        "--executable",
        help="Path to the stock-research-ai binary to launch (default: this process's "
        "own resolved executable path)",
    )
    extract = commands.add_parser(
        "extract-screener", help="Acquire or replay a public company page"
    )
    extract.add_argument("url", help="Original public company URL (also required for replay)")
    extract.add_argument(
        "--html", type=Path, help="Replay local HTML bytes; disables network acquisition"
    )
    extract.add_argument("--final-url", help="Final acquired URL for replay link resolution")
    extract.add_argument("--output", type=Path, required=True)
    ingest = commands.add_parser("ingest-document", help="Preserve and extract public PDF evidence")
    ingest.add_argument("--company", type=UUID, required=True)
    ingest.add_argument("--url")
    ingest.add_argument("--reference-json", type=Path, help="Saved DocumentReference JSON")
    ingest.add_argument(
        "--type",
        default="other",
        choices=[
            "annual-report",
            "investor-presentation",
            "earnings-presentation",
            "concall-transcript",
            "announcement",
            "credit-rating",
            "other",
        ],
    )
    ingest.add_argument("--title")
    ingest.add_argument("--file", type=Path, help="Offline replay of original PDF bytes")
    ingest.add_argument("--final-url", help="Final URL of an offline capture")
    ingest.add_argument("--output", type=Path, required=True)
    search = commands.add_parser("search-documents", help="Literal search of versioned page chunks")
    search.add_argument("--company", type=UUID, required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    market = commands.add_parser(
        "ingest-market-data", help="Acquire or replay daily market evidence"
    )
    market.add_argument("--symbol", required=True)
    market.add_argument("--exchange", default="NSE")
    market.add_argument("--kind", required=True, choices=["EQUITY", "INDEX"])
    market.add_argument("--start", required=True)
    market.add_argument("--end", required=True)
    market.add_argument("--master-file", type=Path)
    market.add_argument("--candles-file", type=Path)
    market.add_argument("--synthetic", action="store_true")
    market.add_argument("--expected-sessions", help="Comma-separated caller-declared dates")
    market.add_argument("--output", type=Path, required=True)
    publication = commands.add_parser("ingest-publication", help="Official publication evidence")
    publication.add_argument("--request", type=Path, required=True, help="PublicationRequest JSON")
    publication.add_argument("--output", type=Path, required=True)
    macro = commands.add_parser("ingest-macro", help="Official CPI observation evidence")
    macro.add_argument("--request", type=Path, required=True, help="MacroRequest JSON")
    macro.add_argument("--output", type=Path, required=True)
    calculation = commands.add_parser(
        "calculate-financials", help="Calculate pinned financial evidence"
    )
    calculation.add_argument("--request", type=Path, required=True)
    calculation.add_argument("--output", type=Path, required=True)
    market_calc = commands.add_parser("calculate-market", help="Calculate pinned market evidence")
    market_calc.add_argument("--request", type=Path, required=True)
    market_calc.add_argument("--output", type=Path, required=True)
    sector = commands.add_parser(
        "calculate-sector-kpis", help="Evaluate versioned sector KPI eligibility"
    )
    sector.add_argument("--request", type=Path, required=True)
    sector.add_argument("--output", type=Path, required=True)
    patterns = commands.add_parser(
        "evaluate-patterns", help="Evaluate six deterministic financial patterns"
    )
    patterns.add_argument("--request", type=Path, required=True)
    patterns.add_argument("--output", type=Path, required=True)
    technical = commands.add_parser(
        "calculate-technicals", help="Calculate fixed technical indicator series"
    )
    technical.add_argument("--request", type=Path, required=True)
    technical.add_argument("--output", type=Path, required=True)
    valuation = commands.add_parser(
        "evaluate-valuation", help="Evaluate pinned valuation evidence and explicit scenarios"
    )
    valuation.add_argument("--request", type=Path, required=True)
    valuation.add_argument("--output", type=Path, required=True)
    analysis = commands.add_parser(
        "analyze-company",
        help="Produce evidence-cited interpretation claims over pinned Parts 1-5A evidence",
    )
    analysis.add_argument("--request", type=Path, required=True)
    analysis.add_argument("--execution", type=Path, required=True)
    analysis.add_argument("--output", type=Path, required=True)
    challenge = commands.add_parser(
        "challenge-analysis",
        help="Independently re-verify a Part 5B analysis snapshot against its own frozen "
        "closed evidence bundle",
    )
    challenge.add_argument("--analysis-snapshot", type=UUID, required=True)
    challenge.add_argument(
        "--execution", type=Path, help="ChallengerExecutionSpec JSON; omit for deterministic-only"
    )
    challenge.add_argument("--output", type=Path, required=True)
    report = commands.add_parser(
        "generate-report",
        help="Assemble the final institutional report over an exact frozen Part 5C "
        "ChallengeSnapshot - no ChallengeSnapshot means no report",
    )
    report.add_argument("--challenge-snapshot", type=UUID, required=True)
    report.add_argument(
        "--execution", type=Path, help="ReportExecutionSpec JSON; omit for deterministic-only"
    )
    report.add_argument("--output", type=Path, required=True)
    full_research_cmd = commands.add_parser(
        "full-research",
        help="Run the server-owned FULL_RESEARCH_V1 plan end-to-end for one supported "
        "Screener company - the pre-browser proof that a real report can be produced "
        "without test fakes",
    )
    full_research_cmd.add_argument("--source-url", required=True)
    full_research_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    # Normal Python logging defaults to stderr, preserving MCP's protocol stdout.
    logging.basicConfig(level=logging.INFO)
    settings = Settings(database_path=args.database) if args.database is not None else Settings()

    if args.command == "analyze-company":
        from stock_research_ai.bootstrap import analyze_company
        from stock_research_ai.domain.analysis import AnalysisRequest, AnalystExecutionSpec

        try:
            asyncio.run(
                analyze_company(
                    settings,
                    AnalysisRequest.model_validate_json(args.request.read_text()),
                    AnalystExecutionSpec.model_validate_json(args.execution.read_text()),
                    args.output,
                )
            )
        except RuntimeError as exc:
            print(f"Analyst provider unavailable: {exc}", file=sys.stderr)
            raise SystemExit(4) from None
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Analysis exported: {args.output}")
        raise SystemExit(0)
    elif args.command == "challenge-analysis":
        from pydantic import ValidationError

        from stock_research_ai.application.challenge import ChallengeIntegrityError
        from stock_research_ai.bootstrap import challenge_analysis
        from stock_research_ai.domain.challenge import ChallengeRequest, ChallengerExecutionSpec

        execution = (
            ChallengerExecutionSpec.model_validate_json(args.execution.read_text())
            if args.execution is not None
            else None
        )
        try:
            asyncio.run(
                challenge_analysis(
                    settings,
                    ChallengeRequest(analysis_snapshot_id=args.analysis_snapshot),
                    args.output,
                    execution,
                )
            )
        except RuntimeError as exc:
            print(f"Challenger provider unavailable: {exc}", file=sys.stderr)
            raise SystemExit(4) from None
        except ChallengeIntegrityError as exc:
            print(f"Challenge integrity failure: {exc}", file=sys.stderr)
            raise SystemExit(6) from None
        except ValidationError as exc:
            print(f"Challenge validator/system failure: {exc}", file=sys.stderr)
            raise SystemExit(5) from None
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Challenge exported: {args.output}")
        raise SystemExit(0)
    elif args.command == "generate-report":
        from pydantic import ValidationError

        from stock_research_ai.application.report import ReportIntegrityError
        from stock_research_ai.bootstrap import generate_report
        from stock_research_ai.domain.report import ReportExecutionSpec, ReportRequest

        report_execution = (
            ReportExecutionSpec.model_validate_json(args.execution.read_text())
            if args.execution is not None
            else None
        )
        try:
            asyncio.run(
                generate_report(
                    settings,
                    ReportRequest(challenge_snapshot_id=args.challenge_snapshot),
                    args.output,
                    report_execution,
                )
            )
        except RuntimeError as exc:
            print(f"Report narrator unavailable: {exc}", file=sys.stderr)
            raise SystemExit(4) from None
        except ReportIntegrityError as exc:
            print(f"Report integrity failure: {exc}", file=sys.stderr)
            raise SystemExit(6) from None
        except ValidationError as exc:
            print(f"Report validator/system failure: {exc}", file=sys.stderr)
            raise SystemExit(5) from None
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Report exported: {args.output}")
        raise SystemExit(0)
    elif args.command == "full-research":
        from stock_research_ai.application.research_plan import FullResearchRequest
        from stock_research_ai.bootstrap import full_research

        try:
            full_research_result = asyncio.run(
                full_research(
                    settings, FullResearchRequest(source_url=args.source_url), args.output
                )
            )
        except RuntimeError as exc:
            print(f"Analyst provider unavailable: {exc}", file=sys.stderr)
            raise SystemExit(4) from None
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        for warning in full_research_result.stage_warnings:
            print(f"warning [{warning.stage}] {warning.code}: {warning.message}", file=sys.stderr)
        print(f"Full research exported: {args.output}")
        raise SystemExit(0)
    elif args.command == "evaluate-valuation":
        from stock_research_ai.bootstrap import evaluate_valuation
        from stock_research_ai.domain.valuation import ValuationRequest

        try:
            valuation_snapshot = asyncio.run(
                evaluate_valuation(
                    settings,
                    ValuationRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Valuation exported: {args.output}")
        raise SystemExit(
            0 if all(r.status == "AVAILABLE" for r in valuation_snapshot.results) else 3
        )
    elif args.command == "calculate-technicals":
        from stock_research_ai.bootstrap import calculate_technicals
        from stock_research_ai.domain.technicals import TechnicalRequest

        try:
            technical_snapshot = asyncio.run(
                calculate_technicals(
                    settings,
                    TechnicalRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Technical calculation exported: {args.output}")
        raise SystemExit(
            0
            if all(s.status == "AVAILABLE" for s in technical_snapshot.series if s.requested)
            else 3
        )
    elif args.command == "evaluate-patterns":
        from stock_research_ai.bootstrap import evaluate_patterns
        from stock_research_ai.domain.patterns import PatternRequest

        try:
            snapshot = asyncio.run(
                evaluate_patterns(
                    settings,
                    PatternRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"Pattern evaluation exported: {args.output}")
        raise SystemExit(
            0 if all(f.evaluation_status == "AVAILABLE" for f in snapshot.findings) else 3
        )
    elif args.command == "calculate-sector-kpis":
        from stock_research_ai.bootstrap import calculate_sector_kpis
        from stock_research_ai.domain.sector_kpis import SectorKPIRequest

        try:
            evaluation = asyncio.run(
                calculate_sector_kpis(
                    settings,
                    SectorKPIRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        available = sum(r.status == "AVAILABLE" for r in evaluation.results)
        print(
            f"{available}/{len(evaluation.results)} KPIs available; "
            f"inspect eligibility and issues: {args.output}"
        )
        raise SystemExit(0 if available == len(evaluation.results) else 3)
    elif args.command == "calculate-market":
        from stock_research_ai.bootstrap import calculate_market_snapshot
        from stock_research_ai.domain.market_calculations import MarketCalculationRequest

        try:
            market_calculated = asyncio.run(
                calculate_market_snapshot(
                    settings,
                    MarketCalculationRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        available = sum(r.status == "AVAILABLE" for r in market_calculated.results)
        print(
            f"{available}/{len(market_calculated.results)} numerically available: "
            f"{args.output}; inspect limitations"
        )
        raise SystemExit(0 if available == len(market_calculated.results) else 3)
    elif args.command == "calculate-financials":
        from stock_research_ai.bootstrap import calculate_financials
        from stock_research_ai.domain.calculations import CalculationRequest

        try:
            calculated = asyncio.run(
                calculate_financials(
                    settings,
                    CalculationRequest.model_validate_json(args.request.read_text()),
                    args.output,
                )
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        available = sum(r.status == "AVAILABLE" for r in calculated.results)
        print(f"{available}/{len(calculated.results)} calculations available: {args.output}")
        raise SystemExit(0 if available == len(calculated.results) else 3)
    elif args.command in {"ingest-publication", "ingest-macro"}:
        from stock_research_ai.bootstrap import ingest_macro, ingest_publication
        from stock_research_ai.domain.macro import MacroRequest
        from stock_research_ai.domain.publications import PublicationRequest

        try:
            if args.command == "ingest-publication":
                context_result = asyncio.run(
                    ingest_publication(
                        settings,
                        PublicationRequest.model_validate_json(args.request.read_text()),
                        args.output,
                    )
                )
            else:
                macro_result = asyncio.run(
                    ingest_macro(
                        settings,
                        MacroRequest.model_validate_json(args.request.read_text()),
                        args.output,
                    )
                )
            state = (
                context_result.coverage.overall
                if args.command == "ingest-publication"
                else macro_result.coverage.overall
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(f"{state}: {args.output}")
        raise SystemExit({"COMPLETE": 0, "PARTIAL": 3, "FAILED": 1}[state])
    elif args.command == "ingest-market-data":
        from stock_research_ai.bootstrap import ingest_market
        from stock_research_ai.domain.market import MarketRequest

        try:
            request = MarketRequest.model_validate(
                {
                    "symbol": args.symbol,
                    "exchange": args.exchange,
                    "kind": args.kind,
                    "start": args.start,
                    "end": args.end,
                    "master_file": args.master_file,
                    "candles_file": args.candles_file,
                    "evidence_kind": "SYNTHETIC" if args.synthetic else "PROVIDER",
                    "expected_sessions": args.expected_sessions.split(",")
                    if args.expected_sessions
                    else None,
                }
            )
            market_result = asyncio.run(ingest_market(settings, request, args.output))
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(
            f"{market_result.coverage.overall}: "
            f"{len(market_result.observations)} observations; {args.output}"
        )
        raise SystemExit({"COMPLETE": 0, "PARTIAL": 3, "FAILED": 1}[market_result.coverage.overall])
    elif args.command == "init-db":
        from stock_research_ai.bootstrap import initialize_database

        asyncio.run(initialize_database(settings))
        logging.getLogger(__name__).info("Company registry ready: %s", settings.database_path)
    elif args.command == "serve-http":
        import errno

        import uvicorn

        from stock_research_ai.interfaces.http import create_app
        from stock_research_ai.logging_setup import configure_logging

        service_logger = configure_logging(
            settings.log_path,
            known_secrets=[
                secret.get_secret_value()
                for secret in (settings.analyst_api_key, settings.aicredits_api_key)
                if secret is not None
            ],
        )
        # Part 6C-A single-process/single-worker invariant: BrowserJobManager holds
        # in-flight research jobs in memory, so a second worker process would never
        # see (and could never resolve) jobs launched against the first - there is
        # deliberately no `--workers` flag exposed anywhere in this CLI, and
        # `uvicorn.run(app_instance, ...)` (an app object, not an import string) is
        # itself incompatible with uvicorn's multi-worker mode, which requires an
        # import-string target. This log line exists so the invariant is visible and
        # tested, not just implicit.
        service_logger.info(
            "starting serve-http in single-worker mode (required: BrowserJobManager is in-memory)"
        )
        try:
            uvicorn.run(
                create_app(settings=settings),
                host=settings.http_host,
                port=settings.http_port,
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                parser.error(
                    f"Could not start: port {settings.http_port} on {settings.http_host} is "
                    "already in use. Stop the process using it, or set "
                    "STOCK_RESEARCH_AI_HTTP_PORT to a different port."
                )
            raise
    elif args.command == "serve-mcp":
        from stock_research_ai.interfaces.mcp import create_server

        create_server(settings=settings).run()
    elif args.command == "pair-code":
        from stock_research_ai.adapters.http.browser_auth import PairingStore

        code = PairingStore(settings.browser_auth_path).issue_pairing_code()
        print(code)
    elif args.command == "revoke-pairing":
        from stock_research_ai.adapters.http.browser_auth import PairingStore

        PairingStore(settings.browser_auth_path).revoke()
        print("Browser extension pairing revoked. Run `stock-research-ai pair-code` to pair again.")
    elif args.command == "configure-analyst":
        import getpass

        from stock_research_ai.adapters.secret_store import (
            SecretStoreUnavailableError,
            store_analyst_api_key_in_keychain,
        )

        label = "AI Credits" if args.provider == "aicredits" else "Anthropic"
        api_key = getpass.getpass(f"{label} API key (input hidden): ").strip()
        if not api_key:
            parser.error("No API key entered.")
        try:
            store_analyst_api_key_in_keychain(api_key, provider=args.provider)
        except (SecretStoreUnavailableError, ValueError) as exc:
            parser.error(f"Could not store the key in Keychain: {exc}")
        variable = (
            "STOCK_RESEARCH_AI_AICREDITS_API_KEY" if args.provider == "aicredits"
            else "STOCK_RESEARCH_AI_ANALYST_API_KEY"
        )
        print(f"Stored in the macOS Keychain. {variable}, if set, still takes precedence.")
        if args.provider == "aicredits":
            print("Select STOCK_RESEARCH_AI_ANALYST_PROVIDER=aicredits and set "
                  "STOCK_RESEARCH_AI_AICREDITS_MODEL to your confirmed catalog model ID.")
    elif args.command == "diagnostics":
        from stock_research_ai.diagnostics import format_diagnostics, gather_diagnostics

        print(format_diagnostics(asyncio.run(gather_diagnostics(settings))))
    elif args.command == "backup":
        from stock_research_ai.backup import BackupError, create_backup

        try:
            output = asyncio.run(create_backup(settings, args.output))
        except BackupError as exc:
            parser.error(str(exc))
        print(f"Backup written to {output}")
    elif args.command == "restore":
        from stock_research_ai.backup import BackupError, restore_backup

        try:
            asyncio.run(restore_backup(settings, args.input, force=args.force))
        except BackupError as exc:
            parser.error(str(exc))
        print(f"Restored from {args.input}")
    elif args.command == "render-launchagent":
        from stock_research_ai.release_tools import (
            LAUNCH_AGENT_LABEL,
            render_launch_agent_plist,
            validate_launch_agent_plist,
        )

        executable = args.executable or sys.argv[0]
        log_dir = settings.log_path.parent
        plist_bytes = render_launch_agent_plist(
            label=LAUNCH_AGENT_LABEL,
            program_arguments=[executable, "serve-http"],
            stdout_path=log_dir / "launchd-stdout.log",
            stderr_path=log_dir / "launchd-stderr.log",
        )
        problems = validate_launch_agent_plist(plist_bytes, LAUNCH_AGENT_LABEL)
        if problems:
            parser.error("refusing to write an invalid LaunchAgent plist: " + "; ".join(problems))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(plist_bytes)
        print(args.output)
    elif args.command == "ingest-document":
        from stock_research_ai.bootstrap import ingest_document
        from stock_research_ai.domain.documents import (
            DocumentReference,
            DocumentRequest,
            DocumentType,
        )

        try:
            reference = (
                DocumentReference.model_validate_json(args.reference_json.read_text())
                if args.reference_json
                else None
            )
            url = args.url or (reference.link.resolved_url if reference else None)
            if url is None:
                parser.error("ingest-document requires --url or --reference-json")
            document = asyncio.run(
                ingest_document(
                    settings,
                    DocumentRequest(
                        company_id=args.company,
                        url=url,
                        reference=reference,
                        document_type=DocumentType(args.type.replace("-", "_")),
                        title=args.title,
                        replay_path=args.file,
                        final_url=args.final_url,
                    ),
                    args.output,
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logging.getLogger(__name__).error("Document ingestion failed: %s", exc)
            raise SystemExit(1) from exc
        print(f"{document.coverage.overall}: {args.output.resolve()}")
        print(
            f"Version {document.version_id}: {document.coverage.page_count} pages; "
            f"{document.coverage.pages_extracted} extracted; "
            f"{len(document.coverage.suspected_scanned_pages)} OCR_REQUIRED"
        )
        raise SystemExit({"COMPLETE": 0, "PARTIAL": 3, "FAILED": 1}[document.coverage.overall])
    elif args.command == "search-documents":
        from stock_research_ai.bootstrap import document_engine

        async def search_documents() -> None:
            async with document_engine(settings) as engine:
                hits = await engine.search_company_documents(args.company, args.query, args.limit)
                print(
                    json.dumps(
                        [h.model_dump(mode="json") for h in hits], ensure_ascii=False, indent=2
                    )
                )

        try:
            asyncio.run(search_documents())
        except (ValueError, RuntimeError) as exc:
            logging.getLogger(__name__).error("Search failed: %s", exc)
            raise SystemExit(1) from exc
    else:
        from stock_research_ai.bootstrap import extract_snapshot
        from stock_research_ai.domain.extraction import ExtractionRequest

        try:
            result = asyncio.run(
                extract_snapshot(
                    settings,
                    ExtractionRequest(
                        provider="screener",
                        url=args.url,
                        replay_path=args.html,
                        final_url=args.final_url,
                    ),
                    args.output,
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logging.getLogger(__name__).error("Extraction failed: %s", exc)
            raise SystemExit(1) from exc
        print(f"{result.coverage.overall}: {args.output.resolve()}")
        for section in result.coverage.sections:
            print(
                f"{section.section}: {section.presence}/{section.parse_status} "
                f"({section.records} records)"
            )
        raise SystemExit({"COMPLETE": 0, "PARTIAL": 3, "FAILED": 1}[result.coverage.overall])


if __name__ == "__main__":
    main()
