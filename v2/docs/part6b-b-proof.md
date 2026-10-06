# Part 6B-B implementation and proof — Chrome Extension Shell + Screener Adapter + Pairing + Research Job UX

Verified locally: extension test suite (Node's built-in `node --test`, zero external
npm dependencies) - 74 passed. Full Python suite unaffected: 815 passed (unchanged from
the Part 6B-A baseline). Ruff clean. Mypy clean across 88 source files (Python source
count unchanged - Part 6B-B touches no Python file). No dependency changes (Python or
JS). No commit/tag/push was made.

## Scope

Part 6B-B is the actual Chrome extension shell: a Manifest V3 side panel that checks
local service health/version/capabilities, pairs on first use, detects a supported
Screener company page from the active tab's URL only, launches `FULL_RESEARCH_V1`
against the committed Part 6B-A HTTP contract, polls real backend progress stages, and
retains a lightweight `report_snapshot_id` pointer for Part 6B-C. It is not, and does
not attempt to be, the institutional report viewer.

## 1. Stack: dependency-free MV3 + native HTML/CSS/ES modules

No React/Vue/Vite/Webpack, no build step, no runtime npm dependency. `extension/
package.json` exists only to declare `"type": "module"` (so both extension code and
Node's test runner treat `.js` files as ES modules uniformly) and a `test` script
(`node --test`) - it has zero `dependencies`/`devDependencies`. Node is available in
this environment (`v25.6.0`), used exclusively for the offline test suite; nothing about
the shipped extension depends on Node.

## 2. Directory structure

```
extension/
  manifest.json
  package.json
  sidepanel.html
  css/sidepanel.css
  src/
    background.js
    sidepanel.js
    storage.js
    state.js
    job_flow.js
    api/client.js
    portals/{portal_adapter.js, registry.js, screener.js}
  tests/
    screener.test.js
    registry.test.js
    service_url.test.js
    api_client.test.js
    state.test.js
    storage.test.js
    job_flow.test.js
```

`src/job_flow.js` (not in the illustrative tree) was added because the reconnection/
job-update decision logic ("what should local storage do given this job update or
fetch error") is genuinely separable, pure logic that deserved its own unit-testable
module rather than being buried inside the DOM controller - `src/ui/render.js` was
*not* added since `sidepanel.js`'s own `renderState()` function already serves that
role cleanly without needing a separate file.

## 3. Manifest audit

`manifest_version: 3`; `side_panel.default_path = "sidepanel.html"`; `background.
service_worker = "src/background.js"` with `type: "module"`; `action: {}` (no
`default_popup` key at all); `permissions: ["storage", "sidePanel"]` only;
`host_permissions: ["https://www.screener.in/*", "http://127.0.0.1/*"]` only. No
`content_scripts`, no `scripting`, no `cookies`, no `webRequest`, no `history`, no
`tabs`/`activeTab`, no `<all_urls>`, no `externally_connectable`, no remote code
reference of any kind (no CDN script/style/font URL anywhere in the extension).

## 4. Active-tab URL detection without `tabs`/`activeTab`

`chrome.tabs.query({active: true, currentWindow: true})` is called with **no** `tabs`
or `activeTab` permission. This works because Chrome unlocks the sensitive `Tab.url`
field for any tab whose URL matches a declared `host_permissions` origin, regardless of
the `tabs` permission - documented Chrome extension behavior. Since
`https://www.screener.in/*` is declared, `tab.url` is visible exactly for Screener tabs
and `undefined` for everything else - which is precisely the intended behavior (an
unrelated page must not be identifiable at all, let alone researched). No content
script, no DOM access, no page inspection.

## 5. Screener adapter - mirrors the frozen backend contract exactly

`src/portals/screener.js::ScreenerAdapter` mirrors
`adapters/screener/source.py::validate_url` field-for-field: `scheme === "https:"`,
`hostname === "www.screener.in"` exactly, no userinfo, `port` empty or `"443"`, no
query, no fragment, `pathname` matching `^/company/[A-Za-z0-9_-]+/(?:consolidated/)?$`
(covering both alphabetic tickers like `INFY` and numeric/BSE-style codes like
`500209`, exactly as the backend already accepts). This is UX convenience only - the
backend independently and authoritatively re-validates every request
(`POST /v1/research-jobs` calls the same `validate_url` server-side, per the committed
Part 6B-A contract).

## 6. Portal registry

`src/portals/registry.js::registerPortalAdapter`/`resolvePortal` - one flat list,
first-match-wins. `sidepanel.js` registers exactly one adapter (`ScreenerAdapter`).
Adding a future portal is one adapter file + one `registerPortalAdapter()` call; no
other module (pairing, API client, job polling, progress, security/auth) references
portal-specific logic.

## 7. Local service default URL & loopback validation

`src/storage.js::DEFAULT_SERVICE_BASE_URL = "http://127.0.0.1:8000"` - matches
`config.py::Settings.http_host`/`http_port` defaults exactly (no invented port).
`src/api/client.js::validateServiceUrl` is called on **every** `ApiClient` request (not
only at construction) and throws `InvalidServiceUrlError` - refusing to send anything,
including the Bearer token - unless the URL is exactly `http://127.0.0.1[:port]` with
no userinfo/path/query/fragment. Rejects `https://` (even against 127.0.0.1), LAN IPs,
`file://`, `javascript:`, `localhost` (not part of the backend's own loopback contract,
which is `127.0.0.1` specifically), and malformed ports. Proven by 13 tests in
`service_url.test.js` plus a direct "credentials never sent on an invalid base URL" test
in `api_client.test.js` (`fetchImpl.calls.length === 0`).

## 8. Exact CLI commands shown to the user

Inspected `src/stock_research_ai/__main__.py` directly: the start command is
`stock-research-ai serve-http` and the pairing-code command is
`stock-research-ai pair-code` (both confirmed present as literal subparser names in the
committed CLI). These exact strings are hard-coded as constants in `sidepanel.js` and
rendered verbatim in both the "service unavailable" panel and the pairing panel - never
guessed.

## 9. Health / version / capability flow

On boot: `GET /health` (public, no auth) - unreachable/non-`{"status":"ok"}` shows
`SERVICE_UNAVAILABLE` with the exact start command. Then `GET /v1/version` (public) -
`api_version !== "v1"` shows `INCOMPATIBLE` with the reported version in the detail
text; nothing proceeds against an unknown schema. Then the stored token is checked -
absent shows `UNPAIRED`. Only once paired is `GET /v1/capabilities` (public) called
to read `analysis_available`; when `false`, the ready panel shows an explicit warning
that research cannot complete yet - never any suggestion that a fake analyst exists.

## 10. Pairing UX

`panel-unpaired` explains the real flow using the real commands (§8), takes a pairing
code, and calls `POST /v1/pair` with `{pairing_code}` exactly - the browser's own
`Origin: chrome-extension://<id>` header is sent naturally by the browser itself; the
extension never constructs or overrides an Origin header. On success, only the returned
`token` is stored (`chrome.storage.local`); the pairing-code input field is cleared
immediately after use and is never persisted. On failure, a generic "check the code and
try again" message is shown - the raw server error text is not surfaced verbatim
(avoiding leaking backend internals) and the code failed to store nothing.

## 11. Token storage & re-pair

`chrome.storage.local` only (`storage.js` never references `chrome.storage.sync`
anywhere - grep-verified). The token is added as `Authorization: Bearer <token>` solely
inside `ApiClient._request`, only for the four calls that take a `token` argument
(`createResearchJob`, `getResearchJob`, `getReport` - `pair`/`health`/`version`/
`capabilities` never receive one). "Forget pairing / Re-pair" (`onRepairClick`) stops
polling, clears the stored token and the active-job pointer locally, then reboots the
UI to the pairing screen. No new backend route was invented - Part 6B-A's `/v1/pair`
already fully replaces any previous pairing on a fresh `pairing_code`, so a local-only
clear plus re-running the existing pairing flow is the correct, complete re-pair path;
this is documented explicitly (there is no server-side "revoke without repairing"
endpoint today, which is fine since possession of a new one-time code is what
authorizes a new pairing).

## 12. `BrowserResearchRequest` payload

Exactly `{portal: "SCREENER", source_url: "<current supported URL>", research_plan:
"FULL_RESEARCH_V1"}` - built in `onResearchClick` from `adapter.getSourceContext(url)`
plus the hardcoded plan literal. No DOM content, no HTML, no financial value, no
sector, no document URL, no valuation assumption, no prompt - grep-confirmed nothing
else is ever attached to this object.

## 13. Job launch, polling, and stage labels

`POST /v1/research-jobs` (Bearer-authenticated) returns a job promptly; the panel
immediately reflects `RUNNING` and starts a **single** `setTimeout`-based poll loop
(`POLL_INTERVAL_MS = 1500`) against `GET /v1/research-jobs/{job_id}`. Every poll clears
and reschedules the same single timer (`pollTimer`) - never more than one loop per job.
Polling stops the instant `job.status` is `COMPLETED` or `FAILED`
(`job_flow.js::isTerminal`), and on `pagehide` (panel closing). Stage labels
(`src/state.js::STAGE_LABELS`) map only the real six backend stages - `PREPARING`,
`EVIDENCE`, `ANALYSIS`, `CHALLENGE`, `REPORT`, `COMPLETED` - to plain-language text; an
unrecognized/future stage safely falls back to "Working…" rather than crashing or
inventing a percentage (tested).

## 14. Duplicate-job UX

The backend already deduplicates identical running requests (Part 6B-A). The extension
does nothing special here beyond using the job object `POST /v1/research-jobs` returns
verbatim - if that response is the pre-existing running job, the panel simply shows
`RUNNING` with its real current stage, exactly as if this client had launched it.

## 15. Reconnection & restart semantics

On every boot (`panel open`, `chrome.tabs.onActivated`, `chrome.tabs.onUpdated` with a
URL change), the stored `activeJob` pointer (if any) is polled once via
`GET /v1/research-jobs/{job_id}`. A 404 is interpreted by the pure
`job_flow.js::interpretJobFetchError` as `"LOST"` - the active-job pointer is cleared
and the UI honestly falls through to `READY`/`UNSUPPORTED_PAGE`, never claiming the job
is still running. Any other error is `"TRANSIENT"` and shows `SERVICE_UNAVAILABLE`
without discarding the pointer (a genuine network hiccup should not lose reconnection
state). The **completed report pointer is a separate storage key from the active-job
pointer** (`storage.js::KEYS.LAST_REPORT` vs `KEYS.ACTIVE_JOB`) and is never cleared by
`clearActiveJob()` - proven directly by
`storage.test.js::"last-report pointer survives independently of the active job
pointer"` and `job_flow.test.js`'s failed-job case (asserts `lastReport === null` is
returned, i.e. nothing is written to overwrite an existing pointer - `sidepanel.js`
never calls `clearLastReport` anywhere in the codebase, grep-confirmed).

## 16. Completed report pointer

On a `COMPLETED` job, `job_flow.js::storageActionsForJobUpdate` returns
`{reportId, sourceUrl}` from `job.report_snapshot_id`/`job.source_url`, stored via
`storage.setLastReport`. Part 6B-B stops at a lightweight `COMPLETED` panel ("Research
complete" + "View Research", which for now only surfaces the stored report ID inline -
no report content is fetched or rendered; that is Part 6B-C's job entirely).

## 17. Failure UX

`src/state.js::FAILURE_LABELS` maps every one of the 11 committed Part 6B-A
`JobFailureCode` values (including `JOB_LOST_AFTER_SERVER_RESTART`, which the *report*
side never surfaces as a job failure - only the job-polling 404 path does, per §15) to
the exact concise text specified. The raw backend `failure.message` is shown only
inside a collapsed `<details>`/`<summary>` ("Technical details"), never as primary UI,
and never includes a Python traceback (the backend itself never returns one - Part
6B-A's `HTTPException.detail` is always a small typed object).

## 18. Failure vs. coverage

The extension only ever reads `job.status` (`QUEUED`/`RUNNING`/`COMPLETED`/`FAILED`).
It never inspects report section coverage (`PARTIAL`/`UNAVAILABLE`/`NOT_APPLICABLE`/
`INSUFFICIENT_INPUT`/`UNVERIFIED_METRIC_SEMANTICS`) at all in this slice - Part 6B-B
does not fetch report content, so there is no code path that could conflate coverage
with failure even accidentally.

## 19. UI state machine

`src/state.js::deriveState(facts)` - a single pure function, `SERVICE_UNAVAILABLE >
INCOMPATIBLE > UNPAIRED > UNSUPPORTED_PAGE > LAUNCHING > (job status) > READY`
precedence, exhaustively tested (`state.test.js`, 13 tests). `sidepanel.js` calls this
function to decide what to render at every decision point in `boot()`/`reconnectJob()`/
`handleJobUpdate()` - it is not merely tested in isolation while being bypassed by the
real control flow.

## 20. Accessibility & visual design

Semantic HTML throughout: real `<button>` elements (never a clickable `<div>`), a real
`<label for>` on the pairing-code `<input>`, `aria-live="polite"` on the header status
line and the running-job panel, `role="alert"` on pairing/failure error text,
`<details>/<summary>` for progressively-disclosed technical detail. `:focus-visible`
outlines defined for both buttons and the text input (no removed focus ring). No
color-only meaning - every status also has text. Compact system-font typography, no
gradients/neon/decorative elements. `color-scheme: light dark` plus a
`prefers-color-scheme: dark` media query redefines the same token set for dark mode
(no separate dark-only stylesheet, no JS toggle needed).

## 21. No telemetry - proof

Grep across all extension `.js`/`.html`/`.json` files for `analytics|telemetry|sentry|
posthog|mixpanel` (case-insensitive): **zero matches**. The extension makes network
calls from exactly one place (`ApiClient._request`, one `fetch` call site), always to
the validated loopback origin.

## 22. No direct Screener fetch - proof

Grep for `fetch(` across all `.js` files: the only real call site is
`src/api/client.js`, targeting `origin + path` where `origin` is the loopback-validated
service URL. `src/portals/screener.js` contains no network code at all - it is pure
`URL` parsing. No file constructs a request to `www.screener.in`.

## 23. No other external network - proof

Grep for `XMLHttpRequest|WebSocket|EventSource`: zero matches. Grep for `http://`/
`https://` string literals across shipped `src/` code: only `http://127.0.0.1:8000`
(the loopback default) in `storage.js`, and `http://` mentioned in a comment/error
string in `client.js` describing the loopback contract - no other host, and no literal
outside of test files (which never execute inside the shipped extension).

## 24. Token-leak proof

No `console.log`/`console.debug`/`console.warn` call anywhere references `token`,
`Authorization`, `pairing_code`, or any storage dump (grep-confirmed across all `src/`
files - the only `console.error` call, in `background.js`, logs a `chrome.sidePanel`
API error object with no relation to auth). The token is never interpolated into any
URL (`api_client.test.js` explicitly asserts the token string does not appear in the
constructed request URL for both `getResearchJob` and `getReport`). It is never
rendered into the DOM, never copied to the clipboard (only the *start command*, a
non-secret string, has a copy button), and never included in any diagnostic text.

## 25. Manual demo procedure

1. `uv run stock-research-ai init-db` then `uv run stock-research-ai serve-http`
   (loopback `127.0.0.1:8000` by default).
2. In another terminal: `uv run stock-research-ai pair-code` - copy the printed code.
3. Chrome → `chrome://extensions` → Developer mode → "Load unpacked" → select the
   `extension/` directory.
4. Open a real Screener company page, e.g. `https://www.screener.in/company/INFY/`.
5. Click the Stock Research AI toolbar action - the side panel opens (no popup).
6. Panel shows "Service reachable · not paired" and the pairing form with the two real
   CLI commands; paste the pairing code from step 2 and click Pair.
7. Panel now recognizes the current page and shows "Research Company".
8. Click it - a job launches; the panel shows the real current stage, polling every
   1.5s, and needs no further interaction.
9. If `STOCK_RESEARCH_AI_ANALYST_PROVIDER`/`_API_KEY` are not configured, the job
   honestly reaches `FAILED` with "The live analyst is not configured." (never a fake
   result) - this is the expected, correct outcome without live Anthropic credentials
   and requires no automated live call to demonstrate.
10. With a real analyst configured, the job reaches `COMPLETED` and the panel shows
    "Research complete" with the stored report pointer - Part 6B-C's starting point.

No live Anthropic call and no live Screener request were made by this implementation or
its automated tests; only the manual demo (if a human chooses to run it with real
credentials) would do so.

## 26. Known scope boundary for Part 6B-C

- No institutional report viewer exists yet - "View Research" only surfaces the stored
  `report_snapshot_id` inline.
- No `GET /v1/reports/{id}/artifacts/{kind}` call is made from the extension yet -
  intentionally deferred; Part 6B-A's endpoint is already available for 6B-C to use.
- Re-pairing is entirely local-state-plus-existing-pairing-flow; there is no dedicated
  server-side "revoke" endpoint distinct from re-pairing (documented, not a defect).
