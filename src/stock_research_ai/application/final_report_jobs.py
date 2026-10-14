"""DETAILED_RESEARCH_V1 Box 15: the Detailed Research generation job lifecycle.

The extension asks for "this Screener page's Detailed Research"; the backend acquires the
company itself (no stage file, no CLI), runs the orchestrator and stores the report. This
module is only the lifecycle: an injected `generator` does the work (the live adapter wiring
lives in adapters/rendering/generate.py), so it is pure and testable offline.

    QUEUED -> RUNNING (ACQUIRING -> BUILDING) -> READY | PARTIAL | FAILED

Separate from the FULL_RESEARCH_V1 job manager on purpose (that pipeline is frozen); it
follows the same conventions: in-memory jobs, a bounded number active, duplicate requests
for the same page and as_of date join the running job, fixed failure messages (never an
exception text), and every job reaches a terminal state (a whole-job time bound).
"""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from stock_research_ai.application.report_orchestrator import DetailedResearchResult
from stock_research_ai.domain.report_rendering import FailureCode, ScreenerPage

_logger = logging.getLogger("stock_research_ai.final_report_jobs")
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_MAX_ACTIVE = 1
DEFAULT_MAX_RETAINED = 100
#: a generation that has not finished by then is FAILED (it can never stay "Generating")
DEFAULT_TIMEOUT_SECONDS = 1_800.0

Progress = Callable[[str], None]
Generator = Callable[[str, date, Progress], Awaitable[DetailedResearchResult]]

#: one fixed, user-safe sentence per failure code (never exception text)
MESSAGES = {
    FailureCode.COMPANY_IDENTITY_MISMATCH: (
        "The company identity could not be confirmed for this page; no report was produced."),
    FailureCode.SOURCE_ACQUISITION_FAILED: (
        "The company's Screener page could not be acquired. Try again later."),
    FailureCode.UPSTREAM_RESEARCH_FAILED: "A research step failed; no report was produced.",
    FailureCode.REPORT_COMPOSITION_FAILED: "The final report could not be composed.",
    FailureCode.RENDER_FAILED: "The report could not be rendered in any format.",
    FailureCode.PARTIAL_RENDER: "Some report formats could not be produced.",
    FailureCode.OUTPUT_IO_FAILED: "The report could not be saved locally.",
    FailureCode.INPUT_ERROR: "The research inputs were invalid; no report was produced.",
    FailureCode.GENERATION_TIMEOUT: "Detailed Research took too long and was stopped.",
}


