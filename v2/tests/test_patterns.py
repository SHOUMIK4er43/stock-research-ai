"""Offline independent pattern truth and deterministic refusal/boundary proofs."""

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_calculations import evidence as evidence
from test_sector_kpis import request as sector_request

from stock_research_ai.adapters.screener.semantic_mappings import MAPPINGS, ScreenerSemanticResolver
from stock_research_ai.application import calculations, patterns, sector_kpis
from stock_research_ai.bootstrap import evaluate_patterns
from stock_research_ai.domain.financial import PeriodFrequency, ReportingScope
from stock_research_ai.domain.metrics import MetricMappingStatus
from stock_research_ai.domain.patterns import PatternRequest

FIX = Path(__file__).parent / "fixtures/patterns"
INPUTS = json.loads((FIX / "expected_inputs.json").read_text())
EXPECTED = json.loads((FIX / "expected_findings.json").read_text())


def request(source, symbol, names, period="2025-03"):
    base = sector_request(source, symbol).model_dump(exclude={"kpis", "comparison"})
    return PatternRequest.model_validate(
        {
            **base,
            "current_period": period,
            "patterns": [{"pattern_id": n, "version": 1} for n in names],
        }
    )


class MemoryCalculations:
    """A test repository; arithmetic stays in the actual common/sector implementations."""

    def __init__(self, source):
        self.source, self.snapshots = source, {}

    async def calculate_financials(self, req):
        result = calculations.calculate(self.source, req)
        self.snapshots[result.id] = result
        return result

    async def calculate_sector_kpis(self, req):
        result = sector_kpis.evaluate_kpis(self.source, req, ScreenerSemanticResolver())
        self.snapshots[result.id] = result
        return result

    async def get_calculation(self, snapshot_id):
        return self.snapshots.get(snapshot_id)


async def evaluate(source, req, resolver=None, calculator=None, **kwargs):
    calc = calculator or MemoryCalculations(source)
    return await patterns.evaluate(
        source, req, resolver or ScreenerSemanticResolver(), calc, calc, **kwargs
    )


def assert_golden(finding, name):
    expected = EXPECTED[name]
    assert finding.evaluation_status == expected["status"], finding.issues
    assert finding.rule_outcome == expected["outcome"]
    if "fraction" in expected:
        actual = finding.observed[-1]
        assert Fraction(actual.numerator, actual.denominator) == Fraction(*expected["fraction"])
        assert actual.value == Decimal(expected["value"])
        assert actual.display == Decimal(expected["display"])
        assert actual.unit == expected["unit"]


