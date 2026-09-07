# Stock Research AI

A local-first equity research foundation. Part 1 proves the path from FastAPI or
MCP to a provider-independent `ResearchEngine` and a small SQLite company registry.
It does not collect data, analyze securities, call an LLM, or generate reports.

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

Source acquisition/normalization, financial persistence and calculations, sector
analysis, document intelligence, guidance history, LLM synthesis, report exports,
and the Chrome extension remain future phases. Authentication, migrations,
background processing, and additional infrastructure are also deferred.
