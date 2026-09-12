"""Deterministic JSON views of a single validated snapshot and verified evidence."""

import json
from pathlib import Path

from stock_research_ai.application.ports import RawArtifactStore
from stock_research_ai.domain.analysis import AnalysisSnapshot
from stock_research_ai.domain.calculations import CalculationSnapshot
from stock_research_ai.domain.challenge import ChallengeSnapshot
from stock_research_ai.domain.documents import ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, Section
from stock_research_ai.domain.macro import MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.patterns import PatternSnapshot
from stock_research_ai.domain.publications import (
    PublicationSnapshot,
)
from stock_research_ai.domain.report import ReportSectionId, ResearchReportSnapshot
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


def export_analysis(snapshot: AnalysisSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/analysis_snapshot.json", snapshot.model_dump(mode="json"))
    write_json(
        output / "normalized/claims_by_section.json",
        {
            "snapshot_id": str(snapshot.id),
            "sections": {
                section: [
                    c.model_dump(mode="json") for c in snapshot.claims if c.section == section
                ]
                for section in sorted({c.section for c in snapshot.claims})
            },
        },
    )
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "sections": [c.model_dump(mode="json") for c in snapshot.section_coverage],
            "rejected_count": len(snapshot.rejected),
            "accepted_count": len(snapshot.claims),
        },
    )


def export_challenge(snapshot: ChallengeSnapshot, output: Path) -> None:
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/challenge_snapshot.json", snapshot.model_dump(mode="json"))
    write_json(
        output / "claim_reviews.json",
        {
            "snapshot_id": str(snapshot.id),
            "analysis_snapshot_id": str(snapshot.analysis_snapshot_id),
            "reviews": [r.model_dump(mode="json") for r in snapshot.claim_reviews],
        },
    )
    write_json(
        output / "section_reviews.json",
        {
            "snapshot_id": str(snapshot.id),
            "sections": [r.model_dump(mode="json") for r in snapshot.section_reviews],
        },
    )
    write_json(
        output / "findings.json",
        {
            "snapshot_id": str(snapshot.id),
            "findings": [
                f.model_dump(mode="json") for r in snapshot.claim_reviews for f in r.findings
            ],
        },
    )
    write_json(
        output / "uncited_evidence.json",
        {
            "snapshot_id": str(snapshot.id),
            "uncited_relevant_evidence": [
                e.model_dump(mode="json")
                for r in snapshot.claim_reviews
                for e in r.additional_relevant_evidence
            ],
        },
    )
    write_json(
        output / "coverage.json",
        {
            "snapshot_id": str(snapshot.id),
            "independence_basis": snapshot.independence_basis,
            "relevance_registry_version": snapshot.relevance_registry_version,
            "outcomes": {
                str(r.analyst_claim_id): r.outcome for r in snapshot.claim_reviews
            },
        },
    )


_COMPANY_RESEARCH_JSON_GROUPS: dict[str, tuple[ReportSectionId, ...]] = {
    "business": ("BUSINESS_MODEL", "INDUSTRY_AND_COMPETITIVE_STRUCTURE"),
    "financials": (
        "ANNUAL_FINANCIAL_PERFORMANCE", "QUARTERLY_PERFORMANCE_AND_INFLECTION",
        "BALANCE_SHEET_ANALYSIS", "CASH_FLOW_AND_EARNINGS_QUALITY",
    ),
    "calculations": ("CALCULATION_APPENDIX",),
    "sector_kpis": ("SECTOR_SPECIFIC_KPI_ANALYSIS",),
    "management": (
        "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE", "CONCALL_INTELLIGENCE",
        "MANAGEMENT_GUIDANCE_AND_COMMITMENTS", "MANAGEMENT_GUIDANCE_VS_ACTUAL",
        "MANAGEMENT_NARRATIVE_CHANGE_DETECTION", "AGM_INTELLIGENCE",
        "ANNUAL_REPORT_INTELLIGENCE", "INVESTOR_PRESENTATION_INTELLIGENCE",
        "MANAGEMENT_CREDIBILITY_CONSISTENCY", "MANAGEMENT_STATEMENT_APPENDIX",
    ),
    "shareholding": ("OWNERSHIP_AND_SHAREHOLDING",),
    "forensics": ("FORENSIC_AND_QUALITY_REVIEW", "GOVERNANCE_AND_CAPITAL_ALLOCATION"),
    "market": ("MARKET_AND_PRICE_ANALYSIS",),
    "technicals": ("TECHNICAL_ANALYSIS",),
    "valuation": ("VALUATION", "MARKET_IMPLIED_EXPECTATIONS"),
    "scenarios": ("BULL_BASE_BEAR_SCENARIOS",),
    "variant_perception": ("VARIANT_PERCEPTION",),
    "catalysts": ("CATALYSTS",),
    "risks": ("RISK_REGISTER",),
    "challenger": ("INDEPENDENT_CHALLENGER_REVIEW", "CHALLENGER_APPENDIX"),
    "monitoring": ("MONITORING_DASHBOARD",),
    "evidence": ("EVIDENCE_APPENDIX",),
}


