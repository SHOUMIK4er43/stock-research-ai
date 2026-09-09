# Part 1 architecture

## Scope

Part 1 implements a minimal company registry behind the `ResearchEngine`, exposed
through FastAPI and MCP. Financial models establish contracts only. There are no
source requests, financial calculations, LLM calls, report outputs, or workers.

## Dependencies

```text
FastAPI interface ─┐
                  ├── ResearchEngine ── CompanyRepository Protocol
MCP interface ────┘          │                     ▲
                            ▼                     │ implements
                       Domain models         SQLite adapter
```

`application/engine.py` coordinates `get_capabilities`, `register_company`, and
`get_company`. It depends only on domain models and an application-owned Protocol.
Domain and application modules use Python and Pydantic; they never import
FastAPI, MCP, SQLAlchemy, settings, HTTP clients, or provider SDKs.

`adapters/sqlite.py` implements the repository using SQLAlchemy async and
aiosqlite. `bootstrap.py` constructs the adapter and engine and closes resources.
The interface factories accept an engine for testing or use bootstrap in their
lifespan. Each process owns its engine and database resources; HTTP and MCP can use
the same database file without sharing Python sessions.

The CLI stays in `__main__.py`. A lazy `main()` in `__init__.py` preserves the
existing console entry point without starting services on import. Configuration
uses `pydantic-settings`; logging uses only normal Python logging on stderr.

## Company domain and persistence

Company identity consists of an internal UUID, a name, exchange listings, and
separate provider identifiers. The exchange can be unknown. Registration stores
user-supplied information without attempting source verification or deduplication.
Each call creates a new UUID; retrieval is by that UUID only.

Pydantic domain objects are distinct from the SQLAlchemy row model. The registry
uses one companies table with ID, name, and JSON arrays for listings and provider
identifiers. Repository mappings reconstruct validated domain objects on reads;
ORM objects and sessions never leave the adapter.

Each repository operation owns a session. Writes use a transaction and roll back
on failure. `init-db` explicitly initializes the table. Construction, imports, and
help do not create files, and normal startup requires an initialized database.
Schema checks detect missing tables/columns but are not a full compatibility or
migration system. Only the company registry is persisted in Part 1.

## Financial contracts

| Layer | Contract | Purpose |
| --- | --- | --- |
| RAW | `RawArtifact` | Identify preserved content through a reference, SHA-256 hash, source URL/type, aware retrieval timestamp, and collector version. |
| NORMALIZED | `FinancialObservation` | Describe a typed value, original representation, company/metric, unit/currency, period, reporting scope, and artifact/location provenance. |
| DERIVED | `DerivedMetric` | Describe a future computed value with its dimensions, exact input record IDs, formula, calculation version, and timestamp. |

The models are immutable and reject unknown fields. `Decimal` values must be
finite; decimal strings and integers are accepted, while Python floats and
booleans are rejected. Unit scale is an explicit multiplier. Monetary units
require a three-letter uppercase currency code; this checks format, not an
external currency registry. Percent values use percentage points: 25 means 25%.

Periods carry a fiscal label and actual dates. Instant periods have only an end
date. Duration periods require a start date no later than the end date. Standalone
and consolidated scope belongs to each observation, not the company. Missing and
zero are different: missing observations have a null value, while an available
zero is a valid decimal. Available observations retain their original value and
unit; no source parsing or normalization algorithm runs in Part 1.

## Provenance and future extension

The intended lineage is:

```text
Future analysis conclusion
  → derived metric ID
  → exact normalized or prior derived input IDs
  → raw artifact ID and precise source location
  → source URL and retrieval event
```

Part 1 validates record shapes and local invariants. It does not resolve IDs,
store raw bytes, enforce graph-wide referential integrity, or generate conclusions.
Future ingestion must preserve payload bytes, resolve references, reject cycles,
retain restatements as new revisions, and check compatible periods, units, and
reporting scopes before calculations. Formula strings are descriptive metadata;
future arithmetic must be executed by versioned Python functions.

