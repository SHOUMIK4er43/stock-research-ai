"""P1-3: bounded research-job concurrency and history, Screener URL dedup, and the
Summary rate limit. Research execution is replaced by a controllable fake; Screener and
the summary model are offline fakes (see test_summary_api)."""

import asyncio
import logging
from uuid import UUID

import httpx
import pytest
from test_summary_api import EXTENSION_ORIGIN, INFY_URL, YESBANK_URL, FakeSummaryModel, post_summary
from test_summary_api import configured_browser_origin as configured_browser_origin  # noqa: F401
from test_summary_api import summary_api as summary_api  # noqa: F401

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.http.rate_limit import RateLimited, SummaryRateLimiter
from stock_research_ai.application import browser_jobs
from stock_research_ai.application.browser_jobs import (
    BrowserJobManager,
    JobStatus,
    ResearchJobLimitError,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.summary import SummaryErrorCode, SummaryGenerationError
from stock_research_ai.interfaces import http as http_interface

INFY = "https://www.screener.in/company/INFY/"
PLAN = "FULL_RESEARCH_V1"
URLS = [f"https://www.screener.in/company/CO{i}/" for i in range(40)]


class FakeResearch:
    """Stands in for run_full_research: each job waits until the test settles it."""

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Future[None]] = {}
        self.started: list[str] = []

    async def __call__(self, _engine, _artifacts, _broad, request, _spec, progress):
        self.started.append(request.source_url)
        gate = self.gates.setdefault(
            request.source_url, asyncio.get_running_loop().create_future()
        )
        await gate

        class Result:
            challenge_snapshot_id = report_snapshot_id = None
            stage_warnings = ()

        return Result()

    def finish(self, url: str, error: Exception | None = None) -> None:
        gate = self.gates.setdefault(url, asyncio.get_running_loop().create_future())
        if error is None:
            gate.set_result(None)
        else:
            gate.set_exception(error)


@pytest.fixture
def research(monkeypatch):
    fake = FakeResearch()
    monkeypatch.setattr(browser_jobs, "run_full_research", fake)
    return fake


def manager(**limits) -> BrowserJobManager:
    return BrowserJobManager(None, None, None, lambda: None, **limits)  # type: ignore[arg-type]


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def launch(m: BrowserJobManager, url: str):
    return m.launch("SCREENER", url, "FULL_RESEARCH_V1")


# --- concurrency -----------------------------------------------------------------------


async def test_below_at_and_over_the_active_job_limit(research):
    m = manager(max_active_jobs=2)
    first = launch(m, URLS[0])  # below
    second = launch(m, URLS[1])  # exactly at the limit
    with pytest.raises(ResearchJobLimitError):
        launch(m, URLS[2])  # over: rejected, nothing queued or recorded
    assert {j.job_id for j in m.jobs_for("SCREENER", URLS[2], "FULL_RESEARCH_V1")} == set()
    assert len(m._jobs) == 2  # noqa: SLF001
    # A duplicate of a running job is still answered while at the limit.
    assert launch(m, URLS[0]).job_id == first.job_id
    await settle()
    assert research.started == [URLS[0], URLS[1]]
    assert second.status is JobStatus.QUEUED


async def test_slot_released_on_success(research):
    m = manager(max_active_jobs=1)
    job = launch(m, URLS[0])
    await settle()
    research.finish(URLS[0])
    await settle()
    assert m.get(job.job_id).status is JobStatus.COMPLETED
    launch(m, URLS[1])


async def test_slot_released_on_failure(research):
    m = manager(max_active_jobs=1)
    job = launch(m, URLS[0])
    await settle()
    research.finish(URLS[0], RuntimeError("boom"))
    await settle()
    assert m.get(job.job_id).status is JobStatus.FAILED
    launch(m, URLS[1])


@pytest.mark.parametrize("before_start", [False, True])
async def test_slot_released_on_cancellation(research, before_start):
    m = manager(max_active_jobs=1)
    job = launch(m, URLS[0])
    if not before_start:
        await settle()  # the task is running inside the fake research
    m._tasks[job.job_id].cancel()  # noqa: SLF001
    await settle()
    cancelled = m.get(job.job_id)
    assert cancelled.status is JobStatus.FAILED
    assert cancelled.failure.message == "Research job was cancelled"
    assert m._tasks == {}  # noqa: SLF001
    launch(m, URLS[1])


# --- bounded history -------------------------------------------------------------------


async def test_history_evicts_oldest_finished_jobs_only(research):
    m = manager(max_active_jobs=2, max_retained_jobs=5)
    finished: list[UUID] = []
    for url in URLS[:8]:
        job = launch(m, url)
        await settle()
        research.finish(url)
        await settle()
        finished.append(job.job_id)
    assert len(m._jobs) == 5  # noqa: SLF001
    assert list(m._jobs) == finished[3:]  # oldest-finished evicted first  # noqa: SLF001
    # Two active jobs plus history at the cap: launching evicts terminal jobs, never the
    # active ones.
    active = [launch(m, URLS[20]).job_id, launch(m, URLS[21]).job_id]
    assert len(m._jobs) == 5 and all(j in m._jobs for j in active)  # noqa: SLF001
    with pytest.raises(ResearchJobLimitError):
        launch(m, URLS[22])
    assert all(m.get(j).status in (JobStatus.QUEUED, JobStatus.RUNNING) for j in active)


def test_history_bound_must_exceed_active_bound():
    with pytest.raises(ValueError):
        manager(max_active_jobs=5, max_retained_jobs=5)
    with pytest.raises(ValueError):
        manager(max_active_jobs=0)


