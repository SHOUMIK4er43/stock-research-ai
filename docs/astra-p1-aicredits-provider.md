# ASTRA P1 — AI Credits analyst provider

Status: implemented for offline review; no live request or catalog query, no commit.
Parent: `a6f9dfac2627bc7d84d251fe506c09ac27597974` on `astra-development`.
Frozen main: `8453c72b265cb08b43c7b882726a9e941c977355`.

## Architecture and boundaries

The existing engine still assembles the frozen `AnstInputBundle`, builds the exact
R1 `AnalystModelInput`, calls the provider-neutral `AnalystModel`, and submits the
response to the unchanged Part 5B validators. Analysis persistence, independent
Part 5C challenge, and Part 6A report assembly are unchanged. No provider-specific
research semantics, new evidence selection, FACT promotion, valuation or bank changes.

The direct Anthropic adapter remains available. Its instructions, candidate tool
schema, and error class are extracted to `adapters/analyst_contract.py`; its class
implementation and hash function are unchanged. The original import of
`AnalystProviderError` from the Anthropic module remains supported.

## Configuration and credentials

Select `STOCK_RESEARCH_AI_ANALYST_PROVIDER=aicredits` and configure the exact model ID
in `STOCK_RESEARCH_AI_AICREDITS_MODEL`. There is no default AI Credits model, model
catalog request, key-format inference, model switching, or provider fallback.
The API contract used here is the static contract supplied in the P1 request;
`/api/models` and `/v1/credits` were deliberately not queried. No local repository
catalog documentation was found. Current account availability must be confirmed
manually after review.

`STOCK_RESEARCH_AI_AICREDITS_API_KEY` is a separate `SecretStr`. It takes precedence
over the `aicredits-api-key` account in the `stock-research-ai` macOS Keychain service.
The existing Anthropic environment variable and `analyst-api-key` account are unchanged.
An unset selector still recognizes only the legacy Anthropic credential; an AI Credits
credential alone never selects that provider. Explicitly selected providers with
missing credentials fail clearly; unknown selectors fail before credential lookup.

`stock-research-ai configure-analyst --provider aicredits` prompts without echoing and
writes only the separate Keychain account. The CLI explains the required selector,
model setting, and environment precedence. Omitting `--provider` preserves Anthropic.
The command stores credentials; it does not persist provider/model configuration.
Keychain setter errors omit subprocess stderr and suppress exception chaining that
could otherwise expose the key in a timeout's command arguments.

Diagnostics stays offline and uses R2-A read-only SQLite inspection. It reports the
selected provider, selected model, credential source, and whether key/model are
configured. This is not a claim that the remote key, credits, or model are valid.
An R2-A test fixture now uses `None` instead of the unsupported string `"none"` to
represent absent configuration; its schema assertions remain unchanged.

## HTTP and structured output

Uses existing httpx, no new dependency. Fixed HTTPS endpoint:
`POST https://api.aicredits.in/v1/chat/completions`.
Headers: `Authorization: Bearer <credential>` and `Content-Type: application/json`.
Redirects and environment proxy inheritance are disabled. Headers are not persisted.

The JSON request contains:

- `model`: exact configured identifier.
- `max_tokens`: positive integer from `max_output_tokens`, default 4096.
- `messages`: system = unchanged analyst transport instructions; user = exact R1 JSON.
- `tools`: one OpenAI function named `emit_claims`, with the unchanged candidate schema.
- `tool_choice`: `{"type":"function","function":{"name":"emit_claims"}}`.
- `parallel_tool_calls`: false.
- Optional explicitly supplied `temperature` (0–2) and `top_p` (0–1).

Unsupported generation parameters fail before transport; none are silently recorded
as if used. The existing timeout setting defaults to 90 seconds for reads; connect,
write and pool timeouts are 10 seconds. R1 binding and overflow validation occur
before transport. Responses are capped at 2,000,000 decoded bytes.

