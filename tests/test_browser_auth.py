"""Part 6B-A: user-local browser pairing/auth persistence."""

import json
import time

import pytest

from stock_research_ai.adapters.http.browser_auth import PairingError, PairingStore

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
