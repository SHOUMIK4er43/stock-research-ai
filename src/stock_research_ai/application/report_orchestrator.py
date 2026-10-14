"""DETAILED_RESEARCH_V1 Box 15: the end-to-end orchestrator (offline) and the render step.

    ResearchStage (cached acquisition: Box 1B screener datasets, Box 1 catalogue, Box 3
    evidence bundle, Box 12 raw payloads, cited Box 13 inputs, the Box 10 / 11 / 14 requests)
      -> Box 4 -> 5 -> 6 -> 7 -> 8 -> 10 -> 11 -> 12 -> 13   (the existing builders, unchanged)
      -> Box 14 FinalResearchReportDataset
      -> RenderDocument -> JSON / Markdown / HTML / PDF -> artifact store + manifest

No network, no clock, no AI call: every builder is the existing pure Box function, called with
the stage's injected `generated_at`. Acquisition (Screener pages, filings, exchange feeds) is
done beforehand by the existing adapters and captured in the stage. Box 9 needs peer
datasets that a stage does not carry, so it is not run (recorded as NOT_RUN).

Failure model (one code per run, root cause kept): INPUT_ERROR, UPSTREAM_RESEARCH_FAILED (the
failing box is named), REPORT_COMPOSITION_FAILED (Box 14's own code), RENDER_FAILED (every
requested format failed), PARTIAL_RENDER (some formats failed; the manifest names them),
OUTPUT_IO_FAILED.

Partial-render policy: formats are rendered independently. A format that succeeds is kept; a
format that fails is never written (and a stale file of that format in the same run
directory is removed), and the manifest records the exact failed format and code. The run
is never reported as complete unless every requested format was written.
"""

