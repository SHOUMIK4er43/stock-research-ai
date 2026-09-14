"""Part 6B-A: user-local browser pairing/auth persistence."""

import json
import time

import pytest

from stock_research_ai.adapters.http.browser_auth import (
    PairingError,
    PairingLockedOutError,
    PairingStore,
)

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def test_full_pairing_round_trip_then_verify(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code()
    token = store.complete_pairing(EXTENSION_ORIGIN, code)

    assert store.verify(EXTENSION_ORIGIN, token) is True
    assert store.paired_origin() == EXTENSION_ORIGIN


def test_invalid_pairing_code_is_refused(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    store.issue_pairing_code()
    with pytest.raises(PairingError, match="Invalid pairing code"):
        store.complete_pairing(EXTENSION_ORIGIN, "not-the-real-code")
    assert store.paired_origin() is None


def test_reused_one_time_code_is_refused(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code()
    store.complete_pairing(EXTENSION_ORIGIN, code)
    with pytest.raises(PairingError, match="No pairing is currently pending"):
        store.complete_pairing(EXTENSION_ORIGIN, code)


def test_expired_pairing_code_is_refused(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code(ttl_seconds=0)
    time.sleep(0.01)
    with pytest.raises(PairingError, match="expired"):
        store.complete_pairing(EXTENSION_ORIGIN, code)


def test_non_extension_origin_cannot_pair(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code()
    with pytest.raises(PairingError, match="chrome-extension"):
        store.complete_pairing("https://evil.example", code)


def test_repairing_replaces_previous_origin_and_invalidates_old_token(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    old_token = store.complete_pairing(EXTENSION_ORIGIN, store.issue_pairing_code())
    assert store.verify(EXTENSION_ORIGIN, old_token) is True

    new_origin = "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    new_token = store.complete_pairing(new_origin, store.issue_pairing_code())

    assert store.verify(EXTENSION_ORIGIN, old_token) is False  # old token no longer valid
    assert store.verify(new_origin, new_token) is True
    assert store.paired_origin() == new_origin


def test_verify_requires_both_origin_and_token(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    token = store.complete_pairing(EXTENSION_ORIGIN, store.issue_pairing_code())

    assert store.verify(EXTENSION_ORIGIN, None) is False
    assert store.verify(None, token) is False
    assert store.verify(EXTENSION_ORIGIN, "wrong-token") is False
    assert store.verify("chrome-extension://different", token) is False
    assert store.verify(EXTENSION_ORIGIN, token) is True


def test_persisted_file_never_contains_plaintext_code_or_token(tmp_path):
    path = tmp_path / "auth.json"
    store = PairingStore(path)
    code = store.issue_pairing_code()
    token = store.complete_pairing(EXTENSION_ORIGIN, code)

    raw = path.read_text()
    assert code not in raw
    assert token not in raw
    payload = json.loads(raw)
    assert "code_hash" in payload["pending"]
    assert "token_hash" in payload["paired"]


def test_file_permissions_are_restrictive(tmp_path):
    path = tmp_path / "auth.json"
    store = PairingStore(path)
    store.issue_pairing_code()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_pair_code_cli_issues_and_prints_a_usable_code(tmp_path, monkeypatch):
    from stock_research_ai.__main__ import main

    auth_path = tmp_path / "browser_auth.json"
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(auth_path))
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args: printed.append(" ".join(map(str, args))))

    main(["pair-code"])

    assert len(printed) == 1
    code = printed[0].strip()
    assert len(code) > 10

    store = PairingStore(auth_path)
    token = store.complete_pairing(EXTENSION_ORIGIN, code)
    assert store.verify(EXTENSION_ORIGIN, token) is True


# --- Part 6C-A: pairing brute-force hardening ---------------------------------------


class _FakeClock:
    """Deterministic, controllable stand-in for time.monotonic() - no real 60s sleeps
    in this test suite."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _patched_clock(monkeypatch: pytest.MonkeyPatch, start: float = 0.0) -> _FakeClock:
    clock = _FakeClock(start)
    monkeypatch.setattr(
        "stock_research_ai.adapters.http.browser_auth.time.monotonic", clock
    )
    return clock


def test_five_failed_attempts_within_60_seconds_trigger_a_cooldown(tmp_path, monkeypatch):
    _patched_clock(monkeypatch)
    store = PairingStore(tmp_path / "auth.json")
    store.issue_pairing_code()

    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")

    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")


def test_locked_out_error_never_leaks_the_pairing_code_or_token(tmp_path, monkeypatch):
    _patched_clock(monkeypatch)
    store = PairingStore(tmp_path / "auth.json")
    store.issue_pairing_code()
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")

    with pytest.raises(PairingLockedOutError) as excinfo:
        store.complete_pairing(EXTENSION_ORIGIN, "some-other-guess")
    assert "wrong-code" not in str(excinfo.value)
    assert "some-other-guess" not in str(excinfo.value)


def test_cooldown_clears_itself_after_60_seconds(tmp_path, monkeypatch):
    clock = _patched_clock(monkeypatch)
    store = PairingStore(tmp_path / "auth.json")
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")

    clock.advance(61.0)

    # The cooldown has cleared, so a fresh pairing attempt is evaluated normally
    # again (still fails because the original code is long expired/unused, but with
    # an ordinary PairingError, never PairingLockedOutError).
    code = store.issue_pairing_code()
    token = store.complete_pairing(EXTENSION_ORIGIN, code)
    assert store.verify(EXTENSION_ORIGIN, token) is True


def test_a_successful_pair_resets_the_failure_counter(tmp_path, monkeypatch):
    _patched_clock(monkeypatch)
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code()

    for _ in range(4):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")

    # A successful pairing on the 5th attempt must reset the counter, not lock out.
    token = store.complete_pairing(EXTENSION_ORIGIN, code)
    assert store.verify(EXTENSION_ORIGIN, token) is True

    # Confirm the counter really was reset: 4 more failures (against a fresh code)
    # should not yet trigger a lockout.
    new_code = store.issue_pairing_code()
    for _ in range(4):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "still-wrong")
    token2 = store.complete_pairing(EXTENSION_ORIGIN, new_code)
    assert store.verify(EXTENSION_ORIGIN, token2) is True


def test_lockout_state_is_in_memory_only_a_fresh_store_is_never_locked_out(tmp_path, monkeypatch):
    _patched_clock(monkeypatch)
    path = tmp_path / "auth.json"
    store = PairingStore(path)
    for _ in range(5):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")
    with pytest.raises(PairingLockedOutError):
        store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")

    # Simulates a service restart: a brand new PairingStore instance over the same
    # file has no in-memory failure history at all.
    fresh_store = PairingStore(path)
    code = fresh_store.issue_pairing_code()
    token = fresh_store.complete_pairing(EXTENSION_ORIGIN, code)
    assert fresh_store.verify(EXTENSION_ORIGIN, token) is True


def test_old_failures_outside_the_60_second_window_do_not_count(tmp_path, monkeypatch):
    clock = _patched_clock(monkeypatch)
    store = PairingStore(tmp_path / "auth.json")

    for _ in range(4):
        with pytest.raises(PairingError):
            store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")
    clock.advance(61.0)  # the first 4 failures age out of the window
    with pytest.raises(PairingError):
        store.complete_pairing(EXTENSION_ORIGIN, "wrong-code")  # only the 1st in-window failure

    # Still well under 5 failures within any 60s window, so this must not be locked out.
    code = store.issue_pairing_code()
    token = store.complete_pairing(EXTENSION_ORIGIN, code)
    assert store.verify(EXTENSION_ORIGIN, token) is True
