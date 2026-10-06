# Part 6C-B Proof: Packaging / Installation / Distribution

Status: **implementation complete, not committed** (per instruction: DO NOT COMMIT / TAG / PUSH).

## 0. Sleep-interruption recovery

Implementation was interrupted mid-session by the machine sleeping, immediately before writing
`install-macos.sh`. On resume, the on-disk state was inspected directly (`git status --short`, `git diff
--check`) rather than trusted from transcript prose. Everything already written before the interruption was
confirmed present and correct: `extension/icons/*.png` + `source.svg`, `scripts/generate_icons.py`,
`scripts/build_release.py`/`build-release.sh`, `scripts/package_extension.py`/`package-extension.sh`,
`src/stock_research_ai/release_tools.py`, `tests/test_release_tools.py`, the `render-launchagent` CLI
subcommand in `__main__.py`, and its tests in `tests/test_cli_6c_a.py`. The one dev-only script
(`scripts/render_launchagent.py`) that had been deliberately removed before the interruption was confirmed
still absent. No corruption, no lost work, no restart-from-scratch was needed. `plutil -lint` was re-run
against a freshly-generated temp plist to re-confirm structural validity before continuing.

## 1. Baseline

`main`, HEAD == origin/main == `106cabb95fa0865abc3f3261cf3e4a9826c3c10f` (Part 6C-A), clean tree, confirmed
before any new work this slice.

## 2. Scope delivered

