"""Box 11 forecast-integrity boundary: a Box 10 number is used only if Box 10's own formula
reproduces it from the authoritative objects it references (never from the copies)."""

import time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_fundamental_valuation import (
    NOW,
    VM,
    B,
    D,
    K,
    L,
    S,
    a,
    forecast,
    make,
    multiple,
    request,
    rows,
)

from stock_research_ai.application import fundamental_valuation as engine
from stock_research_ai.application.forecast_integrity import (
    ForecastIntegrityError,
    verify_forecast_integrity,
)
from stock_research_ai.application.forecasting import build_forecast, evaluate
from stock_research_ai.application.fundamental_valuation import (
    ValuationInputError,
    build_valuation,
    canonical_valuation_json,
)
from stock_research_ai.domain.financial_canonical import CanonicalFinancialDataset
from stock_research_ai.domain.forecasting import (
    ForecastDataset,
    ForecastFormula,
    ForecastLine,
    ForecastRequest,
    ForecastValue,
    Scenario,
)

ALL = (VM.DCF_FCFF_GORDON, VM.PE, VM.EV_EBITDA)


def full_request() -> Any:
    return request(
        methods=ALL, multiples=[multiple(m, 10, s) for m in (VM.PE, VM.EV_EBITDA) for s in S]
    )


def pick(f: ForecastDataset, line: ForecastLine, s: Scenario, y: int) -> ForecastValue:
    return f.value(line, s, y)


def rebuild(f: ForecastDataset, **changes: Any) -> ForecastDataset:
    """Structurally valid (the Box 10 contract re-validates) forged dataset."""
    return ForecastDataset.model_validate({**f.model_dump(), **changes})


def forge(
    f: ForecastDataset,
    target: UUID,
    new: Decimal,
    *,
    cascade: bool,
    actual: bool = False,
) -> ForecastDataset:
    """Changes one number and patches every copy of it held by its consumers. With `cascade`,
    every downstream value is also recomputed by Box 10's formulas (and its copies patched), so
    only the forged number itself is not its formula's result."""
    changed: dict[UUID, Decimal] = {target: new}
    actuals = [
        v.model_copy(
            update={"value": new, "actual": v.actual.model_copy(update={"value": new})}
            if v.actual
            else {"value": new}
        )
        if v.value_id == target and actual
        else v
        for v in f.actuals
    ]
    values: list[ForecastValue] = []
    for v in f.values:  # Box 10 emits values in calculation order
        if v.value_id == target:
            v = v.model_copy(update={"value": new})
        elif any(i.ref_id in changed for i in v.inputs):
            inputs = tuple(
                i.model_copy(update={"value": changed[i.ref_id]}) if i.ref_id in changed else i
                for i in v.inputs
            )
            update: dict[str, Any] = {"inputs": inputs}
            if cascade and v.formula_id:
                update["value"] = evaluate(v.formula_id, {i.role: i.value for i in inputs})
                changed[v.value_id] = update["value"]
            v = v.model_copy(update=update)
        values.append(v)
    return rebuild(
        f,
        actuals=[x.model_dump() for x in actuals],
        values=[x.model_dump() for x in values],
    )


def rewire(f: ForecastDataset, consumer: UUID, role: str, **update: Any) -> ForecastDataset:
    values = [
        v.model_copy(
            update={
                "inputs": tuple(
                    i.model_copy(update=update) if i.role == role else i for i in v.inputs
                )
            }
        )
        if v.value_id == consumer
        else v
        for v in f.values
    ]
    return rebuild(f, values=[x.model_dump() for x in values])


def refused(
    f: ForecastDataset,
    box4: CanonicalFinancialDataset | None,
    monkeypatch: pytest.MonkeyPatch,
    req: Any = None,
) -> ValuationInputError:
    """Rejected before any valuation step runs (the scenario valuation is never entered)."""

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("valuation started on a forged forecast")

    monkeypatch.setattr(engine, "value_scenario", never)
    monkeypatch.setattr(engine, "_shared", never)
    with pytest.raises(ValuationInputError) as err:
        build_valuation(f, request=req or full_request(), box4=box4, generated_at=NOW)
    assert err.value.code == "FORECAST_INTEGRITY_FAILED"
    assert len(str(err.value)) < 300  # names the value, never dumps the forecast
    return err.value