def _sections_payload(
    snapshot: ResearchReportSnapshot, section_ids: tuple[ReportSectionId, ...]
) -> dict[str, object]:
    matching = [s for s in snapshot.sections if s.section in section_ids]
    return {
        "coverage": [
            {"section": s.section, "status": s.coverage.status, "reason": s.coverage.reason,
             "reason_code": s.coverage.reason_code}
            for s in matching
        ],
        "tables": [t.model_dump(mode="json") for s in matching for t in s.tables],
        "statements": [st.model_dump(mode="json") for s in matching for st in s.statements],
    }


def export_report(snapshot: ResearchReportSnapshot, output: Path) -> None:
    """The canonical structured research package - a pure reshaping of the same
    persisted ResearchReportSnapshot, not a second research process or new data."""
    check_output(output)
    (output / "normalized").mkdir(parents=True, exist_ok=True)
    write_json(output / "normalized/report_snapshot.json", snapshot.model_dump(mode="json"))

    coverage_by_section = {
        s.section: {"status": s.coverage.status, "reason": s.coverage.reason,
                    "reason_code": s.coverage.reason_code}
        for s in snapshot.sections
    }
    analyst_claim_ids = sorted(
        {str(cid) for s in snapshot.sections for st in s.statements for cid in st.source_claim_ids}
    )
    payload = {
        "metadata": {
            "schema_version": snapshot.schema_version,
            "report_id": str(snapshot.id),
            "evaluated_at": snapshot.evaluated_at.isoformat(),
        },
        "company": {"company_id": str(snapshot.company_id)},
        "coverage": coverage_by_section,
        "document_corpus": snapshot.document_corpus.model_dump(mode="json"),
        **{
            key: _sections_payload(snapshot, section_ids)
            for key, section_ids in _COMPANY_RESEARCH_JSON_GROUPS.items()
        },
        "assumptions": [
            st.model_dump(mode="json")
            for s in snapshot.sections
            for st in s.statements
            if st.epistemic_class == "ASSUMPTION"
        ],
        "analyst_claims": analyst_claim_ids,
        "synthesis": snapshot.synthesis.model_dump(mode="json"),
        "limitations": [limit.model_dump(mode="json") for limit in snapshot.limitations],
        "lineage": {
            "analysis_snapshot_id": str(snapshot.analysis_snapshot_id),
            "analysis_snapshot_sha256": snapshot.analysis_snapshot_sha256,
            "challenge_snapshot_id": str(snapshot.challenge_snapshot_id),
            "challenge_snapshot_sha256": snapshot.challenge_snapshot_sha256,
        },
        "reproducibility": snapshot.reproducibility.model_dump(mode="json"),
    }
    write_json(output / "COMPANY_RESEARCH.json", payload)


def export_investor_brief(snapshot: ResearchReportSnapshot, output: Path) -> None:
    """A pure projection of the SAME persisted ResearchReportSnapshot - no second
    research process, no independent conclusions; it cannot contradict the
    institutional report because it shares its exact source content. Does not gate on a
    fresh/empty directory itself - callers writing multiple artifacts into the same output
    directory should call check_output once, before the first exporter runs."""
    output.mkdir(parents=True, exist_ok=True)
    exec_snap = snapshot.executive_snapshot

    def texts(statements: object) -> list[str]:
        return [st.text for st in statements]  # type: ignore[attr-defined]

    payload = {
        "report_id": str(snapshot.id),
        "what_the_company_does": texts(exec_snap.company_overview),
        "what_is_improving": texts(exec_snap.supporting_evidence),
        "financial_trends": texts(exec_snap.key_financial_trend),
        "sector_kpis": texts(exec_snap.key_sector_trend),
        "management_document_takeaways": texts(exec_snap.document_highlights),
        "valuation": texts(exec_snap.valuation_evidence),
        "market_implied_expectations": texts(exec_snap.market_implied_expectations),
        "catalysts": texts(exec_snap.catalysts),
        "risks": texts(exec_snap.risks),
        "challenger_findings": [str(fid) for fid in exec_snap.material_challenger_finding_ids],
        "disconfirming_evidence": [
            ref.model_dump(mode="json") for ref in exec_snap.disconfirming_evidence
        ],
        "data_gaps": [gap.model_dump(mode="json") for gap in exec_snap.major_data_gaps],
        "research_completeness": exec_snap.research_completeness,
        "valuation_evidence_state": exec_snap.valuation_evidence_state,
        "unresolved_questions": list(exec_snap.unresolved_questions),
    }
    write_json(output / "investor_brief.json", payload)
