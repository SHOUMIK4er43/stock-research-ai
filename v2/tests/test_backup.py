"""Part 6C-A: local backup/restore. Fully offline - real temp-file databases, no live
network, no real Anthropic/pairing credential ever created here."""

import pytest

from stock_research_ai.adapters.migrations import CURRENT_SCHEMA_VERSION
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.backup import (
    BackupError,
    BackupMetadata,
    create_backup,
    restore_backup,
)
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company

_TEST_COMPANY_ID = "44444444-4444-4444-4444-444444444444"


async def _settings_with_company(tmp_path, name: str = "db.sqlite3") -> Settings:
    settings = Settings(
        database_path=tmp_path / name,
        browser_auth_path=tmp_path / "auth.json",
        artifact_path=tmp_path / f"artifacts-{name}",
    )
    await initialize_database(settings)
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.repository.save(
            Company(id=_TEST_COMPANY_ID, name="Backup Co", listings=(), provider_identifiers=())
        )
    finally:
        await database.close()
    return settings


async def _get_company(settings: Settings) -> Company | None:
    database = SQLiteDatabase(settings.database_path)
    try:
        return await database.repository.get(_TEST_COMPANY_ID)  # type: ignore[arg-type]
    finally:
        await database.close()


async def test_backup_creates_a_readable_metadata_and_database_file(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)

    output = await create_backup(settings, tmp_path / "backup1")

    assert (output / "backup_metadata.json").is_file()
    assert (output / "research.sqlite3").is_file()
    metadata = BackupMetadata.from_json((output / "backup_metadata.json").read_text())
    assert metadata.schema_version == CURRENT_SCHEMA_VERSION


async def test_backup_never_includes_the_pairing_state_file(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    output = await create_backup(settings, tmp_path / "backup2")
    assert not (output / "browser_auth.json").exists()
    assert not any("auth" in p.name for p in output.rglob("*"))


async def test_backup_copies_the_artifact_directory_when_present(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    settings.artifact_path.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
    (settings.artifact_path / "note.txt").write_text("hello")  # type: ignore[operator]

    output = await create_backup(settings, tmp_path / "backup3")

    assert (output / "artifacts" / "note.txt").read_text() == "hello"


async def test_backup_refuses_a_non_empty_existing_destination(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    destination = tmp_path / "backup4"
    destination.mkdir()
    (destination / "something").write_text("x")

    with pytest.raises(BackupError):
        await create_backup(settings, destination)


async def test_backup_fails_cleanly_when_no_database_exists(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "never-created.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
    )
    with pytest.raises(BackupError):
        await create_backup(settings, tmp_path / "backup5")


async def test_restore_round_trips_data_into_a_fresh_location(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    backup_dir = await create_backup(settings, tmp_path / "backup6")

    restored_settings = Settings(
        database_path=tmp_path / "restored.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
    )
    await restore_backup(restored_settings, backup_dir)

    company = await _get_company(restored_settings)
    assert company is not None
    assert company.name == "Backup Co"


async def test_restore_refuses_to_overwrite_an_existing_database_without_force(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    backup_dir = await create_backup(settings, tmp_path / "backup7")

    other_settings = await _settings_with_company(tmp_path, name="other.sqlite3")

    with pytest.raises(BackupError):
        await restore_backup(other_settings, backup_dir)


async def test_restore_with_force_takes_a_safety_backup_of_the_overwritten_database(
    tmp_path,
) -> None:
    settings = await _settings_with_company(tmp_path)
    backup_dir = await create_backup(settings, tmp_path / "backup8")

    other_settings = await _settings_with_company(tmp_path, name="other2.sqlite3")

    await restore_backup(other_settings, backup_dir, force=True)

    safety_path = other_settings.database_path.with_suffix(".sqlite3.pre-restore-backup")
    assert safety_path.is_file()


async def test_restore_refuses_a_backup_from_a_newer_unsupported_schema_version(
    tmp_path,
) -> None:
    settings = await _settings_with_company(tmp_path)
    backup_dir = await create_backup(settings, tmp_path / "backup9")
    metadata_path = backup_dir / "backup_metadata.json"
    metadata = BackupMetadata.from_json(metadata_path.read_text())
    newer = BackupMetadata(
        application_version=metadata.application_version,
        schema_version=CURRENT_SCHEMA_VERSION + 1,
        created_at=metadata.created_at,
    )
    metadata_path.write_text(newer.to_json())

    restored_settings = Settings(
        database_path=tmp_path / "restored2.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
    )
    with pytest.raises(BackupError):
        await restore_backup(restored_settings, backup_dir)


async def test_restore_refuses_a_directory_that_is_not_a_real_backup(tmp_path) -> None:
    not_a_backup = tmp_path / "not-a-backup"
    not_a_backup.mkdir()
    settings = Settings(
        database_path=tmp_path / "target.sqlite3", browser_auth_path=tmp_path / "auth.json"
    )
    with pytest.raises(BackupError):
        await restore_backup(settings, not_a_backup)


async def test_a_failed_backup_never_leaves_the_source_database_touched(tmp_path) -> None:
    settings = await _settings_with_company(tmp_path)
    original_bytes = settings.database_path.read_bytes()
    destination = tmp_path / "backupfail"
    destination.mkdir()
    (destination / "x").write_text("occupied")

    with pytest.raises(BackupError):
        await create_backup(settings, destination)

    assert settings.database_path.read_bytes() == original_bytes
