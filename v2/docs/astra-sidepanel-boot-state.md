# Side-panel boot state correction

Base: `b295f33fe794548a9d86ae8549d9bd206749d44f`, branch `astra-development`.

## Findings and reproduction

The real controller reads the configured loopback URL, resolves the active tab,
fetches `/health`, checks `status === "ok"`, fetches `/v1/version`, checks
`api_version === "v1"`, then reads the locally stored pairing token. Without a
token it selects `UNPAIRED` and returns before capabilities or any research call.
The exact supplied live response objects already pass that fresh-boot path.
There is no response-field/case mismatch, wrong panel ID, or capability check
before pairing. Retry already calls the complete boot function. Storage errors
are not swallowed by the health/version catch blocks.

Three concrete defects were reproduced:

1. `renderState` sets `panel.hidden = true`, but author CSS `.panel { display:
   flex }` overrides native hidden-element display. In the actual browser fixture,
   the exact live payloads selected “Service reachable · not paired” and set
   `unavailable.hidden` to true, while its computed display remained `flex`.
   The misleading “Stock Research AI is not running” panel remained visible.
2. Boot is invoked at load, on Retry and on tab events. An older pending failed
   health/version request can finish after a successful retry. Its catch sets
   the response to null, and its failure branch overwrites both the status line
   and panel with `SERVICE_UNAVAILABLE`. The real-controller regression reproduces
   this ordering without replacing the controller or contacting the backend.
3. A successful but incompatible version response selects `INCOMPATIBLE` without
   updating the status line, retaining an earlier “Local service not reachable”.

Manual successful DevTools fetches do not establish which controller invocation
ran or its ordering in the installed extension. These are proven source defects,
not a claim to have captured the exact installed-session execution trace.

## Fix

- `.panel[hidden] { display: none; }` preserves controller-selected visibility.
- A boot generation check discards superseded asynchronous boot results before
  readiness/pairing rendering, including results from its reconnect fetch.
- The incompatible branch explicitly reports a reachable, incompatible service.

No changes to API response shapes, CORS, authentication, storage format, capabilities,
backend, providers, research semantics, dependencies, packaging, or database.
Boot continues to issue GET requests only. Tests use synthetic tokens and never
read real extension storage. No production service or database was accessed.

## Verification

Before production changes: the exact-payload fresh boot passed; stale health
failure and stale incompatible-status regressions failed. The separate browser
fixture showed computed `display: flex` despite `hidden: true`.

After changes:

- `node --test extension/tests/sidepanel_boot.test.js extension/tests/reachability.test.js`:
  12 passed, no skips (11 new controller tests).
- `npm test` from `extension/`: 180 passed, no skips.
- Real in-app browser rendering fixture: PASS; status reachable/not paired,
  unavailable hidden and computed `display: none`, pairing `display: flex`, exactly
  two mocked readiness requests.
- `git diff --check`: passed.
- Backend/Python tests, Ruff and mypy: not applicable; no Python changes.

To reproduce the browser check, serve `extension/` on an isolated loopback port,
then open `/tests/fixtures/sidepanel_boot_browser.html` in a fresh tab. The fixture
loads the real HTML/CSS/controller and mocks all backend fetches and Chrome storage.
It must print PASS and show only the pairing panel. This browser fixture is a
separate rendering check, not included in the Node test count. A standalone
headless Chrome attempt was blocked by the execution sandbox (SIGABRT); the
successful rendering check used the in-app browser instead.

## Remaining limits

The installed Chrome extension still needs to be reloaded and retested. A manual
DevTools fetch does not trigger controller boot; use Retry or reopen the panel.
Stored-token presence retains its existing meaning: boot does not independently
validate token expiry. Existing report-fetch 401 handling still clears the token
and returns to pairing. Capabilities behavior and active-job authentication-error
classification were not redesigned. Superseded requests are ignored, not aborted.
This is not a live research/provider validation. No commit or push is included.
