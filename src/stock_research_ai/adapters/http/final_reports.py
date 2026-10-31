"""Box 15: paired-client routes for Detailed Research (the Box 14 final research report).

  POST /v1/final-reports {source_url}             start (or join) generation for this
                                                  Screener page -> 202 + job
  GET  /v1/final-report-jobs/{job_id}             job status (QUEUED/RUNNING/READY/PARTIAL/
                                                  FAILED, typed failure code)
  GET  /v1/final-reports?page=RVNL                this page's reports, newest first (the page
                                                  key recorded from the ACQUIRED dataset)
  GET  /v1/final-reports?symbol=RVNL              by verified NSE symbol (CLI users)
  GET  /v1/final-reports/{artifact_id}            the manifest
  GET  /v1/final-reports/{artifact_id}/view       the side-panel view (summary, sections,
                                                  risks / limitations) from report.json
  GET  /v1/final-reports/{artifact_id}/files/{fmt} JSON | MARKDOWN | HTML | PDF

Generation acquires and researches in the backend (no stage file, no CLI); every file is
served only after re-hashing against its manifest.

Every route sits behind the existing pairing dependency. Identifiers are a validated NSE
symbol, a UUID and a closed format enum - never a path. Download names are generated here
from validated values only, so no header can be injected.
"""

import asyncio
import re
from collections.abc import Callable, Sequence
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from stock_research_ai.adapters.rendering.store import FileFinalReportStore, UnsafePathError
from stock_research_ai.adapters.screener.source import validate_url
from stock_research_ai.application.final_report_jobs import (
    FinalReportJob,
    FinalReportJobLimitError,
    FinalReportJobManager,
)
from stock_research_ai.application.report_panel import FinalReportPanel, build_panel
from stock_research_ai.domain.final_research_report import FinalResearchReportDataset
from stock_research_ai.domain.report_rendering import (
    EXTENSIONS,
    MEDIA_TYPES,
    RenderFormat,
    ReportManifest,
    screener_page_key,
)

FormatName = Literal["JSON", "MARKDOWN", "HTML", "PDF"]
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: an HTML report opened directly from the API can neither run script nor load anything
HTML_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; sandbox")


class FinalReportSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: UUID
    report_id: UUID
    company_name: str
    nse_symbol: str | None
    as_of: str
    status: str
    formats: tuple[FormatName, ...]
    failed_formats: tuple[FormatName, ...]
    box14_sha256: str
    screener_page: str | None = None


class FinalReportList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str | None = None
    page: str | None = None
    reports: tuple[FinalReportSummary, ...]


class FinalReportCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_url: str = Field(max_length=300)


def _summary(m: ReportManifest) -> FinalReportSummary:
    return FinalReportSummary(
        artifact_id=m.artifact_id, report_id=m.report_id, company_name=m.company_name,
        nse_symbol=m.nse_symbol, as_of=str(m.as_of), status=m.status,
        formats=tuple(f.format.value for f in m.files),
        failed_formats=tuple(f.format.value for f in m.failures),
        box14_sha256=m.box14_sha256, screener_page=m.screener_page)


def download_name(m: ReportManifest, fmt: RenderFormat) -> str:
    who = _SAFE.sub("_", m.nse_symbol or "company")[:20] or "company"
    return f"{who}_{m.as_of.isoformat()}_detailed_research.{EXTENSIONS[fmt]}"


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail={"code": code, "message": message})


def _unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                         detail={"code": code, "message": message})


