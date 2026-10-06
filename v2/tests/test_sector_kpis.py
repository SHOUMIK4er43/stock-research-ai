"""Independent sector goldens, semantic gates, lineage and offline CLI proofs."""

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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_calculations import evidence as evidence
from test_calculations import request as financial_request

from stock_research_ai.adapters.screener.semantic_mappings import MAPPINGS, ScreenerSemanticResolver
from stock_research_ai.adapters.sqlite import SQLiteDatabase, decode_calculation
from stock_research_ai.application import calculations as financial
from stock_research_ai.application.sector_kpis import BANK, DEFINITIONS, IT, PROFILES, evaluate_kpis
from stock_research_ai.bootstrap import calculate_sector_kpis, extract_snapshot
from stock_research_ai.domain.calculations import CalculationSnapshot
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.financial import PeriodFrequency, ReportingScope
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation, SectorKPIRequest

FIX = Path(__file__).parent / "fixtures"
INPUTS = json.loads((FIX / "sector_kpis/expected_inputs.json").read_text())
OUTPUTS = json.loads((FIX / "sector_kpis/expected_outputs.json").read_text())


def request(source, symbol="YESBANK", kpis=None):
    common = financial_request(source, symbol)
    return SectorKPIRequest.model_validate(
        dict(
            company_id=common.company_id,
            extraction_id=common.extraction_id,
            reporting_scope=common.reporting_scope,
            classification=common.sector.model_dump(),
            profile_id=BANK if symbol == "YESBANK" else IT,
            profile_version=1,
            alignment=common.alignment,
            current_period="2025-03",
            kpis=tuple(kpis or OUTPUTS[symbol]),
        )
    )


def evaluate(source, req, resolver=None, **kwargs):
    return evaluate_kpis(source, req, resolver or ScreenerSemanticResolver(), **kwargs)


def assert_goldens(snapshot, symbol):
    for result in snapshot.results:
        expected = OUTPUTS[symbol][result.definition.metric_id]
        assert result.status == expected.get("status", "AVAILABLE"), result.issues
        if result.status != "AVAILABLE":
            assert result.value is None and result.display_value is None
            continue
        assert result.value == Decimal(expected["value"])
        assert result.display_value == Decimal(expected["display"])
        assert Fraction(result.exact_numerator, result.exact_denominator) == Fraction(
            *map(int, expected["fraction"])
        )
        assert result.definition.formula == financial.FORMULAS[result.definition.formula.id]
        assert result.snapshot_id == snapshot.id
        assert result.extraction_id == snapshot.request.extraction_id
        assert result.sector == snapshot.request.classification
        assert result.alignment.version == "1"
        assert len(result.inputs) == len(result.mappings)
        for observation in result.inputs:
            (mapping,) = [m for m in result.mappings if m.observation_id == observation.id]
            assert mapping.status == "VERIFIED"
            assert mapping.definition in MAPPINGS
            assert mapping.provider == "screener"
            assert mapping.source_identity == observation.metric
            assert mapping.source_metric_id == observation.metric_id
            assert mapping.provenance == observation.provenance
            assert mapping.provenance.source_location.section == mapping.section.upper()


