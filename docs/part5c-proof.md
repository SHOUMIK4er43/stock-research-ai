# Part 5C implementation and proof

Verified locally: 649 full tests passed (602 from Parts 1-5B, unchanged, plus 47 new Part 5C
tests: 31 in `test_challenge.py`, 16 in `test_challenge_pipeline.py`). Ruff passed on the
full tree. Mypy passed across 73 source files. A fresh offline proof
(`scripts/verify_challenge.py`, into a new, never-reused output directory) ran both required
tracks. No dependencies were added. No commit, tag, or push was made as part of this
implementation pass.

## Scope

Part 5C is the **Independent Challenger / Evidence Checker** — it re-verifies a frozen Part
5B `AnalysisSnapshot` against the exact same closed evidence bundle Part 5B itself used. It
mints no new evidence, no recommendation, no target price, no final report (Part 6A), and no
browser extension (Part 6B). Every check either re-derives something Part 5B itself already
computed (to catch persisted-data tampering or storage corruption between then and now) or
applies a genuinely new lexical/structural rule Part 5B's own validator never ran.

## Closed-evidence guarantee

`application/challenge.py::build_challenger_bundle` re-resolves evidence via
`analyst_engine.resolve_evidence(reviewed.request, company, ...)` — the exact same pinned
`AnalysisRequest` embedded in the reviewed snapshot. No new discovery, no "latest"
substitution, no Screener/Kite/web calls. It also independently re-reads every direct and
transitive raw artifact byte-for-byte (`direct_raw_artifacts`/`transitive_raw_artifacts`,
mirroring `analyze_company`'s own safeguard exactly) — JSON-payload hash comparison alone
never touches bytes on disk, so this was added as a genuine gap-closer during
implementation (see "What changed beyond the approved file list", below).

## §0 structural correction: `suggestion_kind`, never `finding_type`

`ChallengerFindingCandidate` (the *only* shape a `ChallengerModel` may emit) carries
`suggestion_kind: ModelSuggestionKind` (`MATERIAL_EVIDENCE`/`ANALYTICAL_TENSION`/
`ALTERNATIVE_INTERPRETATION`/`LANGUAGE_CONCERN`) — there is no `finding_type` field at all,
so `extra="forbid"` on the shared `CalculationModel` base means a raw candidate dict
containing one fails Pydantic validation before ever reaching the validator
(`test_model_can_never_smuggle_a_finding_type_field`). `application/challenge_validation.py`
is the *only* place that maps an accepted suggestion to a persisted, model-originated
`ChallengeFinding.finding_type`, drawn from a closed set:
`MODEL_SUGGESTED_MATERIAL_EVIDENCE`, `MODEL_SUGGESTED_ANALYTICAL_TENSION`,
`MODEL_SUGGESTED_LANGUAGE_CONCERN` (all fixed `NON_BLOCKING`), or a peer
`AlternativeInterpretation` for `ALTERNATIVE_INTERPRETATION`. `ChallengeFinding` structurally
enforces that only `source="DETERMINISTIC"` may ever carry `severity="BLOCKING"`
(`domain/challenge.py::ChallengeFinding.severity_matches_type`) — a model suggestion can
never become `INVALID_AGAINST_BUNDLE` on its own.

## Finding taxonomy and fixed severities

`SEVERITY_BY_TYPE` in `domain/challenge.py` is a closed dict from every `FindingType` to its
one fixed `Severity`; `ChallengeFinding`'s own validator rejects any other pairing at
construction time. `CHALLENGE_TIER` and `LIMITATION_TIER` are disjoint frozensets used only
by `compute_claim_outcome`, never re-derived elsewhere.

## Deterministic claim-outcome aggregation

`domain/challenge.py::compute_claim_outcome` — the single source of truth, applied in order:
any `BLOCKING` finding → `INVALID_AGAINST_BUNDLE`; else if every finding present is
`INSUFFICIENT_EVIDENCE_TO_VERIFY` → `UNVERIFIABLE_FROM_BUNDLE`; else if any finding's type is
in `CHALLENGE_TIER` → `CHALLENGED`; else if any is in `LIMITATION_TIER` →
`SUPPORTED_WITH_LIMITATIONS`; else → `SUPPORTED_BY_BUNDLE`. `ClaimReview`'s own validator
re-derives this from its own `findings` tuple and rejects any mismatch
(`test_outcome_must_match_deterministic_aggregation`) — the outcome is never set
independently of its findings, by construction.

## Deterministic checks (`application/challenge.py::review_claim`)

Per claim, independently re-verified against the fresh evidence bundle:

- **Citation integrity**: every citation is re-resolved by `(kind, owning_snapshot_id,
  item_key)` against a freshly-built source list (`CITATION_NOT_FOUND` if missing,
  `CITATION_HASH_MISMATCH` if the owning object's freshly-computed hash no longer matches
  the persisted citation's hash — reachable via direct unit calls to `review_claim`, since
  the *stricter* whole-transaction check in `SQLiteChallengeRepository.save_challenge`
  refuses persistence outright for the same underlying corruption, exactly like Part 5B's
  own atomic-integrity standard: see `test_tampered_calculation_row_refuses_challenge_persistence`).
- **Numeric/unit re-check**: `NUMERIC_MISMATCH`/`UNIT_MISMATCH`, reusing
  `evidence_matching.extract_numeric_tokens`/`canonical_values` — an independent rerun of
  Part 5B's own numeric check against the frozen statement text, catching persisted-data
  drift.
- **Period/scope**: `INCOMPATIBLE_PERIOD`/`INCOMPATIBLE_SCOPE` if freshly re-resolved
  citations no longer share one observation period/reporting scope.
- **Conflict**: `DETERMINISTIC_CONFLICT` via a fresh rerun of
  `evidence_matching._has_conflicting_evidence` over the claim's own citations.
- **Causality**: `UNSUPPORTED_CAUSALITY` (`CAUSAL_CONNECTIVE` lexicon) unless the claim
  carries a `ManagementStatement` (QUOTE-mode) — lexical detection is defense-in-depth only,
  never proof of a false causal claim.
- **Forbidden language**: `FORBIDDEN_RECOMMENDATION_LANGUAGE` (BLOCKING),
  `UNSUPPORTED_CERTAINTY` (NON_BLOCKING), `ASSUMPTION_PROMOTED_TO_FACT` for BASE-label
  likelihood language (`FORBIDDEN_BASE_LIKELIHOOD`) or a citation that freshly re-resolves
  as `SCENARIO` despite being recorded as `FACT`.
- **Sector policy**: `SECTOR_POLICY_VIOLATION` (BLOCKING) re-derived structurally from each
  cited `SECTOR_KPI`/`PATTERN` definition's own `applicable_profiles` — independent of
  whatever `SupportAssessment` already claims; `SECTOR_LANGUAGE_CONCERN` (NON_BLOCKING) from
  an independent re-application of `BANK_INDUSTRIAL_TERMS`.
- **Missingness**: `EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE` (BLOCKING) — a fixed evaluative
  lexicon applied to a claim whose cited reference status doesn't freshly resolve
  `AVAILABLE`. This fills a real gap: Part 5B's own numeric validator never inspects
  purely-qualitative overclaims about unavailable metrics.
- **Forensic pattern**: `MISCONDUCT_LANGUAGE_UNSUPPORTED` (BLOCKING) — a fixed
  fraud/misconduct lexicon applied to a claim citing a `PATTERN` reference, enforcing the
  pre-existing `PatternDefinition.interpretation_boundary` field Part 5B's own validator
  never inspects.
- **Document/management**: `MANAGEMENT_QUOTE_ALTERED` (BLOCKING) — the quote is re-verified
  as an exact substring of the *freshly re-fetched* chunk text, never trusted from the
  persisted claim; `ONE_SIDED_INTERPRETATION` (NON_BLOCKING) when a quote is presented
  without any offsetting qualifier.
- **Scenario/assumption**: `SCENARIO_ASSUMPTION_INVALID` for `VARIANT_PERCEPTION` claims
  whose declared `dependency_assumption_ids` no longer resolve exactly to the cited
  scenario's own assumptions.
- **Temporal**: `POINT_IN_TIME_LIMITATION_OMITTED` (NON_BLOCKING) if cited evidence carries
  the unverified point-in-time limitation code but the persisted claim doesn't declare it.

## Omitted-evidence detection (`RELEVANCE_REGISTRY`)

A fixed, hardcoded tuple in `application/challenge.py`, versioned as
`RELEVANCE_REGISTRY_VERSION = "5C.1-v1"`. Two grounding rows, both empirically verified
against real, currently-present, currently-uncited Part 4D evidence during the proposal
phase and re-confirmed by the fresh proof run below:

| rule_id | section | sector_profile | primary → related | relationship |
|---|---|---|---|---|
| `IT_PROFIT_GROWTH_VS_MARGIN_DETERIORATION` | `SECTOR_KPIS` | `IT_SERVICES_PROFILE` | `NET_PROFIT_GROWTH`/`SALES_GROWTH` → `OPERATING_MARGIN_DETERIORATION` | `LINKED_FAMILY_TENSION` |
| `BANK_DEPOSIT_GROWTH_VS_DECELERATION` | `SECTOR_KPIS` | `BANK_PROFILE` | `DEPOSIT_GROWTH` → `DEPOSIT_GROWTH_DECELERATION` | `LINKED_FAMILY_TENSION` |

`OmissionReviewStatus` is always exactly one of `NO_RELEVANT_OMISSION_FOUND`,
`MATERIAL_OMISSION_FOUND`, or `OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES` — the absence of a
matching registry rule for a claim's section/sector is never conflated with "nothing was
omitted"; it is persisted as its own distinct status.

## Peer-only business-model rule: option B (confirmed)

Direct inspection of the frozen Part 5B contract at this session's baseline
(`application/analysis.py::build_source_references`, grepped for `peers`/`PeerSnapshot`)
confirmed zero references — `PeerSnapshot` exists on `CompanyExtractionResult` but is never
independently citable. No `PEER` `EvidenceKind` was fabricated. The peer-only check is
`NOT_EVALUABLE_IN_CLOSED_EVIDENCE_V1` and is absent from `RELEVANCE_REGISTRY`.

## Independence metadata

`compute_independence_basis` compares `reviewed_analysis.model_metadata` against the
challenger's own `ModelMetadata`, never asserted by either model: `DETERMINISTIC_ONLY` (no
challenger), `SEPARATE_MODEL` (different provider), `SAME_PROVIDER_DIFFERENT_MODEL`,
`SAME_MODEL_SEPARATE_PROMPT`, or `SAME_MODEL_SAME_PROMPT`.