class FinalReportGenerationError(ValueError):
    """An expected generation failure with a stable code (raised by a generator)."""

    def __init__(self, code: FailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FinalReportJobLimitError(RuntimeError):
    """`max_active` generations are already running; nothing was queued."""


class FinalReportJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


ACTIVE = (FinalReportJobStatus.QUEUED, FinalReportJobStatus.RUNNING)


class FinalReportJobFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: FailureCode
    message: str


class FinalReportJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    job_id: UUID
    screener_page: ScreenerPage  # the requested page (the report binds to the acquired one)
    source_url: str
    as_of: date
    status: FinalReportJobStatus
    stage: Literal["QUEUED", "ACQUIRING", "BUILDING", "DONE"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    artifact_id: UUID | None = None
    failed_formats: tuple[str, ...] = ()
    failure: FinalReportJobFailure | None = None


def _failure(code: FailureCode) -> FinalReportJobFailure:
    return FinalReportJobFailure(code=code, message=MESSAGES.get(code, MESSAGES[
        FailureCode.UPSTREAM_RESEARCH_FAILED]))


class FinalReportJobManager:
    def __init__(self, generator: Generator, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 max_active: int = DEFAULT_MAX_ACTIVE,
                 max_retained: int = DEFAULT_MAX_RETAINED,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._generator = generator
        self._clock = clock
        self._max_active = max_active
        self._max_retained = max_retained
        self._timeout = timeout_seconds
        self._jobs: OrderedDict[UUID, FinalReportJob] = OrderedDict()
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    # --- queries ---------------------------------------------------------------------------

    def get(self, job_id: UUID) -> FinalReportJob | None:
        return self._jobs.get(job_id)

    def _active(self) -> list[FinalReportJob]:
        return [j for j in self._jobs.values() if j.status in ACTIVE]

    # --- lifecycle -------------------------------------------------------------------------

    def launch(self, source_url: str, screener_page: str) -> FinalReportJob:
        """Start (or join) the generation for one page and today's as_of date (IST).
        `source_url` must already be a validated Screener company URL."""
        now = self._clock()
        as_of = now.astimezone(IST).date()
        for job in self._active():
            if job.screener_page == screener_page and job.as_of == as_of:
                return job  # the same company and day is already being generated
        if len(self._active()) >= self._max_active:
            raise FinalReportJobLimitError("Another Detailed Research is already running")
        job = FinalReportJob(job_id=uuid4(), screener_page=screener_page,
                             source_url=source_url, as_of=as_of,
                             status=FinalReportJobStatus.QUEUED, stage="QUEUED",
                             created_at=now, updated_at=now)
        self._jobs[job.job_id] = job
        self._trim()
        self._tasks[job.job_id] = asyncio.create_task(self._run(job.job_id))
        return job

    def _update(self, job_id: UUID, **fields: object) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._jobs[job_id] = job.model_copy(update={**fields,
                                                        "updated_at": self._clock()})

    def _trim(self) -> None:
        while len(self._jobs) > self._max_retained:
            oldest = next((k for k, j in self._jobs.items() if j.status not in ACTIVE), None)
            if oldest is None:
                return
            del self._jobs[oldest]

    async def _run(self, job_id: UUID) -> None:
        job = self._jobs[job_id]
        self._update(job_id, status=FinalReportJobStatus.RUNNING, stage="ACQUIRING")

        def progress(stage: str) -> None:
            if stage in ("ACQUIRING", "BUILDING"):
                self._update(job_id, stage=stage)

        try:
            result = await asyncio.wait_for(
                self._generator(job.source_url, job.as_of, progress), self._timeout)
        except FinalReportGenerationError as exc:
            self._finish(job_id, failure=_failure(exc.code))
            return
        except TimeoutError:
            self._finish(job_id, failure=_failure(FailureCode.GENERATION_TIMEOUT))
            return
        except asyncio.CancelledError:
            self._finish(job_id, failure=_failure(FailureCode.UPSTREAM_RESEARCH_FAILED))
            raise
        except Exception:  # noqa: BLE001 - logged, never shown: the job still terminates
            _logger.exception("Detailed Research generation failed")
            stage = self._jobs[job_id].stage
            self._finish(job_id, failure=_failure(
                FailureCode.SOURCE_ACQUISITION_FAILED if stage == "ACQUIRING"
                else FailureCode.UPSTREAM_RESEARCH_FAILED))
            return
        finally:
            self._tasks.pop(job_id, None)
        manifest = result.manifest
        failed = tuple(f.format.value for f in manifest.failures) if manifest else ()
        if result.status == "COMPLETE" and manifest is not None:
            self._finish(job_id, status=FinalReportJobStatus.READY,
                         artifact_id=manifest.artifact_id)
        elif result.status == "PARTIAL_RENDER" and manifest is not None:
            self._finish(job_id, status=FinalReportJobStatus.PARTIAL,
                         artifact_id=manifest.artifact_id, failed_formats=failed)
        else:
            code = result.failure.code if result.failure else FailureCode.RENDER_FAILED
            self._finish(job_id, failure=_failure(code), failed_formats=failed)

    def _finish(self, job_id: UUID, *, status: FinalReportJobStatus = FinalReportJobStatus.FAILED,
                failure: FinalReportJobFailure | None = None, **fields: object) -> None:
        self._update(job_id, status=status, stage="DONE", failure=failure, **fields)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


__all__ = [
    "FinalReportGenerationError",
    "FinalReportJob",
    "FinalReportJobFailure",
    "FinalReportJobLimitError",
    "FinalReportJobManager",
    "FinalReportJobStatus",
]
