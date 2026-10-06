"""Independent synthetic checkpoints and market-validation refusal proofs."""

import json
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from stock_research_ai.adapters.kite.source import KiteSource
from stock_research_ai.application import technicals
from stock_research_ai.bootstrap import ingest_market, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.market import MarketRequest
from stock_research_ai.domain.technicals import (
    PUBLIC_INDICATORS,
    TechnicalRequest,
    TechnicalSnapshot,
)

FIX = Path(__file__).parent / "fixtures"
INPUTS = json.loads((FIX / "technicals/inputs.json").read_text())
EXPECTED = json.loads((FIX / "technicals/expected.json").read_text())


async def acquire(settings, root, scenario, symbol="YESBANK", rows=None):
    records = rows or INPUTS[scenario]
    path = root / f"{scenario}-{symbol}.json"
    path.write_text(
        json.dumps(
            {
                "status": "success",
                "data": {"candles": [[r[0] + "T00:00:00+0530", *r[1:]] for r in records]},
            }
        )
    )

    def blocked(req):
        raise AssertionError("Network forbidden")

    return await ingest_market(
        settings,
        MarketRequest(
            symbol=symbol,
            exchange="NSE",
            kind="INDEX" if symbol.startswith("NIFTY") else "EQUITY",
            start=records[0][0],
            end=records[-1][0],
            expected_sessions=tuple(r[0] for r in records),
            evidence_kind="SYNTHETIC",
            master_file=FIX / "market/synthetic_instruments.csv",
            candles_file=path,
        ),
        root / f"{scenario}-{symbol}-evidence",
        KiteSource(transport=httpx.MockTransport(blocked)),
    )