# --- A / B: the audit's two forgeries ---------------------------------------------------------


@pytest.mark.parametrize("with_box4", [True, False])
def test_pat_attributable_one_field_forgery_is_rejected(monkeypatch, with_box4):
    f, box4 = forecast()
    pat = pick(f, L.PAT_ATTRIBUTABLE, S.BASE, 2027)
    assert pick(f, L.EPS, S.BASE, 2027).status == "UNAVAILABLE"  # no Box 10 consumer
    forged = rebuild(
        f,
        values=[
            (
                v.model_copy(update={"value": pat.value * 3}) if v.value_id == pat.value_id else v
            ).model_dump()
            for v in f.values
        ],
    )
    err = refused(forged, box4 if with_box4 else None, monkeypatch)
    assert "PAT_ATTRIBUTABLE BASE FY2027" in str(err)


@pytest.mark.parametrize("cascade", [False, True])
def test_capex_chain_forgery_is_rejected(monkeypatch, cascade):
    f, box4 = forecast()
    capex = pick(f, L.CAPEX, S.BASE, 2029)
    forged = forge(f, capex.value_id, capex.value + 500, cascade=cascade)
    assert pick(forged, L.CAPEX, S.BASE, 2029).value == capex.value + 500
    err = refused(forged, box4, monkeypatch)
    assert "CAPEX BASE FY2029" in str(err)


# --- C-F: other consumed lines, each with its copies (and downstream) patched -----------------


@pytest.mark.parametrize(
    ("line", "year"),
    [
        (L.OPERATING_PROFIT, 2027),
        (L.DEPRECIATION_AMORTIZATION, 2028),
        (L.WORKING_CAPITAL_CHANGE, 2029),
        (L.TRADE_WORKING_CAPITAL, 2027),
        (L.EBITDA, 2028),
        (L.PAT, 2027),
        (L.TAX, 2029),
        (L.PBT, 2027),
        (L.REVENUE, 2027),  # used recursively by FY2028 / FY2029
        (L.GROSS_DEBT, 2027),  # opening debt of FY2028
    ],
)
@pytest.mark.parametrize("scenario", [S.BASE, S.BEAR])
@pytest.mark.parametrize("cascade", [False, True])
def test_consumed_line_forgery_is_rejected(monkeypatch, line, year, scenario, cascade):
    f, box4 = forecast(tax=25, da=5)
    target = pick(f, line, scenario, year)
    forged = forge(f, target.value_id, target.value + 7, cascade=cascade)
    err = refused(forged, box4, monkeypatch)
    assert f"{line.value} {scenario.value} FY{year}" in str(err)


def test_prior_year_forgery_used_recursively_is_rejected(monkeypatch):
    f, box4 = forecast()
    revenue = pick(f, L.REVENUE, S.BASE, 2027)
    forged = forge(f, revenue.value_id, revenue.value * 2, cascade=True)
    later = pick(forged, L.REVENUE, S.BASE, 2028)
    assert later.value == revenue.value * 2  # FY2028 recomputed from the forged FY2027
    assert next(i for i in later.inputs if i.role == "PRIOR_REVENUE").value == later.value
    err = refused(forged, box4, monkeypatch)
    assert "REVENUE BASE FY2027" in str(err)


def test_forged_explicit_assumption_with_consistent_downstream_is_rejected(monkeypatch):
    f, box4 = forecast(tax=25)
    tax = f.assumption(D.TAX_RATE, S.BASE, 2027)
    assert tax is not None
    assumptions = [
        (x.model_copy(update={"value": Decimal(0)}) if x.assumption_id == tax.assumption_id else x)
        for x in f.assumptions
    ]
    changed = {tax.assumption_id: Decimal(0)}
    values = []
    for v in f.values:
        if any(i.ref_id in changed for i in v.inputs):
            inputs = tuple(
                i.model_copy(update={"value": changed[i.ref_id]}) if i.ref_id in changed else i
                for i in v.inputs
            )
            new = evaluate(v.formula_id or "", {i.role: i.value for i in inputs})
            changed[v.value_id] = new
            v = v.model_copy(update={"inputs": inputs, "value": new})
        values.append(v.model_dump())
    forged = rebuild(f, assumptions=[x.model_dump() for x in assumptions], values=values)
    tax_line = forged.value(L.TAX, S.BASE, 2027)
    assert tax_line.value == 0  # every number is its formula's result
    err = refused(forged, box4, monkeypatch)
    assert "TAX_RATE BASE FY2027" in str(err)


