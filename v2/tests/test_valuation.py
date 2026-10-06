"""Independent golden scenarios and refusal contracts; no live acquisition."""

import json
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.adapters.screener.semantic_mappings import ScreenerSemanticResolver
from stock_research_ai.application import valuation
from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.financial import PeriodFrequency, ReportingScope
from stock_research_ai.domain.metrics import MetricMappingStatus
from stock_research_ai.domain.valuation import (
    METHODS,
    ValuationAssumption,
    ValuationRequest,
    ValuationSnapshot,
)

FIX = Path(__file__).parent / "fixtures"
INPUT = json.loads((FIX / "valuation/inputs.json").read_text())
GOLD = json.loads((FIX / "valuation/expected.json").read_text())


async def acquire(settings, output, file="valuation/synthetic.html", symbol="INFY"):
    return await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url=f"https://www.screener.in/company/{symbol}/",
            replay_path=FIX / file,
        ),
        output,
    )


@pytest.fixture
async def evidence(tmp_path):
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await acquire(settings, tmp_path / "source")
    return settings, source


def assumption(name, value):
    unit = (
        "CURRENCY"
        if name.startswith("fcff_") or name in {"net_debt", "starting_fcff"}
        else "SHARES"
        if name == "diluted_shares"
        else "YEARS"
        if name == "horizon"
        else "MULTIPLE_X"
        if name.startswith("exit_")
        else "DECIMAL_RATE"
    )
    return dict(
        assumption_id=str(uuid4()),
        semantic=name,
        value=value,
        unit=unit,
        currency="INR" if unit == "CURRENCY" else None,
        provenance_basis="CALLER_DECLARED",
        declaration="Explicit synthetic scenario input",
    )


def pin(source, metric, period=None):
    rows = [
        o
        for sec in (source.top_metrics, source.profit_loss, source.balance_sheet)
        for o in sec
        if o.metric_id == metric and (not period or o.period.fiscal_label == period)
    ]
    return dict(
        kind="FINANCIAL", snapshot_id=str(source.metadata.id), observation_id=str(rows[-1].id)
    )


def request(source, method, changes=None, bank=False):
    selection = dict(method=method)
    if method != "FCFF_DCF":
        selection["price"] = pin(source, "current_price")
    if method in {METHODS[0], METHODS[4]}:
        selection["denominator"] = pin(
            source,
            "eps",
            "Mar 2026" if source.company.source_company_key != "SYNTHETIC-VALUATION" else None,
        )
    if method in {METHODS[1], METHODS[5]}:
        selection["denominator"] = pin(
            source,
            "bvps" if any(o.metric_id == "bvps" for o in source.balance_sheet) else "book_value",
        )
    if method in METHODS[2:]:
        group = {
            METHODS[2]: "dcf",
            METHODS[3]: "reverse",
            METHODS[4]: "eps_expectation",
            METHODS[5]: "bvps_expectation",
        }[method]
        values = dict(INPUT[group])
        values.update(changes or {})
        selection["scenario"] = dict(
            scenario_id=str(uuid4()),
            label="CUSTOM",
            method=method,
            assumptions=[assumption(k, v) for k, v in values.items()],
        )
    return ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="BANK_PROFILE" if bank else "IT_SERVICES_PROFILE",
            classification=dict(
                classification="BANK" if bank else "NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            selections=[selection],
        )
    )


def evaluate(source, req, **kwargs):
    return valuation.evaluate(
        req, valuation.Evidence(req, source, {}, {}, ScreenerSemanticResolver()), **kwargs
    )


def values(result):
    return {v.name: v.value for v in result.values}


