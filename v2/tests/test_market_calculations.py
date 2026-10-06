"""Synthetic source inputs and independent output goldens stay separate."""

import json
from datetime import date
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from stock_research_ai.adapters.kite.source import KiteSource
from stock_research_ai.application.market_calculations import calculate_market
from stock_research_ai.bootstrap import ingest_market, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.market import MarketObservation, MarketRequest
from stock_research_ai.domain.market_calculations import MarketCalculationRequest

FIX = Path(__file__).parent / "fixtures"
INPUTS = json.loads((FIX / "market_calculations/inputs.json").read_text())
EXPECTED = json.loads((FIX / "market_calculations/expected.json").read_text())
METRICS = ["PRICE_RETURN", "MAX_DRAWDOWN", "REALIZED_VOLATILITY", "SMA"]


def request(source, metrics=METRICS, benchmark=None, **updates):
    return MarketCalculationRequest.model_validate(
        dict(
            extraction_id=source.id,
            instrument_id=source.instrument.id,
            start_date=source.request.start,
            end_date=source.request.end,
            benchmark_extraction_id=benchmark.id if benchmark else None,
            benchmark_instrument_id=benchmark.instrument.id if benchmark else None,
            calculations=[{"metric_id": m} for m in metrics],
            **updates,
        )
    )


async def acquire(settings, root, name, symbol, dates, closes):
    path = root / f"{name}-candles.json"
    # Mechanical REST envelope only; no expected calculation is produced here.
    path.write_text(
        json.dumps(
            {
                "status": "success",
                "data": {
                    "candles": [
                        [
                            f"{day}T00:00:00+0530",
                            int(price),
                            int(price),
                            int(price),
                            int(price),
                            100,
                        ]
                        for day, price in zip(dates, closes, strict=True)
                    ]
                },
            }
        )
    )

    def no_network(req):
        raise AssertionError("Offline replay attempted network")

    return await ingest_market(
        settings,
        MarketRequest.model_validate(
            dict(
                symbol=symbol,
                exchange="NSE",
                kind="INDEX" if symbol.startswith("NIFTY") else "EQUITY",
                start=dates[0],
                end=dates[-1],
                expected_sessions=dates,
                evidence_kind="SYNTHETIC",
                master_file=FIX / "market/synthetic_instruments.csv",
                candles_file=path,
            )
        ),
        root / name,
        KiteSource(transport=httpx.MockTransport(no_network)),
    )


