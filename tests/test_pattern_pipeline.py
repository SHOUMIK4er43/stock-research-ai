"""Pattern persistence, reference validation, revision isolation and real offline CLI."""

import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from test_calculations import evidence as evidence
from test_patterns import assert_golden, request

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.semantic_mappings import MAPPINGS, ScreenerSemanticResolver
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import patterns
from stock_research_ai.application.engine import (
    CalculationServices,
    ResearchEngine,
    SectorKPIServices,
)
from stock_research_ai.bootstrap import extract_snapshot
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.patterns import PatternSnapshot, PinnedCalculation


@pytest.fixture
async def context(evidence):
    settings, sources = evidence
    db = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(settings.artifact_path)
    engine = ResearchEngine(
        db.repository,
        calculations=CalculationServices(db.extractions, db.calculations, store),
        sector_kpis=SectorKPIServices(
            db.extractions, db.calculations, ScreenerSemanticResolver(), store
        ),
        patterns=db.patterns,
    )
    try:
        yield settings, sources, db, engine
    finally:
        await db.close()


def pinned(req, snapshot):
    refs = {r.role: r for f in snapshot.findings for r in f.calculations}
    return req.model_copy(
        update={
            "calculation_mode": "PINNED",
            "pinned_calculations": tuple(
                PinnedCalculation(role=r.role, snapshot_id=r.snapshot_id, metric_id=r.metric_id)
                for r in refs.values()
            ),
        }
    )


async def test_persistence_sha_multi_reference_and_reruns(context):
    settings, sources, db, engine = context
    source = sources["YESBANK"]
    req = request(source, "YESBANK", ["DEPOSIT_CONTRACTION", "DEPOSIT_GROWTH_DECELERATION"])
    first = await engine.evaluate_patterns(req)
    original = first.model_dump_json()
    second = await engine.evaluate_patterns(req)
    assert first.id != second.id
    assert set(first.execution.created_snapshot_ids).isdisjoint(
        second.execution.created_snapshot_ids
    )
    assert len(first.execution.created_snapshot_ids) == 2
    assert [f.observed for f in first.findings] == [f.observed for f in second.findings]
    third = await engine.evaluate_patterns(pinned(req, first))
    assert third.execution.created_snapshot_ids == ()
    assert third.execution.referenced_snapshot_ids == first.execution.referenced_snapshot_ids
    assert await db.patterns.pattern_history(source.metadata.id) == (first, second, third)
    assert (await db.patterns.pattern_history(source.metadata.id))[0].model_dump_json() == original
    for artifact in source.raw_artifacts:
        raw = (settings.artifact_path / artifact.content_reference).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact.content_sha256
    for f in first.findings:
        for ref in f.calculations:
            calc = await db.calculations.get_calculation(ref.snapshot_id)
            assert patterns.digest(calc) == ref.payload_sha256
            assert calc.request.extraction_id == source.metadata.id
            assert len([r for r in calc.results if r.definition.metric_id == ref.metric_id]) == 1
    with pytest.raises(IntegrityError):
        await db.patterns.save_pattern(first)
    async with db._engine.connect() as conn:
        foreign = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("pattern_snapshots"))
        assert len(foreign) == 1 and foreign[0]["referred_table"] == "extractions"
        columns = await conn.run_sync(
            lambda c: [v["name"] for v in inspect(c).get_columns("pattern_snapshots")]
        )
        assert "calculation_snapshot_id" not in columns


@pytest.mark.parametrize(
    "case",
    [
        "missing_snapshot",
        "wrong_metric",
        "wrong_role",
        "stale_source",
        "wrong_company",
        "wrong_scope",
        "wrong_period",
    ],
)
async def test_wrong_pinned_calculation_rejected(context, case):
    _, sources, db, engine = context
    source = sources["INFY"]
    req = request(source, "INFY", ["CFO_TO_NET_PROFIT"])
    first = await engine.evaluate_patterns(req)
    pin = pinned(req, first)
    ref = pin.pinned_calculations[0]
    if case in {"missing_snapshot", "wrong_metric", "wrong_role"}:
        field = {
            "missing_snapshot": "snapshot_id",
            "wrong_metric": "metric_id",
            "wrong_role": "role",
        }[case]
        ref = ref.model_copy(update={field: uuid4() if field == "snapshot_id" else "wrong"})
    else:
        calc = await db.calculations.get_calculation(ref.snapshot_id)
        field = {
            "stale_source": "extraction_id",
            "wrong_company": "company_id",
            "wrong_scope": "reporting_scope",
            "wrong_period": "current_period",
        }[case]
        if case == "wrong_period":
            # Controlled incompatible calculation row; no existing immutable row is altered.
            changed = calc.model_copy(
                update={
                    "id": uuid4(),
                    "results": tuple(
                        r.model_copy(update={"period": "2024-03"}) for r in calc.results
                    ),
                }
            )
        else:
            value = (
                sources["YESBANK"].metadata.id
                if case == "stale_source"
                else sources["YESBANK"].company.company_id
                if case == "wrong_company"
                else ReportingScope.CONSOLIDATED
            )
            changed = calc.model_copy(
                update={"id": uuid4(), "request": calc.request.model_copy(update={field: value})}
            )
        # Raw payload mutation is intentionally an adversarial storage test, not valid evidence.
        async with db.calculations.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO calculation_snapshots (id,extraction_id,calculated_at,payload) "
                    "VALUES (:id,:ex,:at,:payload)"
                ),
                {
                    "id": str(changed.id),
                    "ex": str(source.metadata.id),
                    "at": changed.calculated_at.isoformat(),
                    "payload": json.dumps(
                        {
                            "payload_type": "financial_calculation",
                            "payload_version": 1,
                            "snapshot": changed.model_dump(mode="json"),
                        }
                    ),
                },
            )
        ref = ref.model_copy(update={"snapshot_id": changed.id})
    pin = pin.model_copy(update={"pinned_calculations": (ref,)})
    with pytest.raises(ValueError):
        await engine.evaluate_patterns(pin)
    assert await db.patterns.pattern_history(source.metadata.id) == (first,)


