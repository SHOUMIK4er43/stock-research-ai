"""Offline production CLI replay against pre-authored valuation goldens. No expected writes."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.valuation import METHODS, ValuationSnapshot

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"


async def history(database, snapshot):
    db = SQLiteDatabase(database)
    try:
        assert snapshot in await db.valuations.valuation_history(snapshot.request.company_id)
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    database = output / "proof.sqlite3"
    env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(output / "artifacts"))
    inputs = json.loads((FIX / "valuation/inputs.json").read_text())
    golden = json.loads((FIX / "valuation/expected.json").read_text())
    commands, reports = [], []

    def cli(*args, allowed=(0,)):
        command = [
            sys.executable,
            "-m",
            "stock_research_ai",
            "--database",
            str(database),
            *map(str, args),
        ]
        commands.append(command)
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, env=env, cwd=ROOT
        )
        if result.returncode not in allowed:
            raise RuntimeError(result.stderr)

    def assumption(name, value):
        unit = (
            "CURRENCY"
            if name.startswith("fcff_") or name in {"net_debt", "starting_fcff"}
            else "SHARES"
            if name == "diluted_shares"
            else "YEARS"
            if name == "horizon"
            else "MULTIPLE_X"
            if name.startswith("exit_")
            else "DECIMAL_RATE"
        )
        return dict(
            assumption_id=str(uuid4()),
            semantic=name,
            value=value,
            unit=unit,
            currency="INR" if unit == "CURRENCY" else None,
            provenance_basis="CALLER_DECLARED",
            declaration="Explicit independent synthetic proof assumption",
        )

    cli("init-db")
    for symbol, file, bank in (
        ("SYNTHETIC", "valuation/synthetic.html", False),
        ("INFY", "screener/infy_standalone.html", False),
        ("YESBANK", "screener/yesbank_consolidated.html", True),
    ):
        folder = output / symbol
        evidence = folder / "evidence"
        cli(
            "extract-screener",
            f"https://www.screener.in/company/{'INFY' if symbol == 'SYNTHETIC' else symbol}/",
            "--html",
            FIX / file,
            "--output",
            evidence,
            allowed=(0, 3),
        )
        source = CompanyExtractionResult.model_validate_json(
            (evidence / "normalized/extraction.json").read_text()
        )
        sha = hashlib.sha256(source.model_dump_json().encode()).hexdigest()
        for artifact in source.raw_artifacts:
            assert (
                hashlib.sha256(
                    (output / "artifacts" / artifact.content_reference).read_bytes()
                ).hexdigest()
                == artifact.content_sha256
            )

        def pin(metric, annual=False, source=source):
            obs = [
                o
                for rows in (source.top_metrics, source.profit_loss, source.balance_sheet)
                for o in rows
                if o.metric_id == metric and (not annual or o.period.fiscal_label == "Mar 2026")
            ]
            return dict(
                kind="FINANCIAL",
                snapshot_id=str(source.metadata.id),
                observation_id=str(obs[-1].id),
            )

        cases = [
            (method, method == METHODS[5] if symbol == "SYNTHETIC" else bank)
            for method in (METHODS if symbol == "SYNTHETIC" else METHODS[:2])
        ]
        if symbol == "YESBANK":
            cases += [(METHODS[2], True), (METHODS[3], True)]
        if symbol == "INFY":
            cases += [(METHODS[5], False)]
        for idx, (method, use_bank) in enumerate(cases):
            selection = dict(method=method)
            if method != METHODS[2]:
                selection["price"] = pin("current_price")
            if method in {METHODS[0], METHODS[4]}:
                selection["denominator"] = pin("eps", symbol != "SYNTHETIC")
            if method in {METHODS[1], METHODS[5]}:
                selection["denominator"] = pin("bvps" if symbol == "SYNTHETIC" else "book_value")
            if method in METHODS[2:]:
                group = {
                    METHODS[2]: "dcf",
                    METHODS[3]: "reverse",
                    METHODS[4]: "eps_expectation",
                    METHODS[5]: "bvps_expectation",
                }[method]
                selection["scenario"] = dict(
                    scenario_id=str(uuid4()),
                    label="CUSTOM",
                    method=method,
                    assumptions=[assumption(k, v) for k, v in inputs[group].items()],
                )
            request = dict(
                company_id=str(source.company.company_id),
                financial_extraction_id=str(source.metadata.id),
                reporting_scope=source.company.reporting_scope,
                profile_id="BANK_PROFILE" if use_bank else "IT_SERVICES_PROFILE",
                classification=dict(
                    classification="BANK" if use_bank else "NON_FINANCIAL",
                    basis="CALLER_DECLARED",
                    declaration="Explicit proof classification",
                ),
                selections=[selection],
            )
            req = folder / f"request-{idx}.json"
            req.write_text(json.dumps(request, indent=2))
            destination = folder / f"calculation-{idx}"
            cli("evaluate-valuation", "--request", req, "--output", destination, allowed=(0, 3))
            snapshot = ValuationSnapshot.model_validate_json(
                (destination / "normalized/valuation_snapshot.json").read_text()
            )
            assert snapshot.sources[0].payload_sha256 == sha
            asyncio.run(history(database, snapshot))
            result = snapshot.results[0]
            expected_status = (
                "AVAILABLE"
                if symbol == "SYNTHETIC" or method == METHODS[0]
                else "UNVERIFIED_METRIC_SEMANTICS"
                if method == METHODS[1]
                else "NOT_APPLICABLE"
            )
            assert result.status == expected_status
            comparisons = []
            for actual in result.values:
                if symbol == "SYNTHETIC":
                    expected = Decimal(golden[method][actual.name])
                    tolerance = Decimal(0)
                else:
                    n, d = golden["real"][symbol]["pe_fraction"]
                    with localcontext() as ctx:
                        ctx.prec = 110
                        expected = Decimal(n) / Decimal(d)
                    tolerance = Decimal("1e-45")
                with localcontext() as ctx:
                    ctx.prec = 110
                    passed = abs(expected - actual.value) <= tolerance
                assert passed, (symbol, method, actual.name)
                comparisons.append(
                    dict(
                        name=actual.name,
                        expected=str(expected),
                        actual=str(actual.value),
                        numerical_method=actual.numerical_method,
                        tolerance=str(tolerance),
                        result="PASS",
                    )
                )
            if method == METHODS[3] and result.status == "AVAILABLE":
                assert result.solver.iterations == 1 and result.solver.final_residual == 0
            reports.append(
                dict(
                    symbol=symbol,
                    method=method,
                    expected_status=expected_status,
                    actual_status=result.status,
                    comparisons=comparisons,
                    solver=result.solver.model_dump(mode="json") if result.solver else None,
                    inputs=[i.model_dump(mode="json") for i in result.inputs],
                    source_sha=sha,
                    raw_sha=[a.content_sha256 for a in source.raw_artifacts],
                    snapshot_id=str(snapshot.id),
                    source_reported_multiples=[
                        o.model_dump(mode="json") for o in snapshot.source_reported_multiples
                    ],
                    issues=[i.model_dump(mode="json") for i in result.issues],
                    result="PASS",
                )
            )
    (output / "proof.json").write_text(json.dumps(reports, indent=2) + "\n")
    (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    print(
        f"{len(reports)} production CLI cases PASS; "
        "all component comparisons, SHA and history checks PASS"
    )


if __name__ == "__main__":
    main()
