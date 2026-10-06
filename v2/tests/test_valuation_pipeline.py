"""Temporary persistence, price pinning, revisions and real command-line integration."""

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal, Inexact, localcontext
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from test_technicals import acquire as market_acquire
from test_valuation import METHODS, acquire, assumption, evaluate, evidence, request, values

from stock_research_ai.adapters.screener.semantic_mappings import ScreenerSemanticResolver
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import valuation
from stock_research_ai.bootstrap import evaluate_valuation
from stock_research_ai.domain.valuation import (
    ValuationPrecision,
    ValuationRequest,
    ValuationSnapshot,
)

__all__ = ["evidence"]


async def test_engine_export_sha_history(evidence, tmp_path):
    settings, source = evidence
    req = request(source, METHODS[2])
    snapshot = await evaluate_valuation(settings, req, tmp_path / "export")
    assert values(snapshot.results[0])["scenario_value_per_share"] == 100
    assert snapshot.sources[0].payload_sha256 == valuation.digest(source)
    db = SQLiteDatabase(settings.database_path)
    try:
        assert await db.valuations.valuation_history(req.company_id) == (snapshot,)
        with pytest.raises(IntegrityError):
            await db.valuations.save_valuation(snapshot)
        damaged = snapshot.model_copy(
            update={
                "id": uuid4(),
                "sources": (snapshot.sources[0].model_copy(update={"payload_sha256": "0" * 64}),),
            }
        )
        with pytest.raises(ValueError):
            await db.valuations.save_valuation(damaged)
    finally:
        await db.close()
    for name in ("valuation_snapshot", "multiples", "scenarios", "reverse_expectations"):
        assert (tmp_path / f"export/normalized/{name}.json").exists()
    reconstructed = ValuationSnapshot.model_validate_json(
        (tmp_path / "export/normalized/valuation_snapshot.json").read_text()
    )
    assert reconstructed == snapshot
    before = source.model_dump_json()
    with sqlite3.connect(settings.database_path) as sql:
        count = sql.execute("SELECT count(*) FROM valuation_snapshots").fetchone()[0]
    assert count == 1
    assert source.model_dump_json() == before


async def test_raw_corruption_refuses_before_append(evidence, tmp_path):
    settings, source = evidence
    req = request(source, METHODS[0])
    await evaluate_valuation(settings, req, tmp_path / "good")
    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await evaluate_valuation(settings, req, tmp_path / "bad")
    db = SQLiteDatabase(settings.database_path)
    try:
        assert len(await db.valuations.valuation_history(req.company_id)) == 1
    finally:
        await db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("wacc", "0.11"),
        ("terminal_growth", "0.01"),
        ("fcff_1", "120"),
        ("net_debt", "190"),
        ("diluted_shares", "11"),
        ("required_return", "0.11"),
        ("exit_pe", "21"),
    ],
)
async def test_assumption_revision_history(evidence, field, value):
    settings, source = evidence
    method = METHODS[4] if field in {"required_return", "exit_pe"} else METHODS[2]
    first = evaluate(source, request(source, method))
    immutable = first.model_dump_json()
    second = evaluate(source, request(source, method, {field: value}))
    assert first.id != second.id
    assert values(first.results[0]) != values(second.results[0])
    db = SQLiteDatabase(settings.database_path)
    try:
        await db.valuations.save_valuation(first)
        await db.valuations.save_valuation(second)
        history = await db.valuations.valuation_history(first.request.company_id)
        assert history[0].model_dump_json() == immutable and history[1] == second
    finally:
        await db.close()


async def test_policy_and_source_revisions(evidence, tmp_path):
    settings, source = evidence
    req = request(source, METHODS[3], {"lower_growth": "-0.5", "upper_growth": "0.5"})
    original = evaluate(source, req)
    before = original.model_dump_json()
    specs = tuple(
        d.model_copy(update={"version": 2, "formula_version": 2}) for d in valuation.DEFINITIONS
    )
    method = evaluate(source, req, definitions=specs)
    precision = evaluate(source, req, precision=ValuationPrecision(version=2, working_digits=80))
    data = req.model_dump()
    data["selections"][0]["scenario"]["solver"]["version"] = 2
    data["selections"][0]["scenario"]["solver"]["growth_tolerance"] = Decimal("1e-32")
    solver = evaluate(source, ValuationRequest.model_validate(data))
    revision = await acquire(settings, tmp_path / "revision")
    newer = evaluate(revision, request(revision, METHODS[3]))
    db = SQLiteDatabase(settings.database_path)
    try:
        for snapshot in (original, method, precision, solver, newer):
            await db.valuations.save_valuation(snapshot)
        history = await db.valuations.valuation_history(req.company_id)
        assert len(history) == 5 and history[0].model_dump_json() == before
        assert history[-1].request.financial_extraction_id != req.financial_extraction_id
        # Pinning the original extraction still reconstructs its original source hash.
        again = await evaluate_valuation(settings, req, tmp_path / "pinned-original")
        assert again.sources[0].payload_sha256 == original.sources[0].payload_sha256
    finally:
        await db.close()