## Persistence and atomic integrity

One new append-only table, `challenge_snapshots` (`_ChallengeRow`/
`SQLiteChallengeRepository`), auto-created via `_Base.metadata.create_all` — no other schema
change. `save_challenge` re-verifies, inside one `session.begin()` transaction: the reviewed
`AnalysisSnapshot` row itself (new helper `_load_analysis_snapshot_in_session`), every
distinct pinned source across all thirteen evidence kinds (`_load_evidence_owner_in_session`,
reused unchanged), and every derived kind's upstream lineage
(`_verify_upstream_lineage_in_session`, reused unchanged). Any failure raises
`ChallengeIntegrityError` (a `ValueError` subclass) and nothing is inserted —
`test_snapshot_payload_corruption_between_review_and_insert_refuses_atomically` proves the
exact TOCTOU gap Part 5B's own `SQLiteAnalysisRepository` review closed is closed here too.

## Capabilities

`Capabilities.deterministic_challenge_available` is `True` once `ChallengeServices` is wired
— no provider needed. `Capabilities.live_challenger_available` is `True` only when
`provider_kind == "LIVE"`, mirroring `analysis_available`'s exact gating.
`analysis_available` itself is untouched.

## CLI

`stock-research-ai --database PATH challenge-analysis --analysis-snapshot UUID --output PATH`
(optional `--execution` for model-assisted review). Exit codes: `0` review completed
(`CHALLENGED`/`INVALID_AGAINST_BUNDLE` outcomes are honest success, not failure); `2`
malformed request (unknown snapshot ID, bad JSON/paths); `4` challenger provider unavailable
(only when `--execution` is given without an explicitly injected model); `5` validator/system
failure (`pydantic.ValidationError`); `6` persistence/integrity failure
(`ChallengeIntegrityError`, including closed-evidence re-resolution failures).

