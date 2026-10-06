"""Six independent real-source pattern proofs through the offline production CLI."""

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
from stock_research_ai.domain.patterns import PatternSnapshot

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"


async def verify_history(database, snapshot):
    db = SQLiteDatabase(database)
    try:
        assert snapshot in await db.patterns.pattern_history(snapshot.request.extraction_id)
        for finding in snapshot.findings:
            for ref in finding.calculations:
                calculation = await db.calculations.get_calculation(ref.snapshot_id)
                assert (
                    hashlib.sha256(calculation.model_dump_json().encode()).hexdigest()
                    == ref.payload_sha256
                )
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("Output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    database = output / "proof.sqlite3"
    inputs = json.loads((FIX / "patterns/expected_inputs.json").read_text())
    expected = json.loads((FIX / "patterns/expected_findings.json").read_text())
    report = []
    with tempfile.TemporaryDirectory(prefix="patterns-offline-") as temp:
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
            run = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "stock_research_ai",
                    "--database",
                    str(database),
                    *map(str, args),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            if run.returncode not in allowed:
                raise RuntimeError(run.stderr or run.stdout)

        cli("init-db")
        for symbol, filename, scope, profile, classification, suffix in (
            ("INFY", "infy_standalone", "standalone", "IT_SERVICES_PROFILE", "NON_FINANCIAL", "/"),
            (
                "YESBANK",
                "yesbank_consolidated",
                "consolidated",
                "BANK_PROFILE",
                "BANK",
                "/consolidated/",
            ),
        ):
            evidence = output / f"{symbol}-evidence"
            cli(
                "extract-screener",
                f"https://www.screener.in/company/{symbol}{suffix}",
                "--html",
                FIX / f"screener/{filename}.html",
                "--output",
                evidence,
                allowed=(0, 3),
            )
            source = CompanyExtractionResult.model_validate_json(
                (evidence / "normalized/extraction.json").read_text()
            )
            for artifact in source.raw_artifacts:
                assert (
                    hashlib.sha256(
                        (output / "artifacts" / artifact.content_reference).read_bytes()
                    ).hexdigest()
                    == artifact.content_sha256
                )
            for name, expected_inputs in inputs[symbol].items():
                period = expected_inputs["period"]
                # Independent source input check. No production formula generates expectations.
                for metric, values in expected_inputs.items():
                    if metric == "period":
                        continue
                    field = (
                        source.cashflow
                        if metric == "cash_operating"
                        else source.balance_sheet
                        if metric == "deposits"
                        else source.profit_loss
                    )
                    year = int(period[:4])
                    years = list(range(year, year - len(values), -1))
                    if name == "REPEATED_NEGATIVE_CFO":
                        years.reverse()
                    for y, value in zip(years, values, strict=True):
                        (observation,) = [
                            o
                            for o in field
                            if o.metric_id == metric and o.period.fiscal_label == f"Mar {y}"
                        ]
                        assert observation.value == Decimal(value)
                request = dict(
                    company_id=str(source.company.company_id),
                    extraction_id=str(source.metadata.id),
                    classification=dict(
                        classification=classification,
                        basis="CALLER_DECLARED",
                        declaration=f"Explicit {symbol} proof classification",
                        version=1,
                    ),
                    profile_id=profile,
                    profile_version=1,
                    reporting_scope=scope,
                    alignment=dict(
                        fiscal_year_end_month=3,
                        declaration=(
                            "Caller-declared March fiscal endpoints; duration starts unknown."
                        ),
                    ),
                    current_period=period,
                    patterns=[dict(pattern_id=name, version=1)],
                    calculation_mode="AUTO_CREATE",
                )
                request_path = output / f"{symbol}-{name}-request.json"
                request_path.write_text(json.dumps(request, indent=2) + "\n")
                destination = output / symbol / name
                cli("evaluate-patterns", "--request", request_path, "--output", destination)
                snapshot = PatternSnapshot.model_validate_json(
                    (destination / "normalized/pattern_snapshot.json").read_text()
                )
                assert (
                    snapshot.source_payload_sha256
                    == hashlib.sha256(source.model_dump_json().encode()).hexdigest()
                )
                asyncio.run(verify_history(database, snapshot))
                (finding,) = snapshot.findings
                golden = expected[name]
                assert (
                    finding.evaluation_status == golden["status"]
                    and finding.rule_outcome == golden["outcome"]
                )
                if "fraction" in golden:
                    value = finding.observed[-1]
                    assert Fraction(value.numerator, value.denominator) == Fraction(
                        *golden["fraction"]
                    )
                    assert value.value == Decimal(golden["value"]) and value.display == Decimal(
                        golden["display"]
                    )
                    assert value.unit == golden["unit"]
                report.append(
                    dict(
                        company=symbol,
                        pattern=name,
                        expected=golden,
                        actual=finding.model_dump(mode="json"),
                        execution=snapshot.execution.model_dump(mode="json"),
                        snapshot_id=str(snapshot.id),
                        result="PASS",
                        raw_sha="PASS",
                        source_sha="PASS",
                        persistence="PASS",
                    )
                )
                print(f"{symbol} {name}: {finding.evaluation_status}/{finding.rule_outcome} PASS")
    (output / "golden-proof.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Six real-source golden proofs passed: {output}")


if __name__ == "__main__":
    main()
