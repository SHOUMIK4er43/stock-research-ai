# Part 6B-A implementation and proof — Local Browser API + Auth/Pairing + Research Job Lifecycle

Verified locally: 815 full tests passed (649 frozen prior + 57 frozen Part 6A + 49 frozen
Part 6B0, all unchanged in behavior, plus 60 new Part 6B-A tests: 10 in
`test_browser_auth.py`, 10 in `test_browser_jobs.py`, 40 in `test_browser_api.py`).
`tests/test_http.py`'s existing exact-route-count and resource-ownership tests were
updated mechanically (new routes added to the expected set; a second tracked bootstrap
function added to the ownership test) - no existing assertion was weakened or removed.
Ruff clean. Mypy clean across 88 source files. No dependency changes. No DB schema
changes. No commit/tag/push was made.

**Post-freeze-review fix**: the first pass of this milestone made completed-report
retrieval depend on `BrowserJobManager`'s in-memory job records (via a
`find_report_context` scan), which only worked for reports produced during the same
backend process's lifetime - contradicting the locked V1 persistence boundary (in-flight
jobs are ephemeral; completed `ResearchReportSnapshot`s are persistent). This has been
fixed: report/artifact retrieval now reads directly from the persistent
`ReportRepository`, independent of any job manager or process lifetime. See §15/§16/§18
below for the corrected design and its proof.

## Scope

Part 6B-A builds only the local, browser-facing execution layer: pairing/auth, research
job launch/polling, and report/artifact retrieval, all sitting thinly on top of the
existing, frozen `application.research_plan.run_full_research` (FULL_RESEARCH_V1) -
still the sole execution authority. No Chrome UI, no report viewer, no financial/
valuation/sector semantics of any kind live in this layer.

## 1. Transport: extended the existing FastAPI adapter, not a second backend

`interfaces/http.py` (the same file/app Part 1 already shipped) gained 6 new routes
alongside its 4 existing ones (unchanged): `GET /v1/version`, `POST /v1/pair`,
`POST /v1/research-jobs`, `GET /v1/research-jobs/{job_id}`, `GET /v1/reports/{report_id}`,
`GET /v1/reports/{report_id}/artifacts/{artifact_kind}` - 10 routes total, all under the
existing `/v1/...` convention (no separate `/api/v1` prefix was introduced, since the
existing app never used one - `/health` and `/v1/...` are both reused verbatim).

## 2. Public vs. protected

Public (loopback-reachable, no pairing required): `GET /health`, `GET /v1/capabilities`
(unchanged - exposes only booleans, no secret), `GET /v1/version`, `POST /v1/pair`
(origin-restricted to `chrome-extension://` at the pairing-code layer itself, not merely
by convention). Protected (both exact paired origin AND valid Bearer token required):
`POST /v1/research-jobs`, `GET /v1/research-jobs/{job_id}`, `GET /v1/reports/{report_id}`,
`GET /v1/reports/{report_id}/artifacts/{artifact_kind}`. The pre-existing
`POST /v1/companies`/`GET /v1/companies/{company_id}` remain exactly as before
(unauthenticated) - they are Part 1's own generic API, not part of the browser research
surface this milestone scopes, and changing their behavior was avoided per "do not weaken
old behavior."

## 3. Pairing: dynamic FIRST PAIR, backend-only

`adapters/http/browser_auth.py::PairingStore`, persisted at
`Settings.browser_auth_path` (default `~/.stock-research-ai/browser_auth.json`, the same
user-home convention as `database_path`; never under the Git repo; a dedicated new CLI
command `stock-research-ai pair-code` prints a fresh one-time code, since nothing else in
this backend-only slice could otherwise deliver it to a human).

- `issue_pairing_code()`: `secrets.token_urlsafe(16)`, 5-minute default TTL, only its
  SHA-256 hash persisted.
- `complete_pairing(origin, code)`: origin must start with `chrome-extension://`; code
  must match the pending hash (`hmac.compare_digest`), be unused, and be unexpired.
  Success replaces any previous paired record entirely (old token immediately invalid)
  and returns a `secrets.token_urlsafe(32)` token in plaintext exactly once.