Exactly one choice and one expected function call are accepted. The finish reason
must be `tool_calls` or absent/null; a truncation/refusal finish reason fails. Function
arguments must be a JSON object containing only a `claims` array. Duplicate keys,
non-finite constants, malformed JSON, missing/wrong/multiple calls fail typed, with
no semantic retry. Individual candidates pass unchanged to the existing validators.

## Provenance without frozen-schema changes

`provider` is `aicredits`; `model_id` is the configured/requested model. `model_version`
is the response's model, or null when absent. Any non-null response model that is not
exactly equal to the requested ID fails `MODEL_MISMATCH`; no alias assumption or
successful snapshot is manufactured.

The canonical UTF-8 request JSON is sent verbatim and persisted as `rendered_text`;
`rendered_sha256` hashes those exact bytes, including tools/model/effective parameters.
The unchanged bundle digest and the R1 input binding are retained. Effective generation
parameters (including default output limit) and their hash are retained separately.

The raw response keeps the unchanged candidate list under `claims`. Reserved,
adapter-owned `_provider_metadata` contains body `id` as `response_id`, and the
`x-request-id` response header as `request_id`, when present. It is included in the
raw structured-payload hash and survives existing snapshot persistence. IDs must be
nonempty printable strings of at most 256 characters. The model cannot supply this
reserved metadata through function arguments. No domain field or SQL schema was added.

Only this allowlisted response data is persisted, never the full response headers.
Credential echoes or Authorization-Bearer text in persisted response content cause
a safe typed refusal. The request is also checked for the credential before transport.

## Error and retry contract

| Condition | Code | Attempts |
| --- | --- | --- |
| 401 | AUTHENTICATION_FAILED | 1 |
| 403 | PERMISSION_DENIED | 1 |
| 402 / exact insufficient-credit codes | INSUFFICIENT_CREDITS | 1 |
| 404 / exact model-unavailable codes | MODEL_UNAVAILABLE | 1 |
| Other non-transient HTTP rejection | INVALID_REQUEST | 1 |
| 429 | RATE_LIMITED | Up to 3 |
| 5xx | PROVIDER_UNAVAILABLE | Up to 3 |
| httpx timeout | TIMEOUT | Up to 3 |
| Other httpx transport failure | CONNECTION_FAILED | Up to 3 |
| Bad response / arguments / call / model / size | Specific typed parsing error | 1 |

Transport retries use 1- and 4-second waits, the same request body and model, and no
provider fallback. Exact error codes recognized for credits: `insufficient_credits`,
`insufficient_quota`, `payment_required`; for models: `model_not_found`,
`model_unavailable`. Unknown prose is never guessed or echoed. Error-envelope details
need manual provider validation. No credits preflight is added.

## Verification and remaining limits

Tests were added first. Before production edits, adapter tests failed import and
configuration tests reported 10 failures / 2 passes. Subsequent checks cover real
frozen evidence and exact references, unchanged validators, complete offline
analysis/challenge/report persistence, strict parsing, bounded retries, configurable
models, independent credentials, CLI, diagnostics, and safe failure paths.

Final command results are recorded in the accompanying review report. Existing
fixtures/golden truth, dependencies, domain/application source, migration/SQLite
implementation, packaging, extension, and R1 implementation files are unchanged.

Live model/catalog availability, forced-tool dialect support, exact model-name echo,
provider context-window fit, output quality, real Keychain operation and credits/error
format remain unverified. Strict model equality may reject provider aliases. Timeout
retries may cause duplicate billed upstream generations; this API contract establishes
no idempotency guarantee. One logical generation is not an exactly-once billing claim.

Rendered provenance contains substantive evidence and increases local storage/provider
data volume. Existing third-party AnalystModel implementations still must adopt R1's
required `model_input`. LaunchAgent environment configuration is unchanged; selected
provider/model must reach the service process through its existing settings mechanism.
No real credentials were read or stored, no live provider/Screener calls were made,
and the production database was not accessed. Part 6C-C remains paused for
`PROVIDER_CONFIGURATION`; P1 does not authorize resuming it or starting R2-B/C/D.
