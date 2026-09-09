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

Use Python 3.12 and the existing uv-managed `.venv`. SQLAlchemy's `asyncio` extra
includes its required `greenlet` runtime dependency, including on Apple Silicon.
The installed MCP SDK is **2.1.1**, using `from mcp.server import MCPServer`.
The console entry point remains `stock-research-ai = "stock_research_ai:main"`.

The commands below deliberately disable environment synchronization, lockfile
updates, and Python bytecode writes:

```sh
cd /Users/shoumikganguly/stock-research-ai
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
cd /Users/shoumikganguly/stock-research-ai
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
cd /Users/shoumikganguly/stock-research-ai
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
cd /Users/shoumikganguly/stock-research-ai
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
cd /Users/shoumikganguly/stock-research-ai
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