- `verify(origin, token)`: **both** must match - origin by exact string equality, token
  by constant-time hash comparison. Neither alone ever suffices.
- File permissions set to `0600` after every write (best-effort via `os.chmod`).
- Never stores the Anthropic key or any `analyst_*` setting - a completely separate file,
  separate security domain from `config.py`'s `SecretStr` analyst credential.

## 4. CORS: dynamic, never a wildcard

`interfaces/http._BrowserCORSMiddleware` (raw ASGI, added because the paired origin is
only known at runtime after pairing - a static `CORSMiddleware` origin list computed at
app-construction time could never reflect it). It reflects `Access-Control-Allow-Origin`
only for: (a) the exact currently-paired `chrome-extension://` origin on any route, or
(b) *any* `chrome-extension://` origin, but *only* on `POST /v1/pair` (the narrow
pre-pairing accommodation the standing instructions explicitly allowed) - never a normal
`https://` site, never a wildcard. Critically, **this middleware does not itself block
any request from reaching a handler** - CORS only controls whether a browser's JS may
*read* a cross-origin response. The actual access-control enforcement is independent:
`_require_paired_client`, a FastAPI dependency that reads the real `Origin` header and
`Authorization: Bearer <token>` header from the request itself (nothing to do with CORS
response headers) and calls `PairingStore.verify(origin, token)`. This is what makes a
malicious `https://evil.example` page's `fetch()` POST to `/v1/research-jobs` fail with
401 even though the request physically reaches the server - the classic CSRF case the
standing instructions called out - proven directly by
`test_normal_https_website_cannot_launch_research`.

## 5. Loopback binding

Unchanged: `Settings.http_host` still defaults to `127.0.0.1` (Part 1, untouched); no new
setting was introduced that could override this to `0.0.0.0` by default. IPv6 not
addressed, as approved for V1.

## 6. `BrowserResearchRequest` and URL validation

