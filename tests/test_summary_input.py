"""SUMMARY_V1 Box 2: pure bounded projection of offline Screener extractions."""

import ast
import json
import re
import socket
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_research_ai.application.summary import (
    canonical_summary_json,
    ensure_within_budget,
    project_summary_input,
)
from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.financial import FinancialUnit, ReportingScope, UnitKind
from stock_research_ai.domain.summary import (
    SUMMARY_INPUT_MAX_BYTES,
    SummaryBlock,
    SummaryInput,
    SummaryInputBudgetExceeded,
    SummaryInputUnavailable,
    SummaryRow,
    SummaryValue,
)

INFY = ("infy_standalone.html", "https://www.screener.in/company/INFY/")
YESBANK = ("yesbank_consolidated.html", "https://www.screener.in/company/YESBANK/consolidated/")


async def replay(tmp_path: Path, fixture_dir: Path, case: tuple[str, str], html: str | None = None):
    name, url = case
    path = fixture_dir / name
    if html is not None:
        path = tmp_path / "mutated.html"
        path.write_text(html)
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "evidence")
    await initialize_database(settings)
    return await extract_snapshot(
        settings,
        ExtractionRequest(provider="screener", url=url, replay_path=path),
        tmp_path / "export",
    )


def rows(block: SummaryBlock) -> dict[str, SummaryRow]:
    return {row.metric_id: row for row in block.rows}


def limitation_set(summary: SummaryInput) -> set[tuple[str, str, tuple[str, ...]]]:
    return {(item.block, item.code, item.metric_ids) for item in summary.limitations}


async def test_infy_standalone_projection(tmp_path, fixture_dir):
    summary = project_summary_input(await replay(tmp_path, fixture_dir, INFY))
    company = summary.company
    assert company.name == "Infosys Ltd"
    assert company.source_company_key == "1489"
    assert company.reporting_scope == ReportingScope.STANDALONE
    assert [(x.exchange, x.symbol, x.exchange_identifier) for x in company.listings] == [
        ("BSE", None, "500209"),
        ("NSE", "INFY", None),
    ]
    assert company.source_url == INFY[1]
    assert company.coverage_overall == "PARTIAL"  # peers are JS-loaded; never required

    annual = rows(summary.annual)
    assert list(annual) == ["sales", "operating_profit", "net_profit", "eps"]
    assert [annual["sales"].label, annual["operating_profit"].label] == [
        "Sales",
        "Operating Profit",
    ]
    sales = annual["sales"]
    assert [v.period for v in sales.values] == ["Mar 2023", "Mar 2024", "Mar 2025", "Mar 2026"]
    assert sales.ttm is not None and sales.ttm.period == "TTM"
    assert str(sales.ttm.value) == "153501"
    assert sales.unit == FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=10_000_000)
    assert sales.reporting_scope == ReportingScope.STANDALONE

    quarterly = rows(summary.quarterly)
    assert list(quarterly) == ["sales", "operating_profit", "net_profit", "eps"]
    # five quarters: the latest one and the same quarter a year earlier (year on year)
    assert [v.period for v in quarterly["sales"].values] == [
        "Jun 2025",
        "Sep 2025",
        "Dec 2025",
        "Mar 2026",
        "Jun 2026",
    ]
    assert all(row.ttm is None for row in summary.quarterly.rows)

    assert list(rows(summary.balance_sheet)) == [
        "equity_capital",
        "reserves",
        "borrowings",
        "total_assets",
    ]
    assert list(rows(summary.cash_flow)) == [
        "cash_operating",
        "cash_investing",
        "cash_financing",
        "source_free_cashflow",
    ]
    for block, cap in ((summary.balance_sheet, 2), (summary.cash_flow, 2)):
        assert all(len(row.values) == cap for row in block.rows)
    assert [m.metric_id for m in summary.snapshot] == [
        "market_cap",
        "current_price",
        "price_earnings",
        "book_value",
        "dividend_yield",
        "roce",
        "roe",
    ]
    # Optional bank-only rows are absent without noise; nothing required is missing.
    assert summary.limitations == ()
    assert len(canonical_summary_json(summary)) < SUMMARY_INPUT_MAX_BYTES


async def test_yesbank_consolidated_projection_keeps_bank_labels(tmp_path, fixture_dir):
    summary = project_summary_input(await replay(tmp_path, fixture_dir, YESBANK))
    assert summary.company.name == "Yes Bank Ltd"
    assert summary.company.source_company_key == "3783"
    assert summary.company.reporting_scope == ReportingScope.CONSOLIDATED
    assert [(x.exchange, x.symbol, x.exchange_identifier) for x in summary.company.listings] == [
        ("BSE", None, "532648"),
        ("NSE", "YESBANK", None),
    ]
    annual = rows(summary.annual)
    assert list(annual) == ["revenue", "financing_profit", "net_profit", "eps"]
    assert annual["financing_profit"].label == "Financing Profit"
    quarterly = rows(summary.quarterly)
    assert list(quarterly) == [
        "revenue",
        "financing_profit",
        "net_profit",
        "eps",
        "gross_npa_percent",
        "net_npa_percent",
    ]
    assert quarterly["gross_npa_percent"].unit.kind == UnitKind.PERCENT
    assert str(quarterly["gross_npa_percent"].values[-1].value) == "1.30"
    balance = rows(summary.balance_sheet)
    assert "deposits" in balance
    assert balance["borrowings"].label == "Borrowing"  # source label kept, not "total debt"
    encoded = canonical_summary_json(summary)
    assert b"Operating Profit" not in encoded
    assert b"operating_profit" not in encoded
    assert summary.limitations == ()
    assert len(encoded) < SUMMARY_INPUT_MAX_BYTES


