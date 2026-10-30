"""SUMMARY_V1 Box 3 step 1: prompt contract and pure generation over a fake model only."""

import ast
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_summary_input import INFY, YESBANK, replay

from stock_research_ai.application import summary_generation
from stock_research_ai.application.summary import canonical_summary_json, project_summary_input
from stock_research_ai.application.summary_generation import (
    SYSTEM_PROMPT,
    build_summary_claims,
    build_summary_messages,
    generate_summary,
    serialized_request_bytes,
)
from stock_research_ai.domain.financial import (
    FinancialUnit,
    ReportingScope,
    UnitKind,
    ValueStatus,
)
from stock_research_ai.domain.summary import (
    SUMMARY_REQUEST_MAX_BYTES,
    SummaryBlock,
    SummaryCompany,
    SummaryErrorCode,
    SummaryGenerationError,
    SummaryInput,
    SummaryMetric,
    SummaryModelOutput,
    SummaryResult,
    SummaryRow,
    SummaryUsage,
    SummaryValue,
)

MODEL = "anthropic/claude-sonnet-5"
NOW = datetime(2026, 10, 5, 9, 30, tzinfo=UTC)
SUMMARY_ID = UUID("00000000-0000-4000-8000-000000000001")
FAKE_KEY = "sk-test-only-not-a-real-credential"
VALID_TEXT = '{"claim_ids":[]}'


class FakeModel:
    def __init__(self, **overrides: object) -> None:
        fields: dict[str, object] = {
            "summary_text": VALID_TEXT,
            "requested_model": MODEL,
            "reported_model": "claude-sonnet-5",
            "finish_reason": "stop",
            "usage": SummaryUsage(prompt_tokens=3100, completion_tokens=700),
        }
        fields.update(overrides)
        self.output = SummaryModelOutput.model_validate(fields)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> SummaryModelOutput:
        self.calls.append((system, user))
        return self.output


async def summary_input(tmp_path: Path, fixture_dir: Path, case=INFY) -> SummaryInput:
    return project_summary_input(await replay(tmp_path, fixture_dir, case))


async def generate(value: SummaryInput, model: FakeModel) -> SummaryResult:
    return await generate_summary(value, model, now=NOW, summary_id=SUMMARY_ID)


#: the observations a calculated ratio claim cites (numerator and denominator)
RATIO_INPUTS = {"operating_margin": {"operating_profit", "sales", "revenue"},
                "cash_conversion": {"cash_operating", "net_profit"},
                "derived_price_earnings": {"current_price", "eps"}}

def selection(value: SummaryInput, *claim_ids: str) -> str:
    return json.dumps({"claim_ids": list(claim_ids)})


def synthetic_input(
    *,
    company_name: str = "Example Ltd",
    scope: ReportingScope = ReportingScope.STANDALONE,
    rows: tuple[SummaryRow, ...] = (),
    snapshot: tuple[SummaryMetric, ...] = (),
) -> SummaryInput:
    company = SummaryCompany(
        name=company_name,
        source_company_key="SYNTHETIC-1",
        reporting_scope=scope,
        source_url="https://www.screener.in/company/EXAMPLE/",
        coverage_overall="COMPLETE",
    )
    return SummaryInput(
        company=company,
        snapshot=snapshot,
        annual=SummaryBlock(rows=rows),
        quarterly=SummaryBlock(),
        balance_sheet=SummaryBlock(),
        cash_flow=SummaryBlock(),
    )


def _month_end(label: str) -> date | None:
    """A Screener column label's end date ('Mar 2026' -> 31-Mar-2026), as the projection
    records it."""
    try:
        start = datetime.strptime(label, "%b %Y").date()
    except ValueError:
        return None
    following = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return following - timedelta(days=1)