`adapters/http/browser_schemas.py::BrowserResearchRequest{portal: Literal["SCREENER"],
source_url: str, research_plan: Literal["FULL_RESEARCH_V1"] = "FULL_RESEARCH_V1"}` -
exactly the locked contract; the Bearer token is never a field on it. An unknown portal
value is already rejected by FastAPI's own Pydantic `Literal` validation (422) before the
handler ever runs - `BrowserJobManager.launch()` re-checks defensively for any non-HTTP
caller. `source_url` is independently re-validated server-side via the existing, frozen
`adapters.screener.source.validate_url` (unchanged, unmodified) - the extension being
paired grants no trust over what URL it may submit. This structurally rejects `file://`,
`javascript:`, `data:`, `localhost`, `127.0.0.1`, arbitrary/lookalike domains, and
unapproved paths (all proven by `test_unsupported_source_urls_are_rejected`); a redirect
escaping `www.screener.in` is rejected later, during real acquisition, by the same
already-proven Part 2 redirect-revalidation loop (Part 6B0's own prior audit) - Part 6B-A
adds no new acquisition or redirect logic of its own.

## 7. Research job model and progress

`application/browser_jobs.py::BrowserResearchJob` - in-memory only (no DB table):
`job_id, portal, source_url, research_plan, status, current_stage, created_at,
started_at, updated_at, completed_at, challenge_snapshot_id, report_snapshot_id,
failure, warnings`. `JobStatus = QUEUED | RUNNING | COMPLETED | FAILED` (no `CANCELLED` -
cancellation is genuinely not implemented, per the standing instruction not to add it
without implementing it). `current_stage` is populated *only* from Part 6B0's own
`ProgressSink` callback (`PREPARING/EVIDENCE/ANALYSIS/CHALLENGE/REPORT/COMPLETED`) via a
thin `_JobProgressSink` adapter - `browser_jobs.py` invents no stage of its own and adds
no percentage.

## 8. Async execution

`BrowserJobManager.launch()` returns a `BrowserResearchJob` synchronously
(`status=QUEUED`) after starting `asyncio.create_task(self._run(...))` - it never awaits
`run_full_research` itself. `POST /v1/research-jobs` therefore returns promptly (202)
regardless of how long the underlying research takes; the extension polls
`GET /v1/research-jobs/{job_id}` afterward. No Celery/Redis/RQ/external queue - a single
in-process `asyncio.Task` per job, appropriate for this single-machine V1
(`test_launch_returns_promptly_and_transitions_to_completed`,
`test_disconnected_caller_does_not_cancel_the_job`).

## 9. Restart semantics

Confirmed exactly as specified: jobs live purely in `BrowserJobManager._jobs` (a plain
dict); a fresh `BrowserJobManager` (simulating a process restart) has zero memory of a
prior job_id (`test_stale_job_id_after_simulated_restart_is_genuinely_unknown`). The HTTP
layer maps *any* unknown `job_id` to a 404 with `{"code": "JOB_LOST_AFTER_SERVER_RESTART",
...}` - since a fresh in-memory manager cannot distinguish "genuinely never existed" from
"existed before a restart," treating every unknown ID this way is the honest, conservative
choice: it never claims a job is still running.

## 10. Duplicate-run policy

Keyed by `(portal, normalized_source_url, research_plan)` - normalization lowercases
scheme/host and strips a trailing slash from the path (`_canonical_key`). A second launch
request against the same key while the first is `QUEUED`/`RUNNING` returns the *existing*
job unchanged (`test_duplicate_running_request_returns_the_same_job`); after completion
or failure, the same request is free to start a genuinely new job - the prior report is
never mutated or automatically rerun. **Documented V1 limitation**: this key is computed
once from the source URL, before extraction resolves any canonical company identity, and
is never upgraded mid-run to that identity - matching the standing instruction to "safely
handle the source URL phase" before identity is known, without building the fuller
identity-based key this early.

## 11. OPEN EXISTING REPORT vs. RUN FRESH RESEARCH (documented limitation)

`BrowserJobManager.jobs_for(portal, source_url, research_plan)` exposes this process's
own in-memory job history (most-recent-first) so a future UI can distinguish these two
actions - but Part 6B-A does not persist this history beyond the process lifetime, and
does not itself choose between them (POST always launches, subject only to the
RUNNING-dedup rule above). **No large new report-history system was built**: there is no
existing repository method to look up "the latest report for a company" (`ReportRepository`
is keyed by `challenge_snapshot_id`, not company), and adding one was out of scope for
this slice per the standing instruction - left explicitly for Part 6B-B/C.

## 12. Typed failure model

`JobFailureCode`: `UNSUPPORTED_PORTAL, UNSUPPORTED_SOURCE_URL,
LOCAL_CONFIGURATION_ERROR, ANALYST_UNAVAILABLE, EVIDENCE_ACQUISITION_FAILED,
ANALYSIS_FAILED, CHALLENGE_FAILED, REPORT_GENERATION_FAILED, INTEGRITY_FAILURE,
INTERNAL_ERROR` (`JOB_LOST_AFTER_SERVER_RESTART` is a job-*retrieval*-time state, not a
job-*failure* code, since a lost job was never actually attempted by this process
instance). `UNSUPPORTED_PORTAL`/`UNSUPPORTED_SOURCE_URL` are raised synchronously by
`launch()`/route validation, before any job exists. All other codes are assigned inside
`BrowserJobManager._run`'s exception handler, classified by (a) `ChallengeIntegrityError`/
`ReportIntegrityError` isinstance checks (both frozen, application-layer exception types,
imported without violating the application/adapters layering rule), (b) an
"analyst services not configured"/"no live analyst provider" message match ->
`ANALYST_UNAVAILABLE`, or (c) the last observed `ProgressSink` stage otherwise. No Python
traceback is ever the primary API response - `HTTPException.detail` is always a
`{"code", "message"}` object; `JobFailure.message` is `str(exc)` only, never a traceback.

## 13. Failure vs. coverage - proven, not merely asserted

`test_launch_returns_promptly_and_transitions_to_completed` (and the underlying Part 6B0
tests it delegates to) confirms a `COMPLETED` job can and does carry best-effort warnings
(`CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE`, `DOCUMENT_RECENCY_UNVERIFIED`, etc.) - missing or
partial evidence never turns a run into `FAILED`. `FAILED` is reserved for genuine
required-stage exceptions, proven per-stage by
`test_required_stage_failure_yields_the_matching_typed_failure_code` (parametrized over
all 4 required stages, reusing `test_research_plan.py`'s own forced-failure doubles).

## 14. Missing Anthropic credential

`bootstrap.browser_engine` attaches `AnalystServices` only when `live_analyst_model`
actually returns a configured adapter - otherwise the engine's `analyst` slot stays
`None`, exactly mirroring the existing bare-engine convention. `Capabilities.
analysis_available` is unaffected code (unchanged) and correctly reports `False`.
`health`/`version`/`capabilities`/company routes are all unaffected and keep working
(`test_service_boots_and_health_works_without_analyst_configured`). A launched research
job reaches `ANALYSIS`, then fails there with `ANALYST_UNAVAILABLE` - never `FakeAnalystModel`,
never a silent `COMPLETED`.

## 15. Report/artifact retrieval - persistent, trusted IDs only, no reassembly

**Corrected design** (post-freeze-review fix): `GET /v1/reports/{report_id}` reads
directly from the persistent `ReportRepository` - `ReportRepository.get_report(
report_snapshot_id) -> ResearchReportSnapshot | None`, a new, additive, exact-ID lookup
method on the existing, frozen `ReportRepository` Protocol (`application/ports.py`) and
its `SQLiteReportRepository` implementation (`adapters/sqlite.py`). It mirrors
`AnalysisRepository.get_analysis`/`ChallengeRepository.get_challenge` **verbatim** -
both already existed with exactly this shape (`save_X`/`X_history(parent_id)`/
`get_X(id)`); `ReportRepository` was simply missing the third method, an evident gap
in the original Part 6A design that this fix closes using the established convention,
not a new one. Implementation is a plain `session.get(_ReportRow, str(id))` -
`_ReportRow.id` (the `report_snapshots` table's primary key) already **is** the report
snapshot ID, so this required no schema change whatsoever. Read-only: no
re-verification, no mutation, no new snapshot - integrity was already established once,
permanently, inside `save_report`'s own atomic `BEGIN IMMEDIATE` transaction (unchanged).

The HTTP layer's `_resolve_report` no longer touches `BrowserJobManager` at all - it
calls `current_report_repository().get_report(report_id)`, where `current_report_
repository()` resolves an independent `ReportRepository` handle derived from the same
already-wired engine the job manager itself uses (no second database connection is
opened for this purpose - see §9 below), but tracked as its own accessor, not read
through the job manager's in-memory `_jobs` dict. `BrowserJobManager.report_snapshot_id`/
`.challenge_snapshot_id` remain on `BrowserResearchJob` as informational job-status
fields only - **not** the source of truth for report retrieval.
`find_report_context` (which scanned `_jobs`) has been **removed entirely**: it has no
remaining legitimate purpose once retrieval reads directly from the repository, and
retaining it would have been dead architecture matching the very limitation being fixed.

Artifacts: `adapters/http/browser_artifacts.py` reuses the existing, frozen
`render_institutional_report`/`render_investor_brief` (already pure, in-memory) directly,
and reuses `export_report` **unmodified**, via a throwaway temp directory, for
`RESEARCH_JSON` - guaranteeing byte-identical output to the existing CLI/proof-script
artifact by construction (same function call), not a second implementation that could
drift. **Zero changes to `markdown_export.py`/`json_export.py`.** `artifact_kind` is a
closed `Literal["INSTITUTIONAL_REPORT","INVESTOR_BRIEF","RESEARCH_JSON"]` path parameter;
FastAPI/Starlette reject anything else before any handler code runs - proven for a range
of path-traversal payloads (`test_artifact_path_traversal_attempts_are_rejected`): a
percent-encoded value containing a real `/` never even matches the single-segment route
(404, rejected at routing); one without `/` reaches the handler and fails the closed
`Literal` (422). No filesystem path is ever accepted from or constructed for the client.

## 15a. Restart proof

`test_completed_report_survives_restart_but_old_job_does_not` (offline, real SQLite
file in `tmp_path`, no test-fake job manager reuse): process A runs a job to
`COMPLETED`, capturing `report_id`; its `BrowserJobManager`, `ResearchEngine`, and
`SQLiteDatabase` are then fully discarded (`del`, `await database.close()`). Process B
opens a **brand-new** `SQLiteDatabase`/`ResearchEngine`/`BrowserJobManager` against the
*same* database file, whose Screener transport is rigged to raise `AssertionError` if
called at all (proving retrieval triggers no new acquisition). Against process B: the
old `job_id` returns 404 `JOB_LOST_AFTER_SERVER_RESTART` (genuinely lost, exactly as V1
restart semantics require); `GET /v1/reports/{report_id}` returns 200 with the correct
ID; all three artifact kinds (`INSTITUTIONAL_REPORT`, `INVESTOR_BRIEF`, `RESEARCH_JSON`)
return 200 with real content. This is the direct proof of the fix.

## 15b. Integrity proof

`test_persisted_report_retrieval_uses_integrity_checked_reconstruction`: after a report
completes, its persisted row's `payload` column is corrupted in place (`schema_version`
set to an invalid value) via a raw SQL update, bypassing `save_report`'s own integrity
checks entirely (simulating a corrupted row already present in the DB). `GET /v1/reports/
{report_id}` then fails with a Pydantic `ValidationError` on the exact same canonical
`ResearchReportSnapshot.model_validate` reconstruction every other reader already uses -
proving the browser route has no separate "fast deserialize" path that could silently
serve corrupted data.

## 16. `ports.py`/`ReportRepository` - additive only

No frozen Protocol's existing methods changed. `ReportRepository` gained exactly one new
method (`get_report`), matching `AnalysisRepository`/`ChallengeRepository`'s own existing
convention verbatim. `application/browser_jobs.py` otherwise only imports
`application.ports.BroadIndustrySource`/`RawArtifactStore` (both already existed, added
in Part 6B0).

## 17. Files touched

New: `src/stock_research_ai/adapters/http/{__init__.py, browser_auth.py,
browser_schemas.py, browser_artifacts.py}`, `src/stock_research_ai/application/
browser_jobs.py`, `tests/test_browser_{auth,jobs,api}.py`.

Modified (additive only): `bootstrap.py` (+`browser_engine`, +`browser_execution_spec`
async context manager and helper - `full_research`/`live_analyst_model` themselves
untouched), `interfaces/http.py` (+6 routes, +CORS middleware, +auth dependency, +an
independent `current_report_repository()` accessor - the 4 pre-existing routes/handlers
are byte-identical), `adapters/sqlite.py` (+`SQLiteReportRepository.get_report`, purely
additive, no schema change), `application/ports.py` (+`ReportRepository.get_report` in
the Protocol), `config.py` (+`browser_auth_path` setting), `__main__.py` (+`pair-code`
subcommand), `tests/conftest.py` (+one env var so no test can ever touch a real
`~/.stock-research-ai/browser_auth.json`), `tests/test_http.py` (mechanical:
route-count assertion extended to 10 routes; the resource-ownership test now also
tracks the second, new `browser_engine` bootstrap call it was previously unaware of - no
existing assertion removed or weakened).

**Zero changes** to `domain/`, `application/analysis.py`, `application/
analysis_validation.py`, `application/evidence_matching.py`, `application/report.py`,
`application/challenge.py`, `application/valuation.py`, `application/research_plan.py`,
`application/sector_kpis.py`, `adapters/markdown_export.py`, `adapters/json_export.py`,
or any Part 1-6B0 research/financial/valuation/sector semantics. No DB schema/migration
(the `report_snapshots` table and its primary key were already exactly what the new
lookup needed).

## 9-audit. Two-SQLiteDatabase-connection audit (findings, not a refactor)

Two independent `SQLiteDatabase`/`ResearchEngine` instances are still opened in the
default (nothing injected) lifespan path: `_company_engine_context()` (a bare engine,
via the pre-existing `bootstrap.research_engine`, serving Part 1's own `/v1/companies`/
`/v1/capabilities`) and `_job_manager_context()` (a fully-wired engine, via
`bootstrap.browser_engine`, serving the job manager *and* - after this fix - report/
artifact retrieval too).

- **Why two exist**: `browser_engine`'s engine is in fact a strict superset of the bare
  engine's capabilities (it also supports `register_company`/`get_company`/
  `get_capabilities`, since those never depended on which optional services are
  attached). The separation is not driven by a real capability gap - it exists so that
  `tests/test_http.py::test_http_resource_ownership` (a frozen Part 1 test asserting
  open/close lifecycle around `research_engine` specifically) did not need to be
  restructured beyond the mechanical route/bootstrap-tracking update already made this
  slice. Consolidating to one engine would mean removing that call from the default
  path entirely, which is a conceptual rewrite of that test's premise, not a trivial
  extension - so, per the standing instruction's own fallback ("if not \[trivial\],
  leave it alone and document why it is safe enough"), it was left alone.
- **Are both necessary?** Not on data-model grounds - only on "avoid restructuring an
  unrelated, currently-passing Part 1 test" grounds.
- **Concurrent background-write + HTTP-read contention**: `SQLiteReportRepository.
  save_report` opens `BEGIN IMMEDIATE` (an exclusive write lock) for its integrity-
  check-then-insert transaction; `get_report`/`get_company`/etc. are plain reads.
  `SQLiteDatabase.__init__` configures only `PRAGMA foreign_keys=ON` - **no**
  `journal_mode=WAL` and **no** `busy_timeout` are set (confirmed by direct inspection;
  this is pre-existing, project-wide configuration, not something Part 6B-A
  introduced or could safely change in this slice's scope). In SQLite's default
  rollback-journal mode, a concurrent reader that lands exactly inside another
  connection's `BEGIN IMMEDIATE` window will raise `SQLITE_BUSY` (a loud, immediate
  `OperationalError`), not hang and not corrupt data.
- **Is this safe enough for V1?** Yes, in the sense that matters: it fails loudly and
  recoverably, never silently. The write transaction is brief (in-memory integrity
  checks then one insert, no network/I/O wait held inside it), and the product is
  explicitly single-user/single-machine, so the collision window in practice is small.
  A transient `SQLITE_BUSY` on a report-status poll is recoverable by the client's own
  next poll (it already polls repeatedly). Introducing WAL mode or a busy-timeout retry
  would be a real, valuable fix, but touches `SQLiteDatabase.__init__` - shared by every
  Part of this system - which is out of scope for this slice; **not done here**,
  **not silently worked around**, and flagged as a candidate for a dedicated,
  reviewed change later rather than an incidental one.
- No pooling infrastructure and no new dependency were introduced.

## 18. Known V1 limitations (carried into Part 6B-B/C)

- Duplicate-run key is URL-based, never upgraded to canonical company identity mid-run.
- `jobs_for()` exposes only this process's own in-memory history; no persisted
  cross-restart *report-history/listing* exists yet (single-known-ID retrieval is now
  fully restart-safe, per the fix above - this limitation is about listing/browsing,
  which remains explicitly out of scope).
- No pairing UI; a human obtains a pairing code via the new `pair-code` CLI command only.
- Two SQLite connections opened per HTTP process by default (see two-connection audit
  above) - safe-but-not-graceful under rare concurrent write/read collision; no WAL/
  busy-timeout configured project-wide.