Future source adapters will acquire raw content through application-owned
interfaces. Separate normalizers will produce domain observations. Source URLs,
selectors, HTTP behavior, and browser objects stay outside the engine. Those
interfaces and implementations are deferred until their use cases exist.

Future sector policies will select applicable metrics and comparisons, avoiding a
universal metric set for banks, IT, and manufacturing. Future LLM adapters will
accept evidence and Python-calculated values and return structured claims, keeping
provider SDKs outside the core. Conclusions must distinguish FACT, CALCULATION,
INTERPRETATION, and SCENARIO. Those conclusion models and execution workflows are
not implemented in this phase.

## Interfaces and verification

FastAPI exposes only `/health`, `/v1/capabilities`, and company registration and
UUID retrieval under `/v1/companies`. MCP uses the installed SDK 2.1.1
`MCPServer`, async handlers, typed outputs, and a resource-owning lifespan. Its
stdio tools are `health_check`, `get_capabilities`, `register_company`, and
`get_company`. Neither interface duplicates application logic.

Tests cover domain invariants, fake-repository engine behavior, SQLite round trips
and transactions, HTTP contracts, MCP tools, and actual stdio communication.
Databases are temporary. Dependency-boundary checks keep infrastructure out of
the core. Ruff and strict mypy run against the installed environment.

## Limits

The unauthenticated service is intended for local development and defaults to
loopback. There are no source adapters, browser integration, financial persistence,
calculators, documents, LLM providers, migrations, queues, or report generation.
SQLite writes remain short and operation-scoped; durable research jobs and schema
evolution need a separate design when later phases require them.

## Part 2 design review: institutional research data

**Status: design proposal, not implemented.** The implemented system remains the
Part 1 foundation described above. No separate Part 2 implementation or detailed
source-adapter proposal existed in this repository at the time of this review.
This addendum defines the requirements that must guide that proposal.

The product target is an institutional-grade equity research system for a research
head, portfolio manager, professional analyst, family office, or sophisticated
investor. Part 2 is limited to acquisition, normalization, validation, storage,
and retrieval of evidence. Analysis engines, technical indicators, news impact
assessment, valuation, scenarios, thesis generation, and monitoring remain later
phases. Capability reporting must continue to distinguish implemented features
from this target.

The existing dependency direction is suitable. The Part 1 financial contracts
need deliberate extension before financial storage is introduced. Their current
company-only identity, limited units, and retrieval-only timestamps do not yet
support the full target. Versioned contracts and explicit migrations can reduce
future redesign; no design can promise that schemas will never evolve.

### Decisions to make before the first persisted financial dataset

| Current foundation | Required Part 2 design |
| --- | --- |
| `Company.id`, exchange listings, and provider identifiers | Preserve company IDs; distinguish issuer/company, security, listing, and source identifier. Support effective-dated identifier mappings and unresolved matches. |
| `FinancialObservation.company_id` | Keep company financial observations, and introduce typed subjects for instruments, indices, industries, sectors, economies/geographies, and commodity or currency series. A macro series must not require a dummy company. |
| Free-text `metric` | Versioned metric definitions with meaning, allowed units, dimensions, period semantics, and comparability qualifiers. Source labels map to definitions through source-specific normalizers. |
| Fiscal label plus start/end dates | Add frequency, fiscal calendar, reporting-period basis, and duration metadata; distinguish quarter, YTD, annual, source-reported TTM, daily, monthly, and irregular observations. |
| Retrieval timestamp on the artifact | Separately model observation/effective time, source publication time, system first-seen/retrieval time, and normalization time, with timestamp precision and unknown values. |
| Immutable model instances | Add persistent record IDs, source revisions, supersession links, and versioned selection policies. Do not overwrite earlier observations or silently choose between conflicting sources. |
| Money/number/shares/percent/ratio units | Add explicit quantity and rate units, per-share denominators, index levels, basis points, physical quantities, currency pairs, and unit-definition versions as supported datasets require them. |
| Available versus missing | Extend availability and quality reasons: not reported, not applicable, source unavailable, parse failure, partial period, and provisional value. Keep quality separate from whether a numeric zero exists. |
| Raw artifact ID and source locator | Preserve raw bytes, acquisition records, extraction/normalization versions, and source-to-output mappings; allow multiple evidence references where needed. |
| Formula plus input IDs | Reserve definition/version, named inputs, parameters, rounding policy, and input-snapshot references for later calculated results; no calculation executor in Part 2. |