def grounded_row(
    metric_id: str,
    label: str,
    values: tuple[tuple[str, str], ...],
    *,
    scope: ReportingScope = ReportingScope.STANDALONE,
    unit: FinancialUnit | None = None,
) -> SummaryRow:
    fact_unit = unit or FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=10_000_000)
    return SummaryRow(
        label=label,
        metric_id=metric_id,
        unit=fact_unit,
        reporting_scope=scope,
        values=tuple(
            SummaryValue(
                period=period,
                period_end=_month_end(period),
                value=value,
                status=ValueStatus.AVAILABLE,
                source_observation_id=UUID(int=i + 100),
                source_artifact_id=UUID(int=i + 200),
                source_location=f"table/row/{i}",
            )
            for i, (period, value) in enumerate(values)
        ),
    )


async def test_prompt_is_byte_identical_for_the_same_input(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    first = build_summary_messages(value)
    second = build_summary_messages(value)
    assert [m.encode() for m in first] == [m.encode() for m in second]
    catalog = json.loads(first[1].split("\n", 1)[1])
    assert (
        json.dumps(catalog, separators=(",", ":"), ensure_ascii=False) == first[1].split("\n", 1)[1]
    )


async def test_infy_prompt_contract(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    system, user = build_summary_messages(value)
    assert system.startswith("SUMMARY_PROMPT_V2\n")
    assert system == SYSTEM_PROMPT
    assert user.startswith("SUMMARY_CLAIM_CATALOG_V2 JSON:\n")
    catalogue = json.loads(user.split("\n", 1)[1])
    assert catalogue["schema_version"] == "SUMMARY_CLAIM_CATALOG_V2"
    assert {claim["id"] for claim in catalogue["claims"]}
    assert any("Revenue" in claim["statement"] for claim in catalogue["claims"])
    assert any("Operating profit" in claim["statement"] for claim in catalogue["claims"])
    # Screener's profit is named as Screener's concept (it can include NCI), never 'PAT'
    assert any("Net profit (Screener definition)" in claim["statement"]
               for claim in catalogue["claims"])
    assert any("EPS" in claim["statement"] for claim in catalogue["claims"])
    for rule in (
        "Do not write prose, numbers, labels,",
        "Return only a JSON object",
        "No other keys or text",
    ):
        assert rule in system
    assert serialized_request_bytes(system, user) < SUMMARY_REQUEST_MAX_BYTES


async def test_yesbank_prompt_preserves_bank_terminology_and_scope(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir, YESBANK)
    system, user = build_summary_messages(value)
    assert '"reporting_scope":"consolidated"' in user
    catalog = json.loads(user.split("\n", 1)[1])
    statements = [claim["statement"] for claim in catalog["claims"]]
    assert any("Financing profit" in item for item in statements)
    assert any("Gross NPA ratio" in item for item in statements)
    assert any("Deposits" in item for item in statements)
    assert not any("Operating profit" in item for item in statements)
    assert serialized_request_bytes(system, user) < SUMMARY_REQUEST_MAX_BYTES


async def test_prompt_contains_no_clock_or_random_values(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    system, user = build_summary_messages(value)
    assert NOW.isoformat() not in system + user
    assert str(SUMMARY_ID) not in system + user


async def test_oversized_request_is_rejected_before_any_model_call(
    tmp_path, fixture_dir, monkeypatch
):
    value = await summary_input(tmp_path, fixture_dir)
    system, user = build_summary_messages(value)
    monkeypatch.setattr(
        summary_generation, "SUMMARY_REQUEST_MAX_BYTES", serialized_request_bytes(system, user) - 1
    )
    model = FakeModel()
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, model)
    assert error.value.code == SummaryErrorCode.SUMMARY_REQUEST_TOO_LARGE
    assert model.calls == []


async def test_oversized_input_is_rejected_before_any_model_call(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    row = SummaryRow(
        label="L" * 120,
        metric_id="sales",
        unit=value.annual.rows[0].unit,
        reporting_scope=ReportingScope.STANDALONE,
        values=tuple(
            SummaryValue(period="P" * 120, value=None, status="missing") for _ in range(4)
        ),
    )
    big = SummaryBlock(rows=(row,) * 16).model_dump()
    oversized = SummaryInput.model_validate({**value.model_dump(), "annual": big, "quarterly": big})
    model = FakeModel()
    with pytest.raises(SummaryGenerationError) as error:
        await generate(oversized, model)
    assert error.value.code == SummaryErrorCode.SUMMARY_INPUT_TOO_LARGE
    assert model.calls == []


async def test_success_calls_model_exactly_once_and_maps_result(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    model = FakeModel()
    result = await generate(value, model)
    assert model.calls == [build_summary_messages(value)]
    assert result.schema_version == "SUMMARY_RESULT_V1"
    assert result.prompt_version == "SUMMARY_PROMPT_V2"
    assert result.provider == "aicredits"
    assert result.summary_id == SUMMARY_ID
    assert result.generated_at == NOW
    assert result.company.name == "Infosys Ltd"
    assert result.company.source_company_key == "1489"
    assert result.company.reporting_scope == ReportingScope.STANDALONE
    assert result.source_url == value.company.source_url
    assert result.data_retrieved_at == value.company.retrieved_at
    assert result.limitations == value.limitations
    assert result.input_sha256 == hashlib.sha256(canonical_summary_json(value)).hexdigest()
    assert result.summary_text.startswith(
        "Company overview\nInfosys Ltd — Standalone figures from the Screener page")
    assert result.grounding_claims
    assert all(claim.text in result.summary_text.splitlines() for claim in result.grounding_claims)
    assert result.requested_model == MODEL
    assert result.reported_model == "claude-sonnet-5"
    assert result.finish_reason == "stop"
    assert result.usage == SummaryUsage(prompt_tokens=3100, completion_tokens=700)


async def test_a_fabricated_revenue_cannot_be_returned_as_summary_text():
    value = synthetic_input(rows=(grounded_row("sales", "Sales", (("Mar 2025", "10000"),)),))
    fabricated = FakeModel(summary_text="Revenue was ₹12,500 Cr in FY2025.")
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, fabricated)
    assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE


async def test_b_grounded_pat_value_and_period_are_accepted():
    value = synthetic_input(
        rows=(grounded_row("net_profit", "Net Profit", (("Mar 2025", "500"),)),)
    )
    claim = next(c for c in build_summary_claims(value) if c.metric_id == "net_profit")
    result = await generate(value, FakeModel(summary_text=selection(value, claim.claim_id)))
    assert claim.text in result.summary_text.splitlines()
    assert claim.values == (500,)
    assert claim.periods == ("Mar 2025",)
    assert len(claim.source_observation_ids) == 1
    assert len(claim.source_artifact_ids) == 1


async def test_c_wrong_period_pat_cannot_be_emitted():
    value = synthetic_input(
        rows=(grounded_row("net_profit", "Net Profit", (("Mar 2025", "500"), ("Mar 2026", "700"))),)
    )
    forged = FakeModel(summary_text="FY2026 PAT was ₹500 Cr.")
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, forged)
    assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE
    claims = build_summary_claims(value)
    actual_2026 = next(
        c for c in claims if c.metric_id == "net_profit" and c.periods == ("Mar 2026",)
    )
    result = await generate(value, FakeModel(summary_text=selection(value, actual_2026.claim_id)))
    assert "FY2026: ₹700 Cr" in result.summary_text
    assert "FY2026: ₹500 Cr" not in result.summary_text


async def test_d_borrowings_cannot_be_relabelled_as_cash():
    value = synthetic_input(
        rows=(grounded_row("borrowings", "Borrowings", (("Mar 2025", "3000"),)),)
    )
    claim = next(c for c in build_summary_claims(value) if c.metric_id == "borrowings")
    result = await generate(value, FakeModel(summary_text=selection(value, claim.claim_id)))
    assert "Borrowings" in result.summary_text
    assert "Cash balance" not in result.summary_text
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, FakeModel(summary_text="Cash balance was ₹3,000 Cr."))
    assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE


async def test_e_unavailable_roce_cannot_be_invented():
    value = synthetic_input(
        rows=(grounded_row("net_profit", "Net Profit", (("Mar 2025", "500"),)),)
    )
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, FakeModel(summary_text="ROCE was 27.4%."))
    assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE
    assert not any(c.metric_id == "roce" for c in build_summary_claims(value))


