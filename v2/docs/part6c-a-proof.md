# Part 6C-A Proof: Runtime / Reliability Hardening

Status: **implementation complete, not committed** (per instruction: DO NOT COMMIT / TAG / PUSH).

## 1. Scope delivered

Pure backend operational hardening. `extension/` is completely untouched (confirmed: `git diff --stat --
extension/` is empty). No research/financial/valuation/AnalystModel/Part 5B/5C/6A semantics were touched.

### New files
- `src/stock_research_ai/adapters/migrations.py` — `PRAGMA user_version`-based forward-only migration
  registry. `CURRENT_SCHEMA_VERSION = 1`. One migration, `0 -> 1`, with a no-op body (establishes version
  ownership for every pre-6C-A database; no table/column changed). `ensure_schema_version(connection)`
  reads the version, applies any pending migrations in order inside the caller's transaction, refuses
  (`UnsupportedSchemaVersionError`) a database newer than this build supports, and never advances the
  version past the last successfully-applied step on failure (`MigrationFailedError`).
- `src/stock_research_ai/adapters/secret_store.py` — `SecretStore` Protocol, `EnvironmentSecretStore`
  (read-only wrapper over `Settings.analyst_api_key`), `MacOSKeychainSecretStore` (built on
  `/usr/bin/security` via an injectable subprocess runner — **zero new dependency**),
  `resolve_analyst_api_key()` (the one place precedence — environment, then Keychain, then none — is
  decided), `store_analyst_api_key_in_keychain()`.
- `src/stock_research_ai/logging_setup.py` — stdlib `RotatingFileHandler` (2 MiB × 5 files) to
  `Settings.log_path`, `RedactingFilter` (redacts only the exact configured secret value(s) and the
  structural `Authorization: Bearer <token>` header shape — never a generic keyword).
- `src/stock_research_ai/diagnostics.py` — `gather_diagnostics()`/`format_diagnostics()`: safe production
  state only (booleans, paths, small enums) — never a secret, token, or research-data value.
- `src/stock_research_ai/backup.py` — `create_backup()`/`restore_backup()` using SQLite's own online
  backup API (`sqlite3.Connection.backup()`, run in a worker thread) — safe under WAL and against a
  concurrent writer, unlike a raw file copy.
- 6 new test files (74 new tests total — see §9).

### Modified files
- `src/stock_research_ai/adapters/sqlite.py` — WAL/busy_timeout/synchronous PRAGMAs on every connection;
  `initialize()` sets `user_version` directly and best-effort `chmod 0600`s the DB file; `check_ready()`
  now runs `ensure_schema_version()` transactionally (upgrading a legacy `user_version=0` database
  automatically, once, on next startup).
- `src/stock_research_ai/bootstrap.py` — `live_analyst_model()`/`browser_engine()`/`full_research()` gained
  an optional `secret_store` parameter threading Keychain fallback through; provider defaults to
  `"anthropic"` when unset but a Keychain credential exists.
- `src/stock_research_ai/config.py` — added `log_path` (same `~/.stock-research-ai/...` pattern as every
  other path field).
- `src/stock_research_ai/adapters/http/browser_auth.py` — pairing brute-force cooldown (5 failed attempts
  / 60s → 60s cooldown, in-memory only, success resets it, restart clears it).
- `src/stock_research_ai/interfaces/http.py` — catches the new `PairingLockedOutError` and returns 429;
  `FastAPI(version=...)` now uses the unified `__version__` instead of a hardcoded literal.
- `src/stock_research_ai/__main__.py` — new `configure-analyst`, `diagnostics`, `backup`, `restore`
  subcommands; `serve-http` now configures logging, logs the single-worker invariant, and reports a port
  conflict as a clean CLI error instead of a raw traceback.
- `src/stock_research_ai/__init__.py` — `__version__` now sourced from `importlib.metadata.version(...)`
  (backed by `pyproject.toml`) instead of a second hand-duplicated string.
- 3 existing test files updated **only** to keep them hermetic against the new Keychain-fallback code path
  (see §10 — this was the one necessary, minimal correction this slice required).