@pytest.mark.parametrize(
    "section_id,block,code,metric_ids",
    [
        ("cash-flow", "cash_flow", "MISSING_SECTION", ("cash_operating",)),
        ("quarters", "quarterly", "SOURCE_UNAVAILABLE", ("sales", "revenue")),
    ],
)
async def test_missing_section_emits_limitations(
    section_id, block, code, metric_ids, tmp_path, fixture_dir
):
    html = (fixture_dir / YESBANK[0]).read_text()
    replacement = (
        "" if code == "MISSING_SECTION" else f'<section id="{section_id}">Loading</section>'
    )
    html, count = re.subn(
        rf'<section id="{section_id}".*?</section>', replacement, html, count=1, flags=re.S
    )
    assert count == 1
    summary = project_summary_input(await replay(tmp_path, fixture_dir, YESBANK, html))
    assert getattr(summary, block).rows == ()
    found = limitation_set(summary)
    assert (block, code, ()) in found
    assert (block, "NOT_PRESENT", metric_ids) in found
    # Other blocks are unaffected and the projection still succeeds.
    assert rows(summary.annual)
    # Only codes, never source text or exception messages.
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", item.code) for item in summary.limitations)


async def test_projection_is_deterministic(tmp_path, fixture_dir):
    extraction = await replay(tmp_path, fixture_dir, YESBANK)
    first = canonical_summary_json(project_summary_input(extraction))
    second = canonical_summary_json(project_summary_input(extraction))
    assert first == second
    assert json.loads(first)["schema_version"] == "SUMMARY_INPUT_V1"


async def test_budget_exceeded_fails_loudly_without_trimming(tmp_path, fixture_dir):
    summary = project_summary_input(await replay(tmp_path, fixture_dir, INFY))
    row = SummaryRow(
        label="L" * 120,
        metric_id="sales",
        unit=FinancialUnit(kind=UnitKind.NUMBER),
        reporting_scope=ReportingScope.STANDALONE,
        values=tuple(
            SummaryValue(period="P" * 120, value=None, status="missing") for _ in range(4)
        ),
    )
    big = SummaryBlock(rows=(row,) * 16)
    oversized = SummaryInput.model_validate(
        {**summary.model_dump(), "annual": big.model_dump(), "quarterly": big.model_dump()}
    )
    before = canonical_summary_json(oversized)
    assert len(before) > SUMMARY_INPUT_MAX_BYTES
    with pytest.raises(SummaryInputBudgetExceeded) as error:
        ensure_within_budget(oversized)
    assert error.value.actual_bytes == len(before)
    assert error.value.max_bytes == SUMMARY_INPUT_MAX_BYTES
    assert canonical_summary_json(oversized) == before  # nothing trimmed
    with pytest.raises(SummaryInputBudgetExceeded):
        ensure_within_budget(summary, max_bytes=100)


async def test_values_are_copied_from_observations_not_derived(tmp_path, fixture_dir):
    for case in (INFY, YESBANK):
        extraction = await replay(tmp_path / case[0], fixture_dir, case)
        summary = project_summary_input(extraction)
        sources = {
            (section, o.metric.removeprefix("source:"), o.metric_id, o.period.fiscal_label): (
                o.value,
                o.value_status,
                o.unit,
                o.reporting_scope,
            )
            for section in ("profit_loss", "quarterly", "balance_sheet", "cashflow")
            for o in getattr(extraction, section)
        }
        blocks = {
            "profit_loss": summary.annual,
            "quarterly": summary.quarterly,
            "balance_sheet": summary.balance_sheet,
            "cashflow": summary.cash_flow,
        }
        for section, block in blocks.items():
            for row in block.rows:
                for value in (*row.values, *([row.ttm] if row.ttm else [])):
                    assert sources[(section, row.label, row.metric_id, value.period)] == (
                        value.value,
                        value.status,
                        row.unit,
                        row.reporting_scope,
                    )
        top = {(o.metric_id, o.value) for o in extraction.top_metrics}
        assert all((m.metric_id, m.observation.value) in top for m in summary.snapshot)
        text = canonical_summary_json(summary).decode().lower()
        for derived in ("cagr", "yoy", "growth", "ebitda", "total_debt", "price_target", "margin_"):
            assert derived not in text


async def test_overlong_identity_text_is_rejected_not_truncated(tmp_path, fixture_dir):
    extraction = await replay(tmp_path, fixture_dir, INFY)
    assert extraction.company is not None
    company = extraction.company.model_copy(update={"name": "N" * 121})
    with pytest.raises(ValidationError):
        project_summary_input(extraction.model_copy(update={"company": company}))


async def test_missing_identity_is_a_typed_failure(tmp_path, fixture_dir):
    extraction = await replay(tmp_path, fixture_dir, INFY)
    with pytest.raises(SummaryInputUnavailable):
        project_summary_input(extraction.model_copy(update={"company": None}))


async def test_projection_never_touches_the_network(tmp_path, fixture_dir, monkeypatch):
    extraction: CompanyExtractionResult = await replay(tmp_path, fixture_dir, YESBANK)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("projection must not open sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert project_summary_input(extraction).company.name == "Yes Bank Ltd"


def test_summary_modules_import_only_domain_pydantic_and_standard_library():
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    forbidden = (
        "adapters",
        "aicredits",
        "analyst",
        "browser_jobs",
        "research_plan",
        "evidence",
        "documents",
        "kite",
        "valuation",
        "sector_kpis",
        "bootstrap",
        "httpx",
    )
    for source in (root / "application" / "summary.py", root / "domain" / "summary.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                base = module.split(".")[0]
                assert (
                    base in sys.stdlib_module_names
                    or base == "pydantic"
                    or module.startswith("stock_research_ai.domain.")
                ), f"{source.name} imports {module}"
                assert not any(word in module for word in forbidden), module
