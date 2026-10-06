"""Part 6C-A: `stock-research-ai diagnostics` - safe, read-only production state for
support/troubleshooting. Every field here has been individually checked against the
"never print" list (Anthropic key, pairing token, Authorization headers, report
contents, raw database rows) - this module returns only booleans, small enums, paths,
and counts, never a secret or research-data value.
"""

from dataclasses import dataclass
from pathlib import Path

from stock_research_ai import __version__
from stock_research_ai.adapters.http.browser_schemas import API_VERSION
from stock_research_ai.adapters.migrations import CURRENT_SCHEMA_VERSION
from stock_research_ai.adapters.secret_store import (
    CredentialSource,
    SecretStore,
    resolve_analyst_api_key,
)
from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase
from stock_research_ai.config import Settings
from stock_research_ai.domain.report import ResearchReportSnapshot

#: The same derivation browser_schemas.py uses for VersionResponse.report_schema_version
#: - re-derived here rather than importing that module's private constant, so this
#: stays a public, independent computation from the one true source (the domain
#: model's own frozen Literal default), not a second hop through another module's
#: internal name.
_REPORT_SCHEMA_VERSION: str = ResearchReportSnapshot.model_fields["schema_version"].default


@dataclass(frozen=True)
class Diagnostics:
    application_version: str
    api_version: str
    report_schema_version: str
    database_path: Path
    database_reachable: bool
    database_schema_version: int | None
    database_schema_supported: bool
    http_host: str
    http_port: int
    analyst_provider_configured: bool
    credential_source: CredentialSource
    analyst_model: str
    pairing_configured: bool
    log_path: Path
    artifact_path: Path
    browser_auth_path: Path
    analyst_provider: str | None = None


async def gather_diagnostics(
    settings: Settings, secret_store: SecretStore | None = None
) -> Diagnostics:
    """Read-only: never creates the database, never mutates pairing state, never
    performs any network call (no Anthropic, no Screener) - exactly the "local,
    deterministic" inspection bar the Part 6C-A audit requires. Reachability means
    SQLite's version can be read; supported means version compatibility, not full
    table-shape readiness. Existing WAL databases may need SQLite-managed shared
    memory sidecars for reads; mode=ro does not checkpoint or modify application
    data, user_version, or journal mode. A running service may modify this database,
    so it must not be opened with SQLite's immutable=1 assertion.

    `secret_store` mirrors bootstrap.live_analyst_model's injection point (a
    SecretStore, typically only ever overridden in tests to avoid touching the real
    macOS Keychain); production callers leave it as None, which falls back to the
    real Keychain exactly like live_analyst_model does."""
    database = SQLiteDatabase(settings.database_path, read_only=True)
    database_reachable = False
    schema_version: int | None = None
    schema_supported = False
    try:
        schema_version = await database.inspect_schema_version()
        database_reachable = True
        schema_supported = schema_version <= CURRENT_SCHEMA_VERSION
    except DatabaseNotReadyError:
        database_reachable = False
    finally:
        await database.close()

    _api_key, credential_source = resolve_analyst_api_key(settings, secret_store)

    provider = settings.analyst_provider or ("anthropic" if credential_source != "none" else None)
    model = (settings.aicredits_model or "") if provider == "aicredits" else settings.analyst_model
    configured = (
        provider in ("anthropic", "aicredits")
        and credential_source != "none"
        and bool(model.strip())
        and (provider != "aicredits" or model == model.strip())
    )

    from stock_research_ai.adapters.http.browser_auth import PairingStore

    pairing_store = PairingStore(settings.browser_auth_path)
    pairing_configured = pairing_store.paired_origin() is not None

    return Diagnostics(
        application_version=__version__,
        api_version=API_VERSION,
        report_schema_version=_REPORT_SCHEMA_VERSION,
        database_path=settings.database_path,
        database_reachable=database_reachable,
        database_schema_version=schema_version,
        database_schema_supported=schema_supported,
        http_host=settings.http_host,
        http_port=settings.http_port,
        analyst_provider_configured=configured,
        analyst_provider=provider,
        credential_source=credential_source,
        analyst_model=model,
        pairing_configured=pairing_configured,
        log_path=settings.log_path,
        artifact_path=settings.artifact_path or settings.database_path.parent / "artifacts",
        browser_auth_path=settings.browser_auth_path,
    )


def format_diagnostics(diagnostics: Diagnostics) -> str:
    """Plain, stable key: value lines - safe to paste into a support request. Every
    line here was reviewed against the "never print" list; if you add a field, review
    it against that list before adding it here too."""
    lines = [
        f"application_version: {diagnostics.application_version}",
        f"api_version: {diagnostics.api_version}",
        f"report_schema_version: {diagnostics.report_schema_version}",
        f"database_path: {diagnostics.database_path}",
        f"database_reachable: {diagnostics.database_reachable}",
        f"database_schema_version: {diagnostics.database_schema_version}",
        f"database_schema_supported: {diagnostics.database_schema_supported}",
        f"http_bind: {diagnostics.http_host}:{diagnostics.http_port}",
        f"analyst_provider: {diagnostics.analyst_provider}",
        f"analyst_provider_configured: {diagnostics.analyst_provider_configured}",
        f"credential_source: {diagnostics.credential_source}",
        f"analyst_model: {diagnostics.analyst_model}",
        f"pairing_configured: {diagnostics.pairing_configured}",
        f"log_path: {diagnostics.log_path}",
        f"artifact_path: {diagnostics.artifact_path}",
        f"browser_auth_path: {diagnostics.browser_auth_path}",
    ]
    return "\n".join(lines)
