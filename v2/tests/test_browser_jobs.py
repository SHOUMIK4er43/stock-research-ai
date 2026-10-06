"""Part 6B-A: in-memory browser research job lifecycle. Reuses test_research_plan.py's
real (offline, fully-mocked) engine fixtures rather than faking ResearchEngine itself -
BrowserJobManager's own job is calling application.research_plan.run_full_research
faithfully, so its tests should exercise that call for real.
"""

import asyncio

import pytest
from test_research_plan import YESBANK_URL
from test_research_plan import failure_engine_factory as failure_engine_factory  # noqa: F401
from test_research_plan import full_research_engine as full_research_engine  # noqa: F401

from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.application.browser_jobs import (
    BrowserJobManager,
    JobFailureCode,
    JobStatus,
    UnsupportedPortalError,
    UnsupportedResearchPlanError,
)
from stock_research_ai.domain.analysis import AnalystExecutionSpec


def _execution_factory() -> AnalystExecutionSpec:
    return AnalystExecutionSpec(template_id="full-research-v1", template_version=1)


async def _wait_until_terminal(manager: BrowserJobManager, job_id, timeout: float = 10.0):
    async def poll():
        while True:
            job = manager.get(job_id)
            assert job is not None
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                return job
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout)


async def test_launch_returns_promptly_and_transitions_to_completed(full_research_engine):
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)

    job = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    assert job.status in (JobStatus.QUEUED, JobStatus.RUNNING)  # returned before completion

    final = await _wait_until_terminal(manager, job.job_id)
    assert final.status == JobStatus.COMPLETED
    assert final.report_snapshot_id is not None
    assert final.challenge_snapshot_id is not None
    assert final.current_stage == "COMPLETED"
    # A successful COMPLETED run can still carry best-effort warnings (missing/partial
    # evidence never turns into FAILED - see application/research_plan.py).
    assert any(w.code == "CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE" for w in final.warnings)


async def test_disconnected_caller_does_not_cancel_the_job(full_research_engine):
    """Nothing about polling (or not polling) `get()` affects the background task."""
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)
    job = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    await asyncio.sleep(0)  # yield once, then simulate the caller vanishing entirely
    final = await _wait_until_terminal(manager, job.job_id)
    assert final.status == JobStatus.COMPLETED


async def test_duplicate_running_request_returns_the_same_job(full_research_engine):
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)

    first = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    second = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    assert first.job_id == second.job_id  # no second run created

    await _wait_until_terminal(manager, first.job_id)
    # Once completed, the same request is free to start a genuinely NEW job - the old
    # report is never mutated or rerun automatically.
    third = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    assert third.job_id != first.job_id
    await _wait_until_terminal(manager, third.job_id)


async def test_jobs_for_exposes_history_for_a_future_open_vs_rerun_decision(full_research_engine):
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)
    job = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    await _wait_until_terminal(manager, job.job_id)

    history = manager.jobs_for("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    assert history[0].job_id == job.job_id
    assert history[0].status == JobStatus.COMPLETED


def test_unsupported_portal_and_plan_are_rejected_synchronously_before_any_job(
    full_research_engine,
):
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)
    with pytest.raises(UnsupportedPortalError):
        manager.launch("TRADINGVIEW", YESBANK_URL, "FULL_RESEARCH_V1")
    with pytest.raises(UnsupportedResearchPlanError):
        manager.launch("SCREENER", YESBANK_URL, "SOME_OTHER_PLAN")
    assert manager.jobs_for("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1") == ()


async def test_stale_job_id_after_simulated_restart_is_genuinely_unknown(full_research_engine):
    """A fresh BrowserJobManager (simulating a backend restart) has no memory of a prior
    process's job_id - the HTTP layer maps this to a typed JOB_LOST_AFTER_SERVER_RESTART
    response rather than pretending the job is still running."""
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)
    job = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    await _wait_until_terminal(manager, job.job_id)

    fresh_manager = BrowserJobManager(
        engine, store, ScreenerBroadIndustrySource(), _execution_factory
    )
    assert fresh_manager.get(job.job_id) is None


@pytest.mark.parametrize(
    "failing_stage,expected_code",
    [
        ("EXTRACTION", JobFailureCode.EVIDENCE_ACQUISITION_FAILED),
        ("ANALYSIS", JobFailureCode.ANALYSIS_FAILED),
        ("CHALLENGE", JobFailureCode.CHALLENGE_FAILED),
        ("REPORT", JobFailureCode.REPORT_GENERATION_FAILED),
    ],
)
async def test_required_stage_failure_yields_the_matching_typed_failure_code(
    failure_engine_factory, failing_stage, expected_code
):
    engine, store = await failure_engine_factory(failing_stage)
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)

    job = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    final = await _wait_until_terminal(manager, job.job_id)

    assert final.status == JobStatus.FAILED
    assert final.report_snapshot_id is None
    assert final.failure is not None
    assert final.failure.code == expected_code
    # A duplicate request after FAILURE is free to try again (not stuck as "running").
    retried = manager.launch("SCREENER", YESBANK_URL, "FULL_RESEARCH_V1")
    assert retried.job_id != job.job_id
