"""Real engine, SQLite history, raw integrity, export and offline CLI checks."""

import hashlib
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from test_market_calculations import EXPECTED, INPUTS, acquire, request, verify_golden
from test_market_calculations import evidence as evidence  # noqa: F401

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.bootstrap import calculate_market_snapshot
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot


async def test_immutable_history(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    first = await calculate_market_snapshot(settings, req, tmp_path / "first")
    second = await calculate_market_snapshot(settings, req, tmp_path / "second")
    assert first.id != second.id
    assert [r.value for r in first.results] == [r.value for r in second.results]
    assert (
        first.sources[0].payload_sha256
        == hashlib.sha256(source.model_dump_json().encode()).hexdigest()
    )
    closes = [*INPUTS["YESBANK"][:-1], "130"]
    changed = await acquire(settings, tmp_path, "changed", "YESBANK", INPUTS["dates"], closes)
    third = await calculate_market_snapshot(settings, request(changed), tmp_path / "third")
    assert third.sources[0].extraction_id != source.id
    assert third.results[0].value != first.results[0].value
    database = SQLiteDatabase(settings.database_path)
    try:
        assert await database.market_calculations.market_calculation_history(source.id) == (
            first,
            second,
        )
        assert await database.market_calculations.market_calculation_history(changed.id) == (third,)
        with pytest.raises(IntegrityError):
            await database.market_calculations.save_market_calculation(first)
        restored = await database.market.get_market(source.id)
        assert restored == source
    finally:
        await database.close()
    assert (
        MarketCalculationSnapshot.model_validate_json(
            (tmp_path / "first/normalized/calculation_snapshot.json").read_text()
        )
        == first
    )
    assert not (tmp_path / "first/raw").exists()
    invalid = first.model_dump()
    invalid["results"][0]["snapshot_id"] = uuid4()
    with pytest.raises(ValidationError):
        MarketCalculationSnapshot.model_validate(invalid)


async def test_engine_rejects_unknown_or_corrupt_source(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    with pytest.raises(ValueError, match="Unknown market extraction"):
        await calculate_market_snapshot(
            settings, req.model_copy(update={"extraction_id": uuid4()}), tmp_path / "absent"
        )
    artifact = source.raw_artifacts[-1]
    (settings.artifact_path / artifact.content_reference).write_bytes(b"corrupt synthetic bytes")
    with pytest.raises(ValueError, match="integrity"):
        await calculate_market_snapshot(settings, req, tmp_path / "corrupt")
    database = SQLiteDatabase(settings.database_path)
    try:
        assert await database.market_calculations.market_calculation_history(source.id) == ()
    finally:
        await database.close()


async def test_offline_cli(evidence, tmp_path):
    settings, sources = evidence
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
        "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(guard),
        "PYTHONDONTWRITEBYTECODE": "1",
        "STOCK_RESEARCH_AI_ARTIFACT_PATH": str(settings.artifact_path),
    }
    cases = [
        ("YESBANK", ["PRICE_RETURN", "MAX_DRAWDOWN", "REALIZED_VOLATILITY", "SMA"], None),
        ("INFY", ["PRICE_RETURN", "SMA"], None),
        ("CAGR", ["PRICE_CAGR"], None),
        ("YESBANK", ["BENCHMARK_RELATIVE_RETURN"], "NIFTY 50"),
        ("YESBANK", ["BENCHMARK_RELATIVE_RETURN"], "NIFTY BANK"),
    ]
    for index, (symbol, metrics, benchmark) in enumerate(cases):
        req = request(sources[symbol], metrics, sources[benchmark] if benchmark else None)
        path = tmp_path / f"request{index}.json"
        path.write_text(req.model_dump_json())
        output = tmp_path / f"calculated{index}"
        run = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "stock_research_ai",
                "--database",
                str(settings.database_path),
                "calculate-market",
                "--request",
                str(path),
                "--output",
                str(output),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        result = MarketCalculationSnapshot.model_validate_json(
            (output / "normalized/calculation_snapshot.json").read_text()
        )
        for r in result.results:
            if benchmark:
                assert str(r.value) == EXPECTED[benchmark]["relative_return"]
            else:
                verify_golden(r, EXPECTED["YESBANK" if symbol == "CAGR" else symbol][r.metric_id])
        coverage = json.loads((output / "coverage.json").read_text())
        assert all(r["status"] == "AVAILABLE" for r in coverage["results"])
        assert all(
            any(i["code"] == "UNVERIFIED_ADJUSTMENT" for i in r["issues"])
            for r in coverage["results"]
        )