async def test_f_period_direction_is_deterministic_and_evidence_linked():
    value = synthetic_input(
        rows=(grounded_row("sales", "Sales", (("Mar 2025", "10000"), ("Mar 2026", "12000"))),)
    )
    trend = next(c for c in build_summary_claims(value) if c.kind == "trend")
    result = await generate(value, FakeModel(summary_text=selection(value, trend.claim_id)))
    assert trend.text in result.summary_text.splitlines()
    # periods are named (fiscal years), and the change is stated
    assert "increased from ₹10,000 Cr in FY2025 to ₹12,000 Cr in FY2026" in trend.text
    assert "+20.0%" in trend.text
    assert len(trend.source_observation_ids) == len(trend.source_artifact_ids) == 2


async def test_g_unverified_qualitative_interpretation_cannot_be_emitted():
    value = synthetic_input(
        rows=(grounded_row("borrowings", "Borrowings", (("Mar 2025", "3000"),)),)
    )
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, FakeModel(summary_text="Debt has reduced materially."))
    assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE


async def test_h_unsupported_selection_is_dropped_without_losing_grounded_claims(
    tmp_path, fixture_dir
):
    value = await summary_input(tmp_path, fixture_dir)
    claim = next(c for c in build_summary_claims(value) if c.metric_id == "net_profit")
    invented_id = "c_ffffffffffffffffffff"
    result = await generate(
        value, FakeModel(summary_text=selection(value, claim.claim_id, invented_id))
    )
    # the selected claim is kept, the invented ID dropped; the only other claims shown are
    # the analytical core (like-for-like trends and calculated ratios), always shown
    ids = [c.claim_id for c in result.grounding_claims]
    assert claim.claim_id in ids and invented_id not in ids
    assert all(c.claim_id == claim.claim_id or c.kind in ("trend", "ratio")
               for c in result.grounding_claims)
    assert result.summary_text.count(claim.text) == 1
    assert "No verified financial observations are available" not in result.summary_text