def test_gross_debt_anchor_forgery_is_rejected_against_box4(monkeypatch):
    f, box4 = forecast()
    anchor = next(v for v in f.actuals if v.line == L.GROSS_DEBT)
    forged = forge(f, anchor.value_id, Decimal(1), cascade=True, actual=True)
    err = refused(forged, box4, monkeypatch)
    assert "GROSS_DEBT ACTUAL" in str(err)
    # without Box 4 the anchor's Box 4 lineage is trusted as recorded (documented boundary),
    # but a value that disagrees with its own recorded lineage is not
    lying = rebuild(
        f,
        actuals=[
            (
                v.model_copy(update={"value": Decimal(1)}) if v.value_id == anchor.value_id else v
            ).model_dump()
            for v in f.actuals
        ],
        values=[
            v.model_copy(
                update={
                    "inputs": tuple(
                        i.model_copy(update={"value": Decimal(1)})
                        if i.ref_id == anchor.value_id
                        else i
                        for i in v.inputs
                    )
                }
            ).model_dump()
            for v in f.values
        ],
    )
    refused(lying, None, monkeypatch)


# --- G-K: graph tampering ---------------------------------------------------------------------


def test_unregistered_formula_id_is_rejected(monkeypatch):
    f, box4 = forecast()
    op = pick(f, L.OPERATING_PROFIT, S.BASE, 2027)
    fake = ForecastFormula(
        formula_id="OPERATING_PROFIT/9", line=L.OPERATING_PROFIT, expression="x", inputs=()
    )
    forged = rebuild(
        f,
        manifest={
            **f.manifest.model_dump(),
            "formulas": [*f.manifest.model_dump()["formulas"], fake.model_dump()],
        },
        values=[
            (
                v.model_copy(update={"formula_id": "OPERATING_PROFIT/9"}) if v == op else v
            ).model_dump()
            for v in f.values
        ],
    )
    assert "manifest" in str(refused(forged, box4, monkeypatch))


@pytest.mark.parametrize(
    ("line", "formula_id"),
    [
        (L.OPERATING_PROFIT, "OTHER_INCOME/1"),  # another line's formula
        (L.CAPEX, "CAPEX/1/RATIO_TO_REVENUE"),  # not the one Box 10 applies (amount given)
        (L.PAT_ATTRIBUTABLE, "PAT_ATTRIBUTABLE/1/LESS_MINORITY"),  # standalone basis
    ],
)
def test_formula_id_tamper_is_rejected(monkeypatch, line, formula_id):
    f, box4 = forecast()
    target = pick(f, line, S.BASE, 2027)
    forged = rebuild(
        f,
        values=[
            (v.model_copy(update={"formula_id": formula_id}) if v == target else v).model_dump()
            for v in f.values
        ],
    )
    refused(forged, box4, monkeypatch)


def test_formula_choice_is_bound_to_the_basis(monkeypatch):
    """Consolidated PAT_ATTRIBUTABLE cannot be passed off as standalone (= total PAT)."""
    f, box4 = forecast(basis=B.CONSOLIDATED, minority=10)
    target = pick(f, L.PAT_ATTRIBUTABLE, S.BASE, 2027)
    pat = pick(f, L.PAT, S.BASE, 2027)
    swapped = target.model_copy(
        update={
            "formula_id": "PAT_ATTRIBUTABLE/1/STANDALONE",
            "formula_text": "PAT (a standalone entity has no minority interest)",
            "inputs": (target.inputs[0],),
            "value": pat.value,
        }
    )
    forged = rebuild(f, values=[(swapped if v == target else v).model_dump() for v in f.values])
    req = request(
        methods=(VM.PE,),
        multiples=[multiple(VM.PE, 15, s) for s in S],
        extra=[a(K.MINORITY_INTEREST, 150)],
    )
    refused(forged, box4, monkeypatch, req)