@pytest.mark.parametrize("symbol", ["YESBANK", "INFY"])
async def test_independent_source_inputs_and_golden_outputs(evidence, tmp_path, symbol):
    settings, sources = evidence
    source = sources[symbol]
    # Check the independent INPUT document against extracted evidence before OUTPUT assertions.
    fields = {
        "deposits": ("balance_sheet", "deposits", ("Mar 2025", "Mar 2024")),
        "annual_net_profit": ("profit_loss", "net_profit", ("Mar 2025",)),
        "assets": ("balance_sheet", "total_assets", ("Mar 2024", "Mar 2025")),
        "source_revenue": ("profit_loss", "revenue", ("Mar 2025", "Mar 2024")),
        **{
            m: ("profit_loss", m, ("Mar 2025", "Mar 2024"))
            for m in ("sales", "net_profit", "operating_profit", "eps")
        },
    }
    for label, values in INPUTS[symbol].items():
        section, metric, periods = fields[label]
        expected = values if isinstance(values, list) else [values]
        for period, value in zip(periods, expected, strict=True):
            (observation,) = [
                o
                for o in getattr(source, section)
                if o.metric_id == metric and o.period.fiscal_label == period
            ]
            assert observation.value == Decimal(value)
    snapshot = await calculate_sector_kpis(settings, request(source, symbol), tmp_path / "kpis")
    assert_goldens(snapshot, symbol)
    assert (
        snapshot.source_payload_sha256
        == hashlib.sha256(source.model_dump_json().encode()).hexdigest()
    )
    originals = {o.id: o for f in ("profit_loss", "balance_sheet") for o in getattr(source, f)}
    for result in snapshot.results:
        for o in result.inputs:
            assert originals[o.id] == o
            (artifact,) = [a for a in source.raw_artifacts if a.id == o.provenance.raw_artifact_id]
            assert (
                hashlib.sha256(
                    (settings.artifact_path / artifact.content_reference).read_bytes()
                ).hexdigest()
                == artifact.content_sha256
            )
    if symbol == "INFY":
        bps = next(r for r in snapshot.results if r.definition.metric_id.endswith("_BPS"))
        assert [o.value for o in bps.inputs] == list(
            map(Decimal, ["33499", "136592", "31757", "128933"])
        )
        assert bps.display_value != Decimal(
            "-11.00"
        )  # Rounded margins would give this wrong result.
    else:
        assert "SOURCE_REVENUE_GROWTH" not in snapshot.profile.supported_kpis
        revenue = snapshot.results[2]
        assert revenue.definition.category == "GENERIC_SOURCE_METRIC"
        assert "SOURCE_REPORTED_MEANING_ONLY" in [i.code for i in revenue.issues]
        assert [o.period.fiscal_label for o in snapshot.results[1].inputs] == [
            "Mar 2025",
            "Mar 2024",
            "Mar 2025",
        ]


