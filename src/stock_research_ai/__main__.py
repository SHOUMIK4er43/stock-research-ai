"""Minimal command-line entry points for the approved Part 1 interfaces."""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

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
