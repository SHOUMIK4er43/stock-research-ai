# Stock Research AI

A local-first foundation for an institutional-grade equity research system serving
research heads, portfolio managers, professional analysts, family offices, and
sophisticated investors. The intended product combines auditable evidence,
historical analysis, sector context, valuation, market behavior, and explicit
investment reasoning; a single score will not replace the underlying research.

Implemented today: Part 1 exposes the company registry through FastAPI and MCP.
Part 2 acquires or replays public Screener HTML through the provider-independent
`ResearchEngine`, preserves exact evidence, normalizes it into versioned SQLite
snapshots, and exports JSON with explicit coverage. Part 3A adds preserved PDF documents,
page text, deterministic chunks, version history, and literal evidence search.
No analysis or LLM calls run.

Part 2 is acquisition and normalization only. The longer-term data architecture
and its institutional research requirements are documented in
[the Part 2 design review](docs/architecture.md#part-2-design-review-institutional-research-data).

## Existing environment

Use Python 3.12 and the existing uv-managed `.venv`. The supported runtime is pinned to
CPython **3.12.14** (the current patched 3.12 release) in `.python-version`; uv installs it
on demand (`uv python install`), CI uses the same file, and `scripts/install-macos.sh`
installs the application with `uv tool install --python 3.12.14`. SQLAlchemy's `asyncio` extra
includes its required `greenlet` runtime dependency, including on Apple Silicon.
The installed MCP SDK is **2.1.1**, using `from mcp.server import MCPServer`.
The console entry point remains `stock-research-ai = "stock_research_ai:main"`.

The commands below deliberately disable environment synchronization, lockfile
updates, and Python bytecode writes:

```sh
cd /path/to/stock-research-ai
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai --help
```

## Configuration and initialization

Settings come from environment variables or an optional `.env` in the working
directory. `.env.example` documents the defaults:

| Setting | Default |
| --- | --- |
| `STOCK_RESEARCH_AI_DATABASE_PATH` | `~/.stock-research-ai/research.sqlite3` |
| `STOCK_RESEARCH_AI_HTTP_HOST` | `127.0.0.1` |
| `STOCK_RESEARCH_AI_HTTP_PORT` | `8000` |

The default database path is independent of the launch directory. Custom relative
paths are resolved against the working directory when settings are loaded.
`--database PATH` overrides the database setting and precedes the subcommand.

Initialize the company table explicitly before starting either interface:

```sh
uv run stock-research-ai init-db
```

Initialization can be repeated without deleting companies. Importing modules,
constructing an application, or displaying CLI help creates no database. Server
startup requires an existing, initialized registry. An incompatible existing table
is rejected; there is no migration system.

For a separate demonstration database, consistently pass its path:

```sh
uv run stock-research-ai --database /tmp/stock-research-ai-demo.sqlite3 init-db
uv run stock-research-ai --database /tmp/stock-research-ai-demo.sqlite3 serve-http
```

## Launch and verify FastAPI

In the first terminal:

```sh
cd /path/to/stock-research-ai
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai init-db
uv run stock-research-ai serve-http
```

In a second terminal:

```sh
curl --fail-with-body http://127.0.0.1:8000/health
curl --fail-with-body http://127.0.0.1:8000/v1/capabilities
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/companies \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example Company","listings":[{"exchange":"NSE","symbol":"EXAMPLE"}],"provider_identifiers":[{"provider":"example","identifier":"example-company"}]}'
```

The POST returns HTTP 201 and a company object containing an `id`. Use that UUID:

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/companies/PASTE_RETURNED_UUID_HERE
```

Only these four HTTP routes are exposed; automatic documentation routes are
disabled. Missing UUIDs return 404; malformed IDs and invalid input return 422.
The service defaults to loopback. Authentication and browser integration are
outside Part 1; use it as a local development service.

## Launch and verify MCP

The MCP interface uses **stdio**. Start it with:

```sh
cd /path/to/stock-research-ai
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai init-db
uv run stock-research-ai serve-mcp
```

The server waits for an MCP client on stdin/stdout. Application logging goes to
stderr. Stop this terminal process with Ctrl-C before using the verifier below,
which launches and closes its own server subprocess. No Inspector or Node package
is needed.

```sh
uv run python -B - <<'PY'
import asyncio
import sys

from mcp import Client, StdioServerParameters

from stock_research_ai.config import Settings


async def verify():
    server = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "stock_research_ai", "serve-mcp"],
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "STOCK_RESEARCH_AI_DATABASE_PATH": str(Settings().database_path),
        },
    )
    async with Client(server) as client:
        print("Tools:", [tool.name for tool in (await client.list_tools()).tools])
        for name in ("health_check", "get_capabilities"):
            print(name, (await client.call_tool(name, {})).model_dump_json(indent=2))
        registered = await client.call_tool("register_company", {"name": "MCP Example"})
        assert not registered.is_error, registered
        assert registered.structured_content is not None
        company_id = registered.structured_content["id"]
        fetched = await client.call_tool("get_company", {"company_id": company_id})
        assert not fetched.is_error, fetched
        assert fetched.structured_content == registered.structured_content
        print("Company round trip:", fetched.model_dump_json(indent=2))


asyncio.run(verify())
PY
```

The tool list is exactly `health_check`, `get_capabilities`, `register_company`, and
`get_company`. `register_company` accepts `name`, optional `listings`, and optional
`provider_identifiers`. `get_company` accepts `company_id`. Validation and missing
companies are reported as MCP tool errors.

## Part 1 behavior and data contracts

Each registration creates a new internal UUID. Names, exchange listings, and
provider identifiers are supplied by the caller and are not verified externally.
There is no deduplication, update, delete, search, or symbol-resolution subsystem.
Provider identifiers never become exchange symbols automatically. Reporting scope
is a financial-observation property, separate from company identity.

Financial domain contracts distinguish RAW artifacts, NORMALIZED observations, and
DERIVED metrics. They include exact decimals, fiscal labels and actual dates,
units/currency, reporting scope, missing values, original representations, and
immutable provenance references. No financial records are persisted and no
calculations are executed. Capabilities explicitly report `analysis_available: false`.

See [the architecture document](docs/architecture.md) for dependency boundaries and
the future provenance chain.

## Verification

Run from the repository root:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Tests use temporary databases, fake repositories, HTTP requests, and the installed
MCP client's in-process and stdio transports. They require no network services,
external accounts, or LLM access. Test and tool caches are ignored by Git.

## Deferred work

Financial calculations, sector analysis, document intelligence, guidance history,
LLM synthesis, research report generation, and the Chrome extension remain future phases. Authentication, migrations,
background processing, and additional infrastructure are also deferred.


## Part 2: evidence pipeline proof

No dependencies were changed for Part 2. `init-db` explicitly adds five tables to an
existing Part 1 database: `listings`, `company_source_keys`, `source_retrievals`,
`raw_artifacts`, and `extractions`. Existing companies and UUIDs are retained.
Explicit registered exchange/provider identifiers are backfilled; missing exchanges
are not invented. Conflicting provider bindings require correction rather than a name match.
Financial records, holdings, peers, and document references stay inside validated
schema `2.0` JSON snapshots. Extraction snapshots are append-only through the repository.

Run the offline fixture replay (choose a fresh output directory on subsequent runs):

```sh
cd /path/to/stock-research-ai
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai --database .local/part2-proof.sqlite3 init-db
uv run stock-research-ai --database .local/part2-proof.sqlite3 extract-screener \
  https://www.screener.in/company/YESBANK/consolidated/ \
  --html tests/fixtures/screener/yesbank_consolidated.html \
  --output output/yesbank-fixture
