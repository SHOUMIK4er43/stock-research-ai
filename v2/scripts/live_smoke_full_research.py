"""OPT-IN, MANUAL live smoke test for FULL_RESEARCH_V1 (Part 6B0). NOT part of normal
pytest - `tests/testpaths` never collects `scripts/`, and this script additionally
requires an explicit `--confirm-live` flag plus real Anthropic credentials before it
will make any network call, so it can never run by accident.

This performs REAL network acquisition against the live www.screener.in and a REAL
Anthropic API call (billed to the configured account) - it is a human-triggered sanity
check, never a correctness proof. Structural checks only: it never asserts on exact
model prose, since that is inherently non-deterministic.

Usage:
    STOCK_RESEARCH_AI_ANALYST_PROVIDER=anthropic \\
    STOCK_RESEARCH_AI_ANALYST_API_KEY=sk-ant-... \\
    uv run python scripts/live_smoke_full_research.py \\
        --source-url https://www.screener.in/company/INFY/ \\
        --output /tmp/full-research-infy \\
        --confirm-live

A Claude Pro/Claude Code subscription is NOT an Anthropic API credential - this needs a
separately configured Anthropic Console API key.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from stock_research_ai.application.research_plan import FullResearchRequest
from stock_research_ai.bootstrap import full_research
from stock_research_ai.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-url", required=True, help="A real https://www.screener.in/company/... URL"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required: acknowledges this makes real network calls and a real, billed "
        "Anthropic API call.",
    )
    args = parser.parse_args()

    if not args.confirm_live:
        print(
            "Refusing to run without --confirm-live. This makes a REAL network call to "
            "www.screener.in and a REAL, billed Anthropic API call.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    settings = Settings(
        database_path=args.output / "research.sqlite3", artifact_path=args.output / "raw"
    )
    if settings.analyst_provider is None or settings.analyst_api_key is None:
        print(
            "STOCK_RESEARCH_AI_ANALYST_PROVIDER and STOCK_RESEARCH_AI_ANALYST_API_KEY must be "
            "set in the environment. A Claude Pro/Claude Code subscription is NOT an Anthropic "
            "API credential - this requires a separately configured Anthropic Console API key.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from stock_research_ai.bootstrap import initialize_database

    asyncio.run(initialize_database(settings))
    result = asyncio.run(
        full_research(
            settings,
            FullResearchRequest(source_url=args.source_url),
            args.output / "result.json",
        )
    )

    # Structural checks only - never assert on exact model-generated prose.
    assert result.company_id
    assert result.extraction_id
    assert result.analysis_snapshot_id
    assert result.challenge_snapshot_id
    assert result.report_snapshot_id

    print(f"Sector classification: {result.sector_classification.classification} "
          f"(basis={result.sector_classification.basis})")
    print(f"Documents ingested: {len(result.document_versions_ingested)}")
    print(f"Document chunks selected: {result.document_chunks_selected}")
    print(f"Analysis snapshot:  {result.analysis_snapshot_id}")
    print(f"Challenge snapshot: {result.challenge_snapshot_id}")
    print(f"Report snapshot:    {result.report_snapshot_id}")
    for warning in result.stage_warnings:
        print(f"warning [{warning.stage}] {warning.code}: {warning.message}")
    print(f"Full result written to: {(args.output / 'result.json').resolve()}")


if __name__ == "__main__":
    main()
