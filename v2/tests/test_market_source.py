import json

import httpx
import pytest
from test_market import FIXTURES, request

from stock_research_ai.adapters.kite.source import KiteSource
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.bootstrap import market_engine


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_http_failures_no_redirect_no_secret_export(status, market_settings):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path == "/instruments/NSE":
            assert "authorization" not in req.headers
            return httpx.Response(
                200, content=(FIXTURES / "synthetic_instruments.csv").read_bytes()
            )
        assert req.headers["authorization"] == "token TEST_KEY:TEST_TOKEN"
        return httpx.Response(
            status, json={"status": "error"}, headers={"location": "https://example.com"}
        )

    source = KiteSource(
        api_key="TEST_KEY", access_token="TEST_TOKEN", transport=httpx.MockTransport(handler)
    )
    async with market_engine(market_settings, source) as engine:
        result = await engine.ingest_market_data(
            request(master_file=None, candles_file=None, evidence_kind="PROVIDER")
        )
    assert len(calls) == 2
    assert result.coverage.overall == "FAILED"
    assert "TEST_KEY" not in result.model_dump_json()
    assert "TEST_TOKEN" not in result.model_dump_json()
    assert result.retrievals[-1].http_status == status


async def test_missing_credentials_explicit(market_settings):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=(FIXTURES / "synthetic_instruments.csv").read_bytes())

    source = KiteSource(transport=httpx.MockTransport(handler))
    async with market_engine(market_settings, source) as engine:
        result = await engine.ingest_market_data(
            request(master_file=None, candles_file=None, evidence_kind="PROVIDER")
        )
    assert len(calls) == 1
    assert result.retrievals[-1].status == "blocked"
    assert result.retrievals[-1].error.startswith("AUTH_REQUIRED")


async def test_live_transport_shape_and_request_limits(market_settings):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path == "/instruments/NSE":
            return httpx.Response(
                200, content=(FIXTURES / "synthetic_instruments.csv").read_bytes()
            )
        assert req.url.path == "/instruments/historical/900001/day"
        assert req.url.params["continuous"] == "0"
        return httpx.Response(200, content=(FIXTURES / "synthetic_candles.json").read_bytes())

    source = KiteSource(api_key="key", access_token="token", transport=httpx.MockTransport(handler))
    async with market_engine(market_settings, source) as engine:
        result = await engine.ingest_market_data(
            request(master_file=None, candles_file=None, evidence_kind="PROVIDER")
        )
    assert result.coverage.overall == "COMPLETE"
    assert len(calls) == 2
    assert result.retrievals[-1].mode == "http"
    payloads = await source.acquire_prices(
        request(
            start="2010-01-01",
            end="2025-01-03",
            expected_sessions=None,
            master_file=None,
            candles_file=None,
            evidence_kind="PROVIDER",
        ),
        result.instrument,
    )
    assert len(payloads) == 3
    from datetime import datetime

    previous = None
    for call in calls[2:]:
        start = datetime.fromisoformat(call.url.params["from"])
        end = datetime.fromisoformat(call.url.params["to"])
        assert (end - start).days <= 1999
        if previous:
            assert (start - previous).total_seconds() == 1
        previous = end


@pytest.mark.parametrize("failure", ["timeout", "oversize", "master_html"])
async def test_bounded_failures(failure, market_settings):
    def handler(req):
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive diagnostic", request=req)
        return httpx.Response(
            200, content=b"<html>blocked</html>" if failure == "master_html" else b"x" * 1001
        )

    source = KiteSource(transport=httpx.MockTransport(handler), max_bytes=1000)
    async with market_engine(market_settings, source) as engine:
        result = await engine.ingest_market_data(
            request(master_file=None, candles_file=None, evidence_kind="PROVIDER")
        )
    assert result.coverage.overall == "FAILED"
    assert "sensitive diagnostic" not in result.model_dump_json()
    if failure == "oversize":
        assert not result.raw_artifacts[0].complete
        assert (
            await FileRawArtifactStore(market_settings.artifact_path).read(result.raw_artifacts[0])
            == b"x" * 1000
        )


async def test_corrupt_raw_bytes_rejected(market_settings):
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request())
    artifact = result.raw_artifacts[1]
    (market_settings.artifact_path / artifact.content_reference).write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        await FileRawArtifactStore(market_settings.artifact_path).read(artifact)


def test_real_cli_offline(tmp_path):
    import os
    import subprocess
    import sys

    # Separate Python process with socket operations explicitly disabled.
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
        "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(guard)}
    for key in list(env):
        if key.startswith("KITE_"):
            del env[key]
    db = tmp_path / "cli.sqlite3"
    base = [sys.executable, "-B", "-m", "stock_research_ai", "--database", str(db)]
    subprocess.run([*base, "init-db"], env=env, check=True, capture_output=True)
    output = tmp_path / "export"
    cmd = [
        *base,
        "ingest-market-data",
        "--symbol",
        "YESBANK",
        "--kind",
        "EQUITY",
        "--start",
        "2025-01-02",
        "--end",
        "2025-01-03",
        "--master-file",
        str(FIXTURES / "synthetic_instruments.csv"),
        "--candles-file",
        str(FIXTURES / "synthetic_candles.json"),
        "--synthetic",
        "--expected-sessions",
        "2025-01-02,2025-01-03",
        "--output",
        str(output),
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "COMPLETE: 2 observations" in result.stdout
    exported = json.loads((output / "normalized/extraction.json").read_text())
    assert exported["request"]["evidence_kind"] == "SYNTHETIC"
    assert exported["instrument"]["provider_identifier"] == "900001"
    assert (output / "raw/manifest.json").is_file()
    second = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert second.returncode == 2
    assert "new or empty" in second.stderr