async def test_i_malformed_structured_response_keeps_fail_closed_behavior(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    for malformed in ("partial response", '{"claim_ids":[', '{"claim_ids":[],"summary":"made up"}'):
        with pytest.raises(SummaryGenerationError) as error:
            await generate(value, FakeModel(summary_text=malformed))
        assert error.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE


async def test_j_hudco_summary_remains_usable_with_explicit_consolidated_scope():
    value = synthetic_input(
        company_name="Housing and Urban Development Corporation Ltd",
        scope=ReportingScope.CONSOLIDATED,
        rows=(
            grounded_row(
                "net_profit",
                "Net Profit",
                (("Mar 2026", "500"),),
                scope=ReportingScope.CONSOLIDATED,
            ),
        ),
    )
    result = await generate(value, FakeModel())
    assert (
        "Housing and Urban Development Corporation Ltd — Consolidated figures from the "
        "Screener page" in result.summary_text
    )
    assert (
        "Net profit (Screener definition) — FY2026: ₹500 Cr (Consolidated)."
        in result.summary_text
    )
    assert result.grounding_claims[0].source_observation_ids == (UUID(int=100),)


@pytest.mark.parametrize(
    "case,scope",
    ((INFY, ReportingScope.STANDALONE), (YESBANK, ReportingScope.CONSOLIDATED)),
)
async def test_real_fixture_summary_claims_trace_to_normalized_source_evidence(
    tmp_path, fixture_dir, case, scope
):
    extraction = await replay(tmp_path / case[0], fixture_dir, case)
    value = project_summary_input(extraction)
    result = await generate(value, FakeModel())
    observations = {
        observation.id: observation
        for group in (
            extraction.top_metrics,
            extraction.quarterly,
            extraction.profit_loss,
            extraction.balance_sheet,
            extraction.cashflow,
        )
        for observation in group
    }
    raw_artifacts = {artifact.id for artifact in extraction.raw_artifacts}
    assert result.company.reporting_scope == scope
    assert result.grounding_claims
    for claim in result.grounding_claims:
        assert claim.text in result.summary_text.splitlines()
        scope_name = {
            ReportingScope.STANDALONE: "Standalone",
            ReportingScope.CONSOLIDATED: "Consolidated",
            ReportingScope.UNKNOWN: "Scope unknown",
        }[claim.reporting_scope]
        assert claim.text.endswith((f"({scope_name}).", f"{scope_name}).")) or (
            f"({scope_name}, " in claim.text)
        allowed = RATIO_INPUTS.get(claim.metric_id, {claim.metric_id})
        for index, observation_id in enumerate(claim.source_observation_ids):
            observation = observations[observation_id]
            assert observation.metric_id in allowed
            assert observation.reporting_scope == claim.reporting_scope or (
                observation.metric_id == "current_price")  # a market price has no basis
            assert observation.value == claim.values[index]
            assert observation.period.fiscal_label == claim.periods[index]
            assert observation.provenance.raw_artifact_id == claim.source_artifact_ids[index]
            assert claim.source_artifact_ids[index] in raw_artifacts
            assert observation.provenance.source_location.locator == claim.source_locations[index]


async def test_limitations_are_copied_when_present(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir, YESBANK)
    limited = SummaryInput.model_validate(
        {
            **value.model_dump(),
            "limitations": [{"block": "cash_flow", "code": "MISSING_SECTION"}],
        }
    )
    assert limited.limitations
    result = await generate(limited, FakeModel())
    assert result.limitations == limited.limitations
    assert result.company.reporting_scope == ReportingScope.CONSOLIDATED


@pytest.mark.parametrize("reported", [MODEL, "claude-sonnet-5", None])
async def test_accepted_model_echoes(reported, tmp_path, fixture_dir):
    result = await generate(
        await summary_input(tmp_path, fixture_dir), FakeModel(reported_model=reported)
    )
    assert result.reported_model == reported


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"summary_text": ""}, SummaryErrorCode.SUMMARY_EMPTY),
        ({"summary_text": " \n\t "}, SummaryErrorCode.SUMMARY_EMPTY),
        ({"summary_text": "x" * 8001}, SummaryErrorCode.SUMMARY_TOO_LARGE),
        ({"summary_text": "ok\x00text"}, SummaryErrorCode.UNSAFE_CONTENT),
        ({"summary_text": "ok\x1b[31mtext"}, SummaryErrorCode.UNSAFE_CONTENT),
        ({"summary_text": "ok‮text"}, SummaryErrorCode.UNSAFE_CONTENT),
        ({"finish_reason": "length"}, SummaryErrorCode.SUMMARY_TRUNCATED),
        ({"finish_reason": "tool_calls"}, SummaryErrorCode.SUMMARY_TRUNCATED),
        ({"finish_reason": None}, SummaryErrorCode.SUMMARY_TRUNCATED),
        ({"reported_model": "openai/gpt-x"}, SummaryErrorCode.MODEL_MISMATCH),
        ({"reported_model": "sonnet-5"}, SummaryErrorCode.MODEL_MISMATCH),
    ],
)
async def test_invalid_output_is_rejected_with_a_typed_code(overrides, code, tmp_path, fixture_dir):
    model = FakeModel(**overrides)
    with pytest.raises(SummaryGenerationError) as error:
        await generate(await summary_input(tmp_path, fixture_dir), model)
    assert error.value.code == code
    assert error.value.ambiguous is False
    assert len(model.calls) == 1  # never retried