Do not silently change the meaning of existing Part 1 fields. Introduce versioned
contracts or explicit conversion paths when these extensions are implemented.
Keep the existing company-registration API small. Ingestion should accept a known
company/subject ID or use an explicit identifier mapping; it must not call
registration repeatedly and create a new company on every refresh. Ambiguous
matches remain unresolved instead of being guessed.

### A shared evidence envelope, with typed record families

Use shared identity, temporal, provenance, and quality fields, with distinct
payload schemas. Do not put every future dataset into a wide company table or an
unvalidated JSON object. A conceptual envelope contains:

```text
record_id, schema_version, record_kind
subject_ref: kind + stable internal ID
definition_ref: metric/dataset definition ID + version
dimensions: validated for this definition
observation_time_or_period
published_at?, first_seen_at, retrieved_at, normalized_at
source_revision?, supersedes_record_id?
evidence_refs, transformation_ref, quality_flags
typed_payload
```

Not every field is meaningful for every record family. For example, reporting
scope is required for company statements but not for an exchange-rate series.
Nullable timestamps mean unknown, not equal to the period end. A value's financial
period and the dates on which people or the system could know it are independent.

The following families are conceptual extension points. Implement only those
needed by the approved Part 2 datasets; avoid creating unused tables for all of
them up front.

| Record family | Important semantics to preserve |
| --- | --- |
| Company financial/KPI observation | Company, metric definition, annual/quarterly period, standalone/consolidated scope, original value/unit, canonical decimal/unit, audited/provisional status where supplied. |
| Segment/operating observation | Segment, product, geography, customer/cohort, volumes, capacity, utilization, realization, and business mix; definitions and segment boundaries can change over time. |
| Industry/macro observation | Geography, industry/sector, frequency, series definition, source vintage, seasonal-adjustment and nominal/real basis where applicable. |
| Ownership observation | Holder or reported holder category, shares/stake, denominator and ownership basis, effective date, pledge information where supplied, and source methodology. |
| Price bar | Instrument/listing, exchange session date and timezone, interval, OHLC, volume and its unit, quote currency, source, and revision. |
| Corporate action | Instrument, action type, announcement/ex/record/effective dates as available, terms, currency, factor/ratio, source, and revision. |
| Membership/relationship | Historical sector/industry classification, index constituents, peer-universe membership, issuer-listing links, and validity dates with supporting evidence. |
| Document/event evidence | Source text or artifact reference, exact locator, title/type, event and publication dates, entity links, and speaker/role or stated facts when available. |
| Source estimate/guidance | Explicit record nature, author/provider, issue/as-of date, target horizon, target metric, range/qualifications, and evidence. Collection is distinct from later extraction and guidance-versus-actual analysis. |

Shared dimensions must have declared types and meanings. Free-form metadata may
preserve unrecognized source fields, but those fields cannot silently become
comparable canonical metrics. Source-reported ratios remain reported observations;
future ratios calculated by Python belong in DERIVED and retain their input IDs.

### Historical coverage and time-aware selection

Acquisition requests must express a date range, frequency, reporting scope where
applicable, requested datasets, and a source preference or policy. Do not limit a
company to a fixed number of columns or overwrite history with a latest snapshot.
Target at least ten years of annual and quarterly company history where available;
retain longer history when returned. Monthly macro series, daily market series,
and irregular event series require their own frequencies.

Each result must report requested versus delivered coverage: earliest/latest
observation, missing intervals, partial current periods, pagination/truncation,
source limitations, retrieval outcome, and freshness. A successful request with
two years of data must not be described as ten-year coverage. Gaps remain gaps;
normalization must not invent zeroes, forward-fill history, or interpolate values.