## 2. SQLite migration/version design

`PRAGMA user_version` only — **no new table**, per the explicit correction in this round's instructions.
`CURRENT_SCHEMA_VERSION = 1`. Every database `initialize()` has ever created (Parts 1–6B) is structurally
identical to what this module calls version 1 — migration `0 -> 1` is a documented no-op that exists solely
to give the pragma a real, owned meaning going forward.

## 3. Legacy `user_version=0` behavior

Verified directly: a database is simulated at `user_version=0` (SQLite's real default for any database
that predates this change — i.e., every existing Part 1–6B database on disk today), `check_ready()` is
called, and the version is confirmed to advance to `1` with **zero rows touched** — a real company row
written before the "upgrade" is proven to still read back correctly afterward
(`test_legacy_database_snapshots_remain_readable_after_migration`). No existing valid database is ever
recreated or deleted.

## 4. WAL behavior

`PRAGMA journal_mode=WAL` applied via the same `event.listens_for(engine, "connect")` hook already used for
`foreign_keys=ON` — applies automatically to **every** connection either of the two independent
`SQLiteDatabase` engines opens. Verified: `PRAGMA journal_mode` reports `"wal"` on a real file-backed
temp database. `:memory:` is not defensively handled because it is structurally unreachable —
`SQLiteDatabase.__init__` only ever accepts a `Path`, so no code path can construct one against `:memory:`;
stated rather than special-cased, per instruction. Production is local-filesystem-only by design (never a
network filesystem), documented here as the WAL compatibility boundary.

## 5. Busy timeout value

