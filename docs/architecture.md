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
