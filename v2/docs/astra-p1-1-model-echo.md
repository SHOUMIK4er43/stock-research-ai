# ASTRA P1.1 — AI Credits model-echo compatibility

Parent: `bd6809f53f89e6ba9091903fc4ca4bdb6ed7ca9c` on `astra-development`.
This change supersedes P1's exact-equality-only description for AI Credits model echoes.

## Supplied evidence and reproduced failure

The user reported an HTTP 200 AI Credits response to requested model
`anthropic/claude-sonnet-5` with response model `claude-sonnet-5`, finish reason
`tool_calls`, and exactly one `emit_claims` call with arguments
`{"claims":[{"text":"AI Credits tool calling works"}]}`.
No live request was made during this implementation. MockTransport replay reproduced
`MODEL_MISMATCH` on the frozen implementation. Initial tests: 3 failed, 18 passed;
failures covered the supplied echo, another exact suffix, and the supplied tool payload.

The supplied argument object demonstrates tool transport, not a valid Part 5B analyst
claim: it lacks the required claim fields. Existing candidate validation is unchanged.

## Exact rule

A non-null response model is accepted when either:

1. It exactly equals the configured model, as before; or
2. The configured model starts with the literal, case-sensitive `anthropic/`, and
   removing exactly that single leading prefix produces the exact, nonempty response model.

An empty response model remains rejected, including a configured `anthropic/` with an
empty suffix. Null/missing response model remains accepted and recorded as null.
No fuzzy matching, case folding, whitespace trimming, suffix search, latest aliases,
repeated prefix removal, catalog lookup, model switching, or provider fallback.
The comparison is local to the AI Credits adapter; the direct Anthropic adapter is unchanged.

Accepted examples:

- `anthropic/claude-sonnet-5` → `anthropic/claude-sonnet-5`
- `anthropic/claude-sonnet-5` → `claude-sonnet-5`
- `claude-sonnet-5` → `claude-sonnet-5`

Rejected examples for requested `anthropic/claude-sonnet-5`:

- `claude-sonnet-4.6`
- `claude-sonnet-5-latest`
- `CLAUDE-SONNET-5`
- `provider/claude-sonnet-5`
- ` claude-sonnet-5` or `claude-sonnet-5 `
- Empty string or a non-string model value

Also rejected: unprefixed requested `claude-sonnet-5` → `claude-sonnet-5-extra`,
reverse prefix addition, different/case-varied configured prefixes, and removal of
multiple `anthropic/` prefixes at once. Mismatches still fail once with MODEL_MISMATCH.

## Provenance and unchanged boundaries

The comparison does not modify either string. `model_id`, the actual outgoing body,
and the exact hashed rendered request retain `anthropic/claude-sonnet-5`.
`model_version` retains the provider-reported `claude-sonnet-5`.
Tests verify both values through ModelMetadata JSON round-trip and check the rendered
request against actual transport bytes and SHA-256. Provider IDs, raw claims and all
other provenance behavior are unchanged.

Only the adapter comparison, a focused new test file, and this document change.
Protocol, R1 projection, ResearchEngine, 5B validators, 5C, 6A, domain/SQL schemas,
valuation, bank safeguards, direct Anthropic behavior, golden fixtures, dependencies,
extension, packaging and backup/restore are unchanged.

## Verification and limits

22 focused MockTransport tests cover exact matches, the new literal-prefix rule,
provenance, malformed/empty/null/missing model values, rejection boundaries, no retry
or fallback, and direct Anthropic compatibility. The accompanying review report records
broader offline verification under UV_NO_SYNC=1, UV_FROZEN=1, UV_OFFLINE=1 and
PYTHONDONTWRITEBYTECODE=1, with isolated temporary databases/artifacts and real network
and platform credential access blocked.

This rule verifies string compatibility, not the provider's internal routing identity.
Other aliases remain deliberately rejected. Missing/null echoes still provide no
provider-reported model identity. The supplied one-request evidence does not establish
context fit, research quality, ongoing account availability, or every error envelope.
Transport retries retain their existing possible duplicate-billing risk. No live smoke,
production database operation, secret inspection, commit, or R2-B work was performed.
