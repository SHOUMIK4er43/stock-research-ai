"""Minimal command-line entry points for the approved Part 1 interfaces."""

import argparse
import asyncio
import json
import logging
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
    args = parser.parse_args(argv)

    # Normal Python logging defaults to stderr, preserving MCP's protocol stdout.
    logging.basicConfig(level=logging.INFO)
    settings = Settings(database_path=args.database) if args.database is not None else Settings()

    if args.command == "init-db":
        from stock_research_ai.bootstrap import initialize_database

        asyncio.run(initialize_database(settings))
        logging.getLogger(__name__).info("Company registry ready: %s", settings.database_path)
    elif args.command == "serve-http":
        import uvicorn

        from stock_research_ai.interfaces.http import create_app

        uvicorn.run(create_app(settings=settings), host=settings.http_host, port=settings.http_port)
    elif args.command == "serve-mcp":
        from stock_research_ai.interfaces.mcp import create_server

        create_server(settings=settings).run()

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
