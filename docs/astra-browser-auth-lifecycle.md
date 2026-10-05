# Browser authentication lifecycle investigation

Base: b436f1c942c7034c7116cfa8bc1c12de24510dd8, astra-development.

## Primary incident: Origin omitted by Chrome

The user captured the actual failing side-panel GET: Authorization was present,
Origin was absent. Pairing POST supplied Origin and returned 200. The old verifier
required both headers, so it rejected the otherwise-valid fresh bearer on GET.
There is no stale paired-state cache or hashing mismatch.

The regression now pairs using the exact configured Origin, then GETs a synthetic
existing job through the real protected route. It expects 200 with either exact
Origin or no Origin, and 401 with any explicitly different Origin. No job is launched.
The absent-Origin regression failed before production changes. Five new tests
failed overall before the fix; 26 passed.

Confirmed identity: chrome-extension://ieplfiijheknpihonflfnnofbdlciepa

## Security contract and rationale

Pairing requires a non-empty HTTP Origin equal to browser_extension_origin, whose
Settings validation restricts it to a valid chrome-extension:// ID. An unset
configuration fails closed. A wrong-origin attempt does not consume the pending
code. The existing one-time code, expiry, successful-pair token rotation, and
constant-time SHA-256 comparison are preserved.

Protected routes always require the paired high-entropy bearer. Origin may be
absent; if explicitly present, including empty string or literal null, it must
exactly equal the paired origin. Persisted identity remains unchanged. This is
bearer-secret authentication, not proof of the caller's process identity: anyone
who steals the token can act without Origin. The old Origin check was also
spoofable by non-browser clients. No caller-supplied identity header or forced
Origin is added to extension JavaScript.

CORS reflects only the configured exact extension origin. For protected response
access it must also match the persisted paired origin. A stale paired origin
cannot expand the configured allowlist. There is no wildcard; websites with
explicit origins are denied even if they present a valid bearer. Unconfigured
pairing/CORS no longer accepts arbitrary extension origins. Origin-less requests
receive no CORS approval headers. CORS remains distinct from authentication.

The user reported token exposure in a DevTools screenshot. The token was not
inspected, printed or rotated here. One fresh re-pair remains a post-review task.

## Exact implementation trace

- CLI pair-code creates PairingStore(settings.browser_auth_path), reads existing
  data, replaces only pending.code_hash/expires_at/used, and writes the file.
- POST /v1/pair passes the HTTP Origin and code to complete_pairing. Origin must
  equal the configured extension origin at the route; pending code must be unused, unexpired and
  match SHA-256 using hmac.compare_digest. Existing lockout checks remain intact.
- Success creates a fresh random token, marks pending used and replaces paired
  with exact Origin, token hash, and paired_at. This revokes the old token.
- The pairing route and auth dependency close over the SAME active_pairing_store.
  CORS uses that same instance. _read loads the file afresh; there is no cached
  paired state. Only failed-attempt history/cooldown is process-local.
- Verification requires a token and equality for any explicitly present Origin, then compares
  the same UTF-8 SHA-256 representation using hmac.compare_digest.
- Generating another code does not revoke the paired token, clear paired origin,
  rotate its hash, or alter Chrome storage. Completing pairing does rotate it.
  Repeated code generation alone does not explain client token/job-pointer loss.
- File writes are not atomic/locked across processes. Concurrent writers are an
  observed limitation, not a demonstrated cause of this incident. No speculative
  persistence redesign was made.

## Safe live-state inspection

The auth file exists, version 1, fields paired/pending/version. It has a paired
origin matching the value above and a token hash. Pending metadata is present
and used. Pairing timestamp: 2026-09-21T07:04:41.242109Z. Pending expiry:
2026-09-21T07:09:32.581921Z. Structure matches the source schema. No credential
values or hashes were emitted.

The existing log contains one server start, no logged shutdown, two successful
pair responses, one POST research-jobs 202, 75 GET responses of 401 for
8f2de0f6-f2d6-46a3-9039-12492cc118b5, and one AI Credits POST HTTP 200.
The accepted job launch precedes the failing GETs. No headers/bodies were printed.

Read-only SQLite inspection (mode=ro, query_only) found schema version 1 and:
8 extractions, 136 document extractions, 8 calculation snapshots, 8 valuation
snapshots; zero analysis_snapshots, challenge_snapshots and report_snapshots.
These are database totals, not a claim that every acquisition belongs to this job.

The job map is process-local and does not evict jobs; if the same server process
continues, the job should remain present. Its current memory status/failure is
not observable under the no-token-recovery constraint. Provider invocation proves
analysis was attempted; HTTP 200 does not prove adapter validation/acceptance.
No AnalysisSnapshot, ChallengeSnapshot or ResearchReportSnapshot is persisted;
no report_snapshot_id was found. Browser Part 6A artifacts are rendered from a
stored report, so none is available through that path for this run. Independent
export directories were not exhaustively searched. No job completion is inferred.

## Confirmed secondary defect and minimal correction

ApiClient already preserves HTTP 401/403/409/422 status and application code.
Only a rejected fetch is converted to NETWORK_ERROR. A browser CORS denial can
reject fetch despite a server response; JS cannot recover that response status.
The additional classification tests pass without changing ApiClient production code.

The real sidepanel controller did mishandle authentication:
- reconnectJob treated 401 as TRANSIENT and rendered SERVICE_UNAVAILABLE;
- pollJob treated 401 as transient and continued polling indefinitely.
Both new regressions failed before the change.

The correction adds an explicit 401 transition: stop polling, clear the rejected
client token, display the reachable/unpaired panel with an authorization message,
and keep active-job and report recovery pointers. Re-pairing can use the existing
boot/reconnect path. No research job is submitted by this transition. No live
extension storage was changed. The backend authentication/CORS changes are limited to the contract above. Request
schemas, providers, persistence, dependencies and research semantics are unchanged.

## Verification

- New lifecycle regressions before the fix: 5 failed, 26 passed.
- Focused backend auth/API/reachability: 113 passed, 7 existing warnings.
- Focused extension client/controller/job-flow: 53 passed.
- Full Python: 1132 passed, 7 existing warnings (261.90 seconds).
- Ruff: passed; strict mypy: passed, 99 source files.
- Full extension: 186 passed, 0 failures, 0 skips.
- git diff --check, including new-file whitespace checks: passed.

All tests use offline transports and
temporary auth/database state. No new live research/provider calls, pairing codes,
backend restarts, production DB/auth writes, commits or pushes were performed.
Main remains unchanged. Real installed-Chrome retesting and exposed-token rotation
are deferred until review.