def test_input_reference_to_another_line_is_rejected(monkeypatch):
    """OPERATING_PROFIT FY2027 REVENUE -> the actual REVENUE anchor (same value, valid schema)."""
    f, box4 = forecast()
    anchor = next(v for v in f.actuals if v.line == L.REVENUE)
    op = pick(f, L.OPERATING_PROFIT, S.BASE, 2027)
    assert next(i for i in op.inputs if i.role == "REVENUE").value == anchor.value
    forged = rewire(f, op.value_id, "REVENUE", kind="ACTUAL", ref_id=anchor.value_id)
    refused(forged, box4, monkeypatch)
    da = pick(f, L.DEPRECIATION_AMORTIZATION, S.BASE, 2027)
    forged = rewire(
        f, da.value_id, "DA_TO_REVENUE", ref_id=f.assumption(D.TAX_RATE, S.BASE, 2027).assumption_id
    )
    refused(forged, box4, monkeypatch)


def test_fiscal_year_reference_tamper_is_rejected(monkeypatch):
    f, box4 = forecast()
    # same-year input pointed at last year's (equal) revenue
    op = pick(f, L.OPERATING_PROFIT, S.BASE, 2028)
    prior = pick(f, L.REVENUE, S.BASE, 2027)
    assert prior.value == pick(f, L.REVENUE, S.BASE, 2028).value
    refused(rewire(f, op.value_id, "REVENUE", ref_id=prior.value_id), box4, monkeypatch)
    # opening debt of FY2028 pointed at the FY2026 anchor instead of FY2027 closing debt
    debt = pick(f, L.GROSS_DEBT, S.BASE, 2028)
    anchor = next(v for v in f.actuals if v.line == L.GROSS_DEBT)
    forged = rewire(f, debt.value_id, "OPENING_GROSS_DEBT", kind="ACTUAL", ref_id=anchor.value_id)
    refused(forged, box4, monkeypatch)
    # an assumption from another year (equal value)
    tax = f.assumption(D.TAX_RATE, S.BASE, 2029)
    t27 = pick(f, L.TAX, S.BASE, 2027)
    refused(rewire(f, t27.value_id, "TAX_RATE", ref_id=tax.assumption_id), box4, monkeypatch)


def test_scenario_reference_tamper_is_rejected():
    f, box4 = forecast()
    op = pick(f, L.OPERATING_PROFIT, S.BASE, 2027)
    bull = pick(f, L.REVENUE, S.BULL, 2027)
    assert bull.value == pick(f, L.REVENUE, S.BASE, 2027).value
    with pytest.raises(ValidationError):  # the Box 10 contract already refuses it
        rewire(f, op.value_id, "REVENUE", ref_id=bull.value_id)
    forged = ForecastDataset.model_construct(
        **{
            **dict(f),
            "values": tuple(
                v.model_copy(
                    update={
                        "inputs": tuple(
                            i.model_copy(update={"ref_id": bull.value_id})
                            if i.role == "REVENUE"
                            else i
                            for i in v.inputs
                        )
                    }
                )
                if v == op
                else v
                for v in f.values
            ),
        }
    )
    with pytest.raises(ForecastIntegrityError, match="OPERATING_PROFIT BASE FY2027"):
        verify_forecast_integrity(forged, box4)  # and so does the integrity check on its own
    with pytest.raises(ValidationError):
        build_valuation(forged, request=full_request(), box4=box4, generated_at=NOW)


def test_copied_input_value_tamper_alone_is_rejected():
    f, box4 = forecast()
    fcf = pick(f, L.FCF_ESTIMATE, S.BASE, 2029)
    with pytest.raises(ValidationError):
        rewire(f, fcf.value_id, "CAPEX", value=Decimal(1))
    forged = ForecastDataset.model_construct(
        **{
            **dict(f),
            "values": tuple(
                v.model_copy(
                    update={
                        "inputs": tuple(
                            i.model_copy(update={"value": Decimal(1)}) if i.role == "CAPEX" else i
                            for i in v.inputs
                        )
                    }
                )
                if v == fcf
                else v
                for v in f.values
            ),
        }
    )
    with pytest.raises(ForecastIntegrityError, match="copied CAPEX"):
        verify_forecast_integrity(forged, box4)
    with pytest.raises(ValidationError):
        build_valuation(forged, request=full_request(), box4=box4, generated_at=NOW)


