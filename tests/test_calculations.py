"""Offline golden arithmetic, failure semantics, immutable persistence and real CLI proof."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.calculations import calculate
from stock_research_ai.bootstrap import calculate_financials, extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.calculations import CalculationRequest, CalculationSnapshot
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.financial import (
    PeriodFrequency,
    PeriodKind,
    ReportingScope,
    UnitKind,
    ValueStatus,
)

FIX = Path(__file__).parent / "fixtures"
INPUTS = json.loads((FIX / "calculations/expected_inputs.json").read_text())
OUTPUTS = json.loads((FIX / "calculations/expected_outputs.json").read_text())


def request(source, symbol="YESBANK"):
    def growth(name, metric, period="2025-03", comparison="ANNUAL_YOY"):
        return dict(
            metric_id=name,
            formula_id="SOURCE_METRIC_GROWTH",
            source_metric=metric,
            current_period=period,
            comparison=comparison,
        )

    if symbol == "YESBANK":
        specs = [
            growth("revenue_growth", "revenue"),
            growth("pat_growth", "net_profit"),
            growth("eps_growth", "eps"),
            growth("quarter_yoy", "net_profit", "2025-06", "QUARTERLY_YOY"),
            growth("quarter_seq", "net_profit", "2025-06", "QUARTERLY_SEQUENTIAL"),
            dict(
                metric_id="assets_return",
                formula_id="AVERAGE_ASSETS_RETURN",
                current_period="2025-03",
            ),
            growth("deposit_growth", "deposits"),
        ]
    else:
        specs = [
            growth("infy_sales_growth", "sales"),
            growth("infy_pat_growth", "net_profit"),
            dict(metric_id="infy_margin", formula_id="OPERATING_MARGIN", current_period="2025-03"),
        ]
    return CalculationRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            extraction_id=source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="BANK" if symbol == "YESBANK" else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration=f"Explicit {symbol} proof classification",
            ),
            alignment=dict(
                fiscal_year_end_month=3,
                declaration=(
                    "Caller declares March fiscal year end and three-month quarters; "
                    "no source start dates invented."
                ),
            ),
            calculations=specs,
        )
    )


@pytest.fixture
async def evidence(tmp_path):
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    sources = {}
    for symbol, filename, suffix in (
        ("YESBANK", "yesbank_consolidated", "/consolidated/"),
        ("INFY", "infy_standalone", "/"),
    ):
        sources[symbol] = await extract_snapshot(
            settings,
            ExtractionRequest(
                provider="screener",
                url=f"https://www.screener.in/company/{symbol}{suffix}",
                replay_path=FIX / f"screener/{filename}.html",
            ),
            tmp_path / symbol,
        )
    return settings, sources


@pytest.mark.parametrize("symbol", ["YESBANK", "INFY"])
async def test_golden(evidence, tmp_path, symbol):
    settings, sources = evidence
    source = sources[symbol]
    # Expected source inputs are verified independently of calculation outputs.
    sections = {
        "annual": source.profit_loss,
        "quarterly": source.quarterly,
        "instant": source.balance_sheet,
    }
    for section, metrics in INPUTS[symbol].items():
        if section == "scope":
            assert source.company.reporting_scope == metrics
            continue
        for label, periods in metrics.items():
            for period, expected in periods.items():
                matches = [
                    o
                    for o in sections[section]
                    if o.original_label == label and o.period.fiscal_label == period
                ]
                assert len(matches) == 1 and matches[0].value == Decimal(expected)
    result = await calculate_financials(settings, request(source, symbol), tmp_path / "calculated")
    for r in result.results:
        expected = OUTPUTS[r.metric_id]
        assert r.status == "AVAILABLE", r.issues
        assert r.value == Decimal(expected["value"])
        assert r.display_value == Decimal(expected["display"])
        assert Fraction(r.exact_numerator, r.exact_denominator) == Fraction(
            int(expected["fraction"][0]), int(expected["fraction"][1])
        )
        assert r.snapshot_id == result.id and r.extraction_id == source.metadata.id
        assert r.formula.version == "1" and r.alignment.version == "1"
        assert r.sector == result.request.sector
        assert r.classification == "CALCULATION"
        originals = {o.id: o for values in sections.values() for o in values}
        assert all(o == originals[o.id] for o in r.inputs)
        assert all(
            o.provenance.raw_artifact_id in {a.id for a in source.raw_artifacts} for o in r.inputs
        )
    assert (
        result.source_payload_sha256
        == hashlib.sha256(source.model_dump_json().encode()).hexdigest()
    )
    assert (
        CalculationSnapshot.model_validate_json((tmp_path / "calculated/snapshot.json").read_text())
        == result
    )
    if symbol == "YESBANK":
        assert "source-reported Revenue" in result.results[0].label
        avg = next(r for r in result.results if r.metric_id == "assets_return")
        assert avg.label == "AVERAGE_ASSETS_RETURN"
        assert [o.period.fiscal_label for o in avg.inputs] == ["Mar 2025", "Mar 2024", "Mar 2025"]
        yoy = next(r for r in result.results if r.metric_id == "quarter_yoy")
        assert [o.value for o in yoy.inputs] == [Decimal("809"), Decimal("516")]


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", "INSUFFICIENT_INPUT"),
        ("zero", "DIVISION_BY_ZERO"),
        ("negative", "AVAILABLE"),
        ("malformed", "INVALID_INPUT"),
        ("scope", "INVALID_INPUT"),
        ("currency", "INVALID_INPUT"),
        ("ttm", "INVALID_INPUT"),
        ("instant", "INVALID_INPUT"),
        ("duplicate", "INVALID_INPUT"),
        ("unknown_unit", "INVALID_INPUT"),
        ("blank", "INSUFFICIENT_INPUT"),
        ("not_reported", "INSUFFICIENT_INPUT"),
        ("unavailable", "INSUFFICIENT_INPUT"),
        ("not_applicable", "NOT_APPLICABLE"),
        ("misaligned", "INVALID_INPUT"),
        ("bank_margin", "NOT_APPLICABLE"),
        ("nonbank_deposits", "NOT_APPLICABLE"),
        ("unknown_sector", "NOT_APPLICABLE"),
        ("duration_conflict", "INVALID_INPUT"),
        ("incomplete_raw", "INVALID_INPUT"),
    ],
)
async def test_failure_semantics(evidence, case, status):
    _, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    spec = req.calculations[1]
    rows = list(source.profit_loss)
    index = next(
        i
        for i, o in enumerate(rows)
        if o.metric_id == "net_profit" and o.period.fiscal_label == "Mar 2024"
    )
    prior = rows[index]
    if case == "missing":
        rows.pop(index)
    if case == "duplicate":
        rows.append(prior)
    if case in {"zero", "negative"}:
        rows[index] = prior.model_copy(
            update={"value": Decimal("0" if case == "zero" else "-1285")}
        )
    if case in {"blank", "not_reported", "unavailable", "not_applicable", "malformed"}:
        rows[index] = prior.model_copy(
            update={
                "value": None,
                "value_status": ValueStatus("parse_error" if case == "malformed" else case),
            }
        )
    if case == "scope":
        rows[index] = prior.model_copy(update={"reporting_scope": ReportingScope.STANDALONE})
    if case in {"currency", "unknown_unit"}:
        unit = (
            prior.unit.model_copy(update={"currency": "USD"})
            if case == "currency"
            else prior.unit.model_copy(update={"kind": UnitKind.UNKNOWN, "currency": None})
        )
        rows[index] = prior.model_copy(update={"unit": unit})
    if case in {"ttm", "instant"}:
        change = (
            {"frequency": PeriodFrequency.TTM} if case == "ttm" else {"kind": PeriodKind.INSTANT}
        )
        rows[index] = prior.model_copy(update={"period": prior.period.model_copy(update=change)})
    if case == "duration_conflict":
        rows[index] = prior.model_copy(
            update={"period": prior.period.model_copy(update={"start_date": date(2024, 1, 1)})}
        )
    if case == "incomplete_raw":
        source = source.model_copy(
            update={
                "raw_artifacts": tuple(
                    a.model_copy(update={"complete": False}) for a in source.raw_artifacts
                )
            }
        )
    if case == "misaligned":
        spec = spec.model_copy(update={"current_period": "2025-02"})
    if case == "bank_margin":
        spec = spec.model_copy(update={"formula_id": "OPERATING_MARGIN"})
    if case == "nonbank_deposits":
        spec = req.calculations[-1]
        req = req.model_copy(
            update={"sector": req.sector.model_copy(update={"classification": "NON_FINANCIAL"})}
        )
    if case == "unknown_sector":
        req = req.model_copy(
            update={"sector": req.sector.model_copy(update={"classification": "UNKNOWN"})}
        )
    req = req.model_copy(update={"calculations": (spec,)})
    result = calculate(source.model_copy(update={"profit_loss": tuple(rows)}), req).results[0]
    assert result.status == status, result.issues
    if status != "AVAILABLE":
        assert result.value is None and result.display_value is None
    else:
        assert "NEGATIVE_BASE_ABSOLUTE_DENOMINATOR_CONVENTION" in result.issues
        assert Fraction(result.exact_numerator, result.exact_denominator) == Fraction(373200, 1285)


async def test_snapshot_history_revision_and_precision(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    first = await calculate_financials(settings, req, tmp_path / "first")
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        second = await calculate_financials(settings, req, tmp_path / "second")
    assert first.id != second.id
    assert [r.value for r in first.results] == [r.value for r in second.results]
    rows = list(source.profit_loss)
    i = next(
        i
        for i, o in enumerate(rows)
        if o.metric_id == "net_profit" and o.period.fiscal_label == "Mar 2025"
    )
    # Controlled revision gets a different extraction, observation and raw evidence through replay.
    html = (FIX / "screener/yesbank_consolidated.html").read_text()
    assert "2,447" in html
    path = tmp_path / "revised.html"
    path.write_text(html.replace("2,447", "2,448"))
    revised = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/YESBANK/consolidated/",
            replay_path=path,
        ),
        tmp_path / "revision-evidence",
    )
    third = await calculate_financials(settings, request(revised), tmp_path / "third")
    assert third.source_payload_sha256 != first.source_payload_sha256
    assert third.results[1].value != first.results[1].value
    db = SQLiteDatabase(settings.database_path)
    try:
        assert await db.calculations.calculation_history(source.metadata.id) == (first, second)
        assert await db.calculations.calculation_history(revised.metadata.id) == (third,)
        with pytest.raises(IntegrityError):
            await db.calculations.save_calculation(first)
        pinned = await db.extractions.get_extraction(source.metadata.id)
        assert pinned.profit_loss[i].value == Decimal("2447")
    finally:
        await db.close()
    invalid = first.model_dump()
    invalid["results"][0]["snapshot_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        CalculationSnapshot.model_validate(invalid)


async def test_real_cli_offline(evidence, tmp_path):
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
    for symbol in sources:
        req = tmp_path / f"{symbol}-request.json"
        req.write_text(request(sources[symbol], symbol).model_dump_json())
        output = tmp_path / f"{symbol}-calculation"
        run = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "stock_research_ai",
                "--database",
                str(settings.database_path),
                "calculate-financials",
                "--request",
                str(req),
                "--output",
                str(output),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        result = CalculationSnapshot.model_validate_json((output / "snapshot.json").read_text())
        assert all(r.value == Decimal(OUTPUTS[r.metric_id]["value"]) for r in result.results)


async def test_raw_integrity_and_identity_guard(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    with pytest.raises(ValueError, match="exact source company"):
        calculate(source, req.model_copy(update={"company_id": uuid4()}))
    with pytest.raises(ValueError, match="Unknown extraction"):
        await calculate_financials(
            settings, req.model_copy(update={"extraction_id": uuid4()}), tmp_path / "absent"
        )
    artifact = source.raw_artifacts[0]
    (settings.artifact_path / artifact.content_reference).write_bytes(
        b"corrupt local test evidence"
    )
    with pytest.raises(ValueError, match="integrity"):
        await calculate_financials(settings, req, tmp_path / "corrupt")
    database = SQLiteDatabase(settings.database_path)
    try:
        assert await database.calculations.calculation_history(source.metadata.id) == ()
    finally:
        await database.close()


async def test_exact_scale_conversion(evidence):
    _, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    rows = list(source.profit_loss)
    index = next(
        i
        for i, o in enumerate(rows)
        if o.metric_id == "net_profit" and o.period.fiscal_label == "Mar 2024"
    )
    prior = rows[index]
    # Same quantity in another explicit scale, not a currency conversion.
    rows[index] = prior.model_copy(
        update={
            "value": prior.value * 10,
            "unit": prior.unit.model_copy(update={"scale": prior.unit.scale / 10}),
        }
    )
    result = calculate(source.model_copy(update={"profit_loss": tuple(rows)}), req)
    assert result.results[1].value == Decimal(OUTPUTS["pat_growth"]["value"])


@pytest.mark.parametrize(
    "case,status",
    [
        ("zero_assets", "DIVISION_BY_ZERO"),
        ("negative_assets", "INVALID_INPUT"),
        ("zero_sales", "DIVISION_BY_ZERO"),
        ("quarter_assets", "NOT_APPLICABLE"),
    ],
)
async def test_ratio_guards(evidence, case, status):
    _, sources = evidence
    source = sources["INFY" if case == "zero_sales" else "YESBANK"]
    req = request(source, "INFY" if case == "zero_sales" else "YESBANK")
    spec = req.calculations[-1] if case == "zero_sales" else req.calculations[-2]
    if case == "quarter_assets":
        spec = spec.model_copy(update={"comparison": "QUARTERLY_YOY", "current_period": "2025-06"})
    else:
        field = "profit_loss" if case == "zero_sales" else "balance_sheet"
        metric = "sales" if case == "zero_sales" else "total_assets"
        rows = tuple(
            o.model_copy(update={"value": Decimal("-1" if case == "negative_assets" else "0")})
            if o.metric_id == metric
            else o
            for o in getattr(source, field)
        )
        source = source.model_copy(update={field: rows})
    result = calculate(source, req.model_copy(update={"calculations": (spec,)})).results[0]
    assert result.status == status
    assert result.value is None