async def test_part4a_dispatch_and_precision(evidence, monkeypatch):
    _, sources = evidence
    calls = []
    original = financial.evaluate

    def spy(source, req, spec, used, issues):
        calls.append(spec.formula_id)
        return original(source, req, spec, used, issues)

    monkeypatch.setattr(financial, "evaluate", spy)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.traps[Inexact] = True
        for symbol, source in sources.items():
            assert_goldens(evaluate(source, request(source, symbol)), symbol)
    assert calls.count("AVERAGE_ASSETS_RETURN") == 1
    assert calls.count("SOURCE_METRIC_GROWTH") == 5
    assert calls.count("OPERATING_MARGIN_CHANGE_BPS") == 1
    assert (
        calls.count("OPERATING_MARGIN") == 3
    )  # Current and prior margin reuse the same primitive.


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("unknown", "NOT_APPLICABLE", "UNKNOWN_SECTOR_PROFILE"),
        ("wrong_profile", "NOT_APPLICABLE", "PROFILE_CLASSIFICATION_MISMATCH"),
        ("missing", "INSUFFICIENT_INPUT", "REQUIRED_SEMANTIC_PERIOD_MISSING"),
        ("identity", "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"),
        ("provider", "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"),
        (
            "mapping_version",
            "UNVERIFIED_METRIC_SEMANTICS",
            "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING",
        ),
        ("definition", "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"),
        ("locator", "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"),
        ("duplicate", "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"),
        ("scope", "INCOMPATIBLE_SCOPE", "INCOMPATIBLE_SCOPE"),
        ("request_scope", "INCOMPATIBLE_SCOPE", "EXTRACTION_SCOPE_MISMATCH"),
        ("frequency", "INCOMPATIBLE_PERIOD", "INCOMPATIBLE_PERIOD_KIND_OR_FREQUENCY"),
        ("duration", "INCOMPATIBLE_PERIOD", "DURATION_CONFLICTS_WITH_ALIGNMENT_POLICY"),
        ("end_date", "INCOMPATIBLE_PERIOD", "CONFLICTING_PERIOD_LABEL_AND_DATE"),
        ("currency", "INVALID_INPUT", "INCOMPATIBLE_UNIT_OR_CURRENCY"),
        ("zero", "INVALID_INPUT", "ZERO_PRIOR_VALUE"),
        ("effective", "NOT_APPLICABLE", "CLASSIFICATION_OUTSIDE_EFFECTIVE_PERIOD"),
    ],
)
async def test_negative_goldens(evidence, case, status, code):
    _, sources = evidence
    source = sources["INFY"]
    req = request(source, "INFY", ("SALES_GROWTH",))
    rows = list(source.profit_loss)
    index = next(
        i
        for i, o in enumerate(rows)
        if o.metric_id == "sales" and o.period.fiscal_label == "Mar 2024"
    )
    prior = rows[index]
    if case == "unknown":
        req = req.model_copy(
            update={
                "classification": req.classification.model_copy(
                    update={"classification": "UNKNOWN"}
                )
            }
        )
    if case == "wrong_profile":
        req = req.model_copy(update={"profile_id": BANK})
    if case == "missing":
        rows.pop(index)
    if case == "duplicate":
        rows.append(prior.model_copy(update={"id": uuid4()}))
    if case == "identity":
        rows[index] = prior.model_copy(update={"metric": "source:Sales (estimated)"})
    if case == "provider":
        source = source.model_copy(
            update={"metadata": source.metadata.model_copy(update={"provider": "other"})}
        )
    if case == "mapping_version":
        source = source.model_copy(
            update={"metadata": source.metadata.model_copy(update={"mapping_version": "2"})}
        )
    if case == "definition":
        source = source.model_copy(
            update={
                "metric_definitions": tuple(
                    d.model_copy(update={"definition": "Unverified interpretation"})
                    if d.id == "sales"
                    else d
                    for d in source.metric_definitions
                )
            }
        )
    if case == "locator":
        rows[index] = prior.model_copy(
            update={
                "provenance": prior.provenance.model_copy(
                    update={
                        "source_location": prior.provenance.source_location.model_copy(
                            update={"section": "RATIOS"}
                        )
                    }
                )
            }
        )
    if case == "scope":
        rows[index] = prior.model_copy(update={"reporting_scope": ReportingScope.CONSOLIDATED})
    if case == "request_scope":
        req = req.model_copy(update={"reporting_scope": ReportingScope.CONSOLIDATED})
    if case in {"frequency", "duration", "end_date"}:
        change = (
            {"frequency": PeriodFrequency.TTM}
            if case == "frequency"
            else {"start_date" if case == "duration" else "end_date": date(2024, 1, 1)}
        )
        rows[index] = prior.model_copy(update={"period": prior.period.model_copy(update=change)})
    if case == "currency":
        rows[index] = prior.model_copy(
            update={"unit": prior.unit.model_copy(update={"currency": "USD"})}
        )
    if case == "zero":
        rows[index] = prior.model_copy(update={"value": Decimal(0)})
    if case == "effective":
        req = req.model_copy(
            update={
                "classification": req.classification.model_copy(
                    update={"effective_end": date(2020, 3, 31)}
                )
            }
        )
    result = evaluate(source.model_copy(update={"profit_loss": tuple(rows)}), req).results[0]
    assert result.status == status, result.issues
    assert code in [i.code for i in result.issues]
    assert result.value is None and result.display_value is None


async def test_candidate_and_source_ratios_are_not_calculated(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    unavailable = tuple(d.metric_id for d in DEFINITIONS if d.formula is None)
    result = evaluate(source, request(source, kpis=unavailable))
    assert all(
        r.status == "INSUFFICIENT_INPUT" and r.definition.formula is None for r in result.results
    )
    html = (FIX / "screener/yesbank_consolidated.html").read_text()
    assert "Financing Profit" in html
    path = tmp_path / "candidate.html"
    path.write_text(html.replace("Financing Profit", "NIM"))
    candidate = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/YESBANK/consolidated/",
            replay_path=path,
        ),
        tmp_path / "candidate",
    )
    nim = evaluate(candidate, request(candidate, kpis=("NIM",))).results[0]
    assert nim.status == "UNVERIFIED_METRIC_SEMANTICS"
    assert nim.mappings and all(m.definition is None for m in nim.mappings)
    assert all(m.source_identity == "source:NIM" for m in nim.mappings)
    assert all(
        m.provenance.raw_artifact_id in {a.id for a in candidate.raw_artifacts}
        for m in nim.mappings
    )