@pytest.mark.parametrize("method", METHODS)
async def test_six_independent_goldens(evidence, method):
    _, source = evidence
    snapshot = evaluate(source, request(source, method, bank=method == METHODS[5]))
    result = snapshot.results[0]
    assert result.status == "AVAILABLE", result.issues
    actual = values(result)
    for name, expected in GOLD[method].items():
        if name in actual:
            assert actual[name] == Decimal(expected)
    if method == METHODS[3]:
        assert result.solver.iterations == 1 and result.solver.final_residual == 0
        assert result.solver.lower_residual == -20
        with localcontext() as ctx:
            ctx.prec = 80
            assert abs(result.solver.upper_residual - Decimal(240) / 11) < Decimal("1e-65")
    assert ValuationSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert "POINT_IN_TIME_AVAILABILITY_UNVERIFIED" in [i.code for i in result.issues]
    assert result.definition.classification == (
        "CALCULATION" if method in METHODS[:2] else "SCENARIO"
    )


@pytest.mark.parametrize(
    "symbol,file,bank",
    [("INFY", "infy_standalone", False), ("YESBANK", "yesbank_consolidated", True)],
)
async def test_real_source_and_separate_reported_multiple(tmp_path, symbol, file, bank):
    settings = Settings(database_path=tmp_path / "real.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await acquire(settings, tmp_path / "real", f"screener/{file}.html", symbol)
    snapshot = evaluate(source, request(source, METHODS[0], bank=bank))
    result = snapshot.results[0]
    assert result.status == "AVAILABLE"
    n, d = GOLD["real"][symbol]["pe_fraction"]
    with localcontext() as ctx:
        ctx.prec = 80
        assert abs(values(result)["multiple"] - Decimal(n) / Decimal(d)) < Decimal("1e-45")
    assert snapshot.source_reported_multiples[0].value == Decimal(GOLD["real"][symbol]["source_pe"])
    assert values(result)["multiple"] != snapshot.source_reported_multiples[0].value
    refusal = evaluate(source, request(source, METHODS[1], bank=bank)).results[0]
    assert refusal.status == "UNVERIFIED_METRIC_SEMANTICS" and not refusal.values


async def test_source_reported_multiple_cannot_satisfy_calculated_denominator(evidence):
    # The source-reported "Stock P/E" observation must never silently stand in for the
    # calculated PRICE_TO_EARNINGS EPS denominator, even if directly pinned by ID.
    _, source = evidence
    reported = next(o for o in source.top_metrics if o.metric_id == "price_earnings")
    assert reported.unit.kind != "money"
    data = request(source, METHODS[0]).model_dump()
    data["selections"][0]["denominator"] = dict(
        kind="FINANCIAL", snapshot_id=str(source.metadata.id), observation_id=str(reported.id)
    )
    result = evaluate(source, ValuationRequest.model_validate(data)).results[0]
    assert result.status == "UNVERIFIED_METRIC_SEMANTICS" and not result.values
    assert result.inputs[-1].value == reported.value
    assert result.inputs[-1].semantic_verified is False


@pytest.mark.parametrize("method", [METHODS[0], METHODS[1]])
@pytest.mark.parametrize("value,prefix", [("0", "ZERO"), ("-1", "NEGATIVE")])
async def test_multiple_nonpositive_denominator(evidence, method, value, prefix):
    _, source = evidence
    req = request(source, method)
    section = "profit_loss" if method == METHODS[0] else "balance_sheet"
    original = getattr(source, section)[0]
    source = source.model_copy(
        update={section: (original.model_copy(update={"value": Decimal(value)}),)}
    )
    result = evaluate(source, req).results[0]
    assert result.status == "INVALID_INPUT"
    assert f"{prefix}_{'EPS' if method == METHODS[0] else 'BVPS'}_MULTIPLE_NOT_MEANINGFUL" in [
        i.code for i in result.issues
    ]
    assert result.inputs[-1].value == Decimal(value)


@pytest.mark.parametrize("method", [METHODS[2], METHODS[3]])
async def test_bank_enterprise_refusal(evidence, method):
    _, source = evidence
    r = evaluate(source, request(source, method, bank=True)).results[0]
    assert r.status == "NOT_APPLICABLE"


async def test_it_bank_expectation_refusal(evidence):
    _, source = evidence
    assert evaluate(source, request(source, METHODS[5])).results[0].status == "NOT_APPLICABLE"


@pytest.mark.parametrize("profile", ["UNKNOWN", "MANUFACTURING_PROFILE"])
async def test_unknown_profile(evidence, profile):
    _, source = evidence
    req = request(source, METHODS[0]).model_copy(update={"profile_id": profile})
    assert evaluate(source, req).results[0].status == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "method,change",
    [
        (METHODS[2], {"diluted_shares": "0"}),
        (METHODS[2], {"diluted_shares": "-1"}),
        (METHODS[2], {"wacc": "0"}),
        (METHODS[2], {"wacc": "-0.1"}),
        (METHODS[2], {"terminal_growth": "-1"}),
        (METHODS[2], {"horizon": "0"}),
        (METHODS[2], {"horizon": "1.5"}),
        (METHODS[2], {"horizon": "3"}),
        (METHODS[3], {"starting_fcff": "0"}),
        (METHODS[3], {"lower_growth": "-1"}),
        (METHODS[3], {"lower_growth": "0.2"}),
        (METHODS[3], {"lower_growth": "0.3"}),
        (METHODS[4], {"required_return": "-1"}),
        (METHODS[4], {"required_return": "-1.1"}),
        (METHODS[4], {"exit_pe": "0"}),
        (METHODS[4], {"exit_pe": "-2"}),
        (METHODS[5], {"exit_pb": "0"}),
        (METHODS[5], {"exit_pb": "-2"}),
    ],
)
async def test_scenario_invalid_inputs(evidence, method, change):
    _, source = evidence
    result = evaluate(source, request(source, method, change, bank=method == METHODS[5])).results[0]
    assert result.status == "INVALID_INPUT" and not result.values


