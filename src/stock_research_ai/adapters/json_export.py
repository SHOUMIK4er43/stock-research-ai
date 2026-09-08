"""Deterministic JSON views of a single validated snapshot and verified evidence."""

import json
from pathlib import Path

from stock_research_ai.application.ports import RawArtifactStore
from stock_research_ai.domain.extraction import CompanyExtractionResult, Section

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