Keep different time concepts separate:

- **Valid/observation time:** the period or instant described by the evidence.
- **Publication/availability time:** when the source released that specific revision,
  where verifiable.
- **System time:** when the system first observed and stored it.

Future queries must distinguish "publicly available by date T" from "known to this
system by date T." Imported historical publications may support the first view
without supporting the second. If the original publication timestamp is unknown,
do not backdate availability to the fiscal period end; flag the limit and use a
documented conservative policy. Original and restated figures, revised macro
releases, and later corrected price bars must coexist.

Restatements append new records with source-revision and supersession information.
Different sources making conflicting assertions are not automatically revisions
of one another. A canonical selection is a reproducible view over preserved
observations, with the chosen source, policy version, and selection reason exposed.

### Metric definitions and sector applicability

A metric definition should specify its economic meaning, unit/dimensions,
reporting basis, permissible frequencies, source aliases, and definition version.
Examples of distinct definitions include basic versus diluted EPS, reported
versus adjusted earnings, gross versus net debt, and closing versus average
balance denominators. Similar source labels do not prove equivalent metrics.

Sector metadata should identify relevant definitions, dimensions, business
drivers, benchmark-universe requirements, and comparability qualifications. The
normalization layer must accommodate bank, NBFC, IT, pharma, FMCG, auto,
industrial, real-estate, insurance, commodity, utility, telecom, and internet KPIs
without adding a column per KPI or routing all companies through one ratio set.
Only definitions needed by incoming datasets need implementation in Part 2.

Actual sector analysis, metric scoring, economic driver modeling, peer rankings,
and valuation policies remain future modules. Mixed-business companies can have
multiple segment/sector relationships. Classifications and peer memberships need
validity dates; historical analysis must not silently use today's survivors or
today's index constituents. Future best/worst rankings also need an explicit
metric-direction and comparability policy, not a generic numeric maximum/minimum.

### Prices, ownership, and source assertions

Preserve raw and provider-adjusted price series separately, with adjustment
methodology, action inputs/factors when available, source, and vintage. Distinguish
price-return and total-return index series. Do not double-adjust prices or present
an adjustment as reproducible when the provider does not disclose its method.
Part 2 does not calculate returns, indicators, corporate-action adjustments, or
technical signals. Missing sessions and trading suspensions must remain visible.

Ownership snapshots alone do not establish investor intent or net buying. Preserve
share counts, denominators, holder classifications, transaction disclosures when
available, and corporate-action context. Strategic acquisition versus broad
institutional accumulation is a later evidence-backed interpretation, not a label
inferred automatically from a change in an ownership percentage.

News and management material should first enter as source evidence. Store event
time separately from publication time, preserve exact quotes and speaker context,
and retain corrections and duplicate-source relationships. Financial impact,
magnitude, duration, causal driver linkage, thesis impact, evasiveness, and
investment confidence are later analytical claims. Extraction confidence and data
quality are separate from confidence in an investment thesis.

Actuals, management guidance, third-party estimates, and consensus datasets must
remain distinguishable. A consensus assertion requires an identified valid source,
as-of date, methodology and contributor coverage where provided. News commentary,
management targets, or the system's own assumptions must not be labeled consensus.

### Adapters and acquisition flow

The dependency direction remains unchanged. The application owns small typed
interfaces, and concrete adapters implement them. A proposed flow is:

```text
AcquisitionRequest
  → source adapter: describe capabilities, discover/fetch requested evidence
  → raw artifact store + acquisition/retrieval record
  → source normalizer: parse and map into versioned typed records
  → contract validation + quality/coverage report
  → normalized repository with immutable records and lineage
  → AcquisitionResult containing IDs, coverage, and explicit failures
```

Potential ports include `SourceAdapter`, `Normalizer`, `ArtifactStore`, and a
repository for each implemented dataset family. Select adapters explicitly in
bootstrap; do not introduce a plugin framework, graph database, generic workflow
engine, background workers, or provider imports in `ResearchEngine`.