import base64
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from stock_research_ai.application.business_intelligence import build_business_intelligence
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_charts import build_chart_bundle
from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.fundamental_valuation import build_valuation
from stock_research_ai.application.management_intelligence import build_management_intelligence
from stock_research_ai.application.market_intelligence import build_market_intelligence
from stock_research_ai.application.report_claims import NS, ReportCompositionError
from stock_research_ai.application.report_html import render_html
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import (
    box14_sha256,
    build_presentation,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.final_research_report import (
    FINAL_REPORT_SCHEMA,
    FinalReportRequest,
    FinalResearchReportDataset,
)
from stock_research_ai.domain.financial_charts import FinancialChartBundle
from stock_research_ai.domain.forecasting import ForecastRequest
from stock_research_ai.domain.fundamental_valuation import ValuationRequest
from stock_research_ai.domain.market_intelligence import (
    FetchStatus,
    MarketIntelligenceDataset,
    MarketIntelligenceRequest,
    PayloadKind,
    RawPayload,
)
from stock_research_ai.domain.report_rendering import (
    ALL_FORMATS,
    FILE_NAMES,
    MEDIA_TYPES,
    ORCHESTRATOR_VERSION,
    RENDERER_VERSION,
    FailureCode,
    ManifestFailure,
    ManifestFile,
    RenderDocument,
    RenderError,
    RenderFormat,
    ReportManifest,
)
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset

_logger = logging.getLogger("stock_research_ai.final_report")
_DETAIL = 300


def _bounded(text: str) -> str:
    one = " ".join(text.split())
    return one[:_DETAIL] + ("..." if len(one) > _DETAIL else "")


# --- the stage ------------------------------------------------------------------------------


class StagePayload(BaseModel):
    """A Box 12 RawPayload with its bytes base64-encoded (JSON-safe, exact)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: PayloadKind
    url: str
    status: FetchStatus
    http_status: int | None = None
    retrieved_at: AwareDatetime
    content_base64: str | None = None
    content_type: str | None = None
    error_code: str | None = None

    @classmethod
    def of(cls, p: RawPayload) -> "StagePayload":
        return cls(kind=p.kind, url=p.url, status=p.status, http_status=p.http_status,
                   retrieved_at=p.retrieved_at, content_type=p.content_type,
                   error_code=p.error_code, content_base64=None if p.content is None
                   else base64.b64encode(p.content).decode("ascii"))

    def payload(self) -> RawPayload:
        content = None if self.content_base64 is None else base64.b64decode(
            self.content_base64, validate=True)
        return RawPayload(kind=self.kind, url=self.url, status=self.status,
                          http_status=self.http_status, retrieved_at=self.retrieved_at,
                          content=content, content_type=self.content_type,
                          error_code=self.error_code)


class ResearchStage(BaseModel):
    """Everything acquisition produced for one company, captured once; the orchestrator
    re-derives Boxes 4-14 from it offline. `screeners[0]` is the primary (analytical) page."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["DETAILED_RESEARCH_STAGE_V1"] = "DETAILED_RESEARCH_STAGE_V1"
    generated_at: AwareDatetime
    screeners: tuple[DetailedScreenerDataset, ...] = Field(min_length=1, max_length=4)
    catalogue: EvidenceCatalogue | None = None
    bundle: EvidenceBundle | None = None
    market_request: MarketIntelligenceRequest | None = None
    market_payloads: tuple[StagePayload, ...] = Field(default=(), max_length=2_000)
    forecast_request: ForecastRequest | None = None
    valuation_request: ValuationRequest | None = None
    diligence_request: DueDiligenceRequest
    report_request: FinalReportRequest


# --- ports ----------------------------------------------------------------------------------

PdfRenderer = Callable[[RenderDocument], bytes]


class FinalReportStore(Protocol):
    def location(self, manifest_seed: "ManifestSeed") -> str: ...

    def load(self, artifact_id: UUID) -> ReportManifest | None: ...

    def verified_formats(self, manifest: ReportManifest) -> set[RenderFormat]: ...

    def publish(self, manifest: ReportManifest, files: dict[RenderFormat, bytes]) -> None: ...


class ManifestSeed(BaseModel):
    """What the store needs to place a run: company, as_of and the artifact ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: UUID
    company_key: str
    nse_symbol: str | None
    as_of: str


# --- results --------------------------------------------------------------------------------


class RunFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: FailureCode
    stage: str  # INPUT / BOX4 ... BOX13 / BOX14 / RENDER / OUTPUT
    error_type: str
    detail: str


class UpstreamStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    box: str
    status: Literal["BUILT", "NOT_RUN", "FAILED"]
    detail: str | None = None


class DetailedResearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["COMPLETE", "PARTIAL_RENDER", "FAILED"]
    failure: RunFailure | None = None
    steps: tuple[UpstreamStep, ...] = ()
    report_id: UUID | None = None
    box14_sha256: str | None = None
    manifest: ReportManifest | None = None
    cached: bool = False
    orchestrator_version: str = ORCHESTRATOR_VERSION


class _Failed(Exception):
    def __init__(self, failure: RunFailure) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


def _failure(code: FailureCode, stage: str, exc: BaseException | None, detail: str) -> _Failed:
    return _Failed(RunFailure(code=code, stage=stage, error_type=type(exc).__name__ if exc
                              else "None", detail=_bounded(detail)))


# --- rendering ------------------------------------------------------------------------------


def report_json(report: FinalResearchReportDataset) -> str:
    """The Box 14 dataset itself, serialised deterministically (generated_at included, so it
    re-validates as a FinalResearchReportDataset). Exact Decimal values are kept as strings."""
    return json.dumps(report.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")) + "\n"


def artifact_id_for(sha: str) -> UUID:
    return uuid5(NS, f"final-report-artifact|{sha}|{RENDERER_VERSION}")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_formats(values: Sequence[str] | None) -> tuple[RenderFormat, ...]:
    """Closed vocabulary; unknown names are an INPUT_ERROR, never a path component."""
    if not values:
        return ALL_FORMATS
    aliases = {"MD": RenderFormat.MARKDOWN, "MARKDOWN": RenderFormat.MARKDOWN,
               "JSON": RenderFormat.JSON, "HTML": RenderFormat.HTML, "PDF": RenderFormat.PDF}
    out: list[RenderFormat] = []
    for v in values:
        key = v.strip().upper()
        if key not in aliases:
            raise ValueError("Unsupported report format")
        out.append(aliases[key])
    return tuple(f for f in ALL_FORMATS if f in out)


def render_formats(report: FinalResearchReportDataset, formats: Sequence[RenderFormat], *,
                   pdf: PdfRenderer | None, charts: FinancialChartBundle | None = None,
                   sha: str | None = None) -> tuple[dict[RenderFormat, bytes],
                                                    list[ManifestFailure], RenderDocument]:
    """Render each requested format independently; a failure in one never touches another."""
    digest = sha or box14_sha256(report)
    doc = build_presentation(report, charts=charts, sha256=digest)
    renderers: dict[RenderFormat, Callable[[], bytes]] = {
        RenderFormat.JSON: lambda: report_json(report).encode("utf-8"),
        RenderFormat.MARKDOWN: lambda: render_markdown(doc).encode("utf-8"),
        RenderFormat.HTML: lambda: render_html(doc).encode("utf-8"),
    }
    if pdf is not None:
        renderers[RenderFormat.PDF] = lambda: pdf(doc)
    files: dict[RenderFormat, bytes] = {}
    failures: list[ManifestFailure] = []
    for fmt in formats:
        fn = renderers.get(fmt)
        if fn is None:
            failures.append(ManifestFailure(format=fmt, code="RENDERER_UNAVAILABLE",
                                            detail=f"no {fmt.value} renderer configured"))
            continue
        try:
            files[fmt] = fn()
        except RenderError as exc:
            failures.append(ManifestFailure(format=fmt, code=exc.code,
                                            detail=_bounded(exc.detail)))
        except Exception as exc:  # format isolation: any renderer defect fails that format
            _logger.exception("final report %s render failed", fmt.value)
            failures.append(ManifestFailure(format=fmt, code="RENDER_ENGINE_ERROR",
                                            detail=type(exc).__name__))
    return files, failures, doc


def _limitations(report: FinalResearchReportDataset) -> tuple[str, ...]:
    kinds = {d.kind for d in report.risks.data_limitations}
    kinds |= {f"APPENDIX_TRUNCATED:{a.appendix_id.value}" for a in report.appendices
              if a.truncated}
    kinds |= {f"SECTION_{s.status.value}:{s.section_id.value}" for s in report.sections
              if s.status.value in ("UNAVAILABLE", "PARTIAL")}
    return tuple(sorted(kinds))


def render_final_report(report: FinalResearchReportDataset, *, store: FinalReportStore,
                        pdf: PdfRenderer | None,
                        formats: Sequence[RenderFormat] = ALL_FORMATS,
                        charts: FinancialChartBundle | None = None,
                        notes: Sequence[str] = ()) -> DetailedResearchResult:
    """Render one Box 14 dataset into the store. Re-uses a stored run of the same Box 14
    content and renderer version when every requested file is present and hash-verified."""
    snapshot = report.model_dump(mode="json")
    sha = box14_sha256(report)
    artifact_id = artifact_id_for(sha)
    m = report.metadata
    seed = ManifestSeed(artifact_id=artifact_id, company_key=m.company_key,
                        nse_symbol=m.nse_symbol, as_of=str(m.as_of))
    try:
        cached = store.load(artifact_id)
        if cached is not None and cached.box14_sha256 == sha and set(formats) <= (
                store.verified_formats(cached)):
            return DetailedResearchResult(status="COMPLETE", report_id=report.report_id,
                                          box14_sha256=sha, manifest=cached, cached=True)
        location = store.location(seed)
    except (OSError, ValueError) as exc:
        return DetailedResearchResult(status="FAILED", failure=_failure(
            FailureCode.OUTPUT_IO_FAILED, "OUTPUT", exc, "the report store is unavailable"
        ).failure, report_id=report.report_id, box14_sha256=sha)
    try:
        files, failures, doc = render_formats(report, formats, pdf=pdf, charts=charts, sha=sha)
    except Exception as exc:  # the presentation model itself failed
        return DetailedResearchResult(status="FAILED", failure=_failure(
            FailureCode.RENDER_FAILED, "RENDER", exc, "the presentation model failed").failure,
            report_id=report.report_id, box14_sha256=sha)
    if report.model_dump(mode="json") != snapshot:  # pragma: no cover - frozen models
        raise RuntimeError("a renderer mutated the Box 14 dataset")
    status: Literal["COMPLETE", "PARTIAL_RENDER", "RENDER_FAILED"] = (
        "COMPLETE" if not failures else "PARTIAL_RENDER" if files else "RENDER_FAILED")
    manifest = ReportManifest(
        artifact_id=artifact_id, location=location, company_key=m.company_key,
        company_name=m.company_name, nse_symbol=m.nse_symbol, isin=m.isin, as_of=m.as_of,
        report_id=report.report_id, box14_sha256=sha, box14_schema=FINAL_REPORT_SCHEMA,
        box14_generated_at=m.generated_at.isoformat(),
        engines=(("final_report", m.engine_version), ("selection_rules",
                                                       m.selection_rules_version),
                 ("renderer", RENDERER_VERSION), ("orchestrator", ORCHESTRATOR_VERSION),
                 ("pdf_engine", "reportlab" if pdf is not None else "none")),
        status=status, requested=tuple(formats),
        files=tuple(ManifestFile(format=f, filename=FILE_NAMES[f], media_type=MEDIA_TYPES[f],
                                 bytes=len(files[f]), sha256=_sha_bytes(files[f]))
                    for f in ALL_FORMATS if f in files),
        failures=tuple(failures), warnings=(*notes, *doc.warnings),
        limitations=_limitations(report))
    try:
        store.publish(manifest, files)
    except (OSError, ValueError) as exc:
        return DetailedResearchResult(status="FAILED", failure=_failure(
            FailureCode.OUTPUT_IO_FAILED, "OUTPUT", exc, "the report files could not be "
            "written").failure, report_id=report.report_id, box14_sha256=sha)
    if status == "COMPLETE":
        return DetailedResearchResult(status="COMPLETE", report_id=report.report_id,
                                      box14_sha256=sha, manifest=manifest)
    code = FailureCode.PARTIAL_RENDER if files else FailureCode.RENDER_FAILED
    failed = ", ".join(f"{f.format.value} ({f.code})" for f in failures)
    return DetailedResearchResult(
        status="PARTIAL_RENDER" if files else "FAILED",
        failure=RunFailure(code=code, stage="RENDER", error_type="RenderError",
                           detail=_bounded(f"failed formats: {failed}")),
        report_id=report.report_id, box14_sha256=sha, manifest=manifest)


# --- the end-to-end chain -------------------------------------------------------------------


def _step[T](steps: list[UpstreamStep], box: str, fn: Callable[[], T]) -> T:
    try:
        value = fn()
    except Exception as exc:
        steps.append(UpstreamStep(box=box, status="FAILED", detail=type(exc).__name__))
        raise _failure(FailureCode.UPSTREAM_RESEARCH_FAILED, box, exc, str(exc)) from exc
    steps.append(UpstreamStep(box=box, status="BUILT"))
    return value


def compose_from_stage(stage: ResearchStage, steps: list[UpstreamStep]) -> tuple[
        FinalResearchReportDataset, FinancialChartBundle | None]:
    """Boxes 4-14 from the stage, through the existing builders only."""
    at: datetime = stage.generated_at
    primary = stage.screeners[0]
    b4 = _step(steps, "BOX4", lambda: build_canonical_dataset(
        company_key=primary.company_key, company_name=primary.profile.name,
        screeners=stage.screeners, bundle=stage.bundle, catalogue=stage.catalogue,
        generated_at=at))
    b5 = _step(steps, "BOX5", lambda: build_financial_analytics(b4, generated_at=at))
    b6: FinancialChartBundle | None = None
    try:  # charts are not critical: a Box 6 failure never stops the report
        b6 = build_chart_bundle(b4, b5, generated_at=at)
        steps.append(UpstreamStep(box="BOX6", status="BUILT"))
    except Exception as exc:
        steps.append(UpstreamStep(box="BOX6", status="FAILED", detail=type(exc).__name__))
    b7 = _step(steps, "BOX7", lambda: build_business_intelligence(
        box4=b4, bundle=stage.bundle, catalogue=stage.catalogue, screener=primary,
        generated_at=at))
    b8 = None
    if stage.bundle is not None and stage.catalogue is not None:
        bundle, catalogue = stage.bundle, stage.catalogue
        b8 = _step(steps, "BOX8", lambda: build_management_intelligence(
            bundle=bundle, catalogue=catalogue, box4=b4, box5=b5, box7=b7, generated_at=at))
    else:
        steps.append(UpstreamStep(box="BOX8", status="NOT_RUN",
                                  detail="no Box 3 bundle / catalogue in the stage"))
    steps.append(UpstreamStep(box="BOX9", status="NOT_RUN",
                              detail="peer datasets are not part of the stage"))
    b10 = _step(steps, "BOX10", lambda: build_forecast(
        b4, request=stage.forecast_request, management=b8, generated_at=at))
    b11 = _step(steps, "BOX11", lambda: build_valuation(
        b10, request=stage.valuation_request, box4=b4, generated_at=at))
    b12: MarketIntelligenceDataset | None = None
    if stage.market_request is not None:
        req = stage.market_request
        b12 = _step(steps, "BOX12", lambda: build_market_intelligence(
            req, [p.payload() for p in stage.market_payloads], generated_at=req.as_of))
        if not b12.identity.usable:
            # Box 13 refuses an unusable identity; exchange data that cannot be tied to the
            # company is not used at all (never partially), and the run says so.
            steps[-1] = UpstreamStep(box="BOX12", status="FAILED", detail=(
                f"IDENTITY_{b12.identity.status.value}: exchange data not used"))
            b12 = None
    else:
        steps.append(UpstreamStep(box="BOX12", status="NOT_RUN",
                                  detail="no exchange acquisition in the stage"))
    b13 = _step(steps, "BOX13", lambda: build_due_diligence(
        b4, b5, request=stage.diligence_request, generated_at=at, bundle=stage.bundle,
        box7=b7, box8=b8, box10=b10, box11=b11, box12=b12))
    try:
        report = build_final_report(
            request=stage.report_request, generated_at=at, box4=b4, box5=b5, box13=b13,
            bundle=stage.bundle, box6=b6, box7=b7, box8=b8, box10=b10, box11=b11, box12=b12)
    except ReportCompositionError as exc:
        steps.append(UpstreamStep(box="BOX14", status="FAILED", detail=exc.code))
        raise _failure(FailureCode.REPORT_COMPOSITION_FAILED, "BOX14", exc,
                       f"{exc.code}: {exc.detail}") from exc
    except Exception as exc:
        steps.append(UpstreamStep(box="BOX14", status="FAILED", detail=type(exc).__name__))
        raise _failure(FailureCode.REPORT_COMPOSITION_FAILED, "BOX14", exc, str(exc)) from exc
    steps.append(UpstreamStep(box="BOX14", status="BUILT"))
    return report, b6


def load_stage(text: str | bytes) -> ResearchStage:
    try:
        return ResearchStage.model_validate_json(text)
    except ValidationError as exc:
        raise _failure(FailureCode.INPUT_ERROR, "INPUT", exc,
                       f"invalid research stage ({exc.error_count()} errors)") from exc


def run_detailed_research(stage: ResearchStage | str | bytes, *, store: FinalReportStore,
                          pdf: PdfRenderer | None,
                          formats: Sequence[str] | None = None) -> DetailedResearchResult:
    """The one entry point: stage -> Boxes 4-14 -> rendered, stored, manifested report."""
    steps: list[UpstreamStep] = []
    try:
        try:
            wanted = parse_formats(formats)
        except ValueError as exc:
            raise _failure(FailureCode.INPUT_ERROR, "INPUT", exc, str(exc)) from exc
        staged = stage if isinstance(stage, ResearchStage) else load_stage(stage)
        report, charts = compose_from_stage(staged, steps)
    except _Failed as failed:
        return DetailedResearchResult(status="FAILED", failure=failed.failure,
                                      steps=tuple(steps))
    notes = tuple(f"{s.box}_{s.status}: {s.detail}" for s in steps if s.status == "FAILED")
    result = render_final_report(report, store=store, pdf=pdf, formats=wanted, charts=charts,
                                 notes=notes)
    return result.model_copy(update={"steps": tuple(steps)})
