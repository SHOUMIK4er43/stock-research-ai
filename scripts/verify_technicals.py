"""Replay synthetic evidence through production CLI; compare frozen independent goldens.

Never generates expected values. No credentials or network are used.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.technicals import PUBLIC_INDICATORS, TechnicalSnapshot

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"


async def verify_history(database, snapshot):
    db = SQLiteDatabase(database)
    try:
        assert snapshot in await db.market_calculations.technical_history(
            snapshot.request.extraction_id
        )
        assert (
            await db.market_calculations.market_calculation_history(snapshot.request.extraction_id)
            == ()
        )
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    database = root / "proof.sqlite3"
    env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(root / "artifacts"))
    commands = []

    def cli(*args):
        command = [sys.executable, "-m", "stock_research_ai", "--database", str(database), *args]
        commands.append(command)
        subprocess.run(command, check=True, capture_output=True, text=True, env=env, cwd=ROOT)

    cli("init-db")
    inputs = json.loads((FIX / "technicals/inputs.json").read_text())
    expectations = json.loads((FIX / "technicals/expected.json").read_text())
    reports = []
    summaries = []
    cases = [(s, "YESBANK") for s in inputs] + [
        ("mixed", symbol) for symbol in ("INFY", "NIFTY 50", "NIFTY BANK")
    ]
    for scenario, symbol in cases:
        folder = root / (scenario + "-" + symbol.replace(" ", "-"))
        folder.mkdir()
        rows = inputs[scenario]
        candles = folder / "synthetic-candles.json"
        candles.write_text(
            json.dumps(
                {
                    "status": "success",
                    "data": {"candles": [[r[0] + "T00:00:00+0530", *r[1:]] for r in rows]},
                }
            )
        )
        evidence = folder / "evidence"
        cli(
            "ingest-market-data",
            "--symbol",
            symbol,
            "--kind",
            "INDEX" if symbol.startswith("NIFTY") else "EQUITY",
            "--start",
            rows[0][0],
            "--end",
            rows[-1][0],
            "--master-file",
            str(FIX / "market/synthetic_instruments.csv"),
            "--candles-file",
            str(candles),
            "--synthetic",
            "--expected-sessions",
            ",".join(r[0] for r in rows),
            "--output",
            str(evidence),
        )
        source = json.loads((evidence / "normalized/extraction.json").read_text())
        req = folder / "request.json"
        req.write_text(
            json.dumps(
                {
                    "extraction_id": source["id"],
                    "instrument_id": source["instrument"]["id"],
                    "start_date": rows[0][0],
                    "end_date": rows[-1][0],
                    "indicators": PUBLIC_INDICATORS,
                },
                indent=2,
            )
        )
        output = folder / "calculation"
        cli("calculate-technicals", "--request", str(req), "--output", str(output))
        snapshot = TechnicalSnapshot.model_validate_json(
            (output / "normalized/calculation_snapshot.json").read_text()
        )
        parsed_source = MarketExtractionResult.model_validate(source)
        assert (
            snapshot.source.payload_sha256
            == hashlib.sha256(parsed_source.model_dump_json().encode()).hexdigest()
        )
        for artifact in parsed_source.raw_artifacts:
            assert (
                hashlib.sha256(
                    (root / "artifacts" / artifact.content_reference).read_bytes()
                ).hexdigest()
                == artifact.content_sha256
            )
        asyncio.run(verify_history(database, snapshot))
        series = {s.definition.indicator_id: s for s in snapshot.series}
        for golden in (e for e in expectations if e["scenario"] == scenario):
            p = series[golden["indicator"]].points[golden["observation"] - 1]
            with localcontext() as ctx:
                ctx.prec = 110
                passed = p.value is not None and (
                    abs(p.value - Decimal(golden["expected"])) <= Decimal(golden["tolerance"])
                )
            reports.append(
                dict(
                    golden,
                    symbol=symbol,
                    actual=str(p.value),
                    numerical_method=p.numerical_method,
                    result="PASS" if passed else "FAIL",
                )
            )
        summaries.append(
            {
                "scenario": scenario,
                "symbol": symbol,
                "snapshot_id": str(snapshot.id),
                "extraction_id": str(snapshot.source.extraction_id),
                "source_reference": snapshot.source.model_dump(mode="json"),
                "snapshot_sha256": hashlib.sha256(
                    (output / "normalized/calculation_snapshot.json").read_bytes()
                ).hexdigest(),
                "point_graph": "PASS",
                "raw_sha_and_source_hash": "PASS",
                "typed_persistence": "PASS",
                "adjustment_basis": snapshot.adjustment_basis,
                "corporate_action_coverage": snapshot.corporate_action_coverage,
                "first_available": {
                    name: {
                        "observation": series[name].definition.first_observation,
                        "date": next(
                            p.date.isoformat()
                            for p in series[name].points
                            if p.status == "AVAILABLE"
                        ),
                    }
                    for name in PUBLIC_INDICATORS
                },
            }
        )
    (root / "checkpoints.json").write_text(json.dumps(reports, indent=2) + "\n")
    (root / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
    (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    failures = sum(r["result"] != "PASS" for r in reports)
    print(f"{len(cases)} offline CLI cases; {len(reports)} checkpoints; {failures} failures")
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
