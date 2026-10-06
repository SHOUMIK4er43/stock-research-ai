"""Offline engine/SQLite/CLI compatibility and immutable revision proofs."""

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal, Inexact, localcontext

import pytest
from sqlalchemy.exc import IntegrityError
from test_market_calculations import request as scalar_request
from test_technicals import FIX, INPUTS, acquire, assert_checkpoints, evidence, request

from stock_research_ai.adapters.sqlite import SQLiteDatabase, decode_market_calculation
from stock_research_ai.application import technicals
from stock_research_ai.application.market_calculations import calculate_market
from stock_research_ai.bootstrap import calculate_technicals, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.technicals import PrecisionPolicy, TechnicalSnapshot

__all__ = ["evidence"]


@pytest.mark.parametrize("symbol", ["YESBANK", "INFY", "NIFTY 50", "NIFTY BANK"])
async def test_four_instruments_engine_history_export(tmp_path, symbol):
    settings = Settings(database_path=tmp_path / "market.db", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await acquire(settings, tmp_path, "mixed", symbol)
    result = await calculate_technicals(settings, request(source), tmp_path / "export")
    assert_checkpoints(result, "mixed")
    assert result.source.extraction_id == source.id
    price = "INDEX_POINTS" if symbol.startswith("NIFTY") else "PRICE"
    for series in result.series:
        if series.definition.indicator_id in {"RSI14", "PLUS_DI14", "MINUS_DI14", "ADX14"}:
            assert series.unit == "DIMENSIONLESS_0_100"
        elif series.definition.indicator_id == "EMA20":
            assert series.unit == price
    database = SQLiteDatabase(settings.database_path)
    try:
        assert await database.market_calculations.technical_history(source.id) == (result,)
        assert await database.market_calculations.market_calculation_history(source.id) == ()
        with pytest.raises(IntegrityError):
            await database.market_calculations.save_technical(result)
        assert await database.market_calculations.technical_history(source.id) == (result,)
    finally:
        await database.close()
    exported = tmp_path / "export/normalized"
    assert (
        TechnicalSnapshot.model_validate_json((exported / "calculation_snapshot.json").read_text())
        == result
    )
    assert (exported / "indicator_series.json").exists()
    latest = json.loads((exported / "latest.json").read_text())
    assert latest["requested_terminal_date"] == source.request.end.isoformat()
    assert (tmp_path / "export/coverage.json").exists()


async def test_legacy_and_new_scalar_history_stays_typed(evidence):
    settings, source = evidence
    # Real legacy serialized fixture, never rewritten or migrated.
    legacy = json.loads((FIX / "technicals/legacy_4b_snapshot.json").read_text())
    assert isinstance(decode_market_calculation(legacy), MarketCalculationSnapshot)
    scalar = calculate_market(source, scalar_request(source, ["SMA"]))
    raw_legacy = scalar.model_dump_json()
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "INSERT INTO market_calculation_snapshots VALUES (?, ?, ?, ?, ?)",
            (str(scalar.id), str(source.id), None, scalar.calculated_at.isoformat(), raw_legacy),
        )
    repository = SQLiteDatabase(settings.database_path)
    try:
        newer = calculate_market(source, scalar_request(source, ["SMA"]))
        await repository.market_calculations.save_market_calculation(newer)
        technical = technicals.calculate(source, request(source))
        await repository.market_calculations.save_technical(technical)
        history = await repository.market_calculations.market_calculation_history(source.id)
        assert set(s.id for s in history) == {scalar.id, newer.id}
        assert all(isinstance(s, MarketCalculationSnapshot) for s in history)
        assert await repository.market_calculations.technical_history(source.id) == (technical,)
    finally:
        await repository.close()
    with sqlite3.connect(settings.database_path) as db:
        assert (
            db.execute(
                "SELECT payload FROM market_calculation_snapshots WHERE id=?", (str(scalar.id),)
            ).fetchone()[0]
            == raw_legacy
        )
    for invalid in ({"payload_type": "unknown", "payload_version": 1}, {"payload_version": 2}):
        with pytest.raises(ValueError):
            decode_market_calculation(invalid)


async def test_immutable_definition_parameter_precision_range_source_revisions(evidence, tmp_path):
    settings, source = evidence
    req = request(source, ("EMA20",))
    original = technicals.calculate(source, req)
    original_json = original.model_dump_json()
    definition = next(d for d in technicals.REGISTRY if d.indicator_id == "EMA20")
    # Private injection simulates future definitions; public requests remain fixed V1.
    revised_definition = definition.model_copy(update={"version": 2, "formula_version": 2})
    parameter = definition.model_copy(
        update={"version": 3, "parameters": (("period", "21"),), "first_observation": 21}
    )
    variants = [
        original,
        technicals.calculate(source, req, registry=(revised_definition,)),
        technicals.calculate(source, req, registry=(parameter,)),
        technicals.calculate(source, req, precision=PrecisionPolicy(version=2, working_digits=80)),
        technicals.calculate(
            source, req.model_copy(update={"start_date": source.observations[1].trading_date})
        ),
    ]
    changed = [r.copy() for r in INPUTS["mixed"]]
    changed[-1][4] += 1  # Within the original high/low; new raw evidence and extraction.
    revised = await acquire(settings, tmp_path, "revision", rows=changed)
    variants.append(technicals.calculate(revised, request(revised, ("EMA20",))))
    assert variants[2].series[0].points[19].value is None
    assert variants[2].series[0].points[20].value is not None
    assert variants[4].series[0].points[19].date == source.observations[20].trading_date
    assert variants[5].series[0].points[-1].value != original.series[0].points[-1].value
    assert len({s.id for s in variants}) == len(variants)
    database = SQLiteDatabase(settings.database_path)
    try:
        for snapshot in variants:
            await database.market_calculations.save_technical(snapshot)
        history = await database.market_calculations.technical_history(source.id)
        assert {s.id for s in history} == {s.id for s in variants[:-1]}
        assert next(s for s in history if s.id == original.id).model_dump_json() == original_json
        assert await database.market_calculations.technical_history(revised.id) == (variants[-1],)
    finally:
        await database.close()


