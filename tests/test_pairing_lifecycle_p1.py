"""P1-4: bearer expiry, explicit revocation, re-pair invalidation and per-Origin pairing
lockout. Temporary auth files only; clocks are injected."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from stock_research_ai.__main__ import main
from stock_research_ai.adapters.http.browser_auth import (
    DEFAULT_TOKEN_TTL,
    PairingError,
    PairingLockedOutError,
    PairingStore,
)
from stock_research_ai.config import Settings
from stock_research_ai.interfaces import http as http_interface

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
FOREIGN_ORIGIN = "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba"


class Clocks:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
        self.mono = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clocks() -> Clocks:
    return Clocks()


def make_store(path, clocks: Clocks, **kwargs) -> PairingStore:
    return PairingStore(path, now=clocks.now, monotonic=clocks.monotonic, **kwargs)


def pair(store: PairingStore, origin: str = EXTENSION_ORIGIN) -> str:
    return store.complete_pairing(origin, store.issue_pairing_code())


# --- expiry ------------------------------------------------------------------------------


def test_token_expires_at_its_server_side_expiry(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    token = pair(store)
    record = json.loads((tmp_path / "auth.json").read_text())
    assert record["version"] == 2
    issued = datetime.fromisoformat(record["paired"]["issued_at"])
    expires = datetime.fromisoformat(record["paired"]["expires_at"])
    assert issued.tzinfo is not None and expires - issued == DEFAULT_TOKEN_TTL == timedelta(days=30)
    assert token not in (tmp_path / "auth.json").read_text()  # only the hash is stored

    clocks.wall += DEFAULT_TOKEN_TTL - timedelta(seconds=1)
    assert store.verify(EXTENSION_ORIGIN, token) is True
    clocks.wall += timedelta(seconds=1)
    assert store.verify(EXTENSION_ORIGIN, token) is False
    assert store.verify(None, token) is False


def test_custom_ttl_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        PairingStore(tmp_path / "auth.json", token_ttl=timedelta(0))


@pytest.mark.parametrize("legacy", ["v1", "corrupt", "unknown_version"])
def test_legacy_or_unreadable_pairing_file_fails_closed(tmp_path, clocks, legacy):
    path = tmp_path / "auth.json"
    token = "legacy-token-value"
    v1 = {
        "version": 1,
        "pending": None,
        "paired": {
            "origin": EXTENSION_ORIGIN,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "paired_at": "2026-01-01T00:00:00Z",
        },
    }
    path.write_text(
        {"v1": json.dumps(v1), "corrupt": "{not json", "unknown_version": json.dumps(
            {"version": 3, "pending": None, "paired": None})}[legacy]
    )
    store = make_store(path, clocks)
    assert store.verify(EXTENSION_ORIGIN, token) is False  # never granted a new lifetime
    assert store.paired_origin() is None
    new_token = pair(store)  # re-pairing works and rewrites the file as version 2
    assert store.verify(EXTENSION_ORIGIN, new_token) is True
    assert json.loads(path.read_text())["version"] == 2


# --- revocation and re-pairing -------------------------------------------------------------


def test_revoke_invalidates_token_and_pending_code(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    token = pair(store)
    code = store.issue_pairing_code()
    store.revoke()
    assert store.verify(EXTENSION_ORIGIN, token) is False
    assert store.paired_origin() is None
    with pytest.raises(PairingError):
        store.complete_pairing(EXTENSION_ORIGIN, code)  # pending code cleared too
    stored = json.loads((tmp_path / "auth.json").read_text())
    assert stored == {"version": 2, "pending": None, "paired": None}


def test_revoke_pairing_cli_needs_no_provider_credential(tmp_path, monkeypatch, capsys):
    path = tmp_path / "browser_auth.json"
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(path))
    for name in [k for k in os.environ if "API_KEY" in k or "ANALYST" in k]:
        monkeypatch.delenv(name)
    store = PairingStore(path)
    token = pair(store)
    main(["revoke-pairing"])
    output = capsys.readouterr().out
    assert "revoked" in output and token not in output
    assert PairingStore(path).verify(EXTENSION_ORIGIN, token) is False


def test_repairing_immediately_invalidates_the_previous_token(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    old = pair(store)
    new = pair(store)
    assert store.verify(EXTENSION_ORIGIN, old) is False
    assert store.verify(EXTENSION_ORIGIN, new) is True


# --- lockout isolation ----------------------------------------------------------------------


def test_foreign_origin_failures_do_not_lock_the_configured_origin(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    store.issue_pairing_code()
    for _ in range(10):
        with pytest.raises(PairingError):
            store.complete_pairing(FOREIGN_ORIGIN, "guess")
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(FOREIGN_ORIGIN, "guess")
    token = pair(store)  # the configured origin is unaffected
    assert store.verify(EXTENSION_ORIGIN, token) is True


def test_configured_origin_brute_force_triggers_its_own_cooldown(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    code = store.issue_pairing_code()
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "guess")
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN, code)  # even the right code waits
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN.upper(), code)  # same normalized origin
    clocks.mono += 61.0
    assert store.verify(EXTENSION_ORIGIN, store.complete_pairing(EXTENSION_ORIGIN, code))


def test_success_clears_that_origins_failures(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    for _ in range(4):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "guess")
    pair(store)
    for _ in range(4):  # a fresh budget of failures, still not locked
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "guess")
    pair(store)


def test_active_token_keeps_working_during_failed_repair_attempts(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    token = pair(store)
    store.issue_pairing_code()
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "guess")
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN, "guess")
    assert store.verify(EXTENSION_ORIGIN, token) is True


def test_tracked_origins_are_bounded(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    for i in range(200):
        with pytest.raises(PairingError):
            store.complete_pairing(f"chrome-extension://origin{i}", "guess")
        clocks.mono += 0.001
    assert len(store._failures) <= 32  # noqa: SLF001


def test_restart_clears_cooldowns_by_design(tmp_path, clocks):
    store = make_store(tmp_path / "auth.json", clocks)
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "guess")
    restarted = make_store(tmp_path / "auth.json", clocks)  # documented: in-memory only
    restarted.issue_pairing_code()
    with pytest.raises(PairingError) as exc:
        restarted.complete_pairing(EXTENSION_ORIGIN, "guess")
    assert not isinstance(exc.value, PairingLockedOutError)


# --- over HTTP -------------------------------------------------------------------------------


async def test_http_expired_revoked_and_locked_out_behaviour(engine, tmp_path, clocks, monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", EXTENSION_ORIGIN)
    store = make_store(tmp_path / "auth.json", clocks)
    token = pair(store)
    app = http_interface.create_app(
        engine=engine,
        settings=Settings(_env_file=None, database_path=tmp_path / "db.sqlite3"),  # type: ignore[call-arg]
        pairing_store=store,
    )
    headers = {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}
    job = "/v1/research-jobs/00000000-0000-4000-8000-000000000000"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get(job, headers=headers)).status_code == 404  # authorized
            for _ in range(6):
                await client.post(
                    "/v1/pair", json={"pairing_code": "guess"}, headers={"Origin": EXTENSION_ORIGIN}
                )
            locked = await client.post(
                "/v1/pair", json={"pairing_code": "guess"}, headers={"Origin": EXTENSION_ORIGIN}
            )
            assert locked.json()["detail"]["code"] == "PAIRING_LOCKED_OUT"
            assert (await client.get(job, headers=headers)).status_code == 404  # still valid
            clocks.wall += DEFAULT_TOKEN_TTL
            expired = await client.get(job, headers=headers)
            assert expired.status_code == 401
            assert expired.json()["detail"]["code"] == "UNAUTHORIZED"
            assert token not in expired.text