```

Exit codes: `0` complete, `3` partial, `1` failed, `2` argument usage error.
The captured YESBANK fixture returns **3** because peers expose a loading placeholder.
All ten expected sections have coverage; a section cannot succeed with zero records.
`UNMAPPED_METRIC` is a warning: its original label, value and safely parsed number survive.
Malformed cells/tables degrade coverage. Record totals alone do not determine success.

Output layout:

```text
output/yesbank-fixture/
├── raw/
│   ├── manifest.json
│   └── <sha256>.html
├── normalized/
│   ├── extraction.json
│   ├── company.json
│   ├── quarterly.json
│   ├── profit_loss.json
│   ├── balance_sheet.json
│   ├── cashflow.json
│   ├── ratios.json
│   ├── shareholding.json
│   ├── peers.json
│   └── documents.json
└── coverage.json
```

`extraction.json` is the canonical validated snapshot. Section files are views of that
same snapshot, each with its extraction ID and coverage. Decimals serialize as strings.
`company.json` includes identity, listings and top metrics. The raw manifest contains
retrieval events, hashes, storage references and portable export paths. HTML response
bytes are HTTP-decoded body bytes, preserved before character decoding or normalization;
they are not compressed wire bytes. Replay preserves the exact supplied file bytes.
Use `--final-url` with `--html` when the original capture redirected, so relative links
resolve against its final acquired URL. External document URLs remain unchanged.

The golden test checks hand-specified values in `tests/fixtures/screener/expected_yesbank.json`
through engine, storage, normalization, snapshot persistence and export. Fixtures are
hashed in `manifest.json`; pytest cannot make live HTTP requests. Tests include an
INFY standalone capture, mutations, raw integrity, historical snapshots and an actual
CLI subprocess with network connections disabled.

### Manual live YESBANK smoke test (outside pytest)

Run only after the offline checks pass. This verifies current compatibility, not
historical fixture financial equality. It makes one anonymous acquisition, following
only validated public company-page redirects; no document bodies are downloaded.

```sh
cd /path/to/stock-research-ai
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai --database .local/part2-proof.sqlite3 init-db
uv run stock-research-ai --database .local/part2-proof.sqlite3 extract-screener \
  https://www.screener.in/company/YESBANK/consolidated/ \
  --output output/yesbank-live
