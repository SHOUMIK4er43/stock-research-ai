# ASTRA R2-A — Database schema safety and observational diagnostics

Base: `astra-development` at `1e5ff9d03957f8ef1842a9e4671140e7c449096a`.
R1 remains frozen; main remains `8453c72b265cb08b43c7b882726a9e941c977355`.
This slice is pending review and is not committed.

## Reproduction before production changes

The initial 21-case offline test file produced **14 failures and 7 passes**.
All databases were disposable, file-backed SQLite files under pytest temporary
paths. No production database or live research service was used.

- A database with `user_version = 10` (current version 1 plus 9) was changed to 1
  through each real entry point: `SQLiteDatabase.initialize`, bootstrap
  `initialize_database`, and CLI `main(["init-db"])`. Both a full application
  schema with existing rows and a future-only table fixture reproduced this.
- Diagnostics changed an initialized legacy database from version 0 to 1, including
  through `main(["diagnostics"])`.
- Diagnostics also changed existing DELETE journal mode to WAL, even for current
  and future databases. This was a separate persistent effect of connection setup.
- Current-version initialization unnecessarily wrote `user_version` again.
  Injecting a failing migration did not affect initialization because it bypassed
  the migration registry entirely.

## Call graph and design conclusion

```text
CLI init-db
  → bootstrap.initialize_database(settings)
  → SQLiteDatabase.initialize()
  BEFORE: create_all → check table shape → unconditional user_version = current
  AFTER: read version → reject unsupported future version
       → create_all → check table shape → existing forward migration registry
       → existing company identity backfill

Installer / upgrade (scripts/install-macos.sh)
  → existing binary backup (upgrade only)
  → uv tool install
  → installed binary init-db → same initialization path above
  → render LaunchAgent / launch service

CLI diagnostics
  → gather_diagnostics(settings)
  BEFORE: default rw connection (sets WAL)
       → check_ready → table check → ensure_schema_version → read version again
  AFTER: mode=ro connection (no WAL/synchronous setters)
       → inspect_schema_version → existing read_schema_version
       → report reachability, actual version, version compatibility

serve-http
  → create_app lifespan
  → research_engine and browser_engine
  → SQLiteDatabase.check_ready
  → existing shape validation → existing ensure_schema_version

backup / restore
  → backup.py: stdlib SQLite online backup API
  → restore validates backup_metadata.json version against current version
  → no initialize/check_ready invocation during restore
  → subsequent normal startup uses check_ready
```

Installer, upgrade shell flow, backup/restore implementation and format, HTTP
startup wiring, research semantics and repository snapshot behavior are unchanged.
Existing installer tests exercise the shell scripts with fake external executables;
new CLI tests exercise the actual initialization endpoint used by those scripts.

The design keeps three distinct responsibilities: initialization may create tables
and establish ownership; readiness retains its approved forward migration behavior;
diagnostics observes without taking ownership or migrating. The existing
`read_schema_version` remains the common authoritative PRAGMA reader.
`require_supported_schema_version` factors the existing future-version rejection
into one shared guard, used both before initialization DDL and by the existing
migration runner. No parallel migration implementation or new version store exists.

## Before / after behavior

| Database state | Initialize after R2-A | check_ready after R2-A | Diagnostics after R2-A |
| --- | --- | --- | --- |
| Missing | Explicitly creates file/schema and establishes current version | Refuses; creates nothing | Unreachable, version null, supported false; creates nothing |
| Existing empty SQLite file, version 0 | Creates required structure, then runs ownership migration | Refuses absent tables | Reachable, version 0, supported true; no tables created |
| Structurally valid legacy version 0 | Runs existing 0 → 1 ownership migration; rows preserved | Existing 0 → 1 behavior preserved | Reachable, version 0, supported true; no migration |
| Current version 1 | Existing table checks/backfill retained; no version write | Existing shape checks; no version write | Reachable, version 1, supported true; no version write |
| Future version > 1 | Raises UnsupportedSchemaVersionError before DDL/backfill/migration; version and rows preserved | Refuses with existing DatabaseNotReadyError boundary; no downgrade | Reachable if SQLite version is readable, actual future version, supported false |
| Invalid/unreadable SQLite file | Existing error behavior | Existing refusal | Unreachable, version null, supported false |
| Migration failure | MigrationFailedError; no false version advancement | Existing wrapped error behavior | Does not enter migration code |

`database_reachable` now means a SQLite version query succeeded. It is not a synonym
for “the application is ready.” `database_schema_supported` reports version
compatibility only. Table/column checks remain in `check_ready`; a reachable empty
version-0 file is correctly reported as version 0 but still cannot start the app.
Future databases no longer misleadingly appear unreachable with an unknown version.

## Exact changes

1. `src/stock_research_ai/adapters/migrations.py`: extract the existing future-version
   guard; keep current version, registry, migrations and PRAGMA reader unchanged.
