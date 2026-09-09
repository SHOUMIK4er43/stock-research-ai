"""Offline, real-CLI proof against independently authored sector expectations.

Run with uv run python scripts/verify_sector_kpis.py --output output/part4c-proof.
Only fixture replay is allowed; subprocess network connections are explicitly blocked.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"


async def check_history(database, source, snapshot):
    db = SQLiteDatabase(database)
    try:
        assert await db.calculations.calculation_history(source.metadata.id) == (snapshot,)
        assert await db.extractions.get_extraction(source.metadata.id) == source
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Proof output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    database = output / "proof.sqlite3"
    expectations = json.loads((FIX / "sector_kpis/expected_outputs.json").read_text())
    report = []
    with tempfile.TemporaryDirectory(prefix="sector-offline-") as temp:
        guard = Path(temp)
        (guard / "sitecustomize.py").write_text(
            'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
            "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
        )
        env = {
            **os.environ,
            "PYTHONPATH": str(guard),
            "PYTHONDONTWRITEBYTECODE": "1",
            "STOCK_RESEARCH_AI_ARTIFACT_PATH": str(output / "artifacts"),
        }

        def cli(*args, allowed=(0,)):
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "stock_research_ai",
                    "--database",
                    str(database),
                    *map(str, args),
                ],
                env=env,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            if result.returncode not in allowed:
                raise RuntimeError(result.stderr or result.stdout)
            print(result.stdout.strip())
            return result.returncode

        cli("init-db")
        for symbol, fixture, scope, profile, classification, suffix in (
            (
                "YESBANK",
                "yesbank_consolidated",
                "consolidated",
                "BANK_PROFILE",
                "BANK",
                "/consolidated/",
            ),
            ("INFY", "infy_standalone", "standalone", "IT_SERVICES_PROFILE", "NON_FINANCIAL", "/"),
        ):
            evidence = output / f"{symbol}-evidence"
            cli(
                "extract-screener",
                f"https://www.screener.in/company/{symbol}{suffix}",
                "--html",
                FIX / f"screener/{fixture}.html",
                "--output",
                evidence,
                allowed=(0, 3),
            )
            source = CompanyExtractionResult.model_validate_json(
                (evidence / "normalized/extraction.json").read_text()
            )
            request = dict(
                company_id=str(source.company.company_id),
                extraction_id=str(source.metadata.id),
                reporting_scope=scope,
                classification=dict(
                    classification=classification,
                    basis="CALLER_DECLARED",
                    declaration=f"Explicit {symbol} offline proof classification",
                    version=1,
                ),
                profile_id=profile,
                profile_version=1,
                alignment=dict(
                    fiscal_year_end_month=3,
                    declaration=(
                        "Caller declares March fiscal end and three-month quarters; "
                        "no start dates inferred."
                    ),
                ),
                current_period="2025-03",
                kpis=list(expectations[symbol]),
            )
            request_path = output / f"{symbol}-request.json"
            request_path.write_text(json.dumps(request, indent=2) + "\n")
            result_dir = output / symbol
            exit_code = cli(
                "calculate-sector-kpis",
                "--request",
                request_path,
                "--output",
                result_dir,
                allowed=(3,),
            )
            snapshot = SectorKPIEvaluation.model_validate_json(
                (result_dir / "normalized/calculation_snapshot.json").read_text()
            )
            assert (
                snapshot.source_payload_sha256
                == hashlib.sha256(source.model_dump_json().encode()).hexdigest()
            )
            for artifact in source.raw_artifacts:
                assert (
                    hashlib.sha256(
                        (output / "artifacts" / artifact.content_reference).read_bytes()
                    ).hexdigest()
                    == artifact.content_sha256
                )
            asyncio.run(check_history(database, source, snapshot))
            for result in snapshot.results:
                expected = expectations[symbol][result.definition.metric_id]
                assert result.status == expected.get("status", "AVAILABLE")
                if result.status == "AVAILABLE":
                    assert result.value == Decimal(expected["value"])
                    assert result.display_value == Decimal(expected["display"])
                    assert Fraction(result.exact_numerator, result.exact_denominator) == Fraction(
                        *map(int, expected["fraction"])
                    )
                report.append(
                    dict(
                        company=symbol,
                        metric=result.definition.metric_id,
                        expected=expected,
                        actual_value=str(result.value) if result.value is not None else None,
                        actual_display=str(result.display_value)
                        if result.display_value is not None
                        else None,
                        actual_fraction=[result.exact_numerator, result.exact_denominator],
                        status=result.status,
                        result="PASS",
                        raw_sha="PASS",
                        snapshot_sha="PASS",
                        persistence="PASS",
                        cli_exit=exit_code,
                    )
                )
    (output / "golden-proof.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"{len(report)} golden outcomes PASS; SHA and persistence PASS: {output}")


if __name__ == "__main__":
    main()