@pytest.fixture
async def evidence(tmp_path):
    settings = Settings(database_path=tmp_path / "market.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    sources = {}
    for symbol in ("YESBANK", "INFY", "NIFTY 50", "NIFTY BANK"):
        sources[symbol] = await acquire(
            settings, tmp_path, symbol, symbol, INPUTS["dates"], INPUTS[symbol]
        )
    sources["CAGR"] = await acquire(
        settings, tmp_path, "CAGR", "YESBANK", INPUTS["cagr"]["dates"], INPUTS["cagr"]["closes"]
    )
    return settings, sources


def verify_golden(result, expected):
    assert result.status == "AVAILABLE", result.issues
    if expected["comparison"] == "EXACT":
        assert result.value == Decimal(expected["value"])
    else:
        assert abs(result.value - Decimal(expected["value"])) <= Decimal(expected["tolerance"])
    assert result.display_value == Decimal(expected["display"])
    assert result.adjustment_basis == "UNKNOWN"
    assert result.corporate_action_coverage == "UNAVAILABLE"
    assert result.corporate_action_reason == "NOT_PROVIDED_BY_SOURCE"
    assert any(
        i.code == "UNVERIFIED_ADJUSTMENT" and i.severity == "LIMITATION" for i in result.issues
    )
    assert not any(i.severity == "ERROR" for i in result.issues)


@pytest.mark.parametrize("symbol", ["YESBANK", "INFY", "CAGR"])
async def test_golden(evidence, symbol):
    _, sources = evidence
    source = sources[symbol]
    metrics = (
        ["PRICE_CAGR"]
        if symbol == "CAGR"
        else ["PRICE_RETURN", "SMA"]
        if symbol == "INFY"
        else METRICS
    )
    result = calculate_market(source, request(source, metrics))
    for r in result.results:
        verify_golden(r, EXPECTED["INFY" if symbol == "INFY" else "YESBANK"][r.metric_id])
        assert all(i.observation in source.observations for i in r.inputs)
        assert all(i.extraction_id == source.id for i in r.inputs)
        assert r.snapshot_id == result.id and r.classification == "CALCULATION"
        assert r.formula.version == "1" and r.formula.precision == 50
        assert r.alignment_policy == "EXACT_DATES_ONLY_V1"
        if r.metric_id == "PRICE_CAGR":
            assert r.elapsed_days == 730 and r.elapsed_years == Decimal("2")
            assert r.annualization_policy == "ACTUAL_DAYS_365_FIXED_V1"
        if r.metric_id == "REALIZED_VOLATILITY":
            assert (r.observation_count, r.return_count, r.annualization_factor) == (20, 19, 252)
            assert r.return_definition == "ARITHMETIC_SIMPLE_RETURN"
            assert r.variance_definition == "SAMPLE_VARIANCE"
            assert r.annualization_policy == "ARITHMETIC_SAMPLE_252_V1"
        if r.metric_id == "MAX_DRAWDOWN":
            golden = EXPECTED["YESBANK"]["MAX_DRAWDOWN"]
            for key in ("peak", "trough", "recovery"):
                assert str(getattr(r.episode, f"{key}_date")) == golden[f"{key}_date"]
                assert getattr(r.episode, f"{key}_value") == Decimal(golden[f"{key}_value"])
                assert any(
                    i.observation.id == getattr(r.episode, f"{key}_observation_id")
                    for i in r.inputs
                )


@pytest.mark.parametrize("symbol", ["NIFTY 50", "NIFTY BANK"])
async def test_benchmark_golden(evidence, symbol):
    _, sources = evidence
    source, benchmark = sources["YESBANK"], sources[symbol]
    result = calculate_market(
        source, request(source, ["BENCHMARK_RELATIVE_RETURN"], benchmark), benchmark
    ).results[0]
    truth = EXPECTED[symbol]
    assert result.status == "AVAILABLE"
    assert result.unit == "PERCENTAGE_POINTS" and result.value == Decimal(truth["relative_return"])
    assert result.company_price_return == Decimal(truth["company_return"])
    assert result.benchmark_price_return == Decimal(truth["benchmark_return"])
    assert result.alignment_policy == "EXACT_SHARED_ENDPOINTS_V1"
    assert len(result.inputs) == 4
    assert [i.observation.trading_date for i in result.inputs[:2]] == [
        i.observation.trading_date for i in result.inputs[2:]
    ]
    assert {s.extraction_id for s in result.sources} == {source.id, benchmark.id}
    assert len({s.instrument.id for s in result.sources}) == 2


@pytest.mark.parametrize("metric", METRICS + ["PRICE_CAGR", "BENCHMARK_RELATIVE_RETURN"])
@pytest.mark.parametrize("bad", ["0", "-1"])
async def test_nonpositive_prices(evidence, metric, bad):
    _, sources = evidence
    source, benchmark = sources["YESBANK"], sources["NIFTY 50"]
    first = source.observations[0].model_copy(update={"close": Decimal(bad)})
    source = source.model_copy(update={"observations": (first, *source.observations[1:])})
    r = calculate_market(source, request(source, [metric], benchmark), benchmark).results[0]
    assert r.status == "INVALID_INPUT" and r.value is None
    assert any(first.id in i.observation_ids for i in r.issues if i.severity == "ERROR")


@pytest.mark.parametrize(
    "case,status",
    [
        ("reverse", "INVALID_PERIOD"),
        ("same_return", "INVALID_PERIOD"),
        ("same_cagr", "INVALID_PERIOD"),
        ("missing_endpoint", "INSUFFICIENT_INPUT"),
        ("one", "INSUFFICIENT_INPUT"),
        ("sma_short", "INSUFFICIENT_INPUT"),
        ("vol_short", "INSUFFICIENT_INPUT"),
        ("duplicate", "INVALID_INPUT"),
        ("duplicate_changed", "INVALID_INPUT"),
        ("unsorted", "INVALID_INPUT"),
        ("outside_id", "INVALID_INPUT"),
        ("missing_session", "INSUFFICIENT_INPUT"),
        ("unknown_sessions", "AVAILABLE"),
        ("partial", "AVAILABLE"),
    ],
)
async def test_edges(evidence, case, status):
    _, sources = evidence
    source = sources["YESBANK"]
    req = request(source, ["PRICE_RETURN"])
    if case == "reverse":
        req = req.model_copy(update={"start_date": req.end_date, "end_date": req.start_date})
    if case in {"same_return", "same_cagr"}:
        req = request(source, ["PRICE_CAGR" if case == "same_cagr" else "PRICE_RETURN"])
        req = req.model_copy(update={"end_date": req.start_date})
    if case == "missing_endpoint":
        req = req.model_copy(update={"start_date": date(2025, 1, 1)})
    if case == "outside_id":
        req = req.model_copy(update={"start_observation_id": uuid4()})
    if case in {"duplicate", "duplicate_changed"}:
        duplicate = source.observations[0]
        if case == "duplicate_changed":
            duplicate = duplicate.model_copy(update={"id": uuid4(), "close": Decimal("101")})
        source = source.model_copy(update={"observations": (duplicate, *source.observations)})
    if case == "unsorted":
        source = source.model_copy(update={"observations": tuple(reversed(source.observations))})
    if case in {"one", "sma_short", "vol_short"}:
        rows = source.observations[: 1 if case == "one" else 2]
        source = source.model_copy(update={"observations": rows})
        req = request(
            source,
            [
                "MAX_DRAWDOWN"
                if case == "one"
                else "SMA"
                if case == "sma_short"
                else "REALIZED_VOLATILITY"
            ],
        )
        req = req.model_copy(update={"end_date": rows[-1].trading_date})
    if case in {"missing_session", "unknown_sessions", "partial"}:
        req = request(source, ["REALIZED_VOLATILITY"])
        source = source.model_copy(
            update={"coverage": source.coverage.model_copy(update={"overall": "PARTIAL"})}
        )
        if case == "missing_session":
            source = source.model_copy(
                update={"observations": (source.observations[0], *source.observations[2:])}
            )
        if case == "unknown_sessions":
            source = source.model_copy(
                update={"request": source.request.model_copy(update={"expected_sessions": None})}
            )
    r = calculate_market(source, req).results[0]
    assert r.status == status, r.issues
    if case == "unknown_sessions":
        assert any(i.code == "OBSERVED_SESSIONS_ONLY" for i in r.issues)
    if case == "partial":
        assert any(i.code == "PARTIAL_SOURCE_COVERAGE" for i in r.issues)
    if status != "AVAILABLE":
        assert r.value is None


@pytest.mark.parametrize(
    "case", ["missing_date", "nonoverlap", "same_identity", "wrong_instrument"]
)
async def test_benchmark_failures(evidence, case):
    _, sources = evidence
    source, benchmark = sources["YESBANK"], sources["NIFTY 50"]
    req = request(source, ["BENCHMARK_RELATIVE_RETURN"], benchmark)
    if case == "missing_date":
        benchmark = benchmark.model_copy(update={"observations": benchmark.observations[1:]})
    if case == "nonoverlap":
        benchmark = benchmark.model_copy(update={"observations": ()})
    if case == "same_identity":
        benchmark = source
        req = request(source, ["BENCHMARK_RELATIVE_RETURN"], source)
    if case == "wrong_instrument":
        benchmark = benchmark.model_copy(update={"instrument": sources["INFY"].instrument})
    r = calculate_market(source, req, benchmark).results[0]
    assert r.status == "INCOMPATIBLE_SERIES" and r.value is None


@pytest.mark.parametrize("name", ["constant", "rising", "falling"])
async def test_special_series(evidence, tmp_path, name):
    settings, _ = evidence
    source = await acquire(settings, tmp_path, name, "YESBANK", INPUTS["dates"][:4], INPUTS[name])
    r = calculate_market(source, request(source, ["MAX_DRAWDOWN", "REALIZED_VOLATILITY"]))
    assert r.results[0].value == Decimal(EXPECTED["special"][f"{name}_drawdown"])
    if name == "falling":
        assert r.results[0].episode.recovery_date is None
        assert any(i.code == "NOT_RECOVERED_WITHIN_WINDOW" for i in r.results[0].issues)
    else:
        assert r.results[0].episode is None
    if name == "constant":
        assert r.results[1].value == Decimal("0")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
async def test_nonfinite_domain_rejected(evidence, bad):
    _, sources = evidence
    data = sources["YESBANK"].observations[0].model_dump()
    data["close"] = Decimal(bad)
    with pytest.raises(ValidationError):
        MarketObservation.model_validate(data)


async def test_ambient_context_independence(evidence):
    _, sources = evidence
    source = sources["YESBANK"]
    with localcontext() as context:
        context.prec = 6
        context.traps[Inexact] = True
        r = calculate_market(source, request(source, ["REALIZED_VOLATILITY"])).results[0]
    verify_golden(r, EXPECTED["YESBANK"]["REALIZED_VOLATILITY"])