@pytest.mark.parametrize("symbol,name", [(s, n) for s in INPUTS for n in INPUTS[s]])
async def test_real_source_goldens(evidence, tmp_path, symbol, name):
    settings, sources = evidence
    source = sources[symbol]
    inputs = INPUTS[symbol][name]
    # Source expectations are checked first, separately from authored output expectations.
    current = inputs["period"]
    for metric, expected_values in inputs.items():
        if metric == "period":
            continue
        field = (
            "cashflow"
            if metric == "cash_operating"
            else "balance_sheet"
            if metric == "deposits"
            else "profit_loss"
        )
        keys = [calculations.previous(current, n * 12) for n in range(len(expected_values))]
        if name == "REPEATED_NEGATIVE_CFO":
            keys.reverse()
        for key, value in zip(keys, expected_values, strict=True):
            (obs,) = [
                o
                for o in getattr(source, field)
                if o.metric_id == metric and calculations.period_key(o) == key
            ]
            assert obs.value == Decimal(value)
            assert obs.unit.currency == "INR" and obs.unit.scale == Decimal("10000000")
    snapshot = await evaluate_patterns(
        settings, request(source, symbol, [name], current), tmp_path / "proof"
    )
    (finding,) = snapshot.findings
    assert_golden(finding, name)
    assert snapshot.source_payload_sha256 == patterns.digest(source)
    for observation in finding.inputs:
        (binding,) = [m for m in finding.mappings if m.observation_id == observation.id]
        assert binding.status == "VERIFIED" and binding.provenance == observation.provenance
        if observation.metric_id == "cash_operating":
            assert binding.canonical_semantic == "SOURCE_REPORTED_CFO"
            assert binding.definition.source_identity == "source:Cash from Operating Activity"
            assert binding.definition.extractor_mapping_version == "1"
            assert binding.provenance.source_location.section == "CASH_FLOW"
    if name == "DEPOSIT_GROWTH_DECELERATION":
        assert len(finding.inputs) == 3 and len(finding.calculations) == 2
        assert len(snapshot.execution.created_snapshot_ids) == 2
        assert finding.observed[-1].formula == calculations.FORMULAS["PERCENTAGE_POINT_DIFFERENCE"]
    assert all(
        ref.snapshot_id in snapshot.execution.created_snapshot_ids for ref in finding.calculations
    )


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("YESBANK", "CFO_TO_NET_PROFIT"),
        ("YESBANK", "PROFIT_VS_CFO_DIVERGENCE"),
        ("YESBANK", "REPEATED_NEGATIVE_CFO"),
        ("YESBANK", "OPERATING_MARGIN_DETERIORATION"),
        ("INFY", "DEPOSIT_CONTRACTION"),
        ("INFY", "DEPOSIT_GROWTH_DECELERATION"),
    ],
)
async def test_wrong_sector(evidence, symbol, name):
    _, sources = evidence
    result = await evaluate(sources[symbol], request(sources[symbol], symbol, [name]))
    (f,) = result.findings
    assert f.evaluation_status == "NOT_APPLICABLE" and f.rule_outcome is None
    assert not result.execution.created_snapshot_ids


