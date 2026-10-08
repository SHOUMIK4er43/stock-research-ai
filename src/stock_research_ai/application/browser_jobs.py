"""In-memory browser research job lifecycle (Part 6B-A).

Calls application.research_plan.run_full_research (FULL_RESEARCH_V1) as the sole
execution authority - this module owns no research/financial/valuation/sector semantics
and never reimplements the execution graph. It owns only: job bookkeeping, duplicate-run
prevention, and mapping a raised exception to a typed, browser-facing failure code.

No DB table: jobs live purely in process memory. A backend restart loses every in-flight
job by design (see docs/part6b-a-proof.md) - completed ResearchReportSnapshots remain
persisted normally via the existing, unrelated report repository.

Resource bounds (single local user, single worker process):
- At most `max_active_jobs` (default 2) jobs are QUEUED/RUNNING at once. Each job runs
  evidence retrieval (2 concurrent downloads), sequential isolated document parsing (one
  child of up to 1 GiB at a time) and several AI calls; two jobs leave room to research a
  second company without letting a caller stack unbounded work. A further launch raises
  `ResearchJobLimitError` immediately (HTTP 429) - nothing is queued.
- At most `max_retained_jobs` (default 100) job records are kept. When a new job would
  exceed it, terminal (COMPLETED/FAILED) jobs are evicted oldest-finished first. Active
  jobs are never evicted; since the history bound always exceeds the active bound, room
  for a new job can always be made from terminal jobs.
"""

import asyncio
import re
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from stock_research_ai.application.challenge import ChallengeIntegrityError
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.application.ports import BroadIndustrySource, RawArtifactStore
from stock_research_ai.application.report import ReportIntegrityError
from stock_research_ai.application.research_plan import (
    FullResearchRequest,
    FullResearchWarning,
    ProgressStage,
    run_full_research,
)
from stock_research_ai.domain.analysis import AnalystExecutionSpec


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobFailureCode(StrEnum):
    UNSUPPORTED_PORTAL = "UNSUPPORTED_PORTAL"
    UNSUPPORTED_SOURCE_URL = "UNSUPPORTED_SOURCE_URL"
    LOCAL_CONFIGURATION_ERROR = "LOCAL_CONFIGURATION_ERROR"
    ANALYST_UNAVAILABLE = "ANALYST_UNAVAILABLE"
    EVIDENCE_ACQUISITION_FAILED = "EVIDENCE_ACQUISITION_FAILED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    CHALLENGE_FAILED = "CHALLENGE_FAILED"
    REPORT_GENERATION_FAILED = "REPORT_GENERATION_FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class UnsupportedPortalError(ValueError):
    """Raised synchronously by `launch()`, before any job is created."""


class UnsupportedResearchPlanError(ValueError):
    """Raised synchronously by `launch()`, before any job is created."""


class ResearchJobLimitError(RuntimeError):
    """Raised synchronously by `launch()` when `max_active_jobs` jobs are already
    QUEUED/RUNNING; no job is created and nothing is queued."""


DEFAULT_MAX_ACTIVE_JOBS = 2
DEFAULT_MAX_RETAINED_JOBS = 100


class JobFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: JobFailureCode
    message: str


class BrowserResearchJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID
    portal: Literal["SCREENER"]
    source_url: str
    research_plan: Literal["FULL_RESEARCH_V1"]
    status: JobStatus
    current_stage: ProgressStage | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    challenge_snapshot_id: UUID | None = None
    report_snapshot_id: UUID | None = None
    failure: JobFailure | None = None
    warnings: tuple[FullResearchWarning, ...] = ()


_SCREENER_PATH = re.compile(r"/company/([A-Za-z0-9_-]+)/(consolidated/)?")