def register_final_report_routes(app: FastAPI, store: Callable[[], FileFinalReportStore],
                                 protected: Sequence[Any],
                                 jobs: Callable[[], FinalReportJobManager] | None = None,
                                 ) -> None:
    deps = list(protected)

    async def _manifest(artifact_id: UUID) -> ReportManifest:
        manifest = await asyncio.to_thread(store().load, artifact_id)
        if manifest is None:
            raise _not_found("FINAL_REPORT_NOT_FOUND", "Unknown final report")
        return manifest

    @app.post("/v1/final-reports", response_model=FinalReportJob,
              status_code=status.HTTP_202_ACCEPTED, dependencies=deps)
    async def create_final_report(payload: FinalReportCreate) -> FinalReportJob:
        try:
            url = validate_url(payload.source_url)
        except ValueError:
            # fixed text, never str(exc): a parser error can quote the submitted URL
            raise _unprocessable("UNSUPPORTED_SOURCE_URL",
                                 "Not a supported Screener company page") from None
        page = screener_page_key(url)
        if page is None:
            raise _unprocessable("UNSUPPORTED_SOURCE_URL", "Not a supported Screener company page")
        if jobs is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={
                "code": "FINAL_REPORT_GENERATION_UNAVAILABLE",
                "message": "Detailed Research generation is not configured"})
        try:
            return jobs().launch(url, page)
        except FinalReportJobLimitError:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail={
                "code": "FINAL_REPORT_JOB_LIMIT_REACHED",
                "message": "Another Detailed Research is already running"}) from None

    @app.get("/v1/final-report-jobs/{job_id}", response_model=FinalReportJob,
             dependencies=deps)
    async def get_final_report_job(job_id: UUID) -> FinalReportJob:
        job = jobs().get(job_id) if jobs is not None else None
        if job is None:
            raise _not_found("FINAL_REPORT_JOB_NOT_FOUND",
                             "Unknown job (in-memory state does not survive a restart)")
        return job

    @app.get("/v1/final-reports", response_model=FinalReportList, dependencies=deps)
    async def list_final_reports(symbol: str | None = Query(default=None, max_length=20),
                                 page: str | None = Query(default=None, max_length=40),
                                 ) -> FinalReportList:
        if (symbol is None) == (page is None):
            raise _unprocessable("INVALID_LOOKUP", "Give exactly one of page or symbol")
        try:
            if page is not None:
                found = await asyncio.to_thread(store().list_for_page, page.upper())
            else:
                assert symbol is not None
                found = await asyncio.to_thread(store().list_for_symbol, symbol)
        except UnsafePathError:
            if page is not None:
                raise _unprocessable("INVALID_PAGE", "Not a Screener company page") from None
            raise _unprocessable("INVALID_SYMBOL", "Not a valid NSE symbol") from None
        return FinalReportList(symbol=symbol, page=page.upper() if page else None,
                               reports=tuple(_summary(m) for m in found))

    @app.get("/v1/final-reports/{artifact_id}", response_model=ReportManifest,
             dependencies=deps)
    async def get_final_report(artifact_id: UUID) -> ReportManifest:
        return await _manifest(artifact_id)

    @app.get("/v1/final-reports/{artifact_id}/view", response_model=FinalReportPanel,
             dependencies=deps)
    async def get_final_report_view(artifact_id: UUID) -> FinalReportPanel:
        manifest = await _manifest(artifact_id)

        def build() -> FinalReportPanel | None:
            data = store().read_file(manifest, RenderFormat.JSON)  # hash-verified bytes
            if data is None:
                return None
            return build_panel(FinalResearchReportDataset.model_validate_json(data), manifest)

        panel = await asyncio.to_thread(build)
        if panel is None:
            raise _not_found("FINAL_REPORT_FILE_UNAVAILABLE",
                             "This report has no verified JSON to show")
        return panel

    @app.get("/v1/final-reports/{artifact_id}/files/{fmt}", dependencies=deps)
    async def get_final_report_file(artifact_id: UUID, fmt: FormatName) -> Response:
        manifest = await _manifest(artifact_id)
        render_format = RenderFormat(fmt)
        data = await asyncio.to_thread(store().read_file, manifest, render_format)
        if data is None:
            raise _not_found("FINAL_REPORT_FILE_UNAVAILABLE",
                             "This format was not produced or failed its integrity check")
        name = download_name(manifest, render_format)
        headers = {
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        }
        if render_format == RenderFormat.HTML:
            headers["Content-Security-Policy"] = HTML_CSP
        return Response(content=data, media_type=MEDIA_TYPES[render_format], headers=headers)
