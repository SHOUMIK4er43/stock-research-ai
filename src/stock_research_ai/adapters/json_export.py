"""Deterministic JSON views of a single validated snapshot and verified evidence."""

import json
from pathlib import Path

from stock_research_ai.application.ports import RawArtifactStore
from stock_research_ai.domain.calculations import CalculationSnapshot
from stock_research_ai.domain.documents import ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, Section
from stock_research_ai.domain.macro import MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.patterns import PatternSnapshot
from stock_research_ai.domain.publications import (
    PublicationSnapshot,
)
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation
from stock_research_ai.domain.technicals import TechnicalSnapshot
from stock_research_ai.domain.valuation import ValuationSnapshot

SECTION_FILES = {
    "quarterly": Section.QUARTERLY,
    "profit_loss": Section.PROFIT_LOSS,
    "balance_sheet": Section.BALANCE_SHEET,
    "cashflow": Section.CASH_FLOW,
    "ratios": Section.RATIOS,
    "shareholding": Section.SHAREHOLDING,
    "peers": Section.PEERS,
    "documents": Section.DOCUMENTS,
}


def check_output(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("Output must be a new or empty directory")


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


async def export_result(
    result: CompanyExtractionResult, output: Path, artifacts: RawArtifactStore
) -> None:
    check_output(output)
    # Verify all evidence before creating an export that could appear complete.
    raw = [(artifact, await artifacts.read(artifact)) for artifact in result.raw_artifacts]
    (output / "raw").mkdir(parents=True, exist_ok=True)
    (output / "normalized").mkdir()
    for artifact, content in raw:
        (output / "raw" / f"{artifact.content_sha256}.html").write_bytes(content)
    data = result.model_dump(mode="json")
    write_json(
        output / "raw/manifest.json",
        {
            "retrievals": data["retrievals"],
            "artifacts": data["raw_artifacts"],
            "export_paths": {
                str(a.id): f"raw/{a.content_sha256}.html" for a in result.raw_artifacts
            },
        },
    )
    write_json(output / "normalized/extraction.json", data)
    write_json(output / "coverage.json", data["coverage"])
    coverage = {c.section: c.model_dump(mode="json") for c in result.coverage.sections}
    common = {"schema_version": result.schema_version, "extraction_id": str(result.metadata.id)}
    write_json(
        output / "normalized/company.json",
        {
            **common,
            "company": data["company"],
            "top_metrics": data["top_metrics"],
            "coverage": [coverage[Section.COMPANY_IDENTITY], coverage[Section.TOP_METRICS]],
        },
    )
    for name, section in SECTION_FILES.items():
        write_json(
            output / "normalized" / f"{name}.json",
            {**common, "coverage": coverage[section], "data": data[name]},
        )


async def export_document(
    document: ParsedDocument, output: Path, artifacts: RawArtifactStore
) -> None:
    check_output(output)
    raw = await artifacts.read(document.artifact) if document.artifact else None
    (output / "raw").mkdir(parents=True, exist_ok=True)
    (output / "normalized").mkdir()
    artifact = document.artifact
    exported = None
    if raw is not None and artifact:
        extension = Path(artifact.content_reference).suffix
        exported = f"raw/{artifact.content_sha256}{extension}"
        (output / exported).write_bytes(raw)
    data = document.model_dump(mode="json")
    write_json(
        output / "raw/manifest.json",
        {"artifact": data["artifact"], "retrieval": data["retrieval"], "export_path": exported},
    )
    write_json(output / "normalized/document.json", data)
    for field in ("pages", "sections", "chunks"):
        write_json(
            output / "normalized" / f"{field}.json",
            {"version_id": str(document.version_id), "data": data[field]},
        )
    write_json(output / "coverage.json", data["coverage"])


async def export_market(
    result: MarketExtractionResult, output: Path, artifacts: RawArtifactStore
) -> None:
    check_output(output)
    raw = [(a, await artifacts.read(a)) for a in result.raw_artifacts]
    (output / "raw").mkdir(parents=True, exist_ok=True)
    (output / "normalized").mkdir()
    paths = {}
    for artifact, content in raw:
        path = f"raw/{artifact.content_sha256}.bin"
        (output / path).write_bytes(content)
        paths[str(artifact.id)] = path
    data = result.model_dump(mode="json")
    write_json(
        output / "raw/manifest.json",
        {
            "retrievals": data["retrievals"],
            "artifacts": data["raw_artifacts"],
            "export_paths": paths,
            "evidence_kind": result.request.evidence_kind,
        },
    )
    write_json(output / "normalized/extraction.json", data)
    write_json(output / "normalized/instrument.json", data["instrument"])
    write_json(output / "normalized/prices.json", data["observations"])
    write_json(
        output / "normalized/corporate_actions.json",
        {
            "status": result.coverage.corporate_action_status,
            "reason": result.coverage.corporate_actions,
            "data": data["corporate_actions"],
        },
    )
    write_json(output / "coverage.json", data["coverage"])


async def export_context(
    result: PublicationSnapshot | MacroSnapshot, output: Path, store: RawArtifactStore
) -> None:
    check_output(output)
    artifacts = (
        result.artifacts
        if isinstance(result, MacroSnapshot)
        else ((result.artifact,) if result.artifact else ())
    )
    raw = [(a, await store.read(a)) for a in artifacts]
    (output / "raw").mkdir(parents=True, exist_ok=True)
    (output / "normalized").mkdir()
    paths = {}
    for a, b in raw:
        path = f"raw/{a.content_sha256}.bin"
        (output / path).write_bytes(b)
        paths[str(a.id)] = path
    data = result.model_dump(mode="json")
    write_json(
        output / "raw/manifest.json",
        {
            "artifacts": [a.model_dump(mode="json") for a in artifacts],
            "retrievals": data.get("retrievals", [data.get("retrieval")]),
            "export_paths": paths,
            "evidence_kind": result.request.evidence_kind,
        },
    )
    write_json(output / "normalized/snapshot.json", data)
    write_json(output / "coverage.json", data["coverage"])
    for key in (
        ("series", "observations")
        if isinstance(result, MacroSnapshot)
        else ("identity", "passages", "subjects", "attachments")
    ):
        write_json(output / "normalized" / f"{key}.json", data[key])


def export_calculation(snapshot: CalculationSnapshot, output: Path) -> None:
    check_output(output)
    output.mkdir(parents=True)
    write_json(output / "snapshot.json", snapshot.model_dump(mode="json"))
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "results": {
                r.metric_id: {"status": r.status, "issues": r.issues} for r in snapshot.results
            },
        },
    )