def _canonical_url(source_url: str) -> str:
    """Canonical form of a Screener company URL for duplicate detection. Only dimensions
    Screener itself treats as equivalent are folded: host case and an explicit default
    port (443), and company slug case - Screener serves /company/infy/ as the INFY page
    (its rel=canonical is /company/INFY/...). The consolidated/standalone view stays
    distinct: they are different financial bases. Anything else (not a Screener company
    URL) keeps the previous scheme/host-lowercased, slash-trimmed form, never merged."""
    parts = urlsplit(source_url.strip())
    match = _SCREENER_PATH.fullmatch(parts.path)
    if (
        parts.scheme.lower() == "https"
        and parts.hostname == "www.screener.in"
        and parts.port in (None, 443)
        and not (parts.username or parts.password or parts.query or parts.fragment)
        and match
    ):
        return f"https://www.screener.in/company/{match.group(1).upper()}/{match.group(2) or ''}"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _canonical_key(portal: str, source_url: str, research_plan: str) -> tuple[str, str, str]:
    """URL-based dedup key. V1 limitation (documented in docs/part6b-a-proof.md): this
    key is never upgraded to a resolved canonical company identity mid-run - it is
    computed once, before extraction, from the source URL alone."""
    return portal, _canonical_url(source_url), research_plan


def _classify_failure(current_stage: ProgressStage | None, exc: Exception) -> JobFailureCode:
    message = str(exc).lower()
    if isinstance(exc, (ChallengeIntegrityError, ReportIntegrityError)):
        return JobFailureCode.INTEGRITY_FAILURE
    if "analyst services not configured" in message or "no live analyst provider" in message:
        return JobFailureCode.ANALYST_UNAVAILABLE
    if current_stage in (None, "PREPARING"):
        return JobFailureCode.EVIDENCE_ACQUISITION_FAILED
    if current_stage == "ANALYSIS":
        return JobFailureCode.ANALYSIS_FAILED
    if current_stage == "CHALLENGE":
        return JobFailureCode.CHALLENGE_FAILED
    if current_stage == "REPORT":
        return JobFailureCode.REPORT_GENERATION_FAILED
    return JobFailureCode.INTERNAL_ERROR


class _JobProgressSink:
    def __init__(self, manager: "BrowserJobManager", job_id: UUID) -> None:
        self._manager = manager
        self._job_id = job_id

    def on_stage_change(self, stage: ProgressStage) -> None:
        self._manager._set_stage(self._job_id, stage)  # noqa: SLF001