def test_duplicate_line_and_duplicate_assumption_are_rejected(monkeypatch):
    f, box4 = forecast()
    pat = pick(f, L.PAT_ATTRIBUTABLE, S.BASE, 2027)
    twin = pat.model_copy(update={"value_id": UUID(int=7), "value": pat.value * 2})
    refused(
        rebuild(f, values=[*(v.model_dump() for v in f.values), twin.model_dump()]),
        box4,
        monkeypatch,
    )
    tax = f.assumption(D.TAX_RATE, S.BASE, 2027)
    twin_tax = tax.model_copy(update={"assumption_id": UUID(int=8), "value": Decimal(50)})
    refused(
        rebuild(f, assumptions=[*(x.model_dump() for x in f.assumptions), twin_tax.model_dump()]),
        box4,
        monkeypatch,
    )


def test_cycle_is_refused_without_recursion():
    f, _ = forecast()
    ebit = pick(f, L.EBIT, S.BASE, 2027)
    ebitda = pick(f, L.EBITDA, S.BASE, 2027)
    looped = ForecastDataset.model_construct(
        **{
            **dict(f),
            "values": tuple(
                v.model_copy(
                    update={
                        "inputs": tuple(
                            i.model_copy(update={"ref_id": ebit.value_id})
                            if i.role == "OPERATING_PROFIT"
                            else i
                            for i in v.inputs
                        )
                    }
                )
                if v == ebitda
                else v
                for v in f.values
            ),
        }
    )
    with pytest.raises(ForecastIntegrityError, match="cycle"):
        verify_forecast_integrity(looped)


# --- L: valid forecasts are accepted and unchanged --------------------------------------------


def test_valid_forecasts_are_accepted_with_identical_results(monkeypatch):
    cases = [
        forecast(),
        forecast(tax=25, da=5),
        forecast(basis=B.CONSOLIDATED, minority=10),
        forecast({s: [100 + 10 * i + k for i in range(5)] for k, s in enumerate(S)}),
    ]
    data = cases[0][1]
    cases.append((build_forecast(data, request=ForecastRequest(horizon=5), generated_at=NOW), data))
    for f, box4 in cases:
        verify_forecast_integrity(f, box4)
        verify_forecast_integrity(f)
        req = request(
            methods=ALL,
            multiples=[multiple(m, 10, s) for m in (VM.PE, VM.EV_EBITDA) for s in S],
            extra=[a(K.MINORITY_INTEREST, 150)] if f.base.reporting_basis == B.CONSOLIDATED else [],
        )
        checked = build_valuation(f, request=req, box4=box4, generated_at=NOW)
        with monkeypatch.context() as m:
            m.setattr(engine, "verify_forecast_integrity", lambda *_: None)
            unchecked = build_valuation(f, request=req, box4=box4, generated_at=NOW)
        assert canonical_valuation_json(checked) == canonical_valuation_json(unchecked)


def test_existing_structural_and_box4_detection_is_kept():
    f, box4 = forecast()
    with pytest.raises(ValidationError):  # reversed fiscal years
        rebuild(f, forecast_years=[p.model_dump() for p in reversed(f.forecast_years)])
    with pytest.raises(ValidationError):  # tax rate out of its driver's bounds
        rebuild(
            f,
            assumptions=[
                (
                    x.model_copy(update={"value": Decimal(101)}) if x.driver == D.TAX_RATE else x
                ).model_dump()
                for x in f.assumptions
            ],
        )
    with pytest.raises(ValuationInputError) as err:
        build_valuation(f, request=full_request(), box4=make(rows(cash=301)), generated_at=NOW)
    assert err.value.code == "BOX4_MISMATCH"


def test_integrity_check_cost_is_small():
    for horizon in (3, 5):
        f, box4 = forecast([100 + 10 * i for i in range(horizon)])
        started = time.perf_counter()
        for _ in range(20):
            verify_forecast_integrity(f, box4)
        elapsed = (time.perf_counter() - started) / 20
        assert elapsed < 0.25, (horizon, elapsed)