def export_market_calculation(snapshot: MarketCalculationSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    data = snapshot.model_dump(mode="json")
    write_json(output / "normalized/calculation_snapshot.json", data)
    write_json(output / "normalized/results.json", data["results"])
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "results": [
                {
                    "metric_id": r.metric_id,
                    "window": r.window,
                    "status": r.status,
                    "issues": [i.model_dump(mode="json") for i in r.issues],
                }
                for r in snapshot.results
            ],
        },
    )


def export_sector_kpis(snapshot: SectorKPIEvaluation, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    data = snapshot.model_dump(mode="json")
    write_json(output / "normalized/calculation_snapshot.json", data)
    write_json(output / "profile.json", data["profile"])
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "results": [
                {
                    "metric_id": r.definition.metric_id,
                    "category": r.definition.category,
                    "status": r.status,
                    "issues": [i.model_dump(mode="json") for i in r.issues],
                }
                for r in snapshot.results
            ],
        },
    )
    for name, results in {
        "available_kpis": [
            r
            for r in data["results"]
            if r["status"] == "AVAILABLE" and r["definition"]["category"] == "SECTOR_KPI"
        ],
        "generic_financial": [
            r for r in data["results"] if r["definition"]["category"] == "GENERIC_SOURCE_METRIC"
        ],
        "unavailable_kpis": [
            r for r in data["results"] if r["status"] not in {"AVAILABLE", "NOT_APPLICABLE"}
        ],
        "not_applicable_kpis": [r for r in data["results"] if r["status"] == "NOT_APPLICABLE"],
    }.items():
        write_json(output / "normalized" / f"{name}.json", results)


def export_patterns(snapshot: PatternSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/pattern_snapshot.json", snapshot.model_dump(mode="json"))
    findings = [f.model_dump(mode="json") for f in snapshot.findings]
    groups = {
        "findings": findings,
        "triggered": [f for f in findings if f["rule_outcome"] == "TRIGGERED"],
        "not_triggered": [f for f in findings if f["rule_outcome"] == "NOT_TRIGGERED"],
        "observations": [f for f in findings if f["rule_outcome"] == "OBSERVATION_ONLY"],
        "unavailable": [
            f for f in findings if f["evaluation_status"] not in {"AVAILABLE", "NOT_APPLICABLE"}
        ],
        "not_applicable": [f for f in findings if f["evaluation_status"] == "NOT_APPLICABLE"],
    }
    for name, records in groups.items():
        write_json(output / "normalized" / f"{name}.json", records)
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "findings": [
                {
                    "pattern_id": f.definition.pattern_id,
                    "evaluation_status": f.evaluation_status,
                    "rule_outcome": f.rule_outcome,
                    "issues": [i.model_dump(mode="json") for i in f.issues],
                }
                for f in snapshot.findings
            ],
        },
    )


def export_technicals(snapshot: TechnicalSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/calculation_snapshot.json", snapshot.model_dump(mode="json"))
    requested = [s for s in snapshot.series if s.requested]
    write_json(
        output / "normalized/indicator_series.json", [s.model_dump(mode="json") for s in requested]
    )
    write_json(
        output / "normalized/latest.json",
        {
            "requested_terminal_date": snapshot.request.end_date.isoformat(),
            "results": [
                {
                    "indicator_id": s.definition.indicator_id,
                    "status": s.status,
                    "point": s.points[-1].model_dump(mode="json")
                    if s.points and s.points[-1].date == snapshot.request.end_date
                    else None,
                }
                for s in requested
            ],
        },
    )
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "issues": [i.model_dump(mode="json") for i in snapshot.issues],
            "indicators": [
                {
                    "indicator_id": s.definition.indicator_id,
                    "status": s.status,
                    "warmup_points": sum(p.status == "INSUFFICIENT_INPUT" for p in s.points),
                }
                for s in requested
            ],
        },
    )


def export_valuation(snapshot: ValuationSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/valuation_snapshot.json", snapshot.model_dump(mode="json"))
    for file, category in (
        ("multiples", "OBSERVED_MULTIPLE_CALCULATION"),
        ("scenarios", "SCENARIO_CALCULATION"),
        ("reverse_expectations", "SCENARIO_EXPECTATION"),
    ):
        write_json(
            output / "normalized" / (file + ".json"),
            {
                "snapshot_id": str(snapshot.id),
                "results": [
                    r.model_dump(mode="json")
                    for r in snapshot.results
                    if r.definition.category == category
                ],
            },
        )
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "methods": [
                {
                    "selection_index": r.selection_index,
                    "method": r.definition.method,
                    "status": r.status,
                    "issues": [i.model_dump(mode="json") for i in r.issues],
                }
                for r in snapshot.results
            ],
        },
    )