**5000 ms**, chosen because it comfortably exceeds any single ORM write transaction in this codebase (every
`save_*` method is one short `session.begin()` block containing only DB operations, never a
network/LLM `await` — see §8) while staying well under human-perceptible delay for the rare genuinely
contended case. Verified via `PRAGMA busy_timeout` reporting `5000`, and behaviorally: a writer holds a
transaction open (via `asyncio.Event` coordination) while a concurrent reader proceeds and completes
without waiting on it at all (WAL's actual benefit), proving the documented two-connection composition is
now safe.

## 6. Synchronous setting and justification

`PRAGMA synchronous=NORMAL`, adopted **because** `journal_mode=WAL` is already set — SQLite's own
documentation guarantees this combination can never corrupt the database; the only risk is losing the most
recent committed transaction(s) in the event of an OS-level crash or power loss (not an ordinary
application crash, which is fully safe). For a local single-user research tool that is not a financial
ledger, this is an acceptable, well-documented tradeoff for materially fewer fsyncs. Verified via
`PRAGMA synchronous` reporting `1` (NORMAL).

## 7. Two-connection concurrency proof

No architectural refactor was performed (per explicit instruction). Instead,
`test_concurrent_writer_and_reader_do_not_immediately_raise_database_is_locked` proves the *actual*
documented composition — one connection holding a write transaction open while another performs a read —
now succeeds without an immediate `database is locked` error, which is exactly the failure mode WAL +
busy_timeout exist to close. The two independent `SQLiteDatabase` instances in `create_app()`
(`research_engine`'s and `browser_engine`'s) remain architecturally unchanged; they are simply now both
configured safely, as instructed.

## 8. Transaction-duration audit

Grep-audited every `session.begin()`/`engine.begin()` block in `adapters/sqlite.py` (21 occurrences): every
one is a tight, self-contained write of already-computed data (`session.add`/`session.get`/
`session.execute` only) — none accepts a network client or performs an HTTP/LLM call inside the
transaction. Data acquisition (Screener HTTP, document PDF parsing, Anthropic calls) happens entirely in
the `application/` layer *before* any `save_*` method is ever called; the transaction itself never spans
an `await` on external I/O. No production bug was found, so no change was made — this is a confirmation,
not a correction.

## 9. Migration failure behavior

`test_a_failed_migration_never_falsely_advances_the_version`: a deliberately-broken migration is injected
(monkeypatched registry), its `apply()` raises, `MigrationFailedError` propagates, and `PRAGMA user_version`
is proven to remain at its pre-migration value — never a half-applied state. A database newer than
`CURRENT_SCHEMA_VERSION` is refused outright (`UnsupportedSchemaVersionError` → wrapped as
`DatabaseNotReadyError` at the `SQLiteDatabase.check_ready()` boundary, the same typed error every existing
caller already handles).

## 10. macOS Keychain architecture

`/usr/bin/security` via an injectable subprocess runner — **zero new dependency**, per the instruction's
explicit preference. `SecretStore` Protocol with `EnvironmentSecretStore` and `MacOSKeychainSecretStore`
adapters. Values are passed as discrete argv elements (never shell-interpolated), proven with a value
containing shell metacharacters. A missing/unreachable `security` binary is caught and reported as
`SecretStoreUnavailableError`, never a crash. **One necessary correction to existing 6B0 tests**: adding
the Keychain fallback meant `live_analyst_model`/`full_research` calls with no configured credential would,
by default, now shell out to the *real* `/usr/bin/security` on the machine running the test suite (harmless
but non-hermetic). Fixed by threading an explicit, injectable `secret_store` parameter through
`live_analyst_model`/`full_research`/`browser_engine` (mirroring the existing dependency-injection
convention already used everywhere else in `bootstrap.py`) and updating the one existing test that hit this
path (`test_full_research_raises_explicitly_at_analysis_when_unconfigured`) plus the pre-existing absent-
configuration test to inject a deterministic no-op stub. No test in the suite touches the real OS Keychain.

## 11. Secret precedence

Exactly two sources, checked in order, both proven exhaustively: `test_environment_value_wins_and_keychain_
is_never_even_consulted` (an exploding fake Keychain proves it is never even called when env is present),
`test_keychain_is_used_when_environment_is_absent`, `test_neither_source_available_reports_none_never_
invents_a_value`, `test_an_unavailable_keychain_backend_reports_none_rather_than_raising`,
`test_no_third_hidden_secret_source_exists`. Diagnostics reports only `credential_source:
environment|keychain|none` — never the value (verified: the configured secret literal is asserted absent
from every diagnostics-rendered string).

## 12. Configure CLI

`stock-research-ai configure-analyst` — `getpass.getpass()` (input never echoed to the terminal), stores
via `store_analyst_api_key_in_keychain()`, rejects an empty key, never writes a plaintext config file.
Verified end-to-end through the real CLI dispatch with a fake Keychain injected.

## 13. Production paths

`log_path` added to `Settings` following the *exact* existing pattern (`Path.home() / ".stock-research-ai"
/ ...`, `.expanduser().resolve()` validator) already used by `database_path`/`artifact_path`/
`browser_auth_path`. No new config-file mechanism was introduced for non-secret settings — every one of
them (model ID, port, DB path) already has a working, tested environment-variable mechanism; only the
*secret* needed a new storage mechanism, which Keychain now provides. This was a deliberate scope decision
(§46 below) to avoid inventing an unneeded new file format.

## 14. File permissions

Best-effort `chmod 0600` (matching `browser_auth.json`'s existing, already-reviewed pattern exactly) now
also applied to: the SQLite database file (`initialize()`), the rotating log file
(`logging_setup.configure_logging()`), and every file a backup/restore writes. All verified with a direct
`stat()` check. Cross-platform caveat documented in each docstring: POSIX permission bits are meaningless
on Windows; this is accepted, not silently pretended-solved.

## 15. Logging architecture

Stdlib `logging` + `RotatingFileHandler` only (2 MiB × 5 backups). Wired **only** into `serve-http` (the
long-running process) — the existing one-shot CLI commands keep their own already-tested stdout/stderr
output, per the stated 6C-A scope. Log lines: `timestamp severity logger.name: message` — component
attribution via `get_logger("jobs")`/`get_logger("pairing")`-style child loggers.

## 16. Redaction proof

Two independent mechanisms, both tested: (a) exact known secret values (passed to `configure_logging` at
startup) are string-replaced with `[REDACTED]` wherever they appear, including inside `%s`-style log args;
(b) the structural `Authorization: Bearer <token>` pattern is redacted via regex regardless of whether the
specific token value was known ahead of time. Explicitly proven **not** to corrupt ordinary text: a real
filing signatory line — `"Signed: Sanjay Abhyankar, Company Secretary"` — is asserted to pass through
completely untouched, directly exercising the exact false-positive the instructions warned about.

## 17. Diagnostics command

`stock-research-ai diagnostics` — application/API/report-schema version, DB path/reachability/schema
version/support, HTTP bind, `analyst_provider_configured` (boolean), `credential_source`, model ID, pairing
configured (boolean), log/artifact/browser-auth paths. Verified never to include: the Anthropic key, the
pairing token or pairing code, or any report/database row content.

## 18. Health vs readiness

**Evaluated, deliberately not implemented as a public-endpoint change this slice.** `/health` remains
exactly as-is (cheap, unauthenticated liveness). Extending `/v1/version` or `/v1/capabilities` was
considered; `/v1/capabilities`'s response model (`application.engine.Capabilities`) is shared verbatim with
the MCP interface and is explicitly "Existing Part 1 routes: unchanged" — touching it for an HTTP-only
operational concern risked exactly the kind of cross-interface semantic bleed the stop-conditions warn
against, for a benefit the new `diagnostics` CLI command already delivers in full (DB reachable, schema
version, config parsable — all present there, all covered by tests). `/v1/version` itself was left
completely behaviorally unchanged (only its already-correct-shaped `application_version` field's *source*
was fixed, not its meaning). This is a scope decision, documented rather than silently skipped — revisit
only if the extension itself is later found to need this at the network layer.

## 19. HTTP production config

`host=127.0.0.1` (unchanged, already correct), `port` configurable (unchanged), `reload` never enabled
(unchanged) — confirmed via source inspection, no dev/debug mode exists to begin with. Logging is now
configured before `uvicorn.run()`, and the single-worker invariant is logged explicitly at startup.

## 20. One-worker enforcement

No `--workers` CLI flag was ever exposed and none was added. Proven behaviorally
(`test_serve_http_never_passes_a_worker_count_to_uvicorn`): `uvicorn.run()` is called with zero `workers`
kwargs and with a real `FastAPI` **instance** (not an import string) — uvicorn's own multi-worker mode
structurally requires the latter, so this call shape cannot run more than one worker even in principle.

## 21. Graceful-shutdown behavior

**Already correct — verified, not built.** `research_engine()`/`browser_engine()` are `@asynccontextmanager`s
with `try/finally: await database.close()` (→ `engine.dispose()`), and `create_app()`'s FastAPI `lifespan`
wraps both; a SIGINT/SIGTERM through `uvicorn.run()` triggers ASGI lifespan shutdown, which runs this
existing, already-tested cleanup path. No new shutdown machinery was built. In-flight `BrowserJobManager`
jobs are lost on shutdown — this is the pre-existing, already-typed (`JOB_LOST_AFTER_SERVER_RESTART`) V1
contract, explicitly retained per instruction (§22/§23: "V1 behavior may remain: service termination loses
active BrowserJob").

## 22. Lost-job behavior

Unchanged from Part 6B. A terminated/crashed service loses any in-flight `BrowserResearchJob` (in-memory
only); the persisted `ResearchReportSnapshot` for any job that had already reached `COMPLETED` remains
fully safe and retrievable (already proven in the Part 6B final audit; re-confirmed unaffected here since
`ReportRepository`/`SQLiteReportRepository` were not touched by this slice beyond the additive
`user_version` pragma work, which never touches report rows).

## 23. Port-conflict behavior

`serve-http` now catches `OSError` with `errno.EADDRINUSE` specifically and reports a clear, actionable CLI
message via `parser.error()` (clean `SystemExit`, not a raw traceback) — any *other* `OSError` still
propagates unchanged (verified: `test_serve_http_reraises_an_unrelated_os_error`). No automatic fallback to
a random port — the operator is told to free the port or set `STOCK_RESEARCH_AI_HTTP_PORT`.

## 24. Path validation

Backup destination: refuses a non-empty existing directory (never silently overwrites). Restore: refuses a
directory that isn't a real backup (`backup_metadata.json` missing), refuses to overwrite an existing
database without `--force`, and even with `--force` takes a safety copy of the current database first
(`*.pre-restore-backup`) before replacing it. All server-controlled paths (DB/artifact/log/browser-auth)
remain resolved via the existing `.expanduser().resolve()` validator pattern — never browser/user-request
input, so there is no new traversal surface (the browser-facing artifact-kind/report-id traversal
protections audited in Part 6B remain untouched and unaffected).

## 25. Pairing brute-force protection

5 failed `/v1/pair` attempts within any 60-second sliding window → a 60-second cooldown, entirely
in-memory (`PairingStore` instance state, no new DB table). A successful pair resets the counter. A service
restart clears it entirely (proven directly: a fresh `PairingStore` over the same file has no lockout).
Old failures outside the window don't count toward the threshold (sliding window, proven). The HTTP layer
returns `429` with `{"code": "PAIRING_LOCKED_OUT"}` — verified through the real route via `httpx.ASGITransport`,
not just at the `PairingStore` unit level. The lockout error message never includes the attempted code or
any token.

## 26. Backup behavior

Uses `sqlite3.Connection.backup()` (SQLite's own online/consistent-snapshot backup API, in a worker
thread) — safe under WAL and against a concurrent writer, unlike copying the raw file (which could miss
data still only in the `-wal` file). Copies the artifact directory (`shutil.copytree`) when present. Writes
`backup_metadata.json` (`application_version`, `schema_version`, `created_at`). **Never** includes the
Anthropic API key (never in the DB/artifacts to begin with) or the pairing state file (excluded, documented
— a restored install re-pairs). A failed backup (e.g. a non-empty destination) is proven to leave the
source database byte-for-byte untouched.

## 27. Restore behavior / decision

Implemented in full this slice (assessed as not materially larger than backup, so not deferred to 6C-B).
Validates `backup_metadata.json`'s `schema_version` against `CURRENT_SCHEMA_VERSION` and refuses a newer,
unsupported backup. Refuses to overwrite an existing database without `force=True`; with `force=True`,
takes a safety backup of the currently-overwritten database first (`*.pre-restore-backup`), proven present
after a forced restore. The caller (the operator) is responsible for stopping `serve-http` first — this is
documented in the function's own docstring, not silently assumed.

## 28. Data retention

Unchanged from the approved architecture: no automatic deletion of research snapshots, raw evidence, or
generated reports anywhere in this slice — nothing in 6C-A introduces any such deletion path. Only the new
rotating log file has bounded retention (2 MiB × 5 files), by design.

## 29. Application versioning

`src/stock_research_ai/__init__.py::__version__` now reads `importlib.metadata.version("stock-research-ai")`
(backed by `pyproject.toml`'s `[project].version`) instead of a second hand-typed `"0.1.0"` literal.
`interfaces/http.py`'s `FastAPI(version=...)` (previously its own separate hardcoded `"0.1.0"`, invisible
today since `docs_url`/`openapi_url` are disabled but a real duplicate nonetheless) now uses the same
`__version__`. `/v1/version`'s `application_version` field is proven, via a new test, to equal
`importlib.metadata.version("stock-research-ai")` directly — there is now exactly one authoritative Python
version string. Extension versioning was explicitly left untouched (belongs to 6C-B per instruction).

## 30. Customer-facing failure behavior

Port conflict: clear CLI message (§23). Unsupported/newer DB schema: `UnsupportedSchemaVersionError` →
`DatabaseNotReadyError`, the same typed, already-user-facing error class every existing caller handles.
Pairing lockout: typed `429`/`PAIRING_LOCKED_OUT`, never a bare 401 indistinguishable from "wrong code".
Backup/restore failures: typed `BackupError` → clean `parser.error()` CLI message, never a raw traceback
(verified). Empty `configure-analyst` input: clean CLI error, never silently stored.

## 31. Telemetry status

Unchanged: **no telemetry, no analytics, no crash SDK, no cloud log shipping** anywhere in this slice.
Logging is a local rotating file only, under the user's own `~/.stock-research-ai/logs/` directory.

## 32. DB table/column changes

**None.** The entire schema diff for this slice is the `PRAGMA user_version` mechanism (not a table) plus
the pre-existing `PRAGMA foreign_keys=ON`/new `journal_mode`/`busy_timeout`/`synchronous` connection
pragmas — zero changes to any existing table's columns, zero new tables. Confirmed via `git diff` scope
(only `adapters/sqlite.py`'s connect-hook and `initialize()`/`check_ready()` methods changed; no
`_Base`-derived model class was added or modified).

## 33. Dependency changes

**None.** `git diff --stat -- pyproject.toml uv.lock` is empty. The macOS Keychain integration uses
`/usr/bin/security` via `subprocess` (stdlib) — no `keyring` package, exactly as instructed ("Do NOT install
keyring unless explicitly reviewed after a blocker" — no blocker was hit).

## 34. Extension changes

**None.** `git diff --stat -- extension/` is empty. `cd extension && npm test` still reports 165/165 passed
— the exact frozen Part 6B baseline, byte-for-byte unchanged.

## 35. Exact changed-file inventory

10 modified, 10 new, **20 total**:

```
 M src/stock_research_ai/__init__.py
 M src/stock_research_ai/__main__.py
 M src/stock_research_ai/adapters/http/browser_auth.py
 M src/stock_research_ai/adapters/sqlite.py
 M src/stock_research_ai/bootstrap.py
 M src/stock_research_ai/config.py
 M src/stock_research_ai/interfaces/http.py
 M tests/test_bootstrap_full_research.py
 M tests/test_browser_api.py
 M tests/test_browser_auth.py
?? src/stock_research_ai/adapters/migrations.py
?? src/stock_research_ai/adapters/secret_store.py
?? src/stock_research_ai/backup.py
?? src/stock_research_ai/diagnostics.py
?? src/stock_research_ai/logging_setup.py
?? tests/test_backup.py
?? tests/test_cli_6c_a.py
?? tests/test_diagnostics.py
?? tests/test_logging_setup.py
?? tests/test_secret_store.py
?? tests/test_sqlite_hardening.py
```

Plus this proof document (`docs/part6c-a-proof.md`).

## 36. Targeted Part 6C-A tests

74 new tests: `test_sqlite_hardening.py` (15), `test_secret_store.py` (15), `test_logging_setup.py` (9),
`test_diagnostics.py` (7), `test_backup.py` (11), `test_cli_6c_a.py` (9), plus 6 new tests appended to
`test_browser_auth.py` (pairing brute-force) and 2 new tests appended to `test_browser_api.py` (HTTP-level
lockout + version-unification). 815 (frozen Part 6B baseline) + 74 = **889**, matching the full-suite count
exactly.

## 37. Extension test count

165 passed (unchanged; `extension/` untouched).

## 38. Python full pytest count

889 passed, 7 pre-existing warnings (unrelated Starlette deprecation, unchanged from every prior part's
baseline).

## 39. Ruff

All checks passed (repo-wide, including all new/modified files).

## 40. mypy/source count

Success — no issues found in **93 source files** (88 + 5 new modules: `migrations.py`, `secret_store.py`,
`logging_setup.py`, `diagnostics.py`, `backup.py`).

## 41. `git diff --check`

Clean (exit 0).

## 42. `git status`

10 modified, 10 new files as listed in §35 — nothing else.

## 43. Unresolved blockers before 6C-B

None identified as blocking. Two deliberate, documented scope decisions carried forward (not blockers,
choices): (a) no general non-secret config-file mechanism was introduced (§13 — every non-secret setting
already has a working env-var path; only the secret needed Keychain); (b) `/v1/version`/`/v1/capabilities`
were not extended with readiness fields (§18 — fully covered instead by the new `diagnostics` CLI command).
Both are revisitable in a later part if a concrete need emerges; neither blocks packaging/distribution work.

## 44. Recommendation

**READY FOR 6C-B.**

PART 6C-A IMPLEMENTATION COMPLETE — AWAITING REVIEW.
