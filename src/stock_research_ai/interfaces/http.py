"""HTTP routes exposing the transport-independent research engine, plus (Part 6B-A) the
thin local browser-facing execution layer: pairing/auth, research job launch/polling,
and report/artifact retrieval. FULL_RESEARCH_V1 (application.research_plan) remains the
sole execution authority - this module owns no research/financial/valuation/sector
semantics; it only translates HTTP <-> the existing application layer.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import ValidationError

from stock_research_ai import __version__
from stock_research_ai.adapters.aicredits.summary import AICreditsSummaryModel
from stock_research_ai.adapters.http.browser_artifacts import media_type_for, render_artifact
from stock_research_ai.adapters.http.browser_auth import (
    PairingError,
    PairingLockedOutError,
    PairingStore,
)
from stock_research_ai.adapters.http.browser_schemas import (
    ArtifactKind,
    BrowserResearchRequest,
    BrowserSummaryRequest,
    PairingCompleteRequest,
    PairingCompleteResponse,
    VersionResponse,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.screener.source import validate_url
from stock_research_ai.application.browser_jobs import (
    BrowserJobManager,
    BrowserResearchJob,
    UnsupportedPortalError,
    UnsupportedResearchPlanError,
)
from stock_research_ai.application.engine import Capabilities, ResearchEngine
from stock_research_ai.application.ports import ReportRepository
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.application.summary_generation import SummaryModel, generate_summary
from stock_research_ai.bootstrap import browser_engine, browser_execution_spec, research_engine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company, CompanyRegistration
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.report import ResearchReportSnapshot
from stock_research_ai.domain.summary import (
    SummaryErrorCode,
    SummaryGenerationError,
    SummaryInputBudgetExceeded,
    SummaryInputUnavailable,
    SummaryResult,
)

_PAIRING_PATH = "/v1/pair"

# SUMMARY_V1 error code -> HTTP status. Bodies carry only the fixed code/message.
_SUMMARY_ERROR_STATUS: dict[SummaryErrorCode, int] = {
    SummaryErrorCode.PROVIDER_NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
    SummaryErrorCode.CREDENTIAL_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    SummaryErrorCode.PROVIDER_TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    SummaryErrorCode.SUMMARY_INPUT_TOO_LARGE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    SummaryErrorCode.SUMMARY_REQUEST_TOO_LARGE: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _summary_http_error(error: SummaryGenerationError) -> HTTPException:
    return HTTPException(
        status_code=_SUMMARY_ERROR_STATUS.get(error.code, status.HTTP_502_BAD_GATEWAY),
        detail={
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable_by_user,
            "ambiguous": error.ambiguous,
        },
    )


_READINESS_PATHS = frozenset({"/health", "/v1/version"})


class _BrowserCORSMiddleware:
    """Reflect only the configured exact extension origin, never arbitrary websites.

    Protected routes require a bearer independently of CORS. An explicit Origin
    must also match the paired identity; Chrome may omit Origin on extension GETs.
    Pairing always requires the configured Origin, even before a token exists.
    """

    def __init__(
        self, app: Any, pairing_store: PairingStore, approved_origin: str | None = None
    ) -> None:
        self.app = app
        self._pairing_store = pairing_store
        self._approved_origin = approved_origin

    def _origin_allowed(self, origin: str | None, path: str) -> bool:
        if not origin or origin != self._approved_origin:
            return False
        if path == _PAIRING_PATH:
            return True
        paired_origin = self._pairing_store.paired_origin()
        if paired_origin is not None:
            return origin == paired_origin
        return path in _READINESS_PATHS and origin == self._approved_origin

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        path = scope.get("path", "")
        allowed = self._origin_allowed(origin, path)

        if scope["method"] == "OPTIONS":
            response_headers = [(b"vary", b"Origin")]
            if allowed and origin:
                response_headers.extend(
                    [
                        (b"access-control-allow-origin", origin.encode()),
                        (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                        (b"access-control-allow-headers", b"authorization, content-type"),
                        (b"access-control-max-age", b"600"),
                    ]
                )
            await send({"type": "http.response.start", "status": 204, "headers": response_headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and allowed and origin:
                headers_list = list(message.get("headers", []))
                headers_list.append((b"access-control-allow-origin", origin.encode()))
                headers_list.append((b"vary", b"Origin"))
                message = {**message, "headers": headers_list}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(
    engine: ResearchEngine | None = None,
    settings: Settings | None = None,
    job_manager: BrowserJobManager | None = None,
    pairing_store: PairingStore | None = None,
    report_repository: ReportRepository | None = None,
    summary_model_factory: Callable[[], SummaryModel] | None = None,
) -> FastAPI:
    """Create an HTTP adapter, owning resources only when none are supplied.

    `report_repository` is independent, persistent source-of-truth for completed
    report/artifact retrieval - it survives a fresh BrowserJobManager (e.g. after a
    simulated backend restart) because it reads directly from the database, never from
    any job manager's in-memory job records."""
    active_engine = engine
    active_job_manager = job_manager
    active_report_repository = report_repository
    if active_report_repository is None and job_manager is not None:
        report_services = job_manager.engine._report  # noqa: SLF001
        active_report_repository = report_services.repository if report_services else None
    runtime_settings = settings if settings is not None else Settings()
    active_pairing_store = pairing_store or PairingStore(runtime_settings.browser_auth_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal active_engine, active_job_manager, active_report_repository
        async with (
            _company_engine_context() as owned_engine,
            _job_manager_context() as owned,
        ):
            if engine is None:
                active_engine = owned_engine
            if job_manager is None and owned is not None:
                active_job_manager = owned[0]
            if report_repository is None and job_manager is None and owned is not None:
                active_report_repository = owned[1]
            try:
                yield
            finally:
                if engine is None:
                    active_engine = None
                if job_manager is None and owned is not None:
                    active_job_manager = None
                if report_repository is None and job_manager is None and owned is not None:
                    active_report_repository = None

    @asynccontextmanager
    async def _company_engine_context() -> AsyncIterator[ResearchEngine | None]:
        if engine is not None:
            yield None
            return
        async with research_engine(runtime_settings) as owned:
            yield owned

    @asynccontextmanager
    async def _job_manager_context() -> (
        AsyncIterator[tuple[BrowserJobManager, ReportRepository | None] | None]
    ):
        if job_manager is not None:
            yield None
            return
        if engine is not None:
            # An injected engine fully substitutes for bootstrap (test convenience,
            # mirroring the existing `engine` injection contract above) - no separate
            # database bootstrap is attempted for the job manager in that case. A bare
            # test engine with no ReportServices attached simply has no report
            # repository available (report routes remain unreachable-but-harmless for
            # such tests, exactly as before).
            artifact_store = FileRawArtifactStore(
                runtime_settings.artifact_path
                or runtime_settings.database_path.parent / "artifacts"
            )
            manager = BrowserJobManager(
                engine,
                artifact_store,
                ScreenerBroadIndustrySource(),
                lambda: browser_execution_spec(runtime_settings),
            )
            report_services = engine._report  # noqa: SLF001
            yield manager, (report_services.repository if report_services else None)
            return
        async with browser_engine(runtime_settings) as (owned_engine, store, broad_industry):
            manager = BrowserJobManager(
                owned_engine,
                store,
                broad_industry,
                lambda: browser_execution_spec(runtime_settings),
            )
            report_services = owned_engine._report  # noqa: SLF001
            yield manager, (report_services.repository if report_services else None)

    app = FastAPI(
        title="Stock Research AI",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        _BrowserCORSMiddleware,
        pairing_store=active_pairing_store,
        approved_origin=runtime_settings.browser_extension_origin,
    )

    def current_engine() -> ResearchEngine:
        if active_engine is None:
            raise RuntimeError("The application lifespan has not started")
        return active_engine

    def current_job_manager() -> BrowserJobManager:
        if active_job_manager is None:
            raise RuntimeError("The application lifespan has not started")
        return active_job_manager

    def current_report_repository() -> ReportRepository:
        if active_report_repository is None:
            raise RuntimeError("The application lifespan has not started")
        return active_report_repository

    def _require_paired_client(
        request: Request, authorization: str | None = Header(default=None)
    ) -> None:
        origin = request.headers.get("origin")
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[len("bearer ") :]
        if not active_pairing_store.verify(origin, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "UNAUTHORIZED",
                    "message": "Missing or invalid pairing credentials",
                },
            )

    protected = [Depends(_require_paired_client)]

    # --- Existing Part 1 routes: unchanged. ---------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def get_capabilities() -> Capabilities:
        return await current_job_manager().engine.get_capabilities()

    @app.post("/v1/companies", response_model=Company, status_code=status.HTTP_201_CREATED)
    async def register_company(registration: CompanyRegistration) -> Company:
        return await current_engine().register_company(registration)

    @app.get("/v1/companies/{company_id}", response_model=Company)
    async def get_company(company_id: UUID) -> Company:
        company = await current_engine().get_company(company_id)
        if company is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
        return company

    # --- Part 6B-A: version (public) ------------------------------------------------

    @app.get("/v1/version", response_model=VersionResponse)
    async def get_version() -> VersionResponse:
        return VersionResponse()

    # --- Part 6B-A: pairing (public bootstrap; origin-restricted at CORS/validation) -

    @app.post("/v1/pair", response_model=PairingCompleteResponse)
    async def pair(payload: PairingCompleteRequest, request: Request) -> PairingCompleteResponse:
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "MISSING_ORIGIN",
                    "message": "An Origin header is required to pair",
                },
            )
        if origin != runtime_settings.browser_extension_origin:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "PAIRING_FAILED", "message": "Pairing origin is not approved"},
            )
        try:
            token = active_pairing_store.complete_pairing(origin, payload.pairing_code)
        except PairingLockedOutError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "PAIRING_LOCKED_OUT", "message": str(exc)},
            ) from exc
        except PairingError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "PAIRING_FAILED", "message": str(exc)},
            ) from exc
        return PairingCompleteResponse(token=token)

    # --- Part 6B-A: research jobs (protected) ----------------------------------------

    @app.post(
        "/v1/research-jobs",
        response_model=BrowserResearchJob,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def create_research_job(payload: BrowserResearchRequest) -> BrowserResearchJob:
        if payload.portal == "SCREENER":
            try:
                validate_url(payload.source_url)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "UNSUPPORTED_SOURCE_URL", "message": str(exc)},
                ) from exc
        try:
            return current_job_manager().launch(
                payload.portal, payload.source_url, payload.research_plan
            )
        except UnsupportedPortalError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "UNSUPPORTED_PORTAL", "message": str(exc)},
            ) from exc
        except UnsupportedResearchPlanError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "UNSUPPORTED_SOURCE_URL", "message": str(exc)},
            ) from exc

    @app.get(
        "/v1/research-jobs/{job_id}", response_model=BrowserResearchJob, dependencies=protected
    )
    async def get_research_job(job_id: UUID) -> BrowserResearchJob:
        job = current_job_manager().get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "JOB_LOST_AFTER_SERVER_RESTART",
                    "message": "Unknown job_id. If this job existed before a server "
                    "restart, its in-memory state was not preserved.",
                },
            )
        return job

    # --- Part 6B-A: report/artifact retrieval (protected) ---------------------------

    async def _resolve_report(report_id: UUID) -> ResearchReportSnapshot:
        """Reads directly from the persistent report repository - never from any job
        manager's in-memory job records. A completed ResearchReportSnapshot is
        retrievable by its exact ID regardless of whether the job that produced it
        still exists in this (or any) process's in-memory job history, and regardless
        of a backend restart in between."""
        report = await current_report_repository().get_report(report_id)
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown report_id")
        return report

    @app.get(
        "/v1/reports/{report_id}", response_model=ResearchReportSnapshot, dependencies=protected
    )
    async def get_report(report_id: UUID) -> ResearchReportSnapshot:
        return await _resolve_report(report_id)

    # --- SUMMARY_V1: one bounded summary for the detected page (protected) ----------

    def new_summary_model() -> SummaryModel:
        if summary_model_factory is not None:
            return summary_model_factory()
        return AICreditsSummaryModel.from_settings(runtime_settings)

    @app.post("/v1/summaries", response_model=SummaryResult, dependencies=protected)
    async def create_summary(payload: BrowserSummaryRequest) -> SummaryResult:
        """Synchronous: Screener page -> extraction -> SummaryInput -> one model call.
        Separate from FULL_RESEARCH_V1 jobs; never retried or resubmitted here."""
        try:
            validate_url(payload.source_url)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "UNSUPPORTED_SOURCE_URL",
                    "message": "Not a supported Screener company page",
                },
            ) from None
        try:
            # Resolve provider configuration before fetching anything from Screener.
            model = new_summary_model()
        except SummaryGenerationError as exc:
            raise _summary_http_error(exc) from None
        extraction = await current_job_manager().engine.extract_company(
            ExtractionRequest(provider="screener", url=payload.source_url)
        )
        try:
            summary_input = project_summary_input(extraction)
        except SummaryInputBudgetExceeded:
            raise _summary_http_error(
                SummaryGenerationError(SummaryErrorCode.SUMMARY_INPUT_TOO_LARGE)
            ) from None
        except (SummaryInputUnavailable, ValidationError):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "COMPANY_DATA_UNAVAILABLE",
                    "message": "Company data could not be read from this page",
                    "retryable": True,
                    "ambiguous": False,
                },
            ) from None
        try:
            return await generate_summary(
                summary_input, model, now=datetime.now(UTC), summary_id=uuid4()
            )
        except SummaryGenerationError as exc:
            raise _summary_http_error(exc) from None

    @app.get("/v1/reports/{report_id}/artifacts/{artifact_kind}", dependencies=protected)
    async def get_report_artifact(report_id: UUID, artifact_kind: ArtifactKind) -> Response:
        report = await _resolve_report(report_id)
        content = render_artifact(report, artifact_kind)
        return Response(content=content, media_type=media_type_for(artifact_kind))

    return app