async def test_text_at_the_limit_is_accepted_and_never_trimmed(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    exact = "a" * 8000
    with pytest.raises(SummaryGenerationError) as invalid:
        await generate(value, FakeModel(summary_text=exact))
    assert invalid.value.code == SummaryErrorCode.INVALID_PROVIDER_RESPONSE
    with pytest.raises(SummaryGenerationError) as error:
        await generate(value, FakeModel(summary_text=exact + "b"))
    assert error.value.code == SummaryErrorCode.SUMMARY_TOO_LARGE


async def test_error_messages_never_carry_provider_prompt_or_secret_text(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    system, user = build_summary_messages(value)
    leaky = f"Authorization: Bearer {FAKE_KEY} PROVIDER-BODY-MARKER\x00" + "x" * 8001
    for overrides in (
        {"summary_text": leaky},
        {"summary_text": "PROVIDER-BODY-MARKER\x00"},
        {"summary_text": "PROVIDER-BODY-MARKER", "finish_reason": "length"},
        {"summary_text": "PROVIDER-BODY-MARKER", "reported_model": "other/model"},
    ):
        with pytest.raises(SummaryGenerationError) as error:
            await generate(value, FakeModel(**overrides))
        rendered = f"{error.value} {error.value.message!r} {error.value.args!r}"
        for forbidden in ("PROVIDER-BODY-MARKER", FAKE_KEY, "Authorization", system[:60], user):
            assert forbidden not in rendered
        assert error.value.__cause__ is None
    for code in SummaryErrorCode:
        error = SummaryGenerationError(code)
        assert str(error).startswith(code.value + ": ")
        assert isinstance(error.retryable_by_user, bool)
        # Only transport outcomes where the request may already have run are ambiguous.
        assert error.ambiguous is (
            code in {SummaryErrorCode.PROVIDER_TIMEOUT, SummaryErrorCode.PROVIDER_CONNECTION_LOST}
        )


def test_result_contract_is_strict():
    base = {
        "summary_id": str(SUMMARY_ID),
        "company": {"name": "X", "source_company_key": "1", "reporting_scope": "standalone"},
        "source_url": "https://www.screener.in/company/X/",
        "data_retrieved_at": None,
        "generated_at": NOW.isoformat(),
        "input_sha256": "a" * 64,
        "summary_text": "ok",
        "requested_model": MODEL,
        "reported_model": None,
        "finish_reason": "stop",
    }
    assert SummaryResult.model_validate(base).provider == "aicredits"
    for bad in (
        {"raw_response": "{}"},
        {"input_sha256": "A" * 64},
        {"summary_text": "x" * 8001},
        {"summary_text": "   "},
        {"finish_reason": "length"},
        {"provider": "anthropic"},
        {"generated_at": "2026-10-05T09:30:00"},
        {"usage": {"prompt_tokens": -1, "completion_tokens": 0}},
    ):
        with pytest.raises(ValidationError):
            SummaryResult.model_validate({**base, **bad})


def test_generation_module_imports_only_the_isolated_summary_path():
    from stock_research_ai.application import period_names

    for node in ast.walk(ast.parse(Path(period_names.__file__).read_text())):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [
                a.name for a in node.names]
            assert all(n.split(".")[0] in sys.stdlib_module_names for n in names), names
    source = Path(summary_generation.__file__).read_text()
    forbidden = (
        "analyst",
        "analyst_contract",
        "analyst_model_input",
        "challenge",
        "evidence",
        "report",
        "browser_jobs",
        "ports",
        "adapters",
        "httpx",
        "bootstrap",
        "config",
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert module.split(".")[0] in sys.stdlib_module_names or module in {
                "stock_research_ai.application.summary",
                "stock_research_ai.domain.summary",
                # the ONE period-naming rule shared with Detailed Research (stdlib only)
                "stock_research_ai.application.period_names",
            }, module
            assert not any(word in module for word in forbidden), module