## What changed beyond the approved file list

- **`AnalysisRepository.get_analysis(analysis_snapshot_id)`** was added (Protocol +
  `SQLiteAnalysisRepository` implementation) — a pure additive fetch-by-primary-key method,
  required because `ChallengeRequest` is deliberately shaped as just
  `{analysis_snapshot_id}` (no `company_id`) and no existing method could resolve a snapshot
  by ID alone. No existing method's signature or behavior changed.
- **Raw-artifact byte re-verification** was added to `build_challenger_bundle` (a
  `RawArtifactStore` dependency on `ChallengeServices`), because JSON-payload hash comparison
  alone never reads bytes on disk — without it, on-disk raw-artifact corruption (the same
  class of corruption Part 5B's own `analyze_company` catches via `services.artifacts.read`)
  would silently pass through Part 5C's own review undetected. This was discovered via a
  failing test (`test_direct_raw_artifact_corruption_refuses_challenge`) during
  implementation, not anticipated in the approved architecture; it is additive only —
  no Part 5B behavior changed.
- **`tests/test_extraction.py::test_snapshot_history_and_identity_binding`** was updated to
  add `"challenge_snapshots"` to its hardcoded expected table set — the same maintenance this
  test required for every prior part's own new table (`analysis_snapshots` for Part 5B, etc.).
  No Part 5B *behavior* changed; this is a structural enumeration test.

## Real proof (INFY, YESBANK) — Track 1

Both real fixtures were used to construct a genuine Part 5B `AnalysisSnapshot` pinning both a
`SECTOR_KPI` growth metric and its linked `PATTERN` evidence, with a claim citing only the
growth metric. `challenge_analysis` independently found the uncited pattern via
`RELEVANCE_REGISTRY` and raised a real `ANALYTICAL_TENSION` finding — not a fabricated
scenario. From the fresh proof run:

- **INFY**: `SALES_GROWTH` cited; `OPERATING_MARGIN_DETERIORATION` pinned but uncited →
  `evaluation_status=AVAILABLE`, `rule_outcome=NOT_TRIGGERED` →
  `omission_review_status=MATERIAL_OMISSION_FOUND`, `claim_outcome=CHALLENGED`. A direct
  `MISCONDUCT_LANGUAGE_UNSUPPORTED` check against the real pinned pattern reference also
  fired correctly.
- **YESBANK**: `DEPOSIT_GROWTH` cited; `DEPOSIT_GROWTH_DECELERATION` pinned but uncited →
  `evaluation_status=AVAILABLE`, `rule_outcome=OBSERVATION_ONLY` →
  `omission_review_status=MATERIAL_OMISSION_FOUND`, `claim_outcome=CHALLENGED`.

## Synthetic proof — Track 2

Reachable through the honest `analyze_company` → `challenge_analysis` pipeline: causal
language without a management quote → `UNSUPPORTED_CAUSALITY` /
`SUPPORTED_WITH_LIMITATIONS`; a model-assisted `ALTERNATIVE_INTERPRETATION` suggestion,
validated and attached to the claim review. Direct mechanical checks (calling
`review_claim` directly on hand-built claims, for finding types Part 5B's own validator
already refuses at claim-creation time and can therefore never reach Part 5C through the
ordinary pipeline): `FORBIDDEN_RECOMMENDATION_LANGUAGE`, `UNSUPPORTED_CERTAINTY`,
`EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE`.

## Known limitations (V1)

- The omitted-evidence registry has exactly two grounding rows; any section/sector/metric
  combination outside them reports `OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES`, never a false
  "nothing omitted."
- Peer-based cross-checks are `NOT_EVALUABLE_IN_CLOSED_EVIDENCE_V1` (no `PEER` evidence kind
  exists anywhere in the closed evidence bundle).
- Lexical checks (causality, certainty, sector language, misconduct, evaluative adjectives)
  are defense-in-depth pattern matches, not semantic proof; they can both under- and
  over-fire relative to true intent, exactly like Part 5B's own equivalent checks.
- `WRONG_COMPANY_EVIDENCE` is structurally prevented upstream by `resolve_evidence`'s own
  company-ownership check for `FINANCIAL`/`SHAREHOLDING` evidence (mirroring
  `AnalystServices`'s own guarantee) — it is reachable only via direct unit-level calls to
  `review_claim`, never through the full engine pipeline, and there is no equivalent
  ownership check for `MARKET` evidence in this codebase to extend it to.

## Part 5C / 6A boundary

Part 5C stops at claim/section/snapshot-level review artifacts (`ClaimReview`,
`SectionReview`, `ChallengeSnapshot`). It produces no final report, no narrative brief, no
BUY/SELL/HOLD language, and no export beyond the existing `export_challenge` JSON views —
composing challenge output into a final research report is explicitly Part 6A's job.