@pytest.mark.parametrize(
    "case,status",
    [
        ("missing", "INSUFFICIENT_INPUT"),
        ("candidate", "UNVERIFIED_METRIC_SEMANTICS"),
        ("duplicate", "UNVERIFIED_METRIC_SEMANTICS"),
        ("version", "UNVERIFIED_METRIC_SEMANTICS"),
        ("provider", "UNVERIFIED_METRIC_SEMANTICS"),
        ("locator", "UNVERIFIED_METRIC_SEMANTICS"),
        ("quarterly", "INCOMPATIBLE_PERIOD"),
        ("scope", "INCOMPATIBLE_SCOPE"),
        ("currency", "INVALID_INPUT"),
        ("unknown_profile", "NOT_APPLICABLE"),
        ("unknown_classification", "NOT_APPLICABLE"),
        ("middle_year", "INSUFFICIENT_INPUT"),
        ("zero_pat", "INVALID_INPUT"),
        ("negative_pat", "AVAILABLE"),
        ("negative_cfo", "AVAILABLE"),
    ],
)
async def test_refusals_and_signs(evidence, case, status):
    _, sources = evidence
    source = sources["INFY"]
    name = "REPEATED_NEGATIVE_CFO" if case == "middle_year" else "CFO_TO_NET_PROFIT"
    req = request(source, "INFY", [name])
    rows = list(source.cashflow)
    key = "Mar 2024" if case == "middle_year" else "Mar 2025"
    index = next(
        i
        for i, o in enumerate(rows)
        if o.metric_id == "cash_operating" and o.period.fiscal_label == key
    )
    row = rows[index]
    if case in {"missing", "middle_year"}:
        rows.pop(index)
    if case == "candidate":
        rows[index] = row.model_copy(
            update={"metric_id": None, "mapping_status": MetricMappingStatus.UNMAPPED}
        )
    if case == "quarterly":
        rows[index] = row.model_copy(
            update={
                "period": row.period.model_copy(update={"frequency": PeriodFrequency.QUARTERLY})
            }
        )
    if case == "scope":
        rows[index] = row.model_copy(update={"reporting_scope": ReportingScope.CONSOLIDATED})
    if case == "currency":
        rows[index] = row.model_copy(
            update={"unit": row.unit.model_copy(update={"currency": "USD"})}
        )
    if case == "locator":
        rows[index] = row.model_copy(
            update={
                "provenance": row.provenance.model_copy(
                    update={
                        "source_location": row.provenance.source_location.model_copy(
                            update={"section": "PROFIT_LOSS"}
                        )
                    }
                )
            }
        )
    if case == "negative_cfo":
        rows[index] = row.model_copy(update={"value": -row.value})
    if case in {"zero_pat", "negative_pat"}:
        source = source.model_copy(
            update={
                "profit_loss": tuple(
                    o.model_copy(update={"value": Decimal(0) if case == "zero_pat" else -o.value})
                    if o.metric_id == "net_profit" and o.period.fiscal_label == "Mar 2025"
                    else o
                    for o in source.profit_loss
                )
            }
        )
    if case in {"version", "provider"}:
        source = source.model_copy(
            update={
                "metadata": source.metadata.model_copy(
                    update={"mapping_version" if case == "version" else "provider": "unverified"}
                )
            }
        )
    if case == "unknown_profile":
        req = req.model_copy(update={"profile_id": "UNKNOWN"})
    if case == "unknown_classification":
        req = req.model_copy(
            update={
                "classification": req.classification.model_copy(
                    update={"classification": "UNKNOWN"}
                )
            }
        )
    resolver = ScreenerSemanticResolver()
    if case == "duplicate":
        resolver = ScreenerSemanticResolver(
            (*MAPPINGS, next(m for m in MAPPINGS if m.canonical_semantic == "SOURCE_REPORTED_CFO"))
        )
    result = await evaluate(source.model_copy(update={"cashflow": tuple(rows)}), req, resolver)
    (finding,) = result.findings
    assert finding.evaluation_status == status, finding.issues
    if status != "AVAILABLE":
        assert finding.rule_outcome is None
    if case in {"unknown_profile", "unknown_classification"}:
        assert finding.issues[0].code == "UNKNOWN_SECTOR_PROFILE"
    if case == "negative_pat":
        assert finding.rule_outcome == "OBSERVATION_ONLY"
        assert finding.observed[-1].value == Decimal(EXPECTED[name]["value"]).copy_negate()
        assert "NEGATIVE_NET_PROFIT_RATIO_INTERPRETATION_LIMITED" in [
            i.code for i in finding.issues
        ]
    if case == "negative_cfo":
        assert finding.rule_outcome == "OBSERVATION_ONLY"


# Independent synthetic margin arithmetic: prior margin=25/100; current sales=100.
# (current operating profit/100 - 25/100)*10000 yields these exact BPS expectations.
@pytest.mark.parametrize(
    "profit,numerator,denominator,expected",
    [
        ("24.5", -50, 1, "TRIGGERED"),
        ("24.50000001", -49999999, 1000000, "NOT_TRIGGERED"),
        ("24.49", -51, 1, "TRIGGERED"),
    ],
)
async def test_synthetic_margin_boundaries(evidence, profit, numerator, denominator, expected):
    _, sources = evidence
    source = sources["INFY"]
    rows = []
    for o in source.profit_loss:
        value = None
        if o.metric_id == "sales" and o.period.fiscal_label in {"Mar 2024", "Mar 2025"}:
            value = Decimal(100)
        if o.metric_id == "operating_profit" and o.period.fiscal_label == "Mar 2024":
            value = Decimal(25)
        if o.metric_id == "operating_profit" and o.period.fiscal_label == "Mar 2025":
            value = Decimal(profit)
        rows.append(o.model_copy(update={"value": value}) if value is not None else o)
    changed = source.model_copy(update={"profit_loss": tuple(rows)})
    result = await evaluate(changed, request(changed, "INFY", ["OPERATING_MARGIN_DETERIORATION"]))
    (f,) = result.findings
    assert f.evaluation_status == "AVAILABLE" and f.rule_outcome == expected
    assert Fraction(f.observed[-1].numerator, f.observed[-1].denominator) == Fraction(
        numerator, denominator
    )
    assert f.definition.threshold == patterns.MARGIN_THRESHOLD
    assert len(f.inputs) == 4 and len(f.calculations) == 1