@pytest.mark.parametrize("case", ["lower", "upper", "no_root", "nonconverge", "negative_base"])
async def test_reverse_boundaries(evidence, case):
    _, source = evidence
    change = {
        "lower": {"lower_growth": "0.1"},
        "upper": {"upper_growth": "0.1"},
        "no_root": {"lower_growth": "0.2", "upper_growth": "0.3"},
        "nonconverge": {"lower_growth": "-0.5", "upper_growth": "0.5"},
        # Signed model: base=-100, net debt=-2200 gives price=100 at 10% growth.
        "negative_base": {"starting_fcff": "-100", "net_debt": "-2200"},
    }[case]
    req = request(source, METHODS[3], change)
    if case == "nonconverge":
        s = req.selections[0]
        sc = s.scenario.model_copy(
            update={"solver": s.scenario.solver.model_copy(update={"max_iterations": 1})}
        )
        req = req.model_copy(update={"selections": (s.model_copy(update={"scenario": sc}),)})
    r = evaluate(source, req).results[0]
    if case in {"no_root", "nonconverge"}:
        assert r.status == "INVALID_INPUT" and not r.values
        assert (
            "NO_SOLUTION_IN_DECLARED_BRACKET" if case == "no_root" else "SOLVER_DID_NOT_CONVERGE"
        ) in [i.code for i in r.issues]
    else:
        assert r.status == "AVAILABLE" and values(r)["implied_growth"] == 10
        if case in {"lower", "upper"}:
            assert r.solver.iterations == 0
        else:
            assert "NEGATIVE_STARTING_FCFF_SCENARIO" in [i.code for i in r.issues]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"net_debt": "-200"}, "140"),
        ({"fcff_1": "-110"}, "80"),
        ({"fcff_1": "-110", "fcff_2": "-121"}, "-140"),
        ({"fcff_2": "0"}, "-10"),
        ({"net_debt": "2000"}, "-80"),
    ],
)
async def test_signed_forward_scenarios(evidence, change, expected):
    _, source = evidence
    r = evaluate(source, request(source, METHODS[2], change)).results[0]
    assert r.status == "AVAILABLE" and values(r)["scenario_value_per_share"] == Decimal(expected)