Adapter capability descriptions should state supported dataset kinds, subjects,
frequencies, scopes, history limits, revision support, and adjustment basis. HTTP
clients, source endpoints, authentication when a future source needs it, rate
limits, request retries, and source-specific parsing remain inside adapters.
`ResearchEngine` receives typed acquisition requests/results and evidence IDs,
never a browser page, HTML selector, provider response object, or vendor SDK type.

Keep acquisition and normalization independently replayable. Raw preservation
allows a corrected parser to rerun without fetching the source again. Preserve
the original payload separately from any cleaned text. Acquisition records should
identify the dataset, safe request metadata, source URL, retrieval time, response
status, collector version, and content hash; never persist credentials as evidence.

Repeated acquisition should reuse identical content blobs while retaining each
retrieval event. Normalization idempotency should include the raw input,
normalizer/configuration version, and record identity; a new source revision must
not be discarded merely because its numeric value is unchanged. Invalid records
retain their raw evidence and explicit validation errors. Failed or partial runs
must not appear as complete datasets. Store raw bytes atomically before committing
references; keep SQLite transactions short and outside network requests.

Allowed normalization includes exact numeric parsing, sign handling, declared
unit/scale conversion, date parsing, and source-label mapping, with transformation
provenance. It excludes inferred figures, growth rates, CFO/PAT analysis, TTM
construction, quarter-from-YTD subtraction, price adjustment, valuation, and other
analytical calculations. Source-reported TTM or quarter/YTD figures may be retained
as supplied, with their basis explicitly labeled.

### Provenance, storage, and reproducible research inputs

Keep the three layers separate:

- **RAW:** immutable payloads plus retrieval events and exact source locators.
- **NORMALIZED:** typed source assertions, versions, quality flags, and all input
  artifact/location references used in their transformation.
- **DERIVED:** future Python-calculated values with definition/version, named
  input IDs, parameters, rounding, and calculation time; no executor in Part 2.

Later claims remain FACT, CALCULATION, INTERPRETATION, or SCENARIO. A claim can cite
both numbers and document/event evidence. Causal links, scenarios, thesis positions,
and contradictory interpretations must retain their supporting and opposing
evidence, rather than replacing source facts.

Use local artifact files and SQLite metadata/normalized records initially. Store
financial decimals losslessly, for example as canonical decimal text. Index
implemented observations by subject, metric definition, period, scope/dimensions,
and source/revision. Repository queries should filter by date range and return
ordered, paginated series. If scale later warrants another store, the repository
ports should contain that change.

A future research-input manifest should freeze selected observation/document IDs,
source revisions, selection policy, universe/membership version, and availability
cutoff. This lets the primary analyst and thesis challenger work from an auditable
evidence set, and lets monitoring compare later evidence against the original
thesis. Define this contract alongside acquisition results; implementing report
execution or a monitoring scheduler is outside Part 2.

Financial persistence requires an explicit schema-version and evolution policy
before real history accumulates. This review does not introduce a migration tool
or authorize a database change. Preserve existing company IDs and existing saved
registrations when the Part 2 storage design is implemented.

### Coverage of the institutional product target

The following maps all requested future capabilities to evidence requirements.
None of the analytical capabilities in this table is implemented by this review.

