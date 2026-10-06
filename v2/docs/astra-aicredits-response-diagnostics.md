# AI Credits response diagnostics and submission investigation

## Scope

The adapter emits one structured `aicredits_response_metadata` JSON log record
through the existing `stock_research_ai.aicredits` logger after a successful HTTP
200 response has decoded to a JSON object, before model/claim-response validation.
The normal `serve-http` rotating service log retains this record even if validation
then raises. No database/domain schema, response acceptance, retry, provider,
research, authentication, or extension behavior changes.

The record is not evidence of successful analysis. It is intentionally emitted for
both accepted and rejected decoded objects. Non-200, invalid JSON, and non-object
JSON responses retain existing error behavior and are outside this diagnostic.

## Diagnostic schema (version 1)

- `event`, `schema_version`, `http_status` (200, established by the transport).
- `response_id`, `request_id` (x-request-id), `reported_model`.
- `choices_count` (list length or null), `choice_type` (first choice JSON type).
- `message_is_object`, `message_gate_passed`, `finish_reason`,
  `finish_reason_gate_passed` (the two existing incomplete-response predicates).
- `content_present`, `content_type`.
- `tool_calls_present`, `tool_calls_type`, `tool_calls_count` (list length or null).
- `first_tool_call_type`, `first_function_name`.
- `arguments_present`, `arguments_type`, `arguments_length` (string length or null).
- `usage`: only numeric, non-boolean, nonnegative values no larger than 2^53-1 for
  `prompt_tokens`, `completion_tokens`, `total_tokens`.
- `redacted_fields`: fixed schema field names whose values were rejected.

Presence means the field exists, including an explicit null. Types are fixed JSON
shape labels; no object keys or contents are copied. Identifier-like fields accept
only 1–128 characters matching `[A-Za-z0-9][A-Za-z0-9._:/-]*`, with known API-key and
credential-shaped strings suppressed. Accepted strings are preserved exactly;
unsafe/oversized/non-string values become null with their field named in
`redacted_fields`. A redacted finish reason cannot be mistaken for an accepted
provider null: the separate gate boolean still reports the original predicate.

No assistant content, argument contents, evidence, full response, request headers,
or arbitrary error prose is recorded. No credential lookup is added. Unknown
secrets deliberately disguised by a provider as syntactically valid identifiers
cannot be recognized generically; the boundary assumes these metadata slots carry
identifiers, applies strict length/character restrictions, and suppresses the known
API key and credential-shaped values. IDs can be absent or redacted; log timestamps
are then the available correlation, not an added job-ID linkage.

The previous rejected responses cannot be recovered with this change. The running
backend is not restarted in this implementation phase; deployment/restart after
review is needed before a subsequently authorized live attempt uses diagnostics.

## Two-submission investigation (read-only)

The existing monitor and backend access log record:

| Job | Monitor observation (2026-09-24 UTC) | Client | Request | Result |
|---|---|---|---|---|
| `2e0cf7fe-8c8f-4d9f-8f01-dc30d52f316e` | 13:02:05.747671 | 127.0.0.1:59846 | POST /v1/research-jobs | 202 |
| `76d20a83-22ef-434a-9504-b56e9116b300` | 14:01:01.828847 | 127.0.0.1:60330 | POST /v1/research-jobs | 202 |

These are log-observer timestamps, not application request timestamps. The access
log does not retain Origin, User-Agent, request body (source_url/research_plan), or
request/correlation IDs. Both passed the protected route's bearer dependency and
payload validation to produce 202. This proves accepted authentication, not the
browser/window/process that presented it. Adjacent GETs correlate the two job IDs;
Screener INFY acquisition and one AI Credits HTTP 200 appear within each sequence.
There is no retained Chrome event trace establishing the triggering action.

Source findings:

- `extension/src/api/client.js` defines `createResearchJob`; exactly one production
  caller exists in `onResearchClick` in `extension/src/sidepanel.js`.
- Research Company and Research Again each bind once to that handler, at module
  scope. `sidepanel.html` includes one module script. Boot does not register these
  listeners again. There is no source evidence of accumulating click listeners.
- Try Again, reload, tab activation and URL updates invoke boot. Boot reconnects by
  GET or renders ready; it never invokes the submission handler.
- The service worker only configures toolbar-to-side-panel behavior. The manifest
  declares no content script. Neither has a submission path.
- Separate open panels can each invoke the click handler; source inspection cannot
  establish whether this happened. No automatic timer invokes submission.
- `BrowserJobManager.launch` already deduplicates the same canonical source/plan
  while queued/running. It intentionally releases that key at terminal completion.
  This does not prevent a distinct new job about 59 minutes later.
- `work/infy-validation/monitor.py` reads log files and mode=ro SQLite only; its POST
  string is a regex matching access logs, not a network request. The observation
  scripts likewise inspect persistence. The generic `run_process.py` delegates CLI
  arguments but does not itself make an HTTP job POST; its existence does not prove
  execution of a research command. No job-creation tool command or UI click was
  performed by this assistant in the monitored interval or investigation.
- Offline tests use ASGI/MockTransport and temporary fixtures, not port 8000.

Conclusion: the second submitter remains undetermined. No concrete automatic
client-submission defect has been established, so no extension change or speculative
submission guard is added. This investigation does not attribute the POST to user
action and cannot exclude another authenticated local process/panel.

## Offline verification

Tests cover both halves of the incomplete-response gate, accepted finish reasons,
missing calls, malformed arguments, malformed choices, bounded identifier handling,
known-secret/prose exclusion, numeric usage filtering, disk-log availability after
an exception, and unchanged single-attempt semantic rejection. All transport is
MockTransport. Golden expectations are unchanged.
