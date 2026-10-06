# Exact-origin readiness CORS before pairing

The extension's actual `sidepanel.js` boot sequence calls `GET /health`, then
`GET /v1/version`, before checking its locally stored pairing token. Any fetch failure
at either step produces "Local service not reachable". Neither request normally needs
a preflight: ApiClient sends a simple GET without custom headers. The actual GET
response still needs appropriate CORS headers in a CORS-enforcing context. A 204
OPTIONS response without Access-Control-Allow-Origin does not grant that permission.

The frozen middleware reflects the exact paired origin for these routes. Before
pairing (or when the extension differs from the stored paired origin), it omits the
headers. Thus successful curl requests do not establish browser readability. The
existing /v1/pair bootstrap exception does not extend to readiness endpoints.

## Narrow fix

Add a non-secret optional setting:

`STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN`

It must be exactly `chrome-extension://` followed by 32 lowercase Chrome ID characters
(a–p). No wildcard, path, query, other scheme, whitespace normalization or default ID.
Before pairing only, the middleware reflects this configured origin for exactly
`/health` and `/v1/version`, on both GET responses and OPTIONS preflights.
Once any origin is paired, the existing paired-origin rule exclusively applies to
these routes. The configured bootstrap origin cannot override an existing pairing.

For the extension identified in this review, configure the service with:

```sh
STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN=chrome-extension://ieplifjjheknpihonflfnnofbdlciepa
```

This setting must reach the actual backend process when it is restarted after review.
It does not create pairing, store credentials, issue a token or bypass bearer auth.
No configuration or restart of the running production service was performed here.
If the setting is absent, behavior remains fail-closed until pairing exists. A stale
pairing for a different origin still requires the normal re-pairing process.

The /v1/pair CORS bootstrap behavior remains unchanged: extension origins can access
that endpoint, but successful pairing still requires the correct one-time code and
honors expiry/lockout. Research and report routes still require the paired Origin
and issued Bearer token. No global CORS widening, wildcard, API credential exposure,
research-engine/provider change, database operation or localhost-binding change.

## Verification

Tests added before the production change reproduced four GET/OPTIONS health/version
failures and six invalid-config acceptance failures (10 failed, 14 passed). New tests
cover the exact supplied origin, wrong origins, wildcard rejection, null default,
paired-origin precedence, path bounds, original pairing schema, one-time use,
authenticated unknown-job access, wrong/missing credentials and environment loading.
The actual sidepanel module is loaded with fake DOM/Chrome/storage and mocked fetch
that enforces the CORS outcome; it displays UNPAIRED instead of SERVICE_UNAVAILABLE
when the exact health/version origin is readable. This is offline controller proof,
not a live installed-Chrome assertion. Existing backend suites cover expiry, brute
force, CORS and job authorization. No test touches production storage or real secrets.

The pre-existing api_client.test.js changes remain useful and unchanged: correct
pairing_code field, returned token handling/storage and subsequent Bearer auth,
and invalid/expired pairing failures. Production pairing payload already matched
the backend, so no frontend request/schema change is introduced.

## Limits

The live observations are user-supplied. Chrome extension host permissions may affect
whether CORS is enforced in the actual execution context; offline CORS/controller
proof does not establish that CORS is the only live cause. Installed extension path,
service environment, stale pairing, and reload state still require live verification
after review. The separate capability-reporting discrepancy is out of scope.
Changing unpacked extension ID requires explicitly updating the approved origin;
existing pairing is never silently replaced. No production service restart occurred.
