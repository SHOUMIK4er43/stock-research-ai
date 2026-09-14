"""Part 6C-A: forward-only SQLite schema-version infrastructure using the database's
own native `PRAGMA user_version` integer - no new metadata table, no Alembic. This
module is application/infrastructure-owned: a migration may only perform deterministic
SQL/DDL operations against the existing tables already defined in adapters/sqlite.py.
It must never touch, reinterpret, or migrate the *meaning* of any research/report row -
that stays entirely owned by the domain layer, untouched by anything here.

Every database `initialize()` has ever created (Parts 1-6B) is, structurally, exactly
what this module calls schema version 1: no table or column changes were needed to
introduce version tracking. Migration 0 -> 1 exists solely to give
`PRAGMA user_version` a real, owned meaning going forward, for any database created
before this file existed (where SQLite's own default of 0 is indistinguishable from
"never initialized").
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

#: The current schema version this application build understands. Bump by exactly one
#: and append exactly one new Migration below whenever a real schema change is needed.
CURRENT_SCHEMA_VERSION = 1


class UnsupportedSchemaVersionError(RuntimeError):
    """The database's PRAGMA user_version is newer than this application build
    supports. Refusing to start rather than guessing at an unknown future schema."""


class MigrationFailedError(RuntimeError):
    """A migration raised. PRAGMA user_version was NOT advanced past the last
    successfully-applied migration - the database is left at a known, safe version,
    never a half-applied one."""


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    description: str
    apply: Callable[[AsyncConnection], Awaitable[None]]


async def _migrate_0_to_1(connection: AsyncConnection) -> None:
    """Establishes version ownership for the pre-6C-A schema baseline (Parts 1-6B).
    No table or column is created, dropped, or altered here - every table this
    "targets" was already created by `_Base.metadata.create_all` in `initialize()`.
    Deliberately a no-op body; the caller advances PRAGMA user_version once this
    returns without raising."""
    return


#: Ordered, forward-only. Each entry's `from_version` must equal the prior entry's
#: `to_version` (enforced by a regression test, not just convention).
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        from_version=0,
        to_version=1,
        description="Establish schema version ownership for the pre-6C-A baseline.",
        apply=_migrate_0_to_1,
    ),
)


async def read_schema_version(connection: AsyncConnection) -> int:
    result = await connection.execute(text("PRAGMA user_version"))
    return int(result.scalar_one())


async def ensure_schema_version(connection: AsyncConnection) -> int:
    """Reads PRAGMA user_version and applies any pending forward migrations, in order,
    on the given connection - the caller controls the transaction boundary (typically
    one `engine.begin()` block, so a mid-sequence failure leaves nothing partially
    committed for that migration, and the version is only ever advanced one step at a
    time so a crash between migrations still leaves a fully-valid prior version).

    Refuses (raises UnsupportedSchemaVersionError) if the database is already newer
    than this build supports - never silently guesses. Returns the final version
    (unchanged if already current: the common case, and does not write in that case).
    """
    version = await read_schema_version(connection)
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Database schema version {version} is newer than this application "
            f"build supports (supports up to {CURRENT_SCHEMA_VERSION}). "
            "Upgrade the application before using this database."
        )
    pending = [m for m in MIGRATIONS if m.from_version >= version]
    pending.sort(key=lambda m: m.from_version)
    for migration in pending:
        if migration.from_version != version:
            # Not next in sequence - either already applied or a gap; either way this
            # migration is not runnable from the current version.
            continue
        try:
            await migration.apply(connection)
        except Exception as exc:
            raise MigrationFailedError(
                f"Migration {migration.from_version} -> {migration.to_version} "
                f"({migration.description}) failed: {exc}"
            ) from exc
        version = migration.to_version
        # PRAGMA does not accept bound parameters; `version` is this module's own
        # controlled int, never external input.
        await connection.execute(text(f"PRAGMA user_version = {version}"))
    return version