@pytest.mark.parametrize("case", ["valid", "date", "instrument", "currency", "negative", "zero"])
async def test_pinned_market_and_temporal_discipline(evidence, tmp_path, case):
    settings, source = evidence
    market = await market_acquire(
        settings,
        tmp_path,
        "valuation-market",
        "INFY",
        rows=[["2025-01-02", 100, 101, 99, 100, 1], ["2025-01-03", 105, 106, 104, 105, 1]],
    )
    # Explicit synthetic contract assertion: currency is known for this unit test only.
    # Current retained provider snapshots with currency=None must refuse below.
    if case != "currency":
        market = market.model_copy(
            update={"instrument": market.instrument.model_copy(update={"currency": "INR"})}
        )
    observation = market.observations[0]
    if case in {"negative", "zero"}:
        observation = observation.model_copy(
            update={
                k: Decimal(-1 if case == "negative" else 0)
                for k in ("open", "high", "low", "close")
            }
        )
        market = market.model_copy(update={"observations": (observation, *market.observations[1:])})
    data = request(source, METHODS[0]).model_dump()
    data["selections"][0]["price"] = dict(
        kind="MARKET",
        snapshot_id=market.id,
        instrument_id=uuid4() if case == "instrument" else market.instrument.id,
        observation_id=observation.id,
        observation_date=market.observations[1].trading_date
        if case == "date"
        else observation.trading_date,
    )
    req = ValuationRequest.model_validate(data)
    if case in {"date", "instrument"}:
        with pytest.raises(ValueError):
            valuation.Evidence(req, source, {market.id: market}, {}, ScreenerSemanticResolver())
        return
    e = valuation.Evidence(req, source, {market.id: market}, {}, ScreenerSemanticResolver())
    snapshot = valuation.evaluate(req, e)
    result = snapshot.results[0]
    if case == "currency":
        assert result.status == "UNVERIFIED_METRIC_SEMANTICS"
    elif case in {"negative", "zero"}:
        assert result.status == "INVALID_INPUT"
    else:
        assert result.status == "AVAILABLE" and values(result)["multiple"] == 20
        assert "POINT_IN_TIME_AVAILABILITY_UNVERIFIED" in [i.code for i in result.issues]
        assert result.inputs[0].market_observation.trading_date.isoformat() == "2025-01-02"
        assert snapshot.request.as_of_policy == "CURRENT_PINNED_SNAPSHOT_V1"
        changed = data.copy()
        changed["selections"] = [dict(data["selections"][0])]
        changed["selections"][0]["price"] = dict(
            data["selections"][0]["price"],
            observation_id=market.observations[1].id,
            observation_date=market.observations[1].trading_date,
        )
        req2 = ValuationRequest.model_validate(changed)
        second = valuation.evaluate(
            req2,
            valuation.Evidence(req2, source, {market.id: market}, {}, ScreenerSemanticResolver()),
        )
        assert second.id != snapshot.id and values(second.results[0])["multiple"] == 21
        assert values(snapshot.results[0])["multiple"] == 20