@pytest.fixture
async def evidence(tmp_path):
    settings = Settings(database_path=tmp_path / "market.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await acquire(settings, tmp_path, "mixed")
    return settings, source


def request(source, names=PUBLIC_INDICATORS, **changes):
    return TechnicalRequest.model_validate(
        dict(
            extraction_id=source.id,
            instrument_id=source.instrument.id,
            start_date=source.request.start,
            end_date=source.request.end,
            indicators=names,
            **changes,
        )
    )


def assert_checkpoints(snapshot, scenario):
    series = {s.definition.indicator_id: s for s in snapshot.series}
    for golden in (e for e in EXPECTED if e["scenario"] == scenario):
        point = series[golden["indicator"]].points[golden["observation"] - 1]
        assert point.status == "AVAILABLE"
        with localcontext() as ctx:
            ctx.prec = 110
            error = abs(point.value - Decimal(golden["expected"]))
            assert error <= Decimal(golden["tolerance"]), (golden, point.value, error)
        assert point.date.isoformat() == golden["date"]
    for name, first in technicals.FIRST.items():
        s = series[name]
        assert s.status == "AVAILABLE"
        assert all(
            p.status == "INSUFFICIENT_INPUT" and p.value is None for p in s.points[: first - 1]
        )
        assert s.points[first - 1].status == "AVAILABLE"
        assert s.points[-1].date == snapshot.request.end_date


@pytest.mark.parametrize("scenario", list(INPUTS))
async def test_independent_checkpoints(tmp_path, scenario):
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await acquire(settings, tmp_path, scenario)
    snapshot = technicals.calculate(source, request(source))
    assert_checkpoints(snapshot, scenario)
    assert snapshot.adjustment_basis == "UNKNOWN"
    assert {"UNVERIFIED_ADJUSTMENT", "CORPORATE_ACTIONS_NOT_PROVIDED_BY_SOURCE"} <= {
        i.code for i in snapshot.issues
    }
    assert snapshot.corporate_action_coverage == "UNAVAILABLE"
    assert snapshot.corporate_action_reason == "NOT_PROVIDED_BY_SOURCE"
    assert TechnicalSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    if scenario in {"flat", "rising", "falling"}:
        s = next(s for s in snapshot.series if s.definition.indicator_id == "RSI14")
        assert all(
            p.value == {"flat": 50, "rising": 100, "falling": 0}[scenario] for p in s.points[14:]
        )
    if scenario == "flat":
        for name in ("TRUE_RANGE", "ATR14", "PLUS_DI14", "MINUS_DI14", "ADX14"):
            assert all(
                p.value == 0
                for p in next(
                    s for s in snapshot.series if s.definition.indicator_id == name
                ).points
                if p.status == "AVAILABLE"
            )


@pytest.mark.parametrize("name", PUBLIC_INDICATORS)
async def test_insufficient_terminal(evidence, name):
    _, source = evidence
    count = technicals.FIRST[name] - 1
    req = request(source, (name,)).model_copy(
        update={"end_date": source.observations[count - 1].trading_date}
    )
    snapshot = technicals.calculate(source, req)
    series = next(s for s in snapshot.series if s.requested)
    assert series.status == "INSUFFICIENT_INPUT"
    assert series.points[-1].date == req.end_date and series.points[-1].value is None


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
async def test_invalid_ohlc(evidence, field, value):
    _, source = evidence
    rows = list(source.observations)
    rows[25] = rows[25].model_copy(update={field: value})
    snapshot = technicals.calculate(
        source.model_copy(update={"observations": tuple(rows)}), request(source)
    )
    assert all(s.status == "INVALID_INPUT" for s in snapshot.series)
    assert not snapshot.inputs


@pytest.mark.parametrize(
    "change",
    [
        {"high": Decimal(1)},
        {"open": Decimal(500)},
        {"close": Decimal(500)},
        {"open": Decimal(1)},
        {"close": Decimal(1)},
    ],
)
async def test_inconsistent_candle(evidence, change):
    _, source = evidence
    rows = list(source.observations)
    rows[20] = rows[20].model_copy(update=change)
    snapshot = technicals.calculate(
        source.model_copy(update={"observations": tuple(rows)}), request(source)
    )
    assert all(s.status == "INVALID_INPUT" for s in snapshot.series)


@pytest.mark.parametrize(
    "case", ["duplicate", "unordered", "gap", "no_sessions", "stale", "instrument", "extraction"]
)
async def test_identity_order_sessions(evidence, case):
    _, source = evidence
    req = request(source)
    rows = list(source.observations)
    if case == "duplicate":
        rows.insert(20, rows[20])
    if case == "unordered":
        rows[20], rows[21] = rows[21], rows[20]
    if case == "gap":
        rows.pop(20)
    if case == "no_sessions":
        source = source.model_copy(
            update={"request": source.request.model_copy(update={"expected_sessions": None})}
        )
    if case == "stale":
        req = req.model_copy(update={"start_observation_id": uuid4()})
    if case == "instrument":
        req = req.model_copy(update={"instrument_id": uuid4()})
    if case == "extraction":
        with pytest.raises(ValueError):
            technicals.calculate(source, req.model_copy(update={"extraction_id": uuid4()}))
        return
    result = technicals.calculate(source.model_copy(update={"observations": tuple(rows)}), req)
    expected = (
        "AVAILABLE"
        if case == "no_sessions"
        else "INSUFFICIENT_INPUT"
        if case == "gap"
        else "INCOMPATIBLE_SERIES"
        if case in {"stale", "instrument"}
        else "INVALID_INPUT"
    )
    assert all(s.status == expected for s in result.series if s.requested)
    if case == "no_sessions":
        assert "OBSERVED_SESSIONS_ONLY" in [i.code for i in result.issues]


async def test_graph_is_resolvable_and_rejects_corruption(evidence):
    _, source = evidence
    snapshot = technicals.calculate(source, request(source))
    for mutation in ("point", "observation", "duplicate", "previous"):
        data = snapshot.model_dump()
        p = data["series"][0]["points"][20]
        if mutation == "point":
            p["dependent_point_ids"] = (uuid4(),)
        if mutation == "observation":
            p["observation_ids"] = (uuid4(),)
        if mutation == "duplicate":
            p["id"] = data["series"][0]["points"][19]["id"]
        if mutation == "previous":
            p["previous_point_id"] = data["series"][0]["points"][21]["id"]
        with pytest.raises(ValidationError):
            TechnicalSnapshot.model_validate(data)
    s = {s.definition.indicator_id: s for s in snapshot.series}
    assert len(s["EMA20"].points[19].seed_observation_ids) == 20
    assert s["EMA20"].points[20].previous_point_id == s["EMA20"].points[19].id
    assert len(s["MACD_SIGNAL9"].points[33].dependent_point_ids) == 9
    assert len(s["MACD_HISTOGRAM"].points[33].dependent_point_ids) == 2
    assert len(s["ADX14"].points[27].dependent_point_ids) == 14
    assert s["ADX14"].points[28].previous_point_id == s["ADX14"].points[27].id
    assert s["TRUE_RANGE"].points[0].value is None


async def test_derived_negative_lower_band_is_valid(evidence):
    _, source = evidence
    rows = tuple(
        o.model_copy(
            update={k: Decimal(10000 if i == 19 else 1) for k in ("open", "high", "low", "close")}
        )
        for i, o in enumerate(source.observations)
    )
    result = technicals.calculate(
        source.model_copy(update={"observations": rows}), request(source, ("BOLLINGER_LOWER20_2",))
    )
    series = next(s for s in result.series if s.requested)
    assert series.points[19].status == "AVAILABLE" and series.points[19].value < 0


@pytest.mark.parametrize("name", ["EMA10", "RSI2", "BUY", "MACD", "SUPERTREND"])
async def test_public_registry_fixed(evidence, name):
    _, source = evidence
    with pytest.raises(ValidationError):
        request(source, (name,))