2. `src/stock_research_ai/adapters/sqlite.py`: optional `read_only=True` connection
   mode; observational version method; future guard before initialization DDL;
   initialization uses the existing migration runner instead of directly stamping
   the version. Read-only initialization is rejected before filesystem creation.
3. `src/stock_research_ai/diagnostics.py`: use read-only inspection, not readiness;
   preserve actual future version and distinguish reachability from compatibility.
4. `tests/test_schema_safety.py`: 27 offline regression cases.
5. `docs/astra-r2a-schema-safety.md`: this design and proof record.

There are no dependency, research table/column, migration table, golden fixture,
backup-format, extension, release packaging or R1 changes.

## SQLite write boundaries

Diagnostics uses SQLite URI `mode=ro`; SQLite itself rejects attempted application
row and `user_version` writes. It sets only connection-local `foreign_keys=ON` and
`busy_timeout=5000`. It does **not** set `journal_mode` or `synchronous`, create the
DB, create tables, run backfills/migrations, checkpoint, or change version ownership.

The tests verify identical main-file bytes, logical dumps, version and journal mode
for closed DELETE-mode fixtures at versions 0, 1 and 10. A live WAL fixture retains
identical main-file and WAL bytes while diagnostics sees the committed version 0
stored in the WAL. Thus inspection does not accidentally ignore the live WAL or
checkpoint it. Explicit version/row writes through the read-only engine fail.

This is **not a guarantee of zero filesystem activity**: SQLite can create/use or
update WAL shared-memory (`-shm`) coordination sidecars and locks during read-only
access, subject to directory permissions and SQLite behavior. Ordinary reads can
also affect filesystem access times. No `immutable=1` assertion is used because a
running service may change the database. Diagnostics does not promise to work on
all read-only directories when WAL coordination files are unavailable.

Default initialization/readiness connections retain their existing WAL, timeout,
synchronous and foreign-key configuration. Consequently even a refused future
initialization/readiness attempt may enable WAL; initialization also retains its
best-effort permission tightening before the version check. The preservation proof
for these mutating entry points concerns version, tables and logical rows, not an
unchanged database-file header or permissions. Diagnostics avoids those mutations.

## Verification coverage and remaining limits

The new cases cover future refusal/preservation across all initialization entry
points, no future DDL, new and legacy initialization, current-version idempotence,
failed migration with row rollback, readiness refusal, observational diagnostics at
all three versions, no ownership creation, missing/invalid file reachability,
unchanged rows/journal/main bytes, CLI diagnostics, committed WAL visibility,
read-only write rejection and normal/future HTTP startup.

Existing hardening tests retain coverage of the forward-only registry, readiness
migration, failed migration version preservation, WAL concurrency, connection
PRAGMAs, row survival, permissions and reopening. Existing diagnostics, CLI,
installer, backup/restore, logging, credential-adapter and browser-job tests run
alongside the new tests. Golden expectations are unchanged.

Residual limits: diagnostics reports version compatibility, not full table-shape
readiness or integrity. WAL sidecar behavior and unchanged mutating-entry-point
connection settings are as described above. Future structural migrations remain
explicit additions to the existing registry; this change introduces none and does
not claim transactional DDL guarantees beyond the existing infrastructure. Unrelated
backup/restore, release, onboarding and logging findings remain out of scope.

R1's context-window fit and live-model quality remain unverified. Rendered evidence
provenance still increases storage/provider data volume, and external AnalystModel
implementations still require model_input. Part 6C-C remains paused for the external
Anthropic credit failure classified PROVIDER_CONFIGURATION. No R2-B work was started.

## Final offline results

All final gates ran with `UV_NO_SYNC=1`, `UV_FROZEN=1`,
`PYTHONDONTWRITEBYTECODE=1`, and `UV_OFFLINE=1`, using the existing virtual
environment. Python tests used a sanitized environment, external working directory,
disabled pytest cache and a verification-only guard blocking the real platform
credential CLI. Explicit test fakes remain available. No dependencies were installed.

| Gate | Result |
| --- | --- |
| R2-A new cases | 27 passed within targeted/full runs |
| Related targeted suites | 135 passed in 23.56 seconds |
| Full pytest | 987 passed in 223.92 seconds; 7 existing Starlette HTTP 422 deprecation warnings |
| Ruff | All checks passed |
| Strict mypy | No issues in 96 source files |
| Extension | 165 passed; zero failures/skips |
| Diff whitespace | git diff --check passed |

Targeted suites: test_schema_safety, test_sqlite, test_sqlite_hardening,
test_diagnostics, test_cli_6c_a, test_backup, test_install_uninstall_macos, test_http,
test_logging_setup, test_secret_store and test_browser_jobs. The full R1 baseline
of 960 passing Python tests is preserved; R2-A adds exactly 27 cases.