# --- dedup -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "https://www.screener.in/company/infy/",
        "https://WWW.SCREENER.IN/company/INFY/",
        "https://www.screener.in:443/company/Infy/",
        "  https://www.screener.in/company/INFY/  ",
    ],
)
async def test_equivalent_screener_urls_share_one_job(research, variant):
    m = manager()
    assert launch(m, INFY).job_id == launch(m, variant).job_id


@pytest.mark.parametrize(
    "other",
    [
        "https://www.screener.in/company/TCS/",
        "https://www.screener.in/company/INFY/consolidated/",  # a different financial basis
        "https://www.screener.in/company/INFYX/",
    ],
)
async def test_different_companies_or_views_never_dedupe(research, other):
    m = manager()
    assert launch(m, INFY).job_id != launch(m, other).job_id


# --- HTTP: job limit -------------------------------------------------------------------


async def test_http_job_limit_is_a_typed_429(research, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", EXTENSION_ORIGIN)
    store = PairingStore(tmp_path / "auth.json")
    token = store.complete_pairing(EXTENSION_ORIGIN, store.issue_pairing_code())
    app = http_interface.create_app(
        engine=engine,
        settings=Settings(_env_file=None, database_path=tmp_path / "db.sqlite3"),  # type: ignore[call-arg]
        job_manager=BrowserJobManager(engine, None, None, lambda: None, max_active_jobs=1),  # type: ignore[arg-type]
        pairing_store=store,
    )
    headers = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:

            def body(url):
                return {"portal": "SCREENER", "source_url": url, "research_plan": PLAN}

            accepted = await client.post("/v1/research-jobs", json=body(INFY), headers=headers)
            response = await client.post(
                "/v1/research-jobs", json=body(YESBANK_URL), headers=headers
            )
    assert accepted.status_code == 202
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "RESEARCH_JOB_LIMIT_REACHED"


# --- Summary rate limit ------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def limiter(monkeypatch):
    clock = Clock()
    shared = SummaryRateLimiter(limit=3, window_seconds=600.0, clock=clock)
    monkeypatch.setattr(http_interface, "SummaryRateLimiter", lambda: shared)
    return shared, clock


BODY = {"portal": "SCREENER", "source_url": INFY_URL}


async def test_summary_quota_threshold_429_and_recovery(summary_api, limiter):
    build, harness, token = summary_api
    _shared, clock = limiter
    app = build()
    for _ in range(3):
        assert (await post_summary(app, token, BODY)).status_code == 200
    rejected = await post_summary(app, token, BODY)
    assert rejected.status_code == 429
    assert rejected.json()["detail"]["code"] == "SUMMARY_RATE_LIMITED"
    assert rejected.headers["Retry-After"] == "600"
    assert len(harness.model.calls) == 3 and len(harness.screener_requests) == 3
    clock.now += 599.0
    assert (await post_summary(app, token, BODY)).status_code == 429
    clock.now += 1.0  # the first request leaves the window
    assert (await post_summary(app, token, BODY)).status_code == 200
    assert len(harness.model.calls) == 4


async def test_rejected_auth_and_invalid_requests_do_not_consume_quota(summary_api, limiter):
    build, harness, token = summary_api
    app = build()
    for _ in range(5):
        assert (await post_summary(app, "not-the-token", BODY)).status_code == 401
        assert (await post_summary(app, None, BODY)).status_code == 401
        bad = {"portal": "SCREENER", "source_url": "https://example.com/x"}
        assert (await post_summary(app, token, bad)).status_code == 422
    harness.factory_error = SummaryGenerationError(SummaryErrorCode.PROVIDER_NOT_CONFIGURED)
    assert (await post_summary(app, token, BODY)).status_code == 503
    harness.factory_error = None
    for _ in range(3):
        assert (await post_summary(app, token, BODY)).status_code == 200


async def test_provider_failure_still_counts(summary_api, limiter):
    build, harness, token = summary_api
    harness.model = FakeSummaryModel(SummaryGenerationError(SummaryErrorCode.PROVIDER_TIMEOUT))
    app = build()
    for _ in range(3):
        assert (await post_summary(app, token, BODY)).status_code == 504
    assert (await post_summary(app, token, BODY)).status_code == 429
    shared, _clock = limiter
    assert shared._in_flight == 0  # released after each failure  # noqa: SLF001


async def test_limiter_state_and_logs_hold_no_credentials(summary_api, limiter, caplog):
    build, _harness, token = summary_api
    caplog.set_level(logging.DEBUG)
    app = build()
    for _ in range(4):
        await post_summary(app, token, BODY)
    shared, _clock = limiter
    assert token not in repr(vars(shared))
    assert all(isinstance(t, float) for t in shared._accepted)  # noqa: SLF001
    assert token not in caplog.text


def test_in_flight_cap_and_bounded_state():
    clock = Clock()
    shared = SummaryRateLimiter(limit=3, window_seconds=60.0, max_in_flight=1, clock=clock)
    shared.acquire()
    with pytest.raises(RateLimited):
        shared.acquire()  # a duplicate while one is still running
    shared.release()
    shared.acquire()
    shared.release()
    shared.acquire()
    shared.release()
    with pytest.raises(RateLimited) as exc:
        shared.acquire()
    assert exc.value.retry_after_seconds == 60
    assert len(shared._accepted) == 3  # never more than `limit` timestamps  # noqa: SLF001
    clock.now += 60.0
    shared.acquire()


def test_polling_gets_are_not_rate_limited():
    # The limiter is only ever consulted by POST /v1/summaries.
    source = http_interface.__file__
    text = open(source).read()
    assert text.count("summary_limiter.acquire()") == 1
