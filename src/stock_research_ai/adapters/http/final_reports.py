"""Box 15: read-only, paired-client routes for stored final research reports (Detailed
Research). Nothing here researches or renders: files are produced by the offline orchestrator
(`stock-research-ai detailed-research`) into the local final-report store, and are served
only after re-hashing against their manifest.

  GET /v1/final-reports?symbol=RVNL               newest first, manifest summaries
  GET /v1/final-reports/{artifact_id}             the manifest
  GET /v1/final-reports/{artifact_id}/files/{fmt} JSON | MARKDOWN | HTML | PDF

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
from pydantic import BaseModel, ConfigDict

from stock_research_ai.adapters.rendering.store import FileFinalReportStore, UnsafePathError
from stock_research_ai.domain.report_rendering import (
    EXTENSIONS,
    MEDIA_TYPES,
    RenderFormat,
    ReportManifest,
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


class FinalReportList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbol: str
    reports: tuple[FinalReportSummary, ...]


def _summary(m: ReportManifest) -> FinalReportSummary:
    return FinalReportSummary(
        artifact_id=m.artifact_id, report_id=m.report_id, company_name=m.company_name,
        nse_symbol=m.nse_symbol, as_of=str(m.as_of), status=m.status,
        formats=tuple(f.format.value for f in m.files),
        failed_formats=tuple(f.format.value for f in m.failures),
        box14_sha256=m.box14_sha256)


def download_name(m: ReportManifest, fmt: RenderFormat) -> str:
    who = _SAFE.sub("_", m.nse_symbol or "company")[:20] or "company"
    return f"{who}_{m.as_of.isoformat()}_detailed_research.{EXTENSIONS[fmt]}"


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail={"code": code, "message": message})


def register_final_report_routes(app: FastAPI, store: Callable[[], FileFinalReportStore],
                                 protected: Sequence[Any]) -> None:
    deps = list(protected)

    async def _manifest(artifact_id: UUID) -> ReportManifest:
        manifest = await asyncio.to_thread(store().load, artifact_id)
        if manifest is None:
            raise _not_found("FINAL_REPORT_NOT_FOUND", "Unknown final report")
        return manifest

    @app.get("/v1/final-reports", response_model=FinalReportList, dependencies=deps)
    async def list_final_reports(symbol: str = Query(max_length=20)) -> FinalReportList:
        try:
            found = await asyncio.to_thread(store().list_for_symbol, symbol)
        except UnsafePathError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail={
                "code": "INVALID_SYMBOL", "message": "Not a valid NSE symbol"}) from None
        return FinalReportList(symbol=symbol, reports=tuple(_summary(m) for m in found))

    @app.get("/v1/final-reports/{artifact_id}", response_model=ReportManifest,
             dependencies=deps)
    async def get_final_report(artifact_id: UUID) -> ReportManifest:
        return await _manifest(artifact_id)

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