async def test_three_negative_and_deposit_trigger(evidence):
    _, sources = evidence
    source = sources["INFY"]
    source = source.model_copy(
        update={
            "cashflow": tuple(
                o.model_copy(update={"value": -abs(o.value)})
                if o.metric_id == "cash_operating"
                else o
                for o in source.cashflow
            )
        }
    )
    (f,) = (await evaluate(source, request(source, "INFY", ["REPEATED_NEGATIVE_CFO"]))).findings
    assert f.evaluation_status == "AVAILABLE" and f.rule_outcome == "TRIGGERED"
    assert [o.period.fiscal_label for o in f.inputs] == ["Mar 2023", "Mar 2024", "Mar 2025"]
    source = sources["YESBANK"]
    source = source.model_copy(
        update={
            "balance_sheet": tuple(
                o.model_copy(update={"value": Decimal(250000)})
                if o.metric_id == "deposits" and o.period.fiscal_label == "Mar 2025"
                else o
                for o in source.balance_sheet
            )
        }
    )
    (f,) = (await evaluate(source, request(source, "YESBANK", ["DEPOSIT_CONTRACTION"]))).findings
    assert f.rule_outcome == "TRIGGERED"
    assert Fraction(f.observed[-1].numerator, f.observed[-1].denominator) == Fraction(
        -1623000, 266230
    )


async def test_calc_reuse_and_mode_validation(evidence, monkeypatch):
    _, sources = evidence
    calls = []
    original = calculations.evaluate

    def spy(source, req, spec, used, issues):
        calls.append(spec.formula_id)
        return original(source, req, spec, used, issues)

    monkeypatch.setattr(calculations, "evaluate", spy)
    for symbol, names in [
        ("INFY", ["CFO_TO_NET_PROFIT", "OPERATING_MARGIN_DETERIORATION"]),
        ("YESBANK", ["DEPOSIT_CONTRACTION", "DEPOSIT_GROWTH_DECELERATION"]),
    ]:
        source = sources[symbol]
        await evaluate(source, request(source, symbol, names))
    assert calls.count("CFO_TO_NET_PROFIT") == 1
    assert calls.count("OPERATING_MARGIN_CHANGE_BPS") == 1
    assert calls.count("OPERATING_MARGIN") == 2
    assert calls.count("SOURCE_METRIC_GROWTH") == 2
    req = request(sources["INFY"], "INFY", ["CFO_TO_NET_PROFIT"])
    with pytest.raises(ValidationError, match="mix"):
        PatternRequest.model_validate(
            {
                **req.model_dump(),
                "pinned_calculations": [
                    {
                        "role": "CFO_TO_NET_PROFIT:2025-03",
                        "snapshot_id": uuid4(),
                        "metric_id": "CFO_TO_NET_PROFIT",
                    }
                ],
            }
        )


async def test_snapshot_contract_rejects_false_outcome_or_lineage(evidence):
    from stock_research_ai.domain.patterns import PatternSnapshot

    _, sources = evidence
    source = sources["INFY"]
    snapshot = await evaluate(source, request(source, "INFY", ["CFO_TO_NET_PROFIT"]))
    for change in ("status", "outcome", "snapshot_id", "mapping", "profile", "execution"):
        payload = snapshot.model_dump()
        finding = payload["findings"][0]
        if change == "status":
            finding["evaluation_status"] = "INSUFFICIENT_INPUT"
        if change == "outcome":
            finding["rule_outcome"] = None
        if change == "snapshot_id":
            finding["snapshot_id"] = uuid4()
        if change == "mapping":
            finding["mappings"] = []
        if change == "profile":
            payload["profile"]["version"] = 99
        if change == "execution":
            payload["execution"]["referenced_snapshot_ids"] = []
        with pytest.raises(ValidationError):
            PatternSnapshot.model_validate(payload)
    with pytest.raises(ValidationError):
        snapshot.findings[0].definition.version = 7