async def test_ambiguous_registry_rejected(evidence):
    _, sources = evidence
    source = sources["YESBANK"]
    mapping = next(m for m in MAPPINGS if m.canonical_semantic == "DEPOSITS")
    r = evaluate(
        source,
        request(source, kpis=("DEPOSIT_GROWTH",)),
        ScreenerSemanticResolver((*MAPPINGS, mapping)),
    ).results[0]
    assert r.status == "UNVERIFIED_METRIC_SEMANTICS"


async def test_profile_mapping_source_revisions_immutable(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    first = await calculate_sector_kpis(settings, req, tmp_path / "first")
    old_json = first.model_dump_json()
    bank_v2 = PROFILES[0].model_copy(
        update={
            "version": 2,
            "description": "Version 2 proof",
            "supported_kpis": tuple(
                k for k in PROFILES[0].supported_kpis if k != "AVERAGE_ASSETS_RETURN"
            ),
        }
    )
    mappings_v2 = tuple(m.model_copy(update={"version": 2}) for m in MAPPINGS)
    req2 = req.model_copy(
        update={
            "profile_version": 2,
            "classification": req.classification.model_copy(
                update={"version": 2, "effective_start": date(2020, 1, 1)}
            ),
        }
    )
    second = evaluate(
        source,
        req2,
        ScreenerSemanticResolver(mappings_v2),
        profiles=(bank_v2,),
        definitions=tuple(d.model_copy(update={"version": 2}) for d in DEFINITIONS),
    )
    assert second.results[0].value == first.results[0].value
    assert second.results[1].status == "NOT_APPLICABLE"
    assert all(m.definition.version == 2 for m in second.results[0].mappings)
    html = (FIX / "screener/yesbank_consolidated.html").read_text()
    assert "284,420" in html
    path = tmp_path / "revision.html"
    path.write_text(html.replace("284,420", "284,421"))
    revised = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/YESBANK/consolidated/",
            replay_path=path,
        ),
        tmp_path / "revision",
    )
    third = await calculate_sector_kpis(settings, request(revised), tmp_path / "third")
    assert third.source_payload_sha256 != first.source_payload_sha256
    assert third.results[0].value != first.results[0].value
    db = SQLiteDatabase(settings.database_path)
    try:
        await db.calculations.save_calculation(second)
        history = await db.calculations.calculation_history(source.metadata.id)
        assert history == (first, second)
        assert history[0].model_dump_json() == old_json
        assert await db.calculations.calculation_history(revised.metadata.id) == (third,)
        assert await db.extractions.get_extraction(source.metadata.id) == source
        with pytest.raises(IntegrityError):
            await db.calculations.save_calculation(first)
    finally:
        await db.close()
    assert first.profile.version == 1 and first.request.classification.version == 1
    assert first.results[0].definition.version == 1
    assert second.results[0].definition.version == 2
    assert all(m.definition.version == 1 for m in first.results[0].mappings)
    with pytest.raises(ValidationError):
        first.profile.version = 9
    payload = first.model_dump()
    payload["results"][0]["snapshot_id"] = uuid4()
    with pytest.raises(ValidationError, match="lineage"):
        SectorKPIEvaluation.model_validate(payload)