```

Inspect `normalized/company.json`: name `Yes Bank Ltd`, provider key `3783`, consolidated
scope, and explicitly displayed NSE/BSE identifiers. Inspect `coverage.json` for all ten
sections and read every issue. Verify the raw hash with `shasum -a 256 output/yesbank-live/raw/*.html`
and compare it to `raw/manifest.json`. Confirm `normalized/extraction.json` references that
artifact and includes statements, holdings, and document references. Peers can be
`UNAVAILABLE/NOT_ATTEMPTED` with a loading/gating reason; that yields exit 3 and is acceptable
for this smoke test. Other missing or failed sections require inspection, even if record
counts are large. Existing nonempty output directories are rejected before acquisition.

Limits: static public HTML only, no lazy endpoint guessing, document bodies, browser,
PDF parser or analysis. Month-only ownership dates and duration starts stay unknown;
TTM is unanchored when the source gives no dates. Explicit `data-date-key` dates are
retained alongside their original labels. Retrieval time is never publication time.
Unknown metric labels are retained without invented mappings. Each retrieval/snapshot
gets a new ID; observation IDs bind to that exact artifact event and normalizer version,
while series keys remain stable across revisions. This does not assert official restatements.
Content blobs are deduplicated by SHA-256. SQLite row metadata and filesystem bytes are
not one atomic transaction; interrupted runs can leave preserved evidence without an
extraction, and a failed export can leave a partial output folder to inspect.


## Part 3A: company-document evidence

The only added dependency is `pypdf` (locked at 6.18.0). No other package versions were
upgraded. `init-db` adds `documents` and `document_extractions` and preserves the existing
six tables and data. Document snapshots reuse `source_retrievals` and `raw_artifacts`;
pages, sections, chunks, metadata and quality reports remain inside canonical JSON snapshots.

Use an existing registered company's UUID and its database:

```sh
export UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1
uv run stock-research-ai --database .local/part3a-proof.sqlite3 init-db
# Replace COMPANY_UUID with the ID returned by company registration or extraction.
uv run stock-research-ai --database .local/part3a-proof.sqlite3 ingest-document \
  --company COMPANY_UUID \
  --url 'https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf' \
  --type investor-presentation \
  --file tests/fixtures/documents/yesbank_presentation.pdf \
  --output output/document-fixture
uv run stock-research-ai --database .local/part3a-proof.sqlite3 search-documents \
  --company COMPANY_UUID --query 'Financial Highlights'
```

`--file` is offline replay; omit it for a separate manual live smoke test after pytest
passes, using a new output directory. `--final-url` records a replay capture's final URL.
Alternatively, `--reference-json` accepts a saved Part 2 `DocumentReference` object and
preserves the source page's link locator and discovery provenance. If `--url` is also
supplied it must match that reference. Explicit type/title hints are labelled as caller
or reference declarations; URL words never classify documents. Unknown types remain OTHER.
The classifier does not claim semantic confirmation of caller declarations.

```text
output/document-fixture/
├── raw/
│   ├── manifest.json
│   └── <sha256>.pdf             # .bin for non-PDF/incomplete responses as applicable
├── normalized/
│   ├── document.json           # canonical immutable parsed-document snapshot
│   ├── pages.json
│   ├── sections.json
│   └── chunks.json
└── coverage.json
```

The raw store preserves HTTP-decoded file bytes before pypdf opens them. All pypdf text
is derived evidence. A page records its raw artifact ID, physical page number, text hash,
parser version and status. Pages are **one-indexed**, including exchange-cover pages;
printed page labels can differ. Chunks contain exact character slices of one page,
section/page/version/artifact IDs, ordinal, character count, word count and text hash.
The validator rejects broken lineage, altered slices, gaps, overlaps and inconsistent counts.
No table reconstruction is performed or claimed; pypdf's numeric/table-like text is retained.

A document identity is bound to company, provider and an explicit source key or original URL.
A new retrieval always produces an immutable version, even for identical bytes; identical
content reuses the content-addressed blob. Page/chunk IDs are deterministic for that artifact,
parser version and offsets. Historical versions remain searchable and retrievable. Search is
case-insensitive literal substring matching within chunks, returning newest versions first;
it does not match phrases split across chunk boundaries or perform semantic ranking.

Quality distinguishes acquisition and parser status, original reported PDF page count,
retained page count, extracted/empty/failed/low-text pages, suspected scanned pages, character
count, metadata availability and section detection. COMPLETE means the configured text
extraction checks passed, not perfect reading order, semantic completeness or verified tables.
Basic section labels use explicit line headings; UNKNOWN sections preserve all their text.
Metadata creation/modification timestamps are retained verbatim and never treated as publication
dates. Publication/fiscal labels are accepted only as explicit caller/reference evidence.

Acquisition accepts public HTTPS URLs, checks public DNS addresses on every redirect, uses a
45-second deadline, and limits response bodies to 25 MB. Oversized prefixes are preserved with
`complete: false`, never parsed as original complete files. Parsing caps page count at 1,500 and
checks decoded page streams against 25 MB; pypdf may allocate memory during decompression before
that check. This is a local evidence tool, not a sandbox for hostile PDFs or a public fetch proxy.
Encrypted PDFs and unsupported formats fail explicitly. Empty or very sparse image-resource
pages are flagged OCR_REQUIRED; sparse text-only pages are LOW_TEXT. This is a heuristic, not
an exhaustive scan detector. No OCR, image decoding, table library, embeddings or AI analysis runs.

Golden proof uses a public 62-page YESBANK presentation/filing and a 16-page transcript/filing.
Business Standard's public filing mirrors were accessible; original BSE viewers/presentation
attachments returned HTTP 406 and issuer/NSE requests timed out during fixture preparation.
Actual mirror URLs and original hashes are preserved. Tests cover selected visually verified
pages, raw-byte readback, page/chunk lineage, persistence, search, revisions, failures and the real
CLI. Golden expectations are hand-specified and protected by AGENTS.md; pytest stays offline.

## Part 3B: market evidence (Kite, personal/local)

Kite supplies acquisition; NSE identifies the source market. The engine uses neutral
market ports. The adapter resolves a current instrument master by exchange, symbol,
and segment; index identities do not require company registration. `instrument_token`
and `exchange_token` remain separate. Historical symbol reuse/renaming is not resolved
by this prototype. Current mappings do not establish historical listing validity.

Market ingestion preserves exact response-body/replay bytes before normalization,
including the instrument master. Each observation references its source artifact and
JSON row; master identity references its CSV data-row number (one-indexed, excluding
header). Immutable `market_extractions` JSON snapshots and `market_instruments` are
the only new tables; retrieval/artifact tables are reused. Run `init-db` explicitly
against existing databases to add these tables. Old snapshots remain available through
`ResearchEngine.get_market` and `market_history`.

Daily timestamps retain the supplied offset and separate trading date. Provider update
time and currency stay null when not supplied. Index levels use INDEX_POINTS; index
volume is preserved as reported but its meaning is UNKNOWN. Zero volume never implies
suspension, a holiday, or absence. Missing-date reasons stay UNKNOWN. Optional expected
sessions are explicitly CALLER_DECLARED, not a generated exchange calendar. COMPLETE
means all declared price sessions validated, not complete corporate-action/adjustment
coverage. Without session evidence, nonempty valid output remains PARTIAL. All-empty,
malformed, blocked or failed output is never empty success.

Kite says historical prices/volumes can be adjusted, but the candle schema lacks a
per-record adjustment indicator and adjustments may be delayed. `adjustment_basis`
remains UNKNOWN; `adjusted_close` stays null. No factors, actions or analytics are
computed. Corporate-action coverage is UNAVAILABLE / NOT_PROVIDED_BY_SOURCE. Historical
values retrieved now prove only knowledge at retrieval time, never availability on the
historical trading date. Replay records a new retrieval event; identical bytes reuse
content-addressed storage and deterministic observation IDs, while every ingestion
creates a new snapshot and retains its own provenance.

Offline verification (all values and provider tokens below are synthetic):

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database .local/market-proof.sqlite3 init-db
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database .local/market-proof.sqlite3 ingest-market-data \
  --symbol YESBANK --exchange NSE --kind EQUITY \
  --start 2025-01-02 --end 2025-01-03 \
  --master-file tests/fixtures/market/synthetic_instruments.csv \
  --candles-file tests/fixtures/market/synthetic_candles.json --synthetic \
  --expected-sessions 2025-01-02,2025-01-03 --output output/market-synthetic-yesbank
```

Substitute `INFY` with EQUITY, or `"NIFTY 50"` / `"NIFTY BANK"` with INDEX and a fresh
output directory. Exit codes: 0 COMPLETE, 3 PARTIAL, 1 FAILED, 2 invalid CLI input.
Exports contain raw/manifest.json and byte files, normalized/extraction.json,
instrument.json, prices.json, corporate_actions.json, and coverage.json.

For manual live verification, set `KITE_API_KEY` and `KITE_ACCESS_TOKEN` using your
local environment or Git-ignored `.env`. The adapter prefers environment values over
`.env`. Secrets are neither domain fields nor exported request headers. Use the normal
Kite login outside the engine; tokens expire at 6 AM the following day and invalid
tokens are reported explicitly. No automatic login, trading endpoints, or SDK is used.
REST requests are sequential, at most ~2.8/second per adapter instance; coordinate
separate processes yourself to respect the account-level 3/second limit. Daily windows
are split into spans below 2,000 days. Redirects are rejected; each response has a
40-second total timeout and 20 MB limit. Oversize evidence is a marked incomplete
prefix and never normalized.

Only after offline checks pass, run the separate four-instrument smoke procedure:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_kite_market.py --output output/market-live-proof
```

This validates the master resolution, January 2–3 2025 candle presence, raw hashes,
lineage and persistence. It does **not** call its own output golden truth. To verify
actual numerical values, independently inspect the source evidence, hand-specify a
separate local JSON file with symbols mapped to arrays of `date`, `open`, `high`,
`low`, `close`, `volume` (numeric values as strings), and supply `--expected PATH` with
a fresh output directory. The separate authenticated check has passed for all four instruments over January
2–3, 2025, against expectations hand-transcribed from original Kite response bodies.
This proves normalization fidelity to Kite, not independent exchange accuracy.
Local expectations are in `.local/kite-market-expected.json`; evidence and the
second-run proof are in `output/market-live-golden-proof/`, all Git-ignored.
Automated tests never invoke this live script.

Kite's general terms restrict permanent copies unless permitted, while instrument
API documentation specifically recommends local master caching. Historical-response
fixture retention/redistribution permission is not established. Tracked test payloads
are wholly synthetic; no real historical payload is a permanent fixture. Personal
live exports remain Git-ignored. Neither an active subscription nor this implementation
grants commercial/redistribution rights. Future licensed adapters can replace Kite.

References: [REST candles](https://kite.trade/docs/connect/v3/historical/),
[instruments](https://kite.trade/docs/connect/v3/market-quotes/),
[authentication](https://kite.trade/docs/connect/v3/user/),
[limits](https://kite.trade/docs/connect/v3/exceptions/),
[terms](https://kite.trade/terms/),
[adjustment policy and corporate actions](https://kite.trade/forum/discussion/15971/getting-the-right-closing-prices-adjusted-for-corporate-actions).

## Part 3C: publication and macro evidence

Part 3C adds provider-neutral publication identities/snapshots, evidence subjects, and macro
series/observations/snapshots. `ResearchEngine` preserves acquired bytes, records retrievals,
reads the stored bytes back, invokes a parser Protocol, and persists a new snapshot. Four
additional tables hold identities and canonical JSON snapshots: `publications`,
`publication_snapshots`, `macro_series`, `macro_snapshots`. Existing retrieval/artifact tables
are reused. `init-db` adds these tables without a migration framework.

Publication subjects support company, sector, industry, geography and macro series, with an
explicit caller declaration or source/mapping provenance. No company record is required for
RBI material. The Part 3A PDF contract has an optional subject extension; its existing company
CLI remains compatible. Publications are not inferred events. Literal publication search and
series/period lookup are available through `ResearchEngine`; lookup returns all captured
vintages. Repeated retrievals append snapshots; changed values never replace earlier values.
Base year and seasonal-adjustment semantics distinguish macro series identities.

From the repository root, replay synthetic fixtures without network access (use new output
directories for each run):

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/context-offline.sqlite3 init-db
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/context-offline.sqlite3 ingest-publication --request tests/fixtures/context/publication_request.json --output output/context-publication
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/context-offline.sqlite3 ingest-macro --request tests/fixtures/context/macro_request.json --output output/context-macro
```

Exit status is 0 for COMPLETE, 3 for PARTIAL, 1 for FAILED and 2 for invalid input. The synthetic
macro example intentionally returns 3: unsupported metadata remains unknown. Each export has
`raw/manifest.json`, exact raw bytes, `normalized/snapshot.json`, component JSON files and
`coverage.json`. Decimal values serialize as strings. Observations reference their series for
unit, scale, geography, frequency and base-year semantics; each retains its own period, raw
status, release/vintage timestamps where supplied, retrieval timestamp and raw locator.

The separate manual live proof uses locally retained request JSON and independently
hand-specified expectations. It runs all three real CLI paths, checks expected values/passages,
checks raw SHA and provenance, replays the exact bytes and checks two immutable history entries:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_context.py --company-request .local/part3c-proof/company.json --industry-request .local/part3c-proof/industry.json --macro-request .local/part3c-proof/macro.json --expected .local/part3c-proof/expected.json --output output/part3c-live-proof-next
```

These local request/expectation files are not distributed. Copy the request shapes above for
another installation; omit replay paths for live access. The live proof expectations must be
checked against original evidence independently of the parser. Never replace expectations to
make a mismatch pass. `scripts/verify_context.py --help` lists the required inputs.

Verified sources and limits:

- YES BANK's 19 July 2025 issuer filing: issuer URLs timed out, so acquisition used a
  [Business Standard filing mirror](https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf).
  Publisher remains YES BANK; acquisition provider is separately recorded. PDF text and
  physical-page locators use existing pypdf; no OCR or table reconstruction is added.
- RBI's [Sectoral Deployment of Bank Credit — June 2024](https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=58400)
  is parsed by a narrow standard-library reader for the release table. Linked attachments
  retain original/resolved URLs and locators, and are explicitly `LINKED_NOT_ACQUIRED`.
- MoSPI's [official structured-data bridge](https://datainnovation.mospi.gov.in/mospi-mcp)
  supplies CPI metadata and values through JSON-RPC POST using httpx, without an LLM.
  The direct REST endpoint failed TLS negotiation; TLS was not weakened. The adapter checks
  metadata codes for All India / Combined / General-Overall and preserves both response
  artifacts plus request parameters. The tested 2012-base January/February 2025 values are
  193.4 / 192.5 index points. Seasonal adjustment, release/vintage times and the meaning of
  opaque source status `F` remain unknown, so coverage is PARTIAL. No inflation is calculated.
  This initial adapter reads one bounded page; pagination or missing/ambiguous data is explicit
  incomplete coverage. It is not a general MoSPI dataset adapter.

Redistribution rights for retained source payloads remain unclear. New tracked fixtures are
synthetic, with hand-specified expectations. Live payloads, databases and proof output remain
Git-ignored under `.local/` and `output/`; no rights to redistribute or commercialize them are
asserted. No new dependencies, generic news scraper, embeddings, event analysis, sentiment,
recommendations or financial calculations were added. All pytest tests block network access
and use temporary databases and artifact storage.

## Part 4 first slice: deterministic financial calculations

`calculate-financials` reads one explicitly selected Part 2 extraction, verifies its retained raw
artifacts, calculates through `ResearchEngine`, and appends an immutable `calculation_snapshots`
row. It does not fetch newer evidence or silently switch to the latest extraction. Run `init-db`
on an existing database to add this table. Calculation JSON and per-result coverage are exported.

Three version-1 formulas are implemented: source-metric growth, operating margin (non-financial
companies only), and `AVERAGE_ASSETS_RETURN`. YESBANK's `Revenue` retains the source-reported
meaning; it is not renamed total income or NII. Average-assets return is annual net profit divided
by the average of prior/current fiscal-year-end total assets, multiplied by 100. It is not ROA.
No market calculations, valuation, peer analytics, technical indicators or AI are included.

A request explicitly supplies the company/extraction UUIDs, reporting scope, sector declaration,
fiscal alignment policy and selected calculations. Example `request.json` (replace both UUIDs
with values from the retained extraction):

```json
{
  "company_id": "00000000-0000-0000-0000-000000000001",
  "extraction_id": "00000000-0000-0000-0000-000000000002",
  "reporting_scope": "consolidated",
  "sector": {
    "classification": "BANK",
    "basis": "CALLER_DECLARED",
    "declaration": "Caller classifies this YESBANK proof as BANK."
  },
  "alignment": {
    "fiscal_year_end_month": 3,
    "declaration": "Caller declares March year end and three-month quarters for this proof."
  },
  "calculations": [
    {
      "metric_id": "SOURCE_NET_PROFIT_QUARTERLY_YOY",
      "formula_id": "SOURCE_METRIC_GROWTH",
      "source_metric": "net_profit",
      "current_period": "2025-06",
      "comparison": "QUARTERLY_YOY"
    },
    {
      "metric_id": "AVERAGE_ASSETS_RETURN",
      "formula_id": "AVERAGE_ASSETS_RETURN",
      "current_period": "2025-03",
      "comparison": "ANNUAL_YOY"
    }
  ]
}
```

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database /path/to/research.sqlite3 init-db
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database /path/to/research.sqlite3 calculate-financials --request request.json --output output/calculation-run
```

Set `STOCK_RESEARCH_AI_ARTIFACT_PATH` when artifacts are not in the database directory's
`artifacts/` subdirectory. Use a new output directory each time. Exit 0 means every requested
calculation is available; 3 means at least one explicit unavailable/invalid result; 2 is a
request or evidence-integrity error. Existing extraction and calculation snapshots are retained.

The alignment policy (`DECLARED_FISCAL_PERIOD_KEYS`, version 1) matches source year/month keys,
frequency and scope, never array positions. Annual comparisons use the same fiscal end one year
apart; quarterly YoY uses twelve months, sequential uses three months. Known boundaries must
agree with the policy. Unknown starts remain unknown and appear as a limitation. TTM, incompatible
currencies/scopes, duplicate inputs and ambiguous metrics are not silently accepted. Unknown or
other-financial sector conventions are currently unsupported. Caller declarations are recorded,
not presented as verified source classifications.

Each result records CALCULATION, snapshot/extraction IDs, formula and alignment versions, full
input observations with their original IDs/provenance, classification provenance, issues and
calculation time. No raw locator is fabricated for the calculation. Exact rational arithmetic
uses standard-library `Fraction` on Decimal inputs (no floats), then records the pre-display
Decimal at 50 significant digits, HALF_EVEN. Two-decimal display rounding is separate. For
nonterminating values, the exact numerator/denominator preserves precision beyond finite Decimal
representation. Negative-base growth uses the explicit absolute-denominator convention and a
limitation; zero denominators never produce a value.

Golden input and output files are separate under `tests/fixtures/calculations/`. The YESBANK
quarterly YoY input for Jun-2024 was checked explicitly: **516**; Jun-2025 is **809**. Ten golden
calculations cover YESBANK and INFY. Revised fixture bytes create a new extraction and calculation;
old results remain reproducible. Full CLI tests block network access and use temporary storage.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_calculations.py
```

## Part 4B: deterministic market calculations

The six supported metrics are `PRICE_RETURN`, `PRICE_CAGR`, `MAX_DRAWDOWN`,
`REALIZED_VOLATILITY`, `SMA`, and `BENCHMARK_RELATIVE_RETURN`. These are calculations on
retained source closes. They are not total, dividend-adjusted or shareholder returns.

Mathematically valid outputs have `status: AVAILABLE`. Source adjustment uncertainty is a
structured issue (`UNVERIFIED_ADJUSTMENT`, severity `LIMITATION`), alongside
`adjustment_basis: UNKNOWN` and corporate-action coverage `UNAVAILABLE / NOT_PROVIDED_BY_SOURCE`.
Zero, negative or non-finite required closes cannot produce a result; offending IDs are retained
in error issues. Known invalid ordering, duplicates and identity/alignment conflicts are explicit
failures rather than silently repaired inputs.

Requests pin market extraction and instrument IDs. A benchmark requires both IDs explicitly;
company names do not select benchmarks. The engine loads those snapshots, verifies raw artifacts,
calculates, and appends a `market_calculation_snapshots` row. Primary/benchmark foreign keys refer
to existing market extractions. Calculation output references retained evidence; it does not copy
raw source payloads. Existing financial calculation storage and source goldens remain intact.

Example request shape (replace UUIDs and dates with your retained evidence):

```json
{
  "extraction_id": "00000000-0000-0000-0000-000000000001",
  "instrument_id": "00000000-0000-0000-0000-000000000002",
  "start_date": "2025-01-02",
  "end_date": "2025-01-29",
  "calculations": [
    {"metric_id": "PRICE_RETURN"},
    {"metric_id": "MAX_DRAWDOWN"},
    {"metric_id": "REALIZED_VOLATILITY"},
    {"metric_id": "SMA", "window": 20}
  ]
}
```

Add `benchmark_extraction_id` and `benchmark_instrument_id` for a
`BENCHMARK_RELATIVE_RETURN` request. Optional `start_observation_id` and `end_observation_id`
can pin the endpoint observations further; IDs must belong to that extraction and date.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database /path/to/research.sqlite3 init-db
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database /path/to/research.sqlite3 calculate-market --request request.json --output output/market-calculation
```

Use a new or empty output directory and set `STOCK_RESEARCH_AI_ARTIFACT_PATH` if artifacts
are outside the database's default `artifacts/` directory. Exports:
`normalized/calculation_snapshot.json`, `normalized/results.json`, `coverage.json`.
Exit 0 means all calculations are numerically AVAILABLE, including limited ones; inspect issues.
Exit 3 indicates calculation failures, and 2 indicates request/source-loading/integrity errors.

Versioned conventions:

- `EXACT_DATES_ONLY_V1` requires both requested boundary dates; no nearest-date shifting.
  Requested and actual selected dates are retained. SMA selects the last N observations inside
  those exact boundaries; its actual start may therefore be later than the requested start.
- `EXACT_SHARED_ENDPOINTS_V1` requires precisely matching company/benchmark endpoints.
  Relative return is company price return minus benchmark price return, in percentage points.
  Both component returns and all four endpoint observations are retained. It is not alpha.
- `ACTUAL_DAYS_365_FIXED_V1` uses actual elapsed calendar days / 365, then
  `(exp(ln(end/start)/elapsed_years)-1)*100`. This is our convention, not an exchange calendar.
  Elapsed days and the working-precision elapsed-years value are retained without display rounding.
- `ARITHMETIC_SAMPLE_252_V1` uses `r=close/prior_close-1`, arithmetic mean, sample variance
  `sum((r-mean)^2)/(n-1)`, and `sqrt(variance)*sqrt(252)*100`. Minimum two returns/three closes.
  Counts, factor 252, return/variance definitions and policy are recorded. No log returns.
- Drawdown is positive loss magnitude `(1-close/running_peak)*100`. Equal worst drawdowns retain
  the earliest episode. Recovery is the first later close **>=** the original peak. Unrecovered
  episodes have null recovery fields plus `NOT_RECOVERED_WITHIN_WINDOW`; a non-declining series
  has zero drawdown and no fabricated episode.
- SMA windows count observations, not calendar days. Known missing required sessions block
  window calculations. Without expected-session evidence, calculations can succeed with
  `OBSERVED_SESSIONS_ONLY`; drawdown then describes only the observed path. Partial source coverage
  is also retained as a limitation. No exchange calendar is inferred.

Shared Part 4A primitives define formula versions, calculation statuses, precision and rounding;
structured issues also live in `domain/calculations.py`. Market-specific contracts add instrument,
window, annualization and episode metadata. Rational arithmetic uses exact fractions; Decimal
ln/exp/sqrt use 70 working digits and results retain 50 digits with separate two-place HALF_EVEN
display rounding. Irrational results are explicitly marked `DECIMAL_TRANSCENDENTAL` and cannot
claim exact rational representation. No binary floating-point conversion is used.

Synthetic inputs and independent outputs are separate in `tests/fixtures/market_calculations/`.
The nine golden calculations include both benchmarks and a two-year CAGR window. Irrational
outputs use independently specified absolute tolerances of `1e-40`; rational goldens use exact
equality. No real Kite payloads were added. Automated tests remain offline with temporary storage.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_market_calculations.py tests/test_market_calculation_pipeline.py
```

A local proof run and its five reusable request files are retained in Git-ignored
`output/part4b-proof/`. For example, rerun its YESBANK calculation with:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/part4b-proof/proof.sqlite3 calculate-market --request output/part4b-proof/request-0.json --output output/part4b-rerun
```

RSI, MACD, other indicators, valuation, signals, recommendations and AI remain deferred.

## Part 4C: sector KPI eligibility and calculations

`calculate-sector-kpis` evaluates an explicitly selected, versioned profile against a pinned
extraction. `BANK_PROFILE / 1` and `IT_SERVICES_PROFILE / 1` require caller/source classification;
company names never determine eligibility. Every result retains the classification basis,
optional effective dates, profile, KPI definition, exact semantic mapping, formula and alignment
versions, actual input observations, exact fraction and 50-digit Decimal representation, separate
two-place HALF_EVEN display, and structured issues.

The first annual proof provides YESBANK deposit growth and `AVERAGE_ASSETS_RETURN` (not bank ROA).
Growth of source-reported Revenue is exported as `GENERIC_SOURCE_METRIC`, outside the bank KPI
inventory; it has no upgraded bank income meaning. INFY supports sales/net-profit/EPS growth,
operating margin and operating-margin change in basis points. The BPS calculation subtracts the
shared Part 4A unrounded margins and retains all four observations. Growth, margin and
average-assets return dispatch to the existing Part 4A formula implementation/version.

Only exact extractor metric identities, source sections, definition/version and provider bindings
establish semantic mappings. NIM, CASA, PCR, credit cost, CRAR, CET1, bank ROA/ROE, advances growth,
credit/deposit ratio, GNPA and NNPA have no calculated outputs in this slice. Source-displayed ratios
remain evidence. An exact candidate `source:NIM` without an approved mapping is
`UNVERIFIED_METRIC_SEMANTICS`; absent required evidence is `INSUFFICIENT_INPUT`. Wrong-profile KPIs
are `NOT_APPLICABLE`; UNKNOWN classification carries `UNKNOWN_SECTOR_PROFILE`. Incompatible periods,
scopes and mathematically invalid inputs retain distinct statuses. Part 4C zero denominators use
`INVALID_INPUT` with `ZERO_PRIOR_VALUE` / `ZERO_DENOMINATOR`; Part 4A's status contract is unchanged.

Run the complete reproducible offline proof in a new output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_sector_kpis.py --output output/part4c-proof-new
```

This executes the production CLI for initialization, retained HTML replay and KPI evaluation with
network connections blocked. It creates YESBANK/INFY request JSON, SQLite and raw artifacts,
exports and `golden-proof.json` comparing actual values with independently authored expectations.
It never generates or updates expectations. Source-input and expected-output fixtures are separate
in `tests/fixtures/sector_kpis/`. The completed review run is in Git-ignored `output/part4c-proof/`.

To repeat a calculation against that retained proof:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/part4c-proof/proof.sqlite3 calculate-sector-kpis --request output/part4c-proof/YESBANK-request.json --output output/part4c-yesbank-rerun
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/part4c-proof/proof.sqlite3 calculate-sector-kpis --request output/part4c-proof/INFY-request.json --output output/part4c-infy-rerun
```

Set `STOCK_RESEARCH_AI_ARTIFACT_PATH` to the retained artifact directory when using a different
location. Exit 0 means every requested KPI is AVAILABLE; exit 3 means the evaluation was exported
with explicit unavailable/not-applicable outcomes (expected for these proof requests); exit 2 is
an invalid request or evidence-loading/integrity error. Always inspect coverage and limitations.
Exports include `profile.json`, `coverage.json`, and `normalized/` containing
`calculation_snapshot.json`, `available_kpis.json`, `generic_financial.json`, `unavailable_kpis.json`
and `not_applicable_kpis.json`.

Persistence reuses `calculation_snapshots` without a table/schema migration. New rows use
`payload_type` (`financial_calculation` or `sector_kpi_evaluation`) and `payload_version: 1` around
the snapshot. Unwrapped Part 4A schema 4.1 rows still deserialize; historical rows are never rewritten.
Tests prove profile, mapping, KPI definition, classification and source revisions preserve V1 history.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_sector_kpis.py tests/test_calculations.py
```

Classification and fiscal alignment remain explicit declarations, not inferred facts. No new source
acquisition, bank ratio conventions, peer analytics, indicators, valuation, recommendations or AI
are added. See `docs/part4c-proof.md` for the numerical and negative proofs.

## Part 4D: deterministic financial patterns

`evaluate-patterns` evaluates exactly six structured-evidence patterns. Findings separate
`evaluation_status` from `rule_outcome`: only AVAILABLE findings have TRIGGERED, NOT_TRIGGERED or
OBSERVATION_ONLY. A trigger records a quantitative review condition; it makes no claim about intent,
fraud, management integrity or accounting quality. No scores, valuation, recommendations or AI.

| Pattern | Profile | V1 rule |
|---|---|---|
| CFO_TO_NET_PROFIT | IT_SERVICES | Source-reported CFO / net profit, RATIO_X; observation only |
| PROFIT_VS_CFO_DIVERGENCE | IT_SERVICES | Current profit > 0 and > prior profit, and current CFO < prior CFO or < 0 |
| REPEATED_NEGATIVE_CFO | IT_SERVICES | Exactly three consecutive comparable annual CFO values, all < 0 |
| OPERATING_MARGIN_DETERIORATION | IT_SERVICES | Existing unrounded margin change <= -50 bps |
| DEPOSIT_CONTRACTION | BANK | Existing annual deposit growth < 0% |
| DEPOSIT_GROWTH_DECELERATION | BANK | Current minus previous unrounded deposit growth, percentage points; observation only |

The margin threshold is `OPERATING_MARGIN_DETERIORATION_SYSTEM_V1`, version 1, basis
SYSTEM_DEFINED, operator LESS_THAN_OR_EQUAL, -50 BASIS_POINTS. It is an internal review heuristic,
not an industry, regulatory or institutional standard. Directional rules retain their exact
conditions; repeated-negative-CFO records N=3. Observation-only findings have no trigger threshold.

The narrow Screener CFO binding verifies `cash_operating`, `source:Cash from Operating Activity`,
CASH_FLOW provenance, provider, exact extractor definition/version and mapping status. It does not
mean free cash flow or cash earnings. Part 4A monetary-input checks now provide a shared helper;
CFO/net-profit is a new common formula. Margin and deposit prerequisites use Part 4C's unchanged
formulas. Exact deposit-growth differences use a common rational subtraction primitive. Rules do
not independently recalculate growth or margin, nor use rounded displays.

Requests pin the extraction, classification/profile, declared fiscal alignment, pattern IDs/versions
and annual evaluation endpoint. `calculation_mode` is explicit in execution output:

- `AUTO_CREATE` (default): create prerequisite snapshots through existing financial/sector production
  paths, retaining every created snapshot ID and finding reference. Reruns append new snapshots.
- `PINNED`: supply all required `pinned_calculations` entries; there is no automatic fallback.
  Each entry contains `role`, `snapshot_id`, `metric_id`. Roles are `CFO_TO_NET_PROFIT:YYYY-MM`,
  `OPERATING_MARGIN_CHANGE_BPS:YYYY-MM`, or `DEPOSIT_GROWTH:YYYY-MM`. Deceleration needs current and
  previous-year growth roles. Missing, extra, wrong-company/source/scope/period/formula references
  are rejected. AUTO_CREATE cannot include pinned entries.

Every reference records formula, extraction, optional sector profile and immutable payload digest.
The selected metric must exist exactly once in the referenced snapshot. Findings preserve source
observations, exact mappings, prerequisite references, exact fractions, 50-digit Decimal values,
separate two-place HALF_EVEN display, and issues. Source monetary observations in `observed` are
scale-normalized (e.g. INR); `inputs` retains original crore values and their scale. Missing duration
start dates remain explicit limitations under the caller-declared annual alignment convention.

Initialize the additive `pattern_snapshots` table explicitly before using an existing database:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database PATH init-db
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database PATH evaluate-patterns --request request.json --output OUTPUT
```

Set `STOCK_RESEARCH_AI_ARTIFACT_PATH` when original artifacts are outside the database's adjacent
`artifacts/` directory. Output must be new or empty. Exit 0 means all requested patterns evaluated,
including triggered findings; exit 3 means explicit unavailable/not-applicable findings were exported;
exit 2 means a request/reference/integrity error. JSON files under `normalized/` are
`pattern_snapshot.json`, `findings.json`, `triggered.json`, `not_triggered.json`, `observations.json`,
`unavailable.json` and `not_applicable.json`; `coverage.json` enumerates every requested pattern.

Run all six retained-source CLI goldens offline in a fresh output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_patterns.py --output output/part4d-proof-new
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_patterns.py tests/test_pattern_pipeline.py tests/test_calculations.py tests/test_sector_kpis.py
```

The completed local proof is in Git-ignored `output/part4d-proof/`, with independently expected and
actual results in `golden-proof.json`, six request files, original artifacts, SQLite and CLI exports.
For example:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run stock-research-ai --database output/part4d-proof/proof.sqlite3 evaluate-patterns --request output/part4d-proof/YESBANK-DEPOSIT_GROWTH_DECELERATION-request.json --output output/part4d-deceleration-rerun
```

The new table has only an extraction foreign key, evaluation time, schema version and immutable
payload. Multiple calculation references are validated before insertion; there is no singular
calculation FK or association table. Existing calculation rows are unchanged. Prerequisites and the
pattern are separate append operations: a later failure may leave valid prerequisite snapshots, but
no completed pattern row. They are not silently deleted or overwritten.

Only the six named patterns are implemented. Receivables, inventory, debt, interest pressure,
dilution/share count, GNPA/NNPA and other banking ratio trends remain deferred. Unknown profiles
produce explicit NOT_APPLICABLE findings. Existing source NPA period semantics are untouched.

## Part 4E: fixed technical indicator evidence

The technical CLI calculates EMA20, RSI14, MACD12/26 with signal9 and histogram,
true range, ATR14, Bollinger20/2, +DI14, −DI14 and ADX14 from a pinned Part 3B extraction.
It emits numerical evidence only. The explicit requested range supplies all seeds;
there is no hidden lookback. Values use 70-digit Decimal working state, 50-digit
storage and separately rounded display values.

Run the complete offline proof in a **new** output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_technicals.py --output output/part4e-proof-manual
```

This initializes an isolated proof database, replays synthetic OHLCV through the market
CLI, runs eight technical CLI cases and compares 996 checkpoints to frozen independent
expectations. It uses no Kite credentials or live data. Results include source SHA
checks, typed snapshot retrieval, full checkpoint values and the exact executed commands.

For an existing extraction, save a request JSON with `extraction_id`, `instrument_id`,
`start_date`, `end_date`, and `indicators` (for example `["EMA20", "ADX14"]`), then run:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run stock-research-ai --database /absolute/path/research.sqlite3 \
  calculate-technicals --request /absolute/path/request.json --output /absolute/path/new-export
```

Use the same `STOCK_RESEARCH_AI_ARTIFACT_PATH` as ingestion when it is customized.
Outputs are `normalized/calculation_snapshot.json`, `normalized/indicator_series.json`,
`normalized/latest.json` and `coverage.json`. Latest always identifies the requested
terminal date; warm-up points do not invalidate a series whose terminal point is available.
Exit status is 0 for all requested outputs available, 3 for unavailable outputs, and
2 for an invalid request. Unknown adjustment and unavailable corporate-action coverage
remain explicit. See [the Part 4E proof report](docs/part4e-proof.md).

## Part 5A — Valuation & Expectations Engine

The valuation CLI evaluates six deterministic, evidence-pinned methods against a
Part 2 financial extraction and, optionally, a Part 3B market extraction or Part
4A/4C calculation: `PRICE_TO_EARNINGS`, `PRICE_TO_BOOK` (observed multiples), and
four scenario/expectation methods driven by explicit caller-declared assumptions —
`FCFF_DCF`, `IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE` (a Decimal-bisection reverse
solve), `REQUIRED_EPS_CAGR_FOR_RETURN` and `REQUIRED_BVPS_CAGR_FOR_RETURN`. Every
fact used is pinned by exact observation ID and hash-verified against its source
snapshot at persistence time; every scenario assumption declares its provenance
(`SOURCE_EVIDENCE` or `CALLER_DECLARED`) and, for source-evidenced assumptions,
must match the pinned evidence exactly. `FCFF_DCF` and
`IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE` apply only to `IT_SERVICES_PROFILE`;
`REQUIRED_BVPS_CAGR_FOR_RETURN` applies only to `BANK_PROFILE`; the other three
methods apply to both. Values use 70-digit Decimal working state, 50-digit
storage and separately rounded display values, matching Parts 4A–4E.

Every result retains `POINT_IN_TIME_AVAILABILITY_UNVERIFIED`: pinning a current
financial extraction and a market observation under `as_of_policy =
CURRENT_PINNED_SNAPSHOT_V1` makes a valuation numerically reproducible, but does
not establish that the financial fact was publicly known as of the pinned market
date. Source-reported multiples (e.g. Screener's own "Stock P/E") are retained
as separate `SOURCE_REPORTED_VALUATION_EVIDENCE` facts on the snapshot and are
never substituted for — or silently accepted in place of — a calculated
multiple's own pinned denominator.

Run the complete offline proof in a **new** output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_valuation.py --output output/part5a-proof-manual
```

This initializes an isolated proof database, replays one synthetic and two real
(INFY standalone, YESBANK consolidated) Screener fixtures through the production
CLI, runs 13 valuation cases and compares every component to independently
hand-derived expectations (synthetic) or exact rational fractions (real). It
uses no network access and no Kite credentials. Real-source evidence proves only
the two observed-multiple methods, because those are the only methods whose full
evidence contract (a pinned price and a pinned annual EPS/BVPS fact) is actually
satisfied by retained public data; `PRICE_TO_BOOK` correctly refuses
`UNVERIFIED_METRIC_SEMANTICS` for both real companies because neither page
publishes a mapped book-value-**per-share** figure. DCF, reverse-DCF and the
required-CAGR methods are proved only against the independent synthetic
scenario — no WACC, terminal growth, forecast FCFF, exit multiple or required
return is invented for a real company.

For an existing extraction, save a request JSON with `company_id`,
`financial_extraction_id`, `reporting_scope`, `profile_id`, `classification`,
and one or more `selections` (each with a `method`, and where applicable a
`price`/`denominator` evidence pin and a `scenario` of caller-declared
assumptions), then run:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run stock-research-ai --database /absolute/path/research.sqlite3 \
  evaluate-valuation --request /absolute/path/request.json --output /absolute/path/new-export
```

Use the same `STOCK_RESEARCH_AI_ARTIFACT_PATH` as ingestion when it is
customized. Outputs are `normalized/valuation_snapshot.json`, `normalized/
multiples.json`, `normalized/scenarios.json`, `normalized/
reverse_expectations.json` and `coverage.json`. Exit status is 0 when every
requested selection resolves `AVAILABLE`, and 3 otherwise. Valuation snapshots
are append-only history rows keyed by company; no historical row is ever
rewritten, and a revised assumption, method version, precision policy, solver
policy or pinned source produces a new, independently reconstructable snapshot.
See [the Part 5A proof report](docs/part5a-proof.md).

## Part 5B — Institutional Analyst Engine

The analyst CLI produces structured, evidence-cited **interpretation** over Parts 1-5A
evidence. It creates no new source facts, no recommendation, no final report. Every claim
an `AnalystModel` may propose is validated deterministically before acceptance: every
citation must resolve to an item explicitly pinned into the request's own evidence
bundle; every numeric token in a claim's statement must exactly match — same value, same
sign, same unit family — a canonical stored value already cited, never a value the model
computed itself; forbidden recommendation language (`buy`/`sell`/`hold`/`target price`),
unsupported certainty language, and sector-inapplicable industrial framing (e.g. "net
debt" for a bank) are all rejected. `epistemic_class` is structurally locked to
`INTERPRETATION` for everything the model authors — FACT, CALCULATION, and SCENARIO
describe only what was cited, never the claim itself.

Section coverage (`AVAILABLE`/`PARTIAL`/`INSUFFICIENT_INPUT`/`NOT_APPLICABLE`/
`UNAVAILABLE`/`UNVERIFIED_METRIC_SEMANTICS`/`INCOMPATIBLE_SCOPE`/`INCOMPATIBLE_PERIOD`) is
computed from the pinned evidence bundle before any model call, and persisted
independently of whether any claim was produced — a section with no genuine coverage is
an honest result, not a pipeline failure. No LIVE language-model provider ships in this
part: `Capabilities.analysis_available` is `false` in every real deployment until a
provider is explicitly wired in a later, separately reviewed increment. The CLI, run as
shipped, always reports "provider unavailable" (exit code 4) — it never silently
substitutes a stub.

Run the complete offline proof in a **new** output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_analysis.py --output output/part5b-proof-manual
```

This exercises two explicitly separate tracks. **Real retained evidence** (INFY, YESBANK):
only genuinely real Screener/document fixtures are used, and sections with no real
retained evidence (market/technical data, industry/macro context, and any
scenario-dependent expectation) are never requested for either company — for example,
YESBANK's real investor-presentation PDF genuinely supports `BUSINESS_MODEL` and
`MANAGEMENT_AND_DOCUMENT_INTELLIGENCE`, while INFY (no retained document evidence) reports
`INSUFFICIENT_INPUT`/`UNAVAILABLE` there instead — an outcome of what evidence actually
exists, not a scripted difference. **Synthetic mechanical proof**: a fully synthetic
company and scenario fixture exercises valuation-expectation, variant-perception, and
bull/base/bear interpretation mechanically; it is never presented as real analysis.

For an existing evidence graph, save a request JSON (`AnalysisRequest`: company, profile,
pinned Part 1-5A evidence IDs, requested sections) and an execution JSON
(`AnstExecutionSpec`: template selection and non-secret generation parameters), then run:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run stock-research-ai --database /absolute/path/research.sqlite3 \
  analyze-company --request /absolute/path/request.json \
  --execution /absolute/path/execution.json --output /absolute/path/new-export
```

Outputs are `normalized/analysis_snapshot.json` (the full snapshot, including the exact
input bundle and the exact rendered model request/response — reconstructable forever),
`normalized/claims_by_section.json`, and `coverage.json`. Analysis snapshots are
append-only history rows keyed by company; a revised template, provider, model, generation
parameter, or pinned source produces a new, independently reconstructable snapshot, and no
historical row is ever rewritten. See [the Part 5B proof report](docs/part5b-proof.md).

## Part 5C — Independent Challenger / Evidence Checker

The challenger CLI independently re-verifies a frozen Part 5B `AnalysisSnapshot` against the
exact same closed evidence bundle Part 5B itself used — no new discovery, no "latest"
substitution, no Screener/Kite/web calls. It re-derives evidence via the reviewed snapshot's
own pinned `AnalysisRequest`, independently re-reads every raw artifact byte-for-byte, and
runs a fixed set of deterministic checks over every accepted claim: citation integrity
(hash/lineage re-verification), numeric/unit re-checks, period/scope compatibility,
conflicting-evidence detection, causal/certainty/recommendation language, structural
sector-policy applicability, evaluative claims on unavailable evidence, forensic-pattern
misconduct language, management-quote re-verification, scenario/assumption lineage, and
point-in-time limitation coverage. A fixed `RELEVANCE_REGISTRY` additionally detects material
evidence that was pinned into the bundle but never cited by a specific claim.

Every accepted `AnalystClaim` receives exactly one `ClaimReview`, whose outcome
(`SUPPORTED_BY_BUNDLE`/`SUPPORTED_WITH_LIMITATIONS`/`CHALLENGED`/`INVALID_AGAINST_BUNDLE`/
`UNVERIFIABLE_FROM_BUNDLE`) is a pure deterministic function of its own findings — never set
independently. A `CHALLENGED`/`INVALID_AGAINST_BUNDLE` outcome is an honest result, not a
pipeline failure: it means the independent review found something, which is the entire
point. An optional `ChallengerModel` may additionally suggest findings, but only from a
closed four-category `suggestion_kind` (material evidence, analytical tension, alternative
interpretation, language concern) — it can never directly set a `finding_type`, `severity`,
claim outcome, citation validity, or integrity status; a model-sourced finding can move a
claim from `SUPPORTED_BY_BUNDLE` to `CHALLENGED`, never to `INVALID_AGAINST_BUNDLE`. No LIVE
challenger-model provider ships in this part: `Capabilities.deterministic_challenge_available`
is `true` once the deterministic engine is wired (no provider needed), while
`Capabilities.live_challenger_available` stays `false` until a provider is explicitly wired
in a later, separately reviewed increment.

Run the complete offline proof in a **new** output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_challenge.py --output output/part5c-proof-manual
```

This exercises two explicitly separate tracks. **Real retained evidence** (INFY, YESBANK):
a genuine Part 5B `AnalysisSnapshot` pins both a `SECTOR_KPI` growth metric and its linked
`PATTERN` evidence, with a claim citing only the growth metric — Part 5C's own
`RELEVANCE_REGISTRY` independently finds the uncited pattern and raises a real
`ANALYTICAL_TENSION` finding. **Synthetic mechanical proof**: causal language, model-assisted
suggestions, and direct checks (calling `review_claim` on hand-built claims) for finding
types Part 5B's own validator already refuses at claim-creation time and can therefore never
reach Part 5C through the ordinary pipeline.

For an existing analysis snapshot, run:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run stock-research-ai --database /absolute/path/research.sqlite3 \
  challenge-analysis --analysis-snapshot <analysis-snapshot-uuid> \
  --output /absolute/path/new-export
```

Outputs are `normalized/challenge_snapshot.json` (the full snapshot, including the exact
reviewed `AnalysisSnapshot` embedded verbatim), `claim_reviews.json`, `section_reviews.json`,
`findings.json`, `uncited_evidence.json`, and `coverage.json`. Challenge snapshots are
append-only history rows keyed by analysis snapshot; no historical row is ever rewritten.
See [the Part 5C proof report](docs/part5c-proof.md).

## Part 6A — Final Report Engine

The report CLI turns a frozen Part 5C `ChallengeSnapshot` into the final research package.
The chain is enforced structurally: evidence → deterministic calculations → Part 5B
`AnalysisSnapshot` → Part 5C `ChallengeSnapshot` → Part 6A `ResearchReportSnapshot`. A
`ReportRequest` carries exactly one field, `challenge_snapshot_id` — there is no
`AnalysisSnapshot`-only fallback anywhere in the engine; a missing `ChallengeSnapshot`
refuses report generation outright.

Every deterministic table cell is a verbatim re-display of an already-canonical upstream
value — no new arithmetic runs in this part. Coverage is rendered using the exact frozen
8-value vocabulary from Part 5B (`AVAILABLE`/`PARTIAL`/`INSUFFICIENT_INPUT`/`NOT_APPLICABLE`/
`UNAVAILABLE`/`UNVERIFIED_METRIC_SEMANTICS`/`INCOMPATIBLE_SCOPE`/`INCOMPATIBLE_PERIOD`), never
widened; sections with no upstream support (management guidance, guidance-vs-actual, AGM
intelligence, governance, competitive structure) render an honest coverage status plus a
closed reason code (`REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED`,
`UPSTREAM_CAPABILITY_NOT_IMPLEMENTED`, `NO_DOCUMENTS_OF_TYPE_PINNED`) rather than fabricated
content. `INVALID_AGAINST_BUNDLE` claims never appear as a standalone conclusion in any
topical section — only inside the Independent Challenger Review section, always paired with
the finding that invalidated them; `CHALLENGED` claims are always rendered alongside their
findings. There is no global investment stance, no BUY/SELL/HOLD, and no 0–100 score —
`research_completeness` and `valuation_evidence_state` are the only synthesis-level fields,
both pure, threshold-free functions of already-computed coverage/category data.

A quote from a `CONCALL_TRANSCRIPT` (mixed management/analyst/moderator voices, with no
speaker field anywhere upstream) is always labeled the neutral `TRANSCRIPT_STATEMENT`, never
`MANAGEMENT_STATEMENT` — speaker identity is never inferred from prose or position. Document
Corpus coverage is explicitly `corpus_scope: PINNED_REPORT_INPUT_ONLY`: it shows only
documents the original Part 5B request actually pinned, never a claim about the full
document universe retained for the company.

An optional `ReportNarrator` may add bounded prose, but every numeric token in its text must
already appear in that section's own deterministic tables (numeric closure), and it can never
author an evidence ID, hash, coverage status, or challenge outcome — its only output shape is
`{section, text}`. A rejected narrator block falls back to deterministic-only content; it
never fails the whole report. No live narrator provider ships in this part.

Run the complete offline proof in a **new** output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_report.py --output output/part6a-proof-manual
```

This exercises two explicitly separate tracks. **Real retained evidence** (INFY, YESBANK):
the full frozen chain over real Screener/document fixtures — for YESBANK, both genuinely
retained document fixtures (an investor presentation and a concall transcript) are ingested
through the normal Part 3A pipeline and cited, proving Document Corpus and Concall/Investor-
Presentation Intelligence render from real evidence with the transcript-neutral speaker rule
applied for real. **Synthetic mechanical proof**: narrator numeric-closure/forbidden-language
rejection and fallback, report-revision append-only history, and raw-artifact-corruption
atomicity.

For an existing challenge snapshot, run:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run stock-research-ai --database /absolute/path/research.sqlite3 \
  generate-report --challenge-snapshot <challenge-snapshot-uuid> \
  --output /absolute/path/new-export
```

Outputs are `COMPANY_RESEARCH.json` (the canonical structured package), `COMPANY_INSTITUTIONAL_
RESEARCH.md` and `COMPANY_INVESTOR_BRIEF.md` (deterministic Markdown renderings), and
`investor_brief.json`. The investor brief is a pure projection of the same persisted
`ResearchReportSnapshot` — it runs no second research process and cannot contradict the
institutional report. Report snapshots are append-only history rows keyed by challenge
snapshot; no historical row is ever rewritten. See [the Part 6A proof report](docs/part6a-proof.md).