@pytest.mark.parametrize("semantic", ["PRICE", "EPS", "BVPS"])
async def test_unverified_semantic(evidence, semantic):
    _, source = evidence
    section = {"PRICE": "top_metrics", "EPS": "profit_loss", "BVPS": "balance_sheet"}[semantic]
    rows = list(getattr(source, section))
    rows[0] = rows[0].model_copy(update={"mapping_status": MetricMappingStatus.UNMAPPED})
    source = source.model_copy(update={section: tuple(rows)})
    result = evaluate(
        source, request(source, METHODS[1] if semantic == "BVPS" else METHODS[0])
    ).results[0]
    assert result.status == "UNVERIFIED_METRIC_SEMANTICS"


@pytest.mark.parametrize("mutation", ["scope", "currency", "ttm"])
async def test_financial_compatibility(evidence, mutation):
    _, source = evidence
    req = request(source, METHODS[0])
    o = source.profit_loss[0]
    if mutation == "scope":
        o = o.model_copy(update={"reporting_scope": ReportingScope.CONSOLIDATED})
    if mutation == "currency":
        o = o.model_copy(update={"unit": o.unit.model_copy(update={"currency": "USD"})})
    if mutation == "ttm":
        o = o.model_copy(
            update={
                "period": o.period.model_copy(
                    update={"frequency": PeriodFrequency.TTM, "end_date": None}
                )
            }
        )
    r = evaluate(source.model_copy(update={"profit_loss": (o,)}), req).results[0]
    assert (
        r.status
        == {
            "scope": "INCOMPATIBLE_SCOPE",
            "currency": "INVALID_INPUT",
            "ttm": "INCOMPATIBLE_PERIOD",
        }[mutation]
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", 0.1, True])
async def test_no_inexact_or_nonfinite_assumptions(value):
    with pytest.raises(ValidationError):
        ValuationAssumption.model_validate(assumption("wacc", value))


@pytest.mark.parametrize(
    "case", ["system", "missing_declaration", "missing_source", "fact", "exit_source"]
)
async def test_assumption_provenance_contract(case):
    data = assumption("exit_pe" if case == "exit_source" else "wacc", "0.1")
    if case == "system":
        data["provenance_basis"] = "SYSTEM_DEFINED"
    if case == "missing_declaration":
        data["declaration"] = ""
    if case in {"missing_source", "exit_source"}:
        data["provenance_basis"] = "SOURCE_EVIDENCE"
    if case == "fact":
        data["classification"] = "FACT"
    with pytest.raises(ValidationError):
        ValuationAssumption.model_validate(data)


@pytest.mark.parametrize("kind", ["financial", "observation", "company", "calculation"])
async def test_wrong_pinned_identity(evidence, kind):
    _, source = evidence
    req = request(source, METHODS[0])
    data = req.model_dump()
    if kind == "financial":
        data["financial_extraction_id"] = uuid4()
    elif kind == "company":
        data["company_id"] = uuid4()
    elif kind == "observation":
        data["selections"][0]["price"]["observation_id"] = uuid4()
    else:
        data["selections"][0]["denominator"] = dict(
            kind="CALCULATION", snapshot_id=uuid4(), metric_id="EPS"
        )
    with pytest.raises(ValueError):
        evaluate(source, ValuationRequest.model_validate(data))


@pytest.mark.parametrize("method", [METHODS[0], METHODS[4]])
async def test_bank_eps_methods_are_eligible(evidence, method):
    _, source = evidence
    assert evaluate(source, request(source, method, bank=True)).results[0].status == "AVAILABLE"


@pytest.mark.parametrize("method", [METHODS[4], METHODS[5]])
@pytest.mark.parametrize("value", ["0", "-1"])
async def test_nonpositive_expectation_base(evidence, method, value):
    _, source = evidence
    req = request(source, method, bank=method == METHODS[5])
    section = "profit_loss" if method == METHODS[4] else "balance_sheet"
    observation = getattr(source, section)[0].model_copy(update={"value": Decimal(value)})
    result = evaluate(source.model_copy(update={section: (observation,)}), req).results[0]
    assert result.status == "INVALID_INPUT" and not result.values
