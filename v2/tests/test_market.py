import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from stock_research_ai.adapters.kite.source import KiteSource
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.bootstrap import ingest_market, market_engine
from stock_research_ai.domain.market import MarketExtractionResult, MarketObservation, MarketRequest

FIXTURES = Path(__file__).parent / "fixtures/market"
EXPECTED = json.loads((FIXTURES / "expected_synthetic.json").read_text())


def request(symbol="YESBANK", **updates):
    values = dict(
        symbol=symbol,
        exchange="NSE",
        kind=EXPECTED["instruments"][symbol]["kind"],
        start="2025-01-02",
        end="2025-01-03",
        evidence_kind="SYNTHETIC",
        master_file=FIXTURES / "synthetic_instruments.csv",
        candles_file=FIXTURES / "synthetic_candles.json",
        expected_sessions=["2025-01-02", "2025-01-03"],
    )
    return MarketRequest.model_validate({**values, **updates})


@pytest.mark.parametrize("symbol", list(EXPECTED["instruments"]))
async def test_synthetic_golden_end_to_end(symbol, market_settings, tmp_path):
    def no_network(req):
        raise AssertionError("Offline replay attempted HTTP")

    result = await ingest_market(
        market_settings,
        request(symbol),
        tmp_path / "export",
        KiteSource(transport=httpx.MockTransport(no_network)),
    )
    assert result.coverage.overall == "COMPLETE"
    assert result.request.evidence_kind == "SYNTHETIC"
    assert result.instrument.provider_identifier == EXPECTED["instruments"][symbol]["token"]
    assert result.instrument.price_unit == EXPECTED["instruments"][symbol]["unit"]
    assert result.instrument.provider == "kite" and result.instrument.exchange == "NSE"
    assert result.instrument.currency is None
    for obs, truth in zip(result.observations, EXPECTED["prices"], strict=True):
        assert obs.trading_date.isoformat() == truth["date"]
        for key in ("open", "high", "low", "close", "volume"):
            assert getattr(obs, key) == Decimal(truth[key])
        assert obs.adjustment_basis == "UNKNOWN" and obs.adjusted_close is None
        assert obs.provider_updated_at is None and obs.session_status == "OBSERVATION_PRESENT"
        assert obs.instrument_id == result.instrument.id
    assert result.coverage.corporate_actions == "NOT_PROVIDED_BY_SOURCE"
    assert result.corporate_actions == ()
    store = FileRawArtifactStore(market_settings.artifact_path)
    for artifact, fixture in zip(
        result.raw_artifacts, ("synthetic_instruments.csv", "synthetic_candles.json"), strict=True
    ):
        original = (FIXTURES / fixture).read_bytes()
        assert await store.read(artifact) == original
        assert artifact.content_sha256 == hashlib.sha256(original).hexdigest()
        assert artifact.retrieval_id in {r.id for r in result.retrievals}
    assert result.instrument.provenance.raw_artifact_id == result.raw_artifacts[0].id
    assert result.observations[0].provenance.raw_artifact_id == result.raw_artifacts[1].id
    assert result.observations[0].provenance.source_location.locator == "$.data.candles[0]"
    restored = MarketExtractionResult.model_validate_json(
        (tmp_path / "export/normalized/extraction.json").read_text()
    )
    assert restored == result
    async with market_engine(market_settings) as engine:
        assert await engine.get_market(result.id) == result
        assert await engine.get_market(uuid4()) is None
        assert await engine.market_history(result.instrument.id) == (result,)


async def test_immutable_history_replay_revision(market_settings, tmp_path):
    async with market_engine(market_settings) as engine:
        first = await engine.ingest_market_data(request())
        second = await engine.ingest_market_data(request())
        path = tmp_path / "revision.json"
        path.write_bytes(
            (FIXTURES / "synthetic_candles.json")
            .read_bytes()
            .replace(b"11.20,12345", b"11.30,12345")
        )
        third = await engine.ingest_market_data(request(candles_file=path))
        assert first.id != second.id != third.id
        assert first.instrument.id == third.instrument.id
        assert first.raw_artifacts[1].content_sha256 == second.raw_artifacts[1].content_sha256
        assert first.raw_artifacts[1].content_sha256 != third.raw_artifacts[1].content_sha256
        assert first.retrievals[1].id != second.retrievals[1].id
        assert first.observations[0].id == second.observations[0].id
        assert first.observations[0].close == Decimal("11.20")
        assert third.observations[0].close == Decimal("11.30")
        assert await engine.market_history(first.instrument.id) == (first, second, third)
    database = SQLiteDatabase(market_settings.database_path)
    try:
        with pytest.raises(IntegrityError):
            await database.market.save_market(first)
    finally:
        await database.close()