async def test_untouched_legacy_snapshot_and_mixed_history(evidence, tmp_path):
    settings, sources = evidence
    literal = (FIX / "sector_kpis/legacy_4a_snapshot.json").read_text()
    legacy = CalculationSnapshot.model_validate_json(literal)
    assert decode_calculation(json.loads(literal)) == legacy
    # A pre-4C row retains its original payload byte-for-byte. The fixture's extraction FK
    # is provisioned separately for this serialization regression (not a new evidence claim).
    db = SQLiteDatabase(settings.database_path)
    try:
        async with db.calculations.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO extractions (id, company_id, provider, reporting_scope, status, "
                    "schema_version, created_at, payload) "
                    "SELECT :id, company_id, provider, reporting_scope, status, "
                    "schema_version, created_at, payload "
                    "FROM extractions WHERE id=:source"
                ),
                {
                    "id": str(legacy.request.extraction_id),
                    "source": str(sources["YESBANK"].metadata.id),
                },
            )
            await session.execute(
                text("INSERT INTO calculation_snapshots VALUES (:id,:source,:time,:payload)"),
                {
                    "id": str(legacy.id),
                    "source": str(legacy.request.extraction_id),
                    "time": legacy.calculated_at.isoformat(),
                    "payload": literal,
                },
            )
        assert await db.calculations.calculation_history(legacy.request.extraction_id) == (legacy,)
        source = sources["YESBANK"]
        old = financial.calculate(source, financial_request(source))
        new = evaluate(source, request(source))
        await db.calculations.save_calculation(old)
        await db.calculations.save_calculation(new)
        assert await db.calculations.calculation_history(source.metadata.id) == (old, new)
        async with db.calculations.sessions() as session:
            stored = await session.scalar(
                text("SELECT payload FROM calculation_snapshots WHERE id=:id"),
                {"id": str(legacy.id)},
            )
            assert stored == literal
            types = (
                (
                    await session.execute(
                        text(
                            "SELECT json_extract(payload,'$.payload_type') "
                            "FROM calculation_snapshots WHERE extraction_id=:id"
                        ),
                        {"id": str(source.metadata.id)},
                    )
                )
                .scalars()
                .all()
            )
            assert set(types) == {"financial_calculation", "sector_kpi_evaluation"}
    finally:
        await db.close()
    for payload in (
        {"payload_type": "unknown", "payload_version": 1},
        {"payload_type": "financial_calculation", "payload_version": 2},
    ):
        with pytest.raises(ValueError):
            decode_calculation(payload)


@pytest.mark.parametrize("symbol", ["YESBANK", "INFY"])
async def test_real_cli_offline(evidence, tmp_path, symbol):
    settings, sources = evidence
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
        "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
    )
    req = tmp_path / "request.json"
    req.write_text(request(sources[symbol], symbol).model_dump_json())
    output = tmp_path / "cli"
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "stock_research_ai",
            "--database",
            str(settings.database_path),
            "calculate-sector-kpis",
            "--request",
            str(req),
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
    assert run.returncode == 3, (
        run.stderr
    )  # Explicit unavailable/not-applicable results, not a crash.
    snapshot = SectorKPIEvaluation.model_validate_json(
        (output / "normalized/calculation_snapshot.json").read_text()
    )
    assert_goldens(snapshot, symbol)
    generic = json.loads((output / "normalized/generic_financial.json").read_text())
    available = json.loads((output / "normalized/available_kpis.json").read_text())
    assert all(r["definition"]["category"] == "SECTOR_KPI" for r in available)
    assert len(generic) == (1 if symbol == "YESBANK" else 0)
    coverage = json.loads((output / "coverage.json").read_text())
    assert len(coverage["results"]) == len(OUTPUTS[symbol])
    db = SQLiteDatabase(settings.database_path)
    try:
        assert await db.calculations.calculation_history(sources[symbol].metadata.id) == (snapshot,)
    finally:
        await db.close()


async def test_raw_sha_and_pinned_identity_guards(evidence, tmp_path):
    settings, sources = evidence
    source = sources["YESBANK"]
    req = request(source)
    with pytest.raises(ValueError, match="exact company/extraction"):
        evaluate(source, req.model_copy(update={"company_id": uuid4()}))
    with pytest.raises(ValueError, match="Unknown extraction"):
        await calculate_sector_kpis(
            settings, req.model_copy(update={"extraction_id": uuid4()}), tmp_path / "missing"
        )
    artifact = source.raw_artifacts[0]
    (settings.artifact_path / artifact.content_reference).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        await calculate_sector_kpis(settings, req, tmp_path / "corrupt")
    db = SQLiteDatabase(settings.database_path)
    try:
        assert await db.calculations.calculation_history(source.metadata.id) == ()
    finally:
        await db.close()


