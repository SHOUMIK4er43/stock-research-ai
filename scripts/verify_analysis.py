"""Offline Part 5B proof: two explicitly separate tracks, never mixed in one result.

TRACK 1 - REAL RETAINED EVIDENCE (INFY, YESBANK): only genuinely real fixtures are used.
Sections with no genuine retained evidence (MARKET_PERFORMANCE, TECHNICAL_STATE,
INDUSTRY_AND_MACRO, EXPECTATIONS, VARIANT_PERCEPTION, BULL_BASE_BEAR) are not requested.

TRACK 2 - SYNTHETIC MECHANICAL PROOF: a fully synthetic company/scenario fixture exercises
the sections and validator behaviors that real evidence cannot yet support.

Ingestion/calculation/valuation/document steps run through the real production CLI
subprocess, exactly like scripts/verify_valuation.py. The `analyze-company` CLI command
itself always reports "provider unavailable" (exit 4) in this build, since no LIVE
AnalystModel ships - proving that is one of this script's own checks. Actually generating
analysis snapshots for the rest of the proof calls `bootstrap.analyze_company` directly
with a FakeAnalystModel, which is the sanctioned way to exercise Part 5B's deterministic
core offline (never through production bootstrap without an explicit injected model).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
sys.path.insert(0, str(ROOT / "tests"))

from fakes.analyst import FakeAnalystModel  # noqa: E402

from stock_research_ai.adapters.sqlite import SQLiteDatabase  # noqa: E402
from stock_research_ai.application import analysis as analyst_engine  # noqa: E402
from stock_research_ai.bootstrap import analyze_company  # noqa: E402
from stock_research_ai.config import Settings  # noqa: E402
from stock_research_ai.domain.analysis import (  # noqa: E402
    AnalysisRequest,
    AnalystExecutionSpec,
)
from stock_research_ai.domain.calculations import CalculationRequest  # noqa: E402
from stock_research_ai.domain.extraction import CompanyExtractionResult  # noqa: E402
from stock_research_ai.domain.sector_kpis import SectorKPIRequest  # noqa: E402
from stock_research_ai.domain.valuation import ValuationRequest  # noqa: E402


def cli(database, *args, allowed=(0,), env=None):
    command = [
        sys.executable,
        "-m",
        "stock_research_ai",
        "--database",
        str(database),
        *map(str, args),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=env, cwd=ROOT)
    if result.returncode not in allowed:
        raise RuntimeError(f"{command}\n{result.stderr}")
    return result


async def build_bundle_and_evidence(settings, request):
    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(request.company_id)
        evidence = await analyst_engine.resolve_evidence(
            request,
            company,
            extractions=database.extractions,
            market=database.market,
            calculations=database.calculations,
            patterns=database.patterns,
            market_calculations=database.market_calculations,
            valuations=database.valuations,
            documents=database.documents,
            context=database.context,
        )
        return analyst_engine.assemble_bundle(request, evidence), evidence
    finally:
        await database.close()


async def build_bundle(settings, request):
    bundle, _ = await build_bundle_and_evidence(settings, request)
    return bundle


def restatement_claim(section, ref, statement):
    return dict(
        section=section,
        statement=statement,
        materiality="PRIMARY",
        citations=[
            dict(
                kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
            )
        ],
    )


async def run_real_company(output, symbol, file, bank, env):
    folder = output / symbol
    database = folder / "proof.sqlite3"
    settings = Settings(database_path=database, artifact_path=folder / "artifacts")
    cli(database, "init-db")
    evidence = folder / "evidence"
    cli(
        database,
        "extract-screener",
        f"https://www.screener.in/company/{symbol}/",
        "--html",
        FIX / file,
        "--output",
        evidence,
        allowed=(0, 3),
    )
    source = CompanyExtractionResult.model_validate_json(
        (evidence / "normalized/extraction.json").read_text()
    )

    growth_metric = "deposits" if bank else "sales"
    growth_id = "deposit_growth" if bank else "sales_growth"
    calc_req = CalculationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="BANK" if bank else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            calculations=[
                dict(
                    metric_id=growth_id,
                    formula_id="SOURCE_METRIC_GROWTH",
                    source_metric=growth_metric,
                    current_period="2025-03",
                    comparison="ANNUAL_YOY",
                )
            ],
        )
    )
    calc_path = folder / "calc_request.json"
    calc_path.write_text(calc_req.model_dump_json())
    calc_out = folder / "calc"
    cli(
        database,
        "calculate-financials",
        "--request",
        calc_path,
        "--output",
        calc_out,
        env=env,
        allowed=(0, 3),
    )
    calc_snapshot_id = json.loads((calc_out / "snapshot.json").read_text())["id"]

    kpis = (
        ("DEPOSIT_GROWTH", "AVERAGE_ASSETS_RETURN", "NIM", "GNPA", "NNPA", "CASA_RATIO")
        if bank
        else ("SALES_GROWTH", "NET_PROFIT_GROWTH", "OPERATING_MARGIN")
    )
    kpi_req = SectorKPIRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            classification=dict(
                classification="BANK" if bank else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id="BANK_PROFILE" if bank else "IT_SERVICES_PROFILE",
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03",
            kpis=kpis,
        )
    )
    kpi_path = folder / "kpi_request.json"
    kpi_path.write_text(kpi_req.model_dump_json())
    kpi_out = folder / "kpi"
    cli(
        database,
        "calculate-sector-kpis",
        "--request",
        kpi_path,
        "--output",
        kpi_out,
        env=env,
        allowed=(0, 3),
    )
    kpi_snapshot_id = json.loads((kpi_out / "normalized/calculation_snapshot.json").read_text())[
        "id"
    ]

    def pin(metric, annual=False):
        obs = [
            o
            for rows in (source.top_metrics, source.profit_loss, source.balance_sheet)
            for o in rows
            if o.metric_id == metric and (not annual or o.period.fiscal_label == "Mar 2026")
        ]
        return dict(
            kind="FINANCIAL", snapshot_id=str(source.metadata.id), observation_id=str(obs[-1].id)
        )

    val_req = ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="BANK_PROFILE" if bank else "IT_SERVICES_PROFILE",
            classification=dict(
                classification="BANK" if bank else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            selections=[
                dict(
                    method="PRICE_TO_EARNINGS",
                    price=pin("current_price"),
                    denominator=pin("eps", True),
                ),
                dict(
                    method="PRICE_TO_BOOK",
                    price=pin("current_price"),
                    denominator=pin("book_value"),
                ),
            ],
        )
    )
    val_path = folder / "valuation_request.json"
    val_path.write_text(val_req.model_dump_json())
    val_out = folder / "valuation"
    cli(
        database,
        "evaluate-valuation",
        "--request",
        val_path,
        "--output",
        val_out,
        allowed=(0, 3),
        env=env,
    )
    valuation_snapshot_id = json.loads(
        (val_out / "normalized/valuation_snapshot.json").read_text()
    )["id"]

    document_chunk_refs = ()
    if bank:
        doc_out = folder / "document"
        cli(
            database,
            "ingest-document",
            "--company",
            str(source.company.company_id),
            "--url",
            "https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf",
            "--type",
            "investor-presentation",
            "--file",
            FIX / "documents/yesbank_presentation.pdf",
            "--output",
            doc_out,
            env=env,
        )
        doc_payload = json.loads((doc_out / "normalized/document.json").read_text())
        document_chunk_refs = ((doc_payload["version_id"], doc_payload["chunks"][0]["id"]),)

    sections = [
        "COMPANY_OVERVIEW",
        "HISTORICAL_FUNDAMENTALS",
        "SECTOR_KPIS",
        "VALUATION",
        "OWNERSHIP_AND_SHAREHOLDING",
        "BUSINESS_MODEL",
        "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        "INDUSTRY_AND_MACRO",
    ]
    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="BANK" if bank else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id="BANK_PROFILE" if bank else "IT_SERVICES_PROFILE",
            financial_extraction_id=str(source.metadata.id),
            calculation_snapshot_ids=(calc_snapshot_id,),
            sector_kpi_snapshot_ids=(kpi_snapshot_id,),
            valuation_snapshot_ids=(valuation_snapshot_id,),
            document_chunk_refs=document_chunk_refs,
            sections=sections,
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-real-proof", template_version=1)
    bundle = await build_bundle(settings, request)
    claims = []
    for ref in bundle.sources:
        if ref.kind == "COMPANY":
            claims.append(
                restatement_claim("COMPANY_OVERVIEW", ref, f"{symbol} is a registered company.")
            )
        if ref.kind == "FINANCIAL_CALCULATION" and ref.item_key == growth_id:
            claims.append(
                restatement_claim(
                    "HISTORICAL_FUNDAMENTALS", ref, f"{growth_metric} growth evidence is cited."
                )
            )
        if ref.kind == "VALUATION":
            claims.append(
                restatement_claim("VALUATION", ref, "Observed valuation evidence is cited.")
            )
        if ref.kind == "DOCUMENT_CHUNK":
            claims.append(
                restatement_claim(
                    "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
                    ref,
                    "Investor presentation evidence is cited.",
                )
            )
    model = FakeAnalystModel(claims=claims)
    snapshot = await analyze_company(
        settings, request, execution, folder / "analysis", model=model, provider_kind="FAKE"
    )
    return {
        "symbol": symbol,
        "track": "REAL_RETAINED_EVIDENCE",
        "section_coverage": {c.section: c.status for c in snapshot.section_coverage},
        "accepted_claims": len(snapshot.claims),
        "rejected_claims": len(snapshot.rejected),
        "bundle_sha256": snapshot.bundle_sha256,
    }


async def run_synthetic(output, env):
    folder = output / "SYNTHETIC"
    database = folder / "proof.sqlite3"
    settings = Settings(database_path=database, artifact_path=folder / "artifacts")
    cli(database, "init-db")
    evidence = folder / "evidence"
    cli(
        database,
        "extract-screener",
        "https://www.screener.in/company/INFY/",
        "--html",
        FIX / "valuation/synthetic.html",
        "--output",
        evidence,
        allowed=(0, 3),
    )
    source = CompanyExtractionResult.model_validate_json(
        (evidence / "normalized/extraction.json").read_text()
    )

    def pin(metric):
        obs = [
            o
            for rows in (source.top_metrics, source.profit_loss, source.balance_sheet)
            for o in rows
            if o.metric_id == metric
        ]
        return dict(
            kind="FINANCIAL", snapshot_id=str(source.metadata.id), observation_id=str(obs[-1].id)
        )

    def assumption(name, value):
        unit = (
            "CURRENCY"
            if name.startswith("fcff_") or name in {"net_debt", "starting_fcff"}
            else "SHARES"
            if name == "diluted_shares"
            else "YEARS"
            if name == "horizon"
            else "DECIMAL_RATE"
        )
        return dict(
            assumption_id=str(uuid4()),
            semantic=name,
            value=value,
            unit=unit,
            currency="INR" if unit == "CURRENCY" else None,
            provenance_basis="CALLER_DECLARED",
            declaration="Explicit synthetic scenario input",
        )

    dcf_values = dict(
        fcff_1="110",
        fcff_2="121",
        horizon="2",
        wacc="0.10",
        terminal_growth="0",
        net_debt="200",
        diluted_shares="10",
    )
    reverse_values = dict(
        starting_fcff="100",
        horizon="2",
        wacc="0.10",
        terminal_growth="0",
        net_debt="200",
        diluted_shares="10",
        lower_growth="0",
        upper_growth="0.2",
    )
    val_req = ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="IT_SERVICES_PROFILE",
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit synthetic classification",
            ),
            selections=[
                dict(
                    method="PRICE_TO_EARNINGS", price=pin("current_price"), denominator=pin("eps")
                ),
                dict(
                    method="FCFF_DCF",
                    scenario=dict(
                        scenario_id=str(uuid4()),
                        label="BASE",
                        method="FCFF_DCF",
                        assumptions=[assumption(k, v) for k, v in dcf_values.items()],
                    ),
                ),
                dict(
                    method="IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE",
                    price=pin("current_price"),
                    scenario=dict(
                        scenario_id=str(uuid4()),
                        label="MARKET_IMPLIED",
                        method="IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE",
                        assumptions=[assumption(k, v) for k, v in reverse_values.items()],
                    ),
                ),
            ],
        )
    )
    val_path = folder / "valuation_request.json"
    val_path.write_text(val_req.model_dump_json())
    val_out = folder / "valuation"
    cli(database, "evaluate-valuation", "--request", val_path, "--output", val_out, env=env)
    valuation_snapshot_id = json.loads(
        (val_out / "normalized/valuation_snapshot.json").read_text()
    )["id"]

    calc_req = CalculationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit synthetic classification",
            ),
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            calculations=[
                dict(
                    metric_id="eps_growth",
                    formula_id="SOURCE_METRIC_GROWTH",
                    source_metric="eps",
                    current_period="2025-03",
                    comparison="ANNUAL_YOY",
                )
            ],
        )
    )
    calc_path = folder / "calc_request.json"
    calc_path.write_text(calc_req.model_dump_json())
    calc_out = folder / "calc"
    cli(
        database,
        "calculate-financials",
        "--request",
        calc_path,
        "--output",
        calc_out,
        env=env,
        allowed=(0, 3),
    )
    calc_snapshot_id = json.loads((calc_out / "snapshot.json").read_text())["id"]

    sections = ["VALUATION", "EXPECTATIONS", "VARIANT_PERCEPTION", "BULL_BASE_BEAR_INTERPRETATION"]
    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit synthetic classification",
            ),
            profile_id="IT_SERVICES_PROFILE",
            financial_extraction_id=str(source.metadata.id),
            calculation_snapshot_ids=(calc_snapshot_id,),
            valuation_snapshot_ids=(valuation_snapshot_id,),
            sections=sections,
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-synthetic-proof", template_version=1)
    bundle, resolved = await build_bundle_and_evidence(settings, request)

    def valuation_refs_by_category(category):
        return [
            r
            for r in bundle.sources
            if r.kind == "VALUATION" and analyst_engine._valuation_category(resolved, r) == category
        ]

    def valuation_refs_by_label(label):
        return [
            r
            for r in bundle.sources
            if r.kind == "VALUATION"
            and analyst_engine._valuation_scenario_label(resolved, r) == label
        ]

    claims = []
    for ref in valuation_refs_by_category("OBSERVED_MULTIPLE_CALCULATION"):
        claims.append(restatement_claim("VALUATION", ref, "A pinned observed multiple is cited."))
    for ref in valuation_refs_by_category("SCENARIO_CALCULATION"):
        claims.append(restatement_claim("VALUATION", ref, "A pinned DCF scenario result is cited."))
    for ref in valuation_refs_by_category("SCENARIO_EXPECTATION"):
        claims.append(
            restatement_claim("EXPECTATIONS", ref, "A market-implied expectation result is cited.")
        )
    for label in ("BASE", "MARKET_IMPLIED"):
        for ref in valuation_refs_by_label(label):
            claims.append(
                restatement_claim(
                    "BULL_BASE_BEAR_INTERPRETATION",
                    ref,
                    f"The {label} scenario's own inputs are cited.",
                )
            )

    calc_ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    expectation_ref = valuation_refs_by_category("SCENARIO_EXPECTATION")[0]
    reverse_scenario = val_req.selections[2].scenario
    claims.append(
        dict(
            section="VARIANT_PERCEPTION",
            statement="Market-implied and fundamental evidence are compared for this scenario.",
            materiality="PRIMARY",
            citations=[
                dict(
                    kind=expectation_ref.kind,
                    owning_snapshot_id=str(expectation_ref.owning_snapshot_id),
                    item_key=expectation_ref.item_key,
                ),
                dict(
                    kind=calc_ref.kind,
                    owning_snapshot_id=str(calc_ref.owning_snapshot_id),
                    item_key=calc_ref.item_key,
                ),
            ],
            dependency_assumption_ids=[str(a.assumption_id) for a in reverse_scenario.assumptions],
        )
    )
    model = FakeAnalystModel(claims=claims)
    snapshot = await analyze_company(
        settings, request, execution, folder / "analysis", model=model, provider_kind="FAKE"
    )
    return {
        "symbol": "SYNTHETIC",
        "track": "SYNTHETIC_MECHANICAL_PROOF",
        "section_coverage": {c.section: c.status for c in snapshot.section_coverage},
        "accepted_claims": len(snapshot.claims),
        "rejected_claims": len(snapshot.rejected),
        "bundle_sha256": snapshot.bundle_sha256,
    }


async def main_async(output):
    output.mkdir(parents=True, exist_ok=False)
    reports = []
    for symbol, file, bank in (
        ("INFY", "screener/infy_standalone.html", False),
        ("YESBANK", "screener/yesbank_consolidated.html", True),
    ):
        env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(output / symbol / "artifacts"))
        reports.append(await run_real_company(output, symbol, file, bank, env))
    env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(output / "SYNTHETIC" / "artifacts"))
    reports.append(await run_synthetic(output, env))

    # Prove the production CLI itself never silently substitutes a provider.
    cli_env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(output / "INFY/artifacts"))
    dummy_request = output / "INFY/dummy_analysis_request.json"
    dummy_request.write_text(
        json.dumps(
            dict(
                company_id=str(uuid4()),
                classification=dict(
                    classification="NON_FINANCIAL", basis="CALLER_DECLARED", declaration="x"
                ),
                profile_id="IT_SERVICES_PROFILE",
                sections=["COMPANY_OVERVIEW"],
            )
        )
    )
    dummy_execution = output / "INFY/dummy_execution.json"
    dummy_execution.write_text(json.dumps(dict(template_id="v1", template_version=1)))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_research_ai",
            "--database",
            str(output / "INFY/proof.sqlite3"),
            "analyze-company",
            "--request",
            str(dummy_request),
            "--execution",
            str(dummy_execution),
            "--output",
            str(output / "INFY/cli-should-refuse"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=cli_env,
        cwd=ROOT,
    )
    cli_proof = {
        "returncode": result.returncode,
        "stderr_mentions_provider": "provider" in result.stderr.lower(),
        "expected_returncode": 4,
    }
    assert result.returncode == 4, result.stderr

    (output / "proof.json").write_text(
        json.dumps({"tracks": reports, "cli_provider_unavailable_proof": cli_proof}, indent=2)
        + "\n"
    )
    print(f"Track 1 (REAL): {reports[0]['symbol']}, {reports[1]['symbol']}")
    print(f"Track 2 (SYNTHETIC): {reports[2]['symbol']}")
    print("CLI analyze-company without a LIVE provider correctly refused with exit 4.")
    print(f"Wrote {output / 'proof.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main_async(args.output.resolve()))


if __name__ == "__main__":
    main()