@pytest.mark.parametrize("bad", [False, True])
async def test_production_cli(evidence, tmp_path, bad):
    settings, source = evidence
    req = request(source, METHODS[2], bank=bad)
    path = tmp_path / "request.json"
    path.write_text(req.model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_research_ai",
            "--database",
            str(settings.database_path),
            "evaluate-valuation",
            "--request",
            str(path),
            "--output",
            str(tmp_path / "cli"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (3 if bad else 0), result.stderr
    snapshot = ValuationSnapshot.model_validate_json(
        (tmp_path / "cli/normalized/valuation_snapshot.json").read_text()
    )
    assert snapshot.results[0].status == ("NOT_APPLICABLE" if bad else "AVAILABLE")
    assert (
        json.loads((tmp_path / "cli/coverage.json").read_text())["methods"][0]["status"]
        == snapshot.results[0].status
    )


@pytest.mark.parametrize(
    "case", ["assumption_id", "result_id", "fact_value", "fact_classification", "mapping"]
)
async def test_lineage_corruption_rejected(evidence, case):
    _, source = evidence
    snapshot = evaluate(source, request(source, METHODS[4]))
    data = snapshot.model_dump()
    r = data["results"][0]
    if case == "assumption_id":
        r["assumption_ids"] = (uuid4(),)
    if case == "result_id":
        r["snapshot_id"] = uuid4()
    if case == "fact_value":
        r["inputs"][0]["value"] = Decimal(999)
    if case == "fact_classification":
        r["inputs"][0]["classification"] = "CALCULATION"
    if case == "mapping":
        r["inputs"][0]["mapping"]["observation_id"] = uuid4()
    with pytest.raises(ValidationError):
        ValuationSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_year",
        "gap",
        "order",
        "missing_wacc",
        "unit",
        "currency",
        "source_assumption",
        "extra_assumption",
    ],
)
async def test_scenario_shape_and_provenance(evidence, case):
    _, source = evidence
    data = request(source, METHODS[2]).model_dump()
    a = list(data["selections"][0]["scenario"]["assumptions"])
    if case == "duplicate_year":
        a.append(dict(a[0], assumption_id=uuid4()))
    if case == "gap":
        a = [x for x in a if x["semantic"] != "fcff_1"]
    if case == "order":
        a[0], a[1] = a[1], a[0]
    if case == "missing_wacc":
        a = [x for x in a if x["semantic"] != "wacc"]
    if case == "unit":
        a[0]["unit"] = "CURRENCY_PER_SHARE"
    if case == "currency":
        a[0]["currency"] = "USD"
    if case == "extra_assumption":
        a.append(assumption("random_growth", "0.1"))
    if case == "source_assumption":
        a[0]["provenance_basis"] = "SOURCE_EVIDENCE"
        a[0]["evidence"] = request(source, METHODS[0]).selections[0].denominator.model_dump()
    data["selections"][0]["scenario"]["assumptions"] = a
    if case == "duplicate_year":
        with pytest.raises(ValidationError):
            ValuationRequest.model_validate(data)
    else:
        r = evaluate(source, ValuationRequest.model_validate(data)).results[0]
        assert r.status != "AVAILABLE" and not r.values


async def test_ambient_precision_independent(evidence):
    _, source = evidence
    req = request(source, METHODS[3], {"lower_growth": "-0.5", "upper_growth": "0.5"})
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        r = evaluate(source, req).results[0]
    assert r.status == "AVAILABLE"
    assert abs(values(r)["implied_growth"] - 10) < Decimal("1e-28")
    assert abs(r.solver.final_residual) <= Decimal("1e-30")
    assert r.solver.final_upper - r.solver.final_lower <= Decimal("1e-30")


async def test_market_pin_revision_persists_refusals_unchanged(evidence, tmp_path):
    settings, source = evidence
    market = await market_acquire(
        settings,
        tmp_path,
        "market-revisions",
        "INFY",
        rows=[["2025-01-02", 100, 101, 99, 100, 1], ["2025-01-03", 105, 106, 104, 105, 1]],
    )
    snapshots = []
    for i, observation in enumerate(market.observations):
        data = request(source, METHODS[0]).model_dump()
        data["selections"][0]["price"] = dict(
            kind="MARKET",
            snapshot_id=market.id,
            instrument_id=market.instrument.id,
            observation_id=observation.id,
            observation_date=observation.trading_date,
        )
        snapshots.append(
            await evaluate_valuation(
                settings, ValuationRequest.model_validate(data), tmp_path / f"market-valuation-{i}"
            )
        )
    # The current adapter retains unknown currency: never silently infer INR to make a proof pass.
    assert all(s.results[0].status == "UNVERIFIED_METRIC_SEMANTICS" for s in snapshots)
    assert snapshots[0].id != snapshots[1].id
    before = snapshots[0].model_dump_json()
    db = SQLiteDatabase(settings.database_path)
    try:
        history = await db.valuations.valuation_history(source.company.company_id)
        assert history == tuple(snapshots)
        assert history[0].model_dump_json() == before
    finally:
        await db.close()