@pytest.mark.parametrize("negative_role,numerator", [("current", -26552500), ("prior", 26552500)])
async def test_negative_values_follow_shared_growth_convention(evidence, negative_role, numerator):
    _, sources = evidence
    source = sources["INFY"]
    label = "Mar 2025" if negative_role == "current" else "Mar 2024"
    rows = tuple(
        o.model_copy(update={"value": -o.value})
        if o.metric_id == "sales" and o.period.fiscal_label == label
        else o
        for o in source.profit_loss
    )
    result = evaluate(
        source.model_copy(update={"profit_loss": rows}), request(source, "INFY", ("SALES_GROWTH",))
    ).results[0]
    assert result.status == "AVAILABLE"
    assert Fraction(result.exact_numerator, result.exact_denominator) == Fraction(numerator, 128933)
    if negative_role == "prior":
        assert "NEGATIVE_BASE_ABSOLUTE_DENOMINATOR_CONVENTION" in [i.code for i in result.issues]


@pytest.mark.parametrize(
    "comparison,prior", [("QUARTERLY_YOY", "Jun 2024"), ("QUARTERLY_SEQUENTIAL", "Mar 2025")]
)
async def test_quarterly_alignment_uses_exact_period_keys(evidence, comparison, prior):
    _, sources = evidence
    source = sources["INFY"]
    req = request(source, "INFY", ("SALES_GROWTH", "OPERATING_MARGIN_CHANGE_BPS")).model_copy(
        update={"current_period": "2025-06", "comparison": comparison}
    )
    result = evaluate(source, req)
    assert all(r.status == "AVAILABLE" for r in result.results)
    assert [o.period.fiscal_label for o in result.results[0].inputs] == ["Jun 2025", prior]
    assert [o.period.fiscal_label for o in result.results[1].inputs] == [
        "Jun 2025",
        "Jun 2025",
        prior,
        prior,
    ]
    assert all(m.section == "quarterly" for r in result.results for m in r.mappings)


async def test_unknown_metric_profile_and_unimplemented_formula(evidence):
    _, sources = evidence
    source = sources["YESBANK"]
    req = request(source, kpis=("DEPOSIT_GROWTH",))
    with pytest.raises(ValueError, match="profile version"):
        evaluate(source, req.model_copy(update={"profile_version": 999}))
    with pytest.raises(ValueError, match="KPI definition"):
        evaluate(source, req.model_copy(update={"kpis": ("MADE_UP",)}))
    changed = DEFINITIONS[0].model_copy(
        update={
            "formula": DEFINITIONS[0].formula.model_copy(
                update={"expression": "unimplemented definition"}
            )
        }
    )
    result = evaluate(source, req, definitions=(changed,)).results[0]
    assert result.status == "INVALID_INPUT"
    assert result.issues[0].code == "FORMULA_VERSION_OR_DEFINITION_NOT_IMPLEMENTED"


@pytest.mark.parametrize(
    "metric,period,comparison",
    [
        ("DEPOSIT_GROWTH", "2025-06", "QUARTERLY_YOY"),
        ("AVERAGE_ASSETS_RETURN", "2025-06", "QUARTERLY_SEQUENTIAL"),
        ("DEPOSIT_GROWTH", "2025-02", "ANNUAL_YOY"),
    ],
)
async def test_formula_specific_period_policy(evidence, metric, period, comparison):
    _, sources = evidence
    source = sources["YESBANK"]
    req = request(source, kpis=(metric,)).model_copy(
        update={"current_period": period, "comparison": comparison}
    )
    result = evaluate(source, req).results[0]
    assert result.status == "INCOMPATIBLE_PERIOD"
    assert result.value is None