| Future capability | Foundation that Part 2 must preserve or accommodate |
| --- | --- |
| 1. Business quality | Segment, geography, customer/product dimensions and cited qualitative business evidence. |
| 2. Historical financial analysis | Ten-plus years where available, annual/quarterly periods, complete time ranges, units, scope, and revisions. |
| 3. Patterns and inflections | Ordered comparable series with frequencies, gaps, seasonality context, and unchanged historical observations. |
| 4. Financial forensics | Granular statement/footnote evidence, accounting definitions, auditor/governance events, and cross-statement lineage. |
| 5. Sector-specific research | Extensible metric definitions, effective-dated sector relationships, and applicability metadata. |
| 6. Industry research | Industry/geography subjects, operating series, classifications, and cited policy/competitive evidence. |
| 7. Industry norms | Comparable company/industry series and versioned historical peer universes; statistics come later. |
| 8. Sector performance | Sector/index identities, constituent history, prices, earnings, valuations reported by sources, and actual flow datasets where available. |
| 9. Macro intelligence | External series with geography, frequency, units, publication vintages, and economic definitions. |
| 10. Current affairs | Dated regulatory/policy/legal/geopolitical evidence and affected entity links; causal interpretation comes later. |
| 11. News intelligence | Source events and text, corrections, timestamps, provenance, and room for separately cited impact hypotheses. |
| 12. Management intelligence | Attributed statements, target horizons, guidance revisions, governance/capital-allocation evidence, and timeline identity. |
| 13. Concall intelligence | Speaker/role, dated transcript references, precise passage locators, question/response structure when available. |
| 14. Ownership and flows | Historical holders/categories, shares and denominators, transaction evidence, pledges, and corporate actions. |
| 15. Market/price history | Instrument/listing identities, OHLCV, sessions/timezones, raw/adjusted basis, actions, and benchmark links. |
| 16. Technical/market analysis | Sufficient daily history, ordered sessions, volume semantics, and adjustment provenance; indicators come later. |
| 17. Fundamental/market divergence | Earnings release availability and price observations that can later be aligned without future information. |
| 18. Peer benchmarking | Definitions, scope/unit comparability, membership history, operational KPIs, and management evidence. |
| 19. Valuation | Capital structure, share classes/counts, debt/cash, prices and dates, segment data, sector applicability, and definition consistency. |
| 20. Expectations analysis | Point-in-time valuation inputs and explicit, separately versioned assumptions; reverse calculations come later. |
| 21. Catalyst engine | Event/milestone identities, announced/expected/actual dates, affected subjects, and supporting evidence. |
| 22. Risk engine | Evidence across business, financial, accounting, governance, industry, macro, regulatory, valuation, and market domains. |
| 23. Scenario engine | Auditable base observations and a future separate assumption set; bear/base/bull computations are deferred. |
| 24. Variant perception | Valid sourced consensus/expectations snapshots when available, source coverage, and conflicting evidence. |
| 25. Thesis challenger | Frozen research inputs, contradiction-preserving evidence, source alternatives, and traceable claims. |
| 26. Thesis monitoring | Stable KPI/event IDs, evidence revisions, expected milestones, and future versioned thesis conditions. |
| 27. Final research output | One versioned machine-readable research state with evidence, calculations, claims, assumptions, and input-manifest references. |

The eventual output names supersede the earlier product-target names:

```text
COMPANY_INSTITUTIONAL_RESEARCH.txt
COMPANY_INVESTOR_BRIEF.txt
COMPANY_RESEARCH.json
```

Both text outputs should be rendered from the same research state. That future
state must carry investment stance and conviction, business quality, growth,
earnings momentum, balance sheet, management, industry, valuation, technical/market
trend, catalysts, the separate risk categories, bear/base/bull cases, thesis
breakers, and monitoring conditions. It must retain contradictory evidence,
limitations, source availability, and assumptions. A 0-100 score cannot replace
these components or their reasoning. No report schema implementation or report
generator is added in Part 2 merely to reserve these concepts.

### Part 2 acceptance criteria for the subsequent implementation proposal

1. Enumerate the first source/dataset slice and its documented coverage; do not
   implicitly commit Part 2 to acquiring every future data family.
2. Show example contracts for a company annual/quarterly series and a non-company
   series, including unavailable history and explicit dimensions.
3. Demonstrate that duplicate acquisition is idempotent while revisions,
   conflicting sources, and repeated retrieval events remain auditable.
4. Test original/as-of/latest selection with restatements and unknown publication
   dates; do not claim historical availability that the evidence cannot establish.
5. Test exact decimals, scale conversion, reporting scope, unit mismatches,
   annual/quarter/YTD separation, missing intervals, and partial datasets.
6. Verify every normalized record resolves to preserved raw evidence and an exact
   source location, including parser/configuration versions and validation errors.
7. Test acquisition failure and replay from retained raw evidence without silently
   replacing previously valid data or marking partial coverage complete.