### New files (19)
- `src/stock_research_ai/release_tools.py` — deterministic, offline-testable release helpers (checksums,
  release manifest, extension ZIP build/validate, LaunchAgent plist render/validate, static secret scan).
  Shipped inside the installed package (same rationale as 6C-A's `backup.py`/`diagnostics.py`).
- `scripts/generate_icons.py` — pure-stdlib PNG generator for the 4 extension icon sizes (zlib + struct
  only, no Pillow/cairosvg).
- `extension/icons/icon{16,32,48,128}.png` + `extension/icons/source.svg` — original, restrained mark (a
  rounded square in the extension's own existing accent color with a simple ascending-bars glyph); no
  third-party branding, no remote fetch.
- `scripts/package_extension.py` / `scripts/package-extension.sh` — build+validate the extension release
  ZIP.
- `scripts/build_release.py` / `scripts/build-release.sh` — assemble the full release bundle.
- `scripts/install-macos.sh` — the pilot installer.
- `scripts/uninstall-macos.sh` — the pilot uninstaller.
- `tests/test_release_tools.py` (28 tests), `tests/test_install_uninstall_macos.py` (12 tests).
- `docs/getting-started.md`, `docs/privacy-disclosure.md`, `docs/chrome-web-store-readiness.md`.
- `CHANGELOG.md`.

### Modified files (4)
- `extension/manifest.json` — additive `icons` key only; `manifest_version`, `permissions`,
  `host_permissions` byte-for-byte unchanged (verified programmatically, not just by eye).
- `src/stock_research_ai/__main__.py` — one new subcommand, `render-launchagent` (installer plumbing; see
  §9). No existing command's behavior changed.
- `tests/test_cli_6c_a.py` — 2 new tests for `render-launchagent`.
- `.gitignore` — narrow addition of `/release/` (generated release-bundle output; mirrors the existing
  `dist/`/`build/` entries).

## 3. Python runtime / install strategy

`uv tool install <wheel>` — the pilot never clones the repository or sets up a virtualenv by hand. The
installer detects `uv` explicitly and refuses with a clear, actionable message (pointing to
https://astral.sh/uv) rather than auto-installing it via an unverified remote script. After install, the
`stock-research-ai` command is directly invocable (no `uv run` prefix needed for normal use).

## 4. Backend package format

Both wheel and sdist, via `uv build` (the project's existing `uv_build` backend — no new packaging
framework added). Confirmed by a real offline build (§30):
`stock_research_ai-0.1.0-py3-none-any.whl` + `stock_research_ai-0.1.0.tar.gz`.

## 5. Installer behavior

`scripts/install-macos.sh`: detects macOS + architecture, requires `uv` on PATH, locates the wheel in
`--release-dir/backend/*.whl`, resolves canonical 6C-A data paths, takes a pre-upgrade safety backup **only
when an existing database is found**, runs `uv tool install --force <wheel>`, runs `init-db` (idempotent —
never recreates an existing DB), renders the LaunchAgent via the *installed binary itself*
(`stock-research-ai render-launchagent`, never duplicated in shell), bootstraps/kickstarts it via
`launchctl`, and polls `http://127.0.0.1:<port>/health` for a bounded 15s (configurable via
`SRAI_HEALTH_TIMEOUT_SECONDS`, used by tests) with no external network call. On failure it prints the log
path and `diagnostics`/`launchctl print` guidance — never a raw traceback, never a secret.

## 6. Install location

User-local only: `uv tool install`'s own per-user tool directory (conventionally `~/.local/bin`, or
`$UV_TOOL_BIN_DIR` if set) for the executable; `~/.stock-research-ai/` for data (unchanged from 6C-A);
`~/Library/LaunchAgents/` for the LaunchAgent. No `sudo`, no `/usr/local`, no system-wide Python change.

## 7. LaunchAgent label

`ai.stockresearch.local` — defined once, as `stock_research_ai.release_tools.LAUNCH_AGENT_LABEL`, and
imported (never re-typed) everywhere it's used: the `render-launchagent` CLI command, `install-macos.sh`,
`uninstall-macos.sh`, and every test.

## 8. LaunchAgent behavior

`RunAtLoad: true`, `KeepAlive: {SuccessfulExit: false}` (restarts after an ordinary crash, not after
`stock-research-ai serve-http` exiting cleanly on purpose), `StandardOutPath`/`StandardErrorPath` under the
canonical `log_path` directory, `ProcessType: Background`. `ProgramArguments` is exactly
`[<installed-binary-path>, "serve-http"]` — the same single-worker, loopback-bound command 6C-A already
hardened; the plist introduces no new argument surface.

## 9. LaunchAgent secret safety

`render_launch_agent_plist()` has no parameter through which a secret could even be passed — structurally
impossible to embed one by construction, not just by convention. `validate_launch_agent_plist()` additionally
scans the rendered bytes for `sk-ant-`, `.env`, and (specifically for plists — see §26 on why this differs
from the general scan) `Authorization:`/`Bearer `, refusing to write if found. The **new** additive CLI
subcommand `render-launchagent` (justification: production installs have no repository checkout, so a
repo-relative dev script cannot render the plist for a real pilot install — the installed binary must be
able to do this itself) reuses this exact same generator; no plist construction is duplicated in shell.

## 10. launchctl strategy

Modern commands throughout: `launchctl bootout gui/$UID/<label>` (ignored if not already loaded) →
`launchctl bootstrap gui/$UID <plist>` → `launchctl kickstart -k gui/$UID/<label>`. No deprecated
`load`/`unload` usage.

## 11. Service health-check behavior

Bounded polling of `/health` only (loopback, no external call), default 15s, overridable for tests. Never
silently succeeds without checking; never calls Anthropic/Screener.

## 12. Uninstall behavior

`scripts/uninstall-macos.sh`: `launchctl bootout` (ignored if not loaded) → removes the plist file → best-
effort `uv tool uninstall stock-research-ai`. **Default preserves all research data** and prints its exact
location (`"Your local research data was preserved at: <path>"`) plus an explicit note that the Keychain
credential is untouched.

## 13. `--remove-data` behavior

Requires `--yes` for non-interactive use, or an interactive typed `DELETE` confirmation otherwise — refuses
on anything else (proven: declining leaves the data byte-for-byte intact). Before deleting, the script
resolves the real path and refuses if it is `/`, equals `$HOME`, or does not end in `.stock-research-ai` —
never a bare/unchecked `rm -rf`. The macOS Keychain credential is **never** touched by this script under any
flag (proven structurally: `uninstall-macos.sh`'s own source contains no `security` invocation at all —
nothing to intercept in a test because there is nothing there to run).

## 14. Upgrade behavior

Re-running `install-macos.sh` is the upgrade path. Proven end-to-end offline (fake `uv`/`launchctl`/`curl`):
a second run against an existing fake database creates exactly one timestamped pre-upgrade backup and
leaves the "upgraded" database byte-for-byte untouched by the backup step itself; `init-db` is a no-op
against an existing, valid database (6C-A's own `initialize()` contract); any real forward migration is
handled automatically by 6C-A's `PRAGMA user_version` mechanism the next time the service starts — this
slice adds no second migration path.

## 15. Pre-upgrade backup behavior

Only triggered when both an existing `stock-research-ai` binary **and** an existing database file are
found — never on a genuinely fresh install (proven: no `backups/` directory is created on a first install).
Uses the real 6C-A `backup` command (SQLite's online backup API under the hood), so it inherits that
command's own guarantees (WAL-safe, excludes the API key) rather than re-implementing anything.

## 16. Rollback policy

Not implemented, and not claimed. Documented explicitly (`getting-started.md`, this proof) that an
application downgrade is unsafe once the database schema has migrated forward, and that the correct
recovery path is restoring from the pre-upgrade safety backup via 6C-A's `restore` command — which itself
already refuses a backup whose schema version is newer than the running build supports.

## 17. Backend version source

`stock_research_ai.release_tools.application_version()` simply re-exposes 6C-A's own
`importlib.metadata.version("stock-research-ai")` — no second hardcoded version was introduced anywhere in
this slice. Verified by test to equal the real installed metadata value.

## 18. Extension version policy

`manifest.json`'s version stays on its own independent, Chrome-enforced monotonic numeric lineage (Chrome
structurally rejects any other kind of version string, which is exactly why it can never be tied to a git
SHA). For this pilot release, backend and extension both happen to be `0.1.0` — a deliberate, documented
choice to ship together for the first pilot, not an assumed permanent coupling.

## 19. API compatibility policy

Unchanged from Part 6B: the extension gates on `GET /v1/version`'s `api_version` field (`"v1"`). This slice
records `api_version`, `research_plan_version` (`FULL_RESEARCH_V1`), `backend_version`, and
`extension_version` together in `RELEASE-MANIFEST.json` so a given release's full compatibility set is
captured in one machine-readable place, without changing the compatibility check itself.

## 20. Manifest changes

Exactly one additive key (`icons`). No permission, host_permission, or `manifest_version` change — confirmed
both by reading the diff and by a small script asserting the parsed JSON's `permissions`/`host_permissions`/
`manifest_version` fields are unchanged.

## 21. Permissions / host_permissions

Still exactly `["storage", "sidePanel"]` and `["https://www.screener.in/*", "http://127.0.0.1/*"]`. No
`tabs`, `scripting`, `downloads`, `content_scripts`, `<all_urls>`, or `externally_connectable` anywhere.

## 22. Icons

16/32/48/128px, generated deterministically by `scripts/generate_icons.py` using only the Python standard
library (`zlib`, `struct`) — no Pillow/cairosvg dependency. An original, restrained rounded-square mark in
the extension's own pre-existing accent color (`#1f4d3a`) with a simple ascending-bars glyph; no third-party
branding, no remote asset fetch. `extension/icons/source.svg` documents the same design for future manual
editing (not itself consumed by the build, since no SVG rasterizer is available without a new dependency).

## 23. Extension ZIP structure

Built by `release_tools.build_extension_zip()`: production contents directly at the ZIP root (never nested
under `extension/`), `package.json` excluded (Chrome never reads it), `tests/`, `node_modules/`, `.git`, and
OS cruft excluded. Deterministic byte-for-byte across repeated builds from identical source (fixed archive
timestamps; proven by a test that builds twice and diffs the bytes). Validated both by a dedicated
`validate_extension_zip()` function (structural: manifest present, MV3, permissions/host_permissions closed,
icons present, no forbidden paths, no embedded secret marker) and by a real build against the actual
`extension/` directory (§30).

## 24. Backend release artifact

`stock_research_ai-0.1.0-py3-none-any.whl` and `stock_research_ai-0.1.0.tar.gz`, produced by a real,
successful, fully offline `uv build` invocation this session (§30) — not merely claimed.

## 25. Release bundle structure

```
release/
    backend/
        stock_research_ai-0.1.0-py3-none-any.whl
        stock_research_ai-0.1.0.tar.gz
    extension/
        stock-research-ai-extension-0.1.0.zip
    install-macos.sh
    uninstall-macos.sh
    getting-started.md
    privacy-disclosure.md
    RELEASE-MANIFEST.json
    SHA256SUMS
```
Confirmed via a real assembly this session (§30), not just described.

## 26. Release manifest fields

`product`, `backend_version`, `extension_version`, `api_version`, `research_plan_version`, `built_at_utc`,
`git_commit`, `artifacts` (relative-path → SHA-256), `platform_target`, `minimum_macos_version`,
`python_runtime_strategy`. Verified by test to never contain an API key, a `/Users/...` path, `$HOME`, or an
`Authorization`/`Bearer` string. (Note on the static-marker scan design: `"Authorization:"`/`"Bearer "` are
checked only for the plist and the manifest, never for general source code or the extension ZIP — the
extension's own `client.js` legitimately builds an `Authorization: Bearer <token>` header, and flagging that
would be exactly the same class of false positive as flagging the word "Secretary" for containing "secret",
which this project has already explicitly guarded against once before. A dedicated regression test
(`test_forbidden_marker_scan_does_not_flag_legitimate_authorization_header_code`) proves this.)

## 27. Checksum behavior

`SHA256SUMS` via Python `hashlib` (not shelling out to `shasum`, for identical behavior on any POSIX
platform later), one `<hex>  <relative-path>` line per artifact, sorted by path, never an absolute local
path. Independently verified this session against the real assembled bundle with macOS's own
`shasum -a 256 -c SHA256SUMS` — all four lines reported `OK`.

## 28. Generated-artifact gitignore behavior

Added `/release/` to `.gitignore` (narrow, mirrors the existing `dist/`/`build/` entries). Nothing under
`extension/` or `scripts/` was ignored. All release builds performed this session were written to `/tmp/`,
outside the repository entirely, and deleted immediately after inspection — the repository's own `git
status` was never polluted by a generated bundle.

## 29. Getting-started UX

`docs/getting-started.md` — 15 concrete steps (obtain bundle → verify checksum → install → configure
credential → diagnostics → health check → load extension → pairing code → pair → open Screener → Research
Company → view report → backup → upgrade → uninstall) plus troubleshooting, using **only** commands that
actually exist in `__main__.py` (`init-db`, `configure-analyst`, `diagnostics`, `backup`, `restore`,
`pair-code`) — no invented CLI names.

## 30. Actual offline release-build proof

A real release bundle was built into `/tmp/srai-release-proof2` (outside the repo, deleted after
inspection) using only local, already-resolved tooling — no live network call was made by the build itself.
Confirmed:
- `uv build` succeeded, producing both a real wheel and sdist.
- The extension ZIP built and validated with **zero** problems (`validate_extension_zip()` returned `[]`).
- `RELEASE-MANIFEST.json` and `SHA256SUMS` were generated with correct content.
- `shasum -a 256 -c SHA256SUMS` reported `OK` for all four listed files.
- `getting-started.md`/`privacy-disclosure.md` were copied into the bundle correctly.
- A static secret scan (`scan_for_forbidden_markers`) across every file in the assembled bundle returned
  **zero** findings.

**A genuine defect was found and fixed during this proof**: `uv build --out-dir` also writes its own
`.gitignore` into the output directory; the first build-tooling draft naively globbed `output_dir.glob("*")`
and would have hashed/published that `.gitignore` as if it were a release artifact. Fixed by filtering to
files ending in `.whl`/`.tar.gz` only, and re-verified with a second real build.

**A second, genuine, low-severity finding was identified during final verification and has since been
fixed (release-hygiene correction, applied before freeze)**: `pyproject.toml` declares
`readme = "README.md"`, so the entire contents of `README.md` are embedded verbatim as the wheel's
`METADATA` long description. `README.md` — a large, pre-existing document from early project parts, not
otherwise part of Part 6C-B — contained 5 literal occurrences of the exact same line,
`cd /Users/shoumikganguly/stock-research-ai` (the original developer's own local machine path/username),
each used purely as a generic "navigate to the repository root" instruction at the start of an otherwise
unrelated shell example (general setup, MCP server startup, and two Part 2 offline-proof replay
walkthroughs). None of the 5 occurrences differed in kind, and nothing else about those examples was
machine-specific.

Each of the 5 occurrences was replaced, narrowly and identically, with the portable placeholder
`cd /path/to/stock-research-ai` — chosen over `~/stock-research-ai` because a repository checkout is not
necessarily located under the user's home directory, so `/path/to/...` makes no incorrect assumption about
where it lives. No other line in any of the 5 surrounding code blocks was touched: every subsequent
command (`export UV_NO_SYNC=1 ...`, `uv run stock-research-ai ...`, database/output-path flags, etc.)
is byte-for-byte unchanged, and no product positioning, research semantics, installation architecture, or
frozen milestone claim elsewhere in `README.md` was altered. `git diff README.md` confirms exactly 5
one-line changes and nothing else.

The release bundle was rebuilt fresh after this fix (still fully offline, no live network call). The
rebuilt wheel's `METADATA` file was inspected directly (`unzip -p ... METADATA`) and confirmed to contain
zero occurrences of `/Users/shoumikganguly` and exactly 5 occurrences of the new portable placeholder. A
broader binary-safe scan of the entire rebuilt bundle for `/Users/shoumikganguly`, `/home/`, and similar
developer-machine-specific patterns found zero matches; the only `$HOME` occurrences found were the
installer/uninstaller scripts' own intentional, generic, already-reviewed use of the real `$HOME`
environment variable for path resolution (system/runtime paths, not a leak — exactly the case the audit
instructions call out as fine). The extension ZIP's checksum was confirmed byte-identical to the pre-fix
build (this change touches only the backend wheel/sdist, never the extension), `RELEASE-MANIFEST.json` and
`SHA256SUMS` were regenerated and re-verified (`shasum -a 256 -c SHA256SUMS` → all `OK`), and the static
secret scan (`scan_for_forbidden_markers`) across the rebuilt bundle returned zero findings. No
developer-machine absolute paths remain in any distributable artifact.

## 31. `git diff --check`

Clean (exit 0).

## 32. `git status`

4 modified, 20 new (19 listed in §2 plus this proof document) — 24 files total, nothing unexpected.

## 33. Secret-scan result

Ran `scan_for_forbidden_markers` across every new/modified file in this slice. 5 matches, all reviewed and
confirmed legitimate: `.gitignore`'s own `.env` exclusion pattern; `release_tools.py`'s own marker-constant
definitions; and three test files' assertions *proving the absence* of `sk-ant-`/`.env` in generated output
(`assert "sk-ant-" not in raw`-style lines) — never a real credential. No usable credential ships anywhere.

A separate, broader manual scan of the fully-assembled release bundle (every file, including the built
wheel/sdist/ZIP as opaque binaries, plus an explicit grep for `$HOME`/absolute-path leakage) also returned
clean. This scan **originally found one exception** (the wheel's `METADATA` embedding 5 developer-machine
example paths from `README.md`) — that finding has since been fixed as a targeted release-hygiene
correction and re-verified clean against a freshly rebuilt bundle; see §30 for the full before/after
detail. No developer-machine absolute paths remain in any distributable artifact.

## 34. Shell syntax / static safety result

`bash -n` passed on all four new shell scripts. `shellcheck` is not installed on this machine and was **not**
installed solely for this task, per instruction. Manual audit against the section-42 checklist: `set -euo
pipefail` present in all four; every path variable is double-quoted; no unquoted glob expansion; the only
`rm -rf` is in `uninstall-macos.sh`'s `--remove-data` path, gated behind a resolved-path safety check (refuses
`/`, `$HOME`, or any path not ending in `.stock-research-ai`) and an explicit confirmation; no destructive
operation ever runs outside a path derived from the canonical data directory; no secret ever appears in an
`argv` (LaunchAgent rendering takes no secret parameter at all).

## 35. Plist validation result

`plutil -lint` (real macOS tool) passed on a freshly-generated temp-directory plist, both via the dedicated
release-tools test and via a fresh ad hoc smoke test during interruption recovery. No real
`~/Library/LaunchAgents/` file was ever written by an automated test — all plist-writing tests target
`tmp_path`.

## 36. `render-launchagent` CLI test

Two tests in `tests/test_cli_6c_a.py`: one exercises the full parser/dispatch path with an explicit
`--executable` and asserts the resulting plist's `Label`/`ProgramArguments` and the absence of `sk-ant-`/
`.env`; the other confirms the `--executable` default resolves to a non-empty path when omitted. Both pass
against the real CLI, no real `launchctl`.

## 37. New 6C-B test count

**42** — `test_release_tools.py` (28), `test_install_uninstall_macos.py` (12), plus 2 appended to
`test_cli_6c_a.py`.

## 38. Extension test count

165 passed — unchanged (`extension/` runtime files untouched; only `manifest.json`'s additive `icons` key
and the new binary icon assets were added, neither exercised by `node --test`).

## 39. Python pytest count

**931 passed** (889 frozen 6C-A baseline + 42 new), 7 pre-existing unrelated warnings.

## 40. Ruff

All checks passed.

## 41. mypy/source count

Success — **94 source files** (93 from 6C-A + `release_tools.py`).

## 42. Dependency changes

None. `pyproject.toml`/`uv.lock` diff is empty (`release_tools.py` uses only `hashlib`, `json`, `plistlib`,
`zipfile`, stdlib `datetime`/`pathlib`; `generate_icons.py` uses only `zlib`/`struct`).

## 43. DB schema changes

None.

## 44. Extension runtime-semantic changes

None. The only extension-directory changes are additive: the `icons` manifest key and the 5 new icon/SVG
asset files. No JS file, no CSS file, no HTML file, no permission was touched. `npm test` remains 165/165.

## 45. Temporary offline release-build result

Success — see §30. Both temporary release directories (`/tmp/srai-release-proof`,
`/tmp/srai-release-proof2`) were deleted after inspection; nothing was left behind outside `/tmp/`, and
nothing was ever written inside the repository's own `release/` (which stays gitignored and was never
created in-repo this session).

## 46. Unresolved blockers before 6C-C

None identified as blocking. Explicitly deferred (not blockers, by design — see §52/§19 of the original
instructions): Apple code signing (NOT IMPLEMENTED), Apple notarization (NOT IMPLEMENTED), Chrome Web Store
submission (NOT SUBMITTED — checklist only, `docs/chrome-web-store-readiness.md`). Pilot known limitations
carried forward from the architecture proposal remain accurate and unchanged: Screener-only, macOS-only
pilot distribution, extension load-unpacked for pilot, one backend worker, in-memory active jobs
(interrupted research is not resumable), completed reports persist, company identity remains URL-keyed, no
telemetry, no report-history UI, no monitoring/alerts, no automated trading, Windows installer deferred.

## 47. Recommendation

**READY FOR 6C-C** (live/release validation) — subject to independent review of this slice first, per
instruction.

PART 6C-B IMPLEMENTATION COMPLETE — AWAITING REVIEW.