@pytest.mark.parametrize(
    "body,code",
    [
        (b"<html>login</html>", "MALFORMED_PAYLOAD"),
        (b'{"status":"error"}', "MALFORMED_PAYLOAD"),
        (b'{"status":"success","data":{"candles":[]}}', "NO_OBSERVATIONS"),
        (b'{"status":"success","data":{"candles":{}}}', "MALFORMED_PAYLOAD"),
    ],
)
async def test_invalid_payloads(body, code, market_settings, tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(body)
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request(candles_file=path))
    assert result.coverage.overall == "FAILED"
    assert code in {i.code for i in result.coverage.issues}
    assert result.raw_artifacts[1].content_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        {1: 100},
        {2: 1},
        {3: 100},
        {5: -1},
        {1: "10.1"},
        {1: True},
        {1: float("nan")},
        {1: float("inf")},
        {0: "2025-01-02"},
        {0: "2025-01-02T00:00:00+0000"},
        {0: "2025-01-02T12:00:00+0530"},
        {0: "2024-01-02T00:00:00+0530"},
    ],
)
async def test_bad_candles_not_repaired(change, market_settings, tmp_path):
    body = json.loads((FIXTURES / "synthetic_candles.json").read_text())
    for k, v in change.items():
        body["data"]["candles"][0][k] = v
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body))
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request(candles_file=path))
    assert result.coverage.overall == "PARTIAL"
    assert len(result.observations) == 1
    assert result.coverage.malformed_records == 1
    assert result.coverage.missing_dates == (date(2025, 1, 2),)
    assert result.coverage.missing_reason == "UNKNOWN"


async def test_duplicates_unordered_unknown_sessions(market_settings, tmp_path):
    body = json.loads((FIXTURES / "synthetic_candles.json").read_text())
    body["data"]["candles"].reverse()
    path = tmp_path / "unordered.json"
    path.write_text(json.dumps(body))
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request(candles_file=path, expected_sessions=None))
        assert result.coverage.overall == "PARTIAL"
        assert {i.code for i in result.coverage.issues} == {"UNORDERED", "SESSION_COVERAGE_UNKNOWN"}
        assert result.coverage.missing_dates == ()
        assert result.coverage.session_basis == "UNKNOWN"
        body["data"]["candles"].append(body["data"]["candles"][0])
        path.write_text(json.dumps(body))
        result = await engine.ingest_market_data(request(candles_file=path))
        assert result.coverage.duplicate_dates == (date(2025, 1, 3),)
        assert [o.trading_date for o in result.observations] == [date(2025, 1, 2)]


async def test_ambiguous_identity_preserved_no_candle_request(market_settings, tmp_path):
    path = tmp_path / "master.csv"
    original = (FIXTURES / "synthetic_instruments.csv").read_text()
    path.write_text(original + original.splitlines()[1] + "\n")
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request(master_file=path))
    assert result.coverage.overall == "FAILED" and result.instrument is None
    assert len(result.retrievals) == 1


async def test_lineage_and_domain_validation(market_settings):
    with pytest.raises(ValidationError):
        request(start="2025-02-01")
    with pytest.raises(ValidationError):
        request(candles_file=None)
    async with market_engine(market_settings) as engine:
        result = await engine.ingest_market_data(request())
    data = result.model_dump()
    data["raw_artifacts"] = ()
    with pytest.raises(ValidationError):
        MarketExtractionResult.model_validate(data)
    data = result.observations[0].model_dump()
    data["high"] = Decimal("NaN")
    with pytest.raises(ValidationError):
        MarketObservation.model_validate(data)