class BrowserJobManager:
    """Owns only in-memory job bookkeeping - it holds a single already-fully-wired
    ResearchEngine (constructed once by bootstrap for the lifetime of the HTTP process)
    and calls run_full_research on it per job. It never constructs its own engine, never
    touches adapters directly, and never reimplements FULL_RESEARCH_V1's execution
    graph."""

    def __init__(
        self,
        engine: ResearchEngine,
        artifacts: RawArtifactStore,
        broad_industry: BroadIndustrySource,
        execution_factory: Callable[[], AnalystExecutionSpec],
        *,
        max_active_jobs: int = DEFAULT_MAX_ACTIVE_JOBS,
        max_retained_jobs: int = DEFAULT_MAX_RETAINED_JOBS,
    ) -> None:
        if not 1 <= max_active_jobs < max_retained_jobs:
            raise ValueError("Require 1 <= max_active_jobs < max_retained_jobs")
        self._max_active_jobs = max_active_jobs
        self._max_retained_jobs = max_retained_jobs
        self._engine = engine
        self._artifacts = artifacts
        self._broad_industry = broad_industry
        self._execution_factory = execution_factory
        self._jobs: dict[UUID, BrowserResearchJob] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._running_by_key: dict[tuple[str, str, str], UUID] = {}
        self._active: set[UUID] = set()
        self._finished: deque[UUID] = deque()  # terminal job ids, oldest-finished first

    @property
    def engine(self) -> ResearchEngine:
        """Exposed so the HTTP layer can reach the same already-wired engine's
        persistent repositories (e.g. for report retrieval) when constructing its own
        independent accessors - this manager itself is never the source of truth for
        completed-report retrieval; `report_snapshot_id`/`challenge_snapshot_id` on a
        BrowserResearchJob are informational only (see docs/part6b-a-proof.md)."""
        return self._engine

    def launch(self, portal: str, source_url: str, research_plan: str) -> BrowserResearchJob:
        if portal != "SCREENER":
            raise UnsupportedPortalError(portal)
        if research_plan != "FULL_RESEARCH_V1":
            raise UnsupportedResearchPlanError(research_plan)
        key = _canonical_key(portal, source_url, research_plan)
        existing_id = self._running_by_key.get(key)
        if existing_id is not None:
            existing = self._jobs.get(existing_id)
            if existing is not None and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                return existing
        if len(self._active) >= self._max_active_jobs:
            raise ResearchJobLimitError(
                f"At most {self._max_active_jobs} research jobs can run at once"
            )
        self._evict_finished(room_for=1)
        now = datetime.now(UTC)
        job_id = uuid4()
        job = BrowserResearchJob(
            job_id=job_id,
            portal=portal,  # type: ignore[arg-type]
            source_url=source_url,
            research_plan=research_plan,  # type: ignore[arg-type]
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        self._jobs[job_id] = job
        self._running_by_key[key] = job_id
        self._active.add(job_id)
        task = asyncio.create_task(self._run(job_id, key))
        self._tasks[job_id] = task
        # A done-callback, not only _run's finally: a task cancelled before its first
        # step never enters _run, and its slot must still be released.
        task.add_done_callback(lambda _: self._release(job_id, key))
        return job

    def _release(self, job_id: UUID, key: tuple[str, str, str]) -> None:
        if job_id not in self._active:
            return
        if self._jobs[job_id].status in (JobStatus.QUEUED, JobStatus.RUNNING):
            self._update(
                job_id,
                status=JobStatus.FAILED,
                failure=JobFailure(
                    code=JobFailureCode.INTERNAL_ERROR, message="Research job was cancelled"
                ),
                completed_at=datetime.now(UTC),
            )
        if self._running_by_key.get(key) == job_id:
            del self._running_by_key[key]
        self._active.discard(job_id)
        self._tasks.pop(job_id, None)
        self._finished.append(job_id)

    def _evict_finished(self, room_for: int) -> None:
        while len(self._jobs) + room_for > self._max_retained_jobs and self._finished:
            self._jobs.pop(self._finished.popleft(), None)

    def get(self, job_id: UUID) -> BrowserResearchJob | None:
        return self._jobs.get(job_id)

    def jobs_for(
        self, portal: str, source_url: str, research_plan: str
    ) -> tuple[BrowserResearchJob, ...]:
        """Exposed only so a future UI can distinguish OPEN EXISTING REPORT from RUN
        FRESH RESEARCH by inspecting this process's own job history; this slice does not
        persist history beyond the process lifetime and does not itself choose an
        action (see docs/part6b-a-proof.md for the documented V1 limitation - there is
        no existing repository lookup for "latest report by company")."""
        key = _canonical_key(portal, source_url, research_plan)
        return tuple(
            job
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            if _canonical_key(job.portal, job.source_url, job.research_plan) == key
        )

    def _update(self, job_id: UUID, **changes: Any) -> None:
        current = self._jobs[job_id]
        self._jobs[job_id] = current.model_copy(
            update={**changes, "updated_at": datetime.now(UTC)}
        )

    def _set_stage(self, job_id: UUID, stage: ProgressStage) -> None:
        self._update(job_id, current_stage=stage)

    async def _run(self, job_id: UUID, key: tuple[str, str, str]) -> None:
        job = self._jobs[job_id]
        self._update(job_id, status=JobStatus.RUNNING, started_at=datetime.now(UTC))
        sink = _JobProgressSink(self, job_id)
        try:
            result = await run_full_research(
                self._engine,
                self._artifacts,
                self._broad_industry,
                FullResearchRequest(source_url=job.source_url),
                self._execution_factory(),
                progress=sink,
            )
        except Exception as exc:  # classified into a typed failure immediately below
            current_stage = self._jobs[job_id].current_stage
            code = _classify_failure(current_stage, exc)
            self._update(
                job_id,
                status=JobStatus.FAILED,
                failure=JobFailure(code=code, message=str(exc)),
                completed_at=datetime.now(UTC),
            )
        else:
            self._update(
                job_id,
                status=JobStatus.COMPLETED,
                challenge_snapshot_id=result.challenge_snapshot_id,
                report_snapshot_id=result.report_snapshot_id,
                warnings=result.stage_warnings,
                completed_at=datetime.now(UTC),
            )
        finally:
            self._release(job_id, key)