8. Preserve `ResearchEngine` dependency boundaries and thin HTTP/MCP interfaces.
   Do not add analytical modules, unsupported consensus claims, or new infrastructure
   to satisfy this long-term target prematurely.


## Implemented Part 2 vertical slice

The historical design review above remains the long-term context. The implemented slice is
`SourceAcquirer → RawArtifactStore → SourceNormalizer → ExtractionRepository → JSON export`,
coordinated by `ResearchEngine.extract_company`. These four ports are provider-neutral;
Screener-specific acquisition, labels, HTML and mappings stay in `adapters/screener`.
FastAPI and MCP retain the four Part 1 endpoints/tools. The CLI wires extraction in bootstrap.

Persistence uses exactly six tables: `companies`, `listings`, `company_source_keys`,
`source_retrievals`, `raw_artifacts`, `extractions`. Explicit `init-db` creates/backfills the
new structures without replacing company UUIDs. A unique provider/key binding resolves
repeated acquisition; names are never used to merge companies. Snapshots contain financials,
ownership, peers and document references as validated schema 2.0 JSON, not dedicated tables.
Raw blobs are content-addressed on disk, and every normalization reads verified preserved bytes.

The implementation retains unknown period boundaries, source-only metric mappings, immutable
snapshot IDs, separate stable series keys, and section coverage independent of record totals.
The YESBANK golden harness, INFY capture, controlled layout mutations and CLI acceptance test
are all offline. See README for executable replay and separate manual smoke-test commands.

## Implemented Part 3C evidence slice

Publication and macro ingestion use separate provider-neutral Protocols and shared raw evidence
storage. Bootstrap selects the narrow issuer/RBI publication adapters and official MoSPI CPI
adapter. `ResearchEngine` has no source-specific parsing, URL rules or metadata mappings.
Acquisition responses are preserved before normalization and are verified on readback.

`PublicationIdentity` identifies source evidence independently of company/sector/geography
associations. Immutable `PublicationSnapshot` carries title/date evidence, literal passages,
attachment locators, coverage and declared subject linkage. No event or interpretation is
inferred. The existing PDF contract gains optional subjects without changing existing company
entry points. Literal publication search returns the original snapshot and passage provenance.

`MacroSeriesIdentity` includes geography, population, frequency, unit/scale, base year and
seasonal-adjustment semantics. Metadata mapping provenance and observation provenance point
to distinct preserved artifacts. A snapshot contains the series plus Decimal observations,
reference periods, source status, available release/vintage timestamps and retrieval time.
Each new retrieval appends a snapshot; deterministic period lookup returns all captured
vintages. A retrieval today proves system knowledge today, not historical availability.
Unknown metadata cannot silently become a complete observation or inferred series identity.

Four new tables (`publications`, `publication_snapshots`, `macro_series`, `macro_snapshots`)
reuse `source_retrievals` and `raw_artifacts`; no subject/observation tables are added. Company
news, RBI banking evidence and CPI use separate CLI requests, exports and offline fixtures.
Live proof captures remain local and Git-ignored; redistribution permission is unresolved.
See README for exact commands, verified source limitations and coverage semantics.

## Part 4 first calculation slice

`CalculationServices` injects the existing extraction reader and raw-artifact store plus a
calculation repository Protocol. `ResearchEngine.calculate_financials` reads the pinned extraction,
verifies original artifacts and invokes the pure application calculation module. Bootstrap and
CLI use that same path. Formulas contain no provider, SQLAlchemy, HTTP or AI dependencies.

`CalculationSnapshot` owns immutable versioned results and a digest of its source snapshot.
Each result retains actual input observations, exact rational/Decimal arithmetic, formula/version,
sector declaration and the caller-declared alignment policy/version. Units/scales are explicit;
no currency conversion or fiscal dates are inferred. The two-point average-assets convention
explicitly uses prior/current fiscal-year-end assets and current annual profit. It is not labelled
ROA. Snapshot storage adds one table with an extraction foreign key; no per-metric tables.