@pytest.mark.parametrize("revision", ["definition", "threshold", "mapping"])
async def test_independent_definition_threshold_mapping_revisions(context, revision):
    _, sources, db, engine = context
    name = "CFO_TO_NET_PROFIT" if revision == "mapping" else "OPERATING_MARGIN_DETERIORATION"
    source = sources["INFY"]
    req = request(source, "INFY", [name])
    first = await engine.evaluate_patterns(req)
    old_json = first.model_dump_json()
    definitions = patterns.DEFINITIONS
    resolver = ScreenerSemanticResolver()
    if revision == "definition":
        definitions = tuple(
            d.model_copy(
                update={
                    "version": 2,
                    "interpretation_boundary": "Version 2 observation-only interpretation boundary",
                }
            )
            if d.pattern_id == name
            else d
            for d in definitions
        )
        req = req.model_copy(
            update={"patterns": tuple(s.model_copy(update={"version": 2}) for s in req.patterns)}
        )
    if revision == "threshold":
        definitions = tuple(
            d.model_copy(
                update={
                    "threshold": d.threshold.model_copy(
                        update={"version": 2, "threshold": Decimal("-5")}
                    )
                }
            )
            if d.pattern_id == name
            else d
            for d in definitions
        )
    if revision == "mapping":
        resolver = ScreenerSemanticResolver(
            tuple(
                m.model_copy(update={"version": 2})
                if m.canonical_semantic == "SOURCE_REPORTED_CFO"
                else m
                for m in MAPPINGS
            )
        )
    second = await patterns.evaluate(
        source, req, resolver, db.calculations, engine, definitions=definitions
    )
    await db.patterns.save_pattern(second)
    if revision == "definition":
        assert second.findings[0].definition.version == 2
    if revision == "threshold":
        assert first.findings[0].rule_outcome == "NOT_TRIGGERED"
        assert second.findings[0].rule_outcome == "TRIGGERED"
        assert first.findings[0].definition.threshold.version == 1
    if revision == "mapping":
        assert (
            next(
                m
                for m in second.findings[0].mappings
                if m.canonical_semantic == "SOURCE_REPORTED_CFO"
            ).definition.version
            == 2
        )
    assert (await db.patterns.pattern_history(source.metadata.id))[0].model_dump_json() == old_json
    assert first.execution.created_snapshot_ids != second.execution.created_snapshot_ids


async def test_source_revision_and_raw_corruption(context, tmp_path):
    settings, sources, db, engine = context
    source = sources["INFY"]
    req = request(source, "INFY", ["CFO_TO_NET_PROFIT"])
    first = await engine.evaluate_patterns(req)
    html = (Path(__file__).parent / "fixtures/screener/infy_standalone.html").read_text()
    assert "29,409" in html
    revision = tmp_path / "revision.html"
    revision.write_text(html.replace("29,409", "29,410"))
    newer = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url="https://www.screener.in/company/INFY/", replay_path=revision
        ),
        tmp_path / "revised",
    )
    second = await engine.evaluate_patterns(request(newer, "INFY", ["CFO_TO_NET_PROFIT"]))
    assert first.source_payload_sha256 != second.source_payload_sha256
    assert first.findings[0].observed[-1].value != second.findings[0].observed[-1].value
    assert await db.patterns.pattern_history(source.metadata.id) == (first,)
    assert await db.patterns.pattern_history(newer.metadata.id) == (second,)
    before = await db.calculations.calculation_history(source.metadata.id)
    (settings.artifact_path / source.raw_artifacts[0].content_reference).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        await engine.evaluate_patterns(req)
    assert await db.patterns.pattern_history(source.metadata.id) == (first,)
    assert await db.calculations.calculation_history(source.metadata.id) == before


@pytest.mark.parametrize("symbol", ["INFY", "YESBANK"])
async def test_offline_production_cli(evidence, tmp_path, symbol):
    settings, sources = evidence
    names = (
        ["CFO_TO_NET_PROFIT", "REPEATED_NEGATIVE_CFO", "OPERATING_MARGIN_DETERIORATION"]
        if symbol == "INFY"
        else ["DEPOSIT_CONTRACTION", "DEPOSIT_GROWTH_DECELERATION"]
    )
    req = request(sources[symbol], symbol, names)
    path = tmp_path / "request.json"
    path.write_text(req.model_dump_json())
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
        "socket.socket.connect=blocked\n"
    )
    output = tmp_path / "cli"
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "stock_research_ai",
            "--database",
            str(settings.database_path),
            "evaluate-patterns",
            "--request",
            str(path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(guard),
            "PYTHONDONTWRITEBYTECODE": "1",
            "STOCK_RESEARCH_AI_ARTIFACT_PATH": str(settings.artifact_path),
        },
    )
    assert run.returncode == 0, run.stderr
    snapshot = PatternSnapshot.model_validate_json(
        (output / "normalized/pattern_snapshot.json").read_text()
    )
    for finding in snapshot.findings:
        assert_golden(finding, finding.definition.pattern_id)
    assert len(json.loads((output / "coverage.json").read_text())["findings"]) == len(names)
    assert not json.loads((output / "normalized/unavailable.json").read_text())
    db = SQLiteDatabase(settings.database_path)
    try:
        assert await db.patterns.pattern_history(sources[symbol].metadata.id) == (snapshot,)
    finally:
        await db.close()