async def test_raw_sha_refuses_tampering_without_append(evidence, tmp_path):
    settings, source = evidence
    await calculate_technicals(settings, request(source), tmp_path / "valid")
    files = [p for p in settings.artifact_path.rglob("*") if p.is_file()]
    assert files
    for path in files:
        path.write_bytes(b"corrupted evidence")
    with pytest.raises(ValueError):
        await calculate_technicals(settings, request(source), tmp_path / "tampered")
    database = SQLiteDatabase(settings.database_path)
    try:
        assert len(await database.market_calculations.technical_history(source.id)) == 1
    finally:
        await database.close()


@pytest.mark.parametrize("short", [False, True])
async def test_real_cli_terminal_semantics(evidence, tmp_path, short):
    settings, source = evidence
    req = request(source, ("MACD_SIGNAL9",))
    if short:
        req = req.model_copy(update={"end_date": source.observations[25].trading_date})
    path = tmp_path / "request.json"
    path.write_text(req.model_dump_json())
    output = tmp_path / "cli"
    env = dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path))
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_research_ai",
            "--database",
            str(settings.database_path),
            "calculate-technicals",
            "--request",
            str(path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == (3 if short else 0), run.stderr
    latest = json.loads((output / "normalized/latest.json").read_text())
    assert latest["requested_terminal_date"] == req.end_date.isoformat()
    result = TechnicalSnapshot.model_validate_json(
        (output / "normalized/calculation_snapshot.json").read_text()
    )
    series = next(s for s in result.series if s.requested)
    assert series.points[-1].date == req.end_date
    assert (series.points[-1].value is None) == short


async def test_working_precision_and_bounded_point_state(evidence):
    _, source = evidence
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        result = technicals.calculate(source, request(source))
    assert_checkpoints(result, "mixed")
    more = technicals.calculate(source, request(source), PrecisionPolicy(working_digits=80))
    for series, higher in zip(result.series, more.series, strict=True):
        for point, other in zip(series.points, higher.points, strict=True):
            if point.value is not None:
                assert isinstance(point.value, Decimal)
                assert len(point.value.as_tuple().digits) <= 50
                with localcontext() as ctx:
                    ctx.prec = 110
                    assert abs(point.value - other.value) < Decimal("1e-45")
            if point.previous_point_id:
                assert len(point.observation_ids) + len(point.dependent_point_ids) <= 1
                assert not point.seed_observation_ids


def test_long_history_keeps_decimal_working_state():
    from datetime import date, timedelta
    from decimal import Context
    from uuid import uuid4

    from stock_research_ai.domain.market import MarketObservation
    from stock_research_ai.domain.technicals import TechnicalRequest

    # Builder-only stress proof: controlled observations avoid acquisition and database work.
    instrument = uuid4()
    rows = []
    for i in range(4000):
        # Copy a validated fixture observation below is unnecessary for arithmetic-only builder:
        # model_construct deliberately omits provenance; engine tests cover validated boundaries.
        close = Decimal(100 + i % 37)
        rows.append(
            MarketObservation.model_construct(
                id=uuid4(),
                instrument_id=instrument,
                trading_date=date(2000, 1, 1) + timedelta(days=i),
                open=close,
                high=close + 2,
                low=close - 1,
                close=close,
            )
        )
    req = TechnicalRequest(
        extraction_id=uuid4(),
        instrument_id=instrument,
        start_date=rows[0].trading_date,
        end_date=rows[-1].trading_date,
        indicators=("EMA20", "ADX14"),
    )
    with localcontext(Context(prec=70)):
        builder = technicals.Builder(rows, req, "PRICE", "INR", technicals.REGISTRY)
        builder.ensure("EMA20")
        builder.ensure("ADX14")
    state = builder.values["EMA20"][-1]
    assert isinstance(state, Decimal) and len(state.as_tuple().digits) == 70
    stored = builder.series["EMA20"].points[-1].value
    assert state != stored and len(stored.as_tuple().digits) == 50
    assert builder.series["ADX14"].points[-1].status == "AVAILABLE"
    assert all(isinstance(v, Decimal) for a in builder.values.values() for v in a if v is not None)
