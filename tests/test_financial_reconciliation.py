"""DETAILED_RESEARCH_V1 Box 4: canonical financial statements & reconciliation (offline)."""

import ast
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from test_document_intelligence import make_pdf
from test_screener_dataset import dataset
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.documents.document_parsers import LocalDocumentParser
from stock_research_ai.adapters.documents.evidence_fetcher import (
    RawArtifactEvidenceReader,
    RawArtifactEvidenceWriter,
    SafeEvidenceFetcher,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.application import financial_parsing, financial_reconciliation
from stock_research_ai.application.document_intelligence import extract_evidence
from stock_research_ai.application.evidence_catalogue import assemble_catalogue
from stock_research_ai.application.evidence_retrieval import retrieve_evidence
from stock_research_ai.application.financial_parsing import (
    MAX_FISCAL_YEAR,
    MIN_FISCAL_YEAR,
    interpret_columns,
    map_filing_label,
    normalize_label,
    parse_financial_number,
    parse_unit_caption,
    period_from,
)
from stock_research_ai.application.financial_reconciliation import (
    _row_values,
    build_canonical_dataset,
    canonical_dataset_json,
    verify_financial_provenance,
)
from stock_research_ai.domain.document_intelligence import ExtractionLimits
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    EvidenceCompany,
    EvidenceDocumentType,
    SourceDiscovery,
    SourceOutcome,
)
from stock_research_ai.domain.evidence_retrieval import RetrievalPolicy
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialLimits,
    Industry,
    NumberStatus,
    Outcome,
    PeriodType,
    ReportingBasis,
    Scale,
    SourceType,
    StatementType,
    ValueKind,
)

T = EvidenceDocumentType
M = CanonicalMetric
B = ReportingBasis
S = StatementType
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
HOST = "https://www.bseindia.com"
COMPANY = EvidenceCompany(
    company_key="screener:INFY",
    name="Infosys Ltd",
    source_company_key="INFY",
    reporting_scope=ReportingScope.STANDALONE,
)


async def filings(root: Path, files: dict[str, tuple[EvidenceDocumentType, list[list[str]]]]):
    """Synthetic PDFs through the real Box 1 catalogue, Box 2 retrieval and Box 3 extraction."""
    bodies = {path: make_pdf(pages) for path, (_kind, pages) in files.items()}
    candidates = [
        DocumentCandidate(
            document_type=kind,
            confidence=ClassificationConfidence.HIGH,
            title=path,
            url=f"{HOST}{path}",
            is_direct_document=True,
            discovery=DocumentDiscovery(
                source_name="test",
                discovered_via_url="https://www.screener.in/company/INFY/",
                locator=path,
                classified_as=kind,
                discovered_at=NOW,
            ),
        )
        for path, (kind, _pages) in files.items()
    ]
    catalogue = assemble_catalogue(
        COMPANY,
        [SourceDiscovery(source_name="test", candidates=tuple(candidates))],
        [SourceOutcome(source_name="test", status="SUCCESS", candidates_found=len(candidates))],
        catalogued_at=NOW,
    )

    def serve(request: httpx.Request) -> httpx.Response:
        body = bodies[request.url.path]
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    async def lookup(host: str) -> list[str]:
        return ["93.184.216.34"]

    manifest = await retrieve_evidence(
        catalogue,
        SafeEvidenceFetcher(httpx.MockTransport(serve), lookup),
        RawArtifactEvidenceWriter(FileRawArtifactStore(root)),
        RetrievalPolicy(concurrency=1, per_type_limit={T.ANNUAL_REPORT: 5, T.QUARTERLY_RESULT: 5}),
        run_id=UUID(int=7),
        clock=lambda: NOW,
    )
    bundle = await extract_evidence(
        catalogue,
        manifest,
        RawArtifactEvidenceReader(root),
        LocalDocumentParser(),
        ExtractionLimits(),
        clock=lambda: NOW,
    )
    return bundle, catalogue


def build(screeners=(), bundle=None, catalogue=None, **kw) -> CanonicalFinancialDataset:
    return build_canonical_dataset(
        company_key="screener:INFY",
        company_name="Infosys Ltd",
        screeners=list(screeners),
        bundle=bundle,
        catalogue=catalogue,
        generated_at=NOW,
        **kw,
    )


def values(data: CanonicalFinancialDataset, metric, basis, end, ptype=None):
    return [
        v
        for v in data.canonical_values
        if v.metric == metric
        and v.reporting_basis == basis
        and v.period.period_end == end
        and (ptype is None or v.period.period_type == ptype)
    ]


def one(data, metric, basis, end, ptype=None):
    found = values(data, metric, basis, end, ptype)
    assert len(found) == 1, (metric, basis, end, found)
    return found[0]


def record(data, metric, basis, end, ptype=None):
    found = [
        r
        for r in data.reconciliation
        if r.metric == metric
        and r.reporting_basis == basis
        and r.period.period_end == end
        and (ptype is None or r.period.period_type == ptype)
    ]
    assert len(found) == 1, (metric, basis, end, found)
    return found[0]


def obs(data, observation_id):
    return next(o for o in data.observations if o.observation_id == observation_id)


FY26, FY25 = date(2026, 3, 31), date(2025, 3, 31)


def profit_and_loss(basis: str, revenue="1,78,650 1,62,990", other="4,322 3,600", head=None):
    return [
        f"{basis} Statement of Profit and Loss",
        "(In Rs. crore, except per equity share data)",
        "Particulars Note Year ended March 31,",
        *(head or ["2026 2025"]),
        f"Revenue from operations 2.18 {revenue}",
        f"Other income, net {other}",
        "Expenses",
        "Employee benefit expenses 95,094 85,950",
        "Finance cost 416 416",
        "Depreciation and amortization expenses 4,902 4,812",
        "Widget licensing charges 1,234 1,100",
        "Profit before tax 39,995 37,608",
        "Tax expense",
        "Current tax 11,767 12,130",
        "Deferred tax (1,246) (1,272)",
        "Total tax expense 10,521 10,858",
        "Profit for the year 29,474 26,750",
        "Earnings per equity share",
        "Basic (Rs.) 71.58 64.50",
        "Diluted (Rs.) 71.46 64.34",
    ]


BALANCE_ASSETS = [
    "Consolidated Balance Sheet",
    "(In Rs. crore)",
    "Particulars Note As at March 31,",
    "2026 2025",
    "Assets",
    "Non-current assets",
    "Property, plant and equipment 2.1 12,651 13,000",
    "Goodwill 10,000 9,000",
    "Total non-current assets 22,651 22,000",
    "Current assets",
    "Cash and cash equivalents 2.9 22,201 24,455",
    "Trade receivables 33,000 30,000",
    "Total current assets 55,201 54,455",
    "Total assets 77,852 76,455",
]
BALANCE_LIABILITIES = [
    "Consolidated Balance Sheet (contd.)",
    "Particulars Note As at March 31,",
    "2026 2025",
    "Equity and liabilities",
    "Equity",
    "Equity share capital 2,024 2,073",
    "Other equity 40,828 42,382",
    "Total equity 42,852 44,455",
    "Liabilities",
    "Current liabilities",
    "Trade payables 4,744 4,164",
    "Other current liabilities 30,256 27,836",
    "Total current liabilities 35,000 32,000",
    "Total equity and liabilities 77,852 76,455",
]
CASH_FLOW = [
    "Consolidated Statement of Cash Flows",
    "(In Rs. crore)",
    "Particulars Note Year ended March 31,",
    "2026 2025",
    "Net cash generated by operating activities 33,986 35,694",
    "Net cash generated / (used in) from investing activities 1,946 (1,946)",
    "Net cash used in financing activities (39,786) (24,161)",
    "Net increase / (decrease) in cash and cash equivalents (3,854) 9,587",
    "Effect of exchange rate changes on cash and cash equivalents 1,600 82",
    "Cash and cash equivalents at the beginning of the period 24,455 14,786",
    "Cash and cash equivalents at the end of the period 22,201 24,455",
]
QUARTERLY = [
    "Audited standalone financial results for the quarter ended December 31, 2025",
    "(In Rs. crore)",
    "Quarter Quarter Nine months",
    "ended ended ended",
    "December 31, December 31, December 31,",
    "2025 2024 2025",
    "Audited Audited Audited",
    "Revenue from operations 41,764 38,994 1,20,000",
    "Profit for the period 6,000 5,800 18,000",
]


# --- parsing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "status", "value"),
    [
        ("1,23,456", NumberStatus.NUMBER, Decimal("123456")),
        ("123,456.78", NumberStatus.NUMBER, Decimal("123456.78")),
        ("1,48,819.40", NumberStatus.NUMBER, Decimal("148819.40")),
        ("(1,946)", NumberStatus.NUMBER, Decimal("-1946")),
        ("-12.5", NumberStatus.NUMBER, Decimal("-12.5")),
        ("12.5%", NumberStatus.NUMBER, Decimal("12.5")),
        ("0", NumberStatus.ZERO_REPORTED, Decimal("0")),
        ("1,234*", NumberStatus.NUMBER, Decimal("1234")),
        ("–", NumberStatus.DASH, None),
        ("-", NumberStatus.DASH, None),
        ("Nil", NumberStatus.NIL, None),
        ("N.A.", NumberStatus.NOT_APPLICABLE, None),
        ("", NumberStatus.BLANK, None),
        ("12,34", NumberStatus.UNPARSEABLE, None),
        ("29,", NumberStatus.UNPARSEABLE, None),
        ("abc", NumberStatus.UNPARSEABLE, None),
    ],
)
def test_number_parser(raw, status, value):
    parsed = parse_financial_number(raw)
    assert (parsed.status, parsed.value) == (status, value)
    assert parsed.raw == raw


def test_number_parser_keeps_percent_decimals_and_footnotes():
    assert parse_financial_number("12.5%").is_percent
    assert parse_financial_number("71.58").decimals == 2
    assert parse_financial_number("1,234*").footnote_marker is True


@pytest.mark.parametrize(
    ("line", "currency", "scale"),
    [
        ("(In Rs. crore, except per equity share data)", Currency.INR, Scale.CRORES),
        ("(In ` crore)", Currency.INR, Scale.CRORES),
        ("(₹ in lakhs)", Currency.INR, Scale.LAKHS),
        ("(Amounts in INR million)", Currency.INR, Scale.MILLIONS),
        ("(US$ in billions)", Currency.USD, Scale.BILLIONS),
        ("(Dollars in millions except equity share data)", Currency.USD, Scale.MILLIONS),
        ("(in millions)", Currency.UNKNOWN, Scale.MILLIONS),
    ],
)
def test_unit_captions(line, currency, scale):
    caption = parse_unit_caption(line)
    assert caption is not None and (caption.currency, caption.scale) == (currency, scale)


def test_narrative_amounts_are_not_unit_captions():
    assert parse_unit_caption("The company spent Rs. 500 crore on capex this year.") is None
    assert parse_unit_caption("Revenue from operations 1,234") is None


def test_quarter_and_year_to_date_headers():
    header = [
        "Quarter Quarter Quarter Year ended",
        "ended ended ended March 31,",
        "June 30, March 31, June 30,",
        "2026 2026 2025 2026",
        "Audited Audited Audited Audited",
    ]
    cols = interpret_columns(header, 4, S.QUARTERLY_RESULTS, 3)
    got = [(c.period.period_type, c.period.period_start, c.period.period_end) for c in cols]
    assert got == [
        (PeriodType.QUARTER, date(2026, 4, 1), date(2026, 6, 30)),
        (PeriodType.QUARTER, date(2026, 1, 1), date(2026, 3, 31)),
        (PeriodType.QUARTER, date(2025, 4, 1), date(2025, 6, 30)),
        (PeriodType.FY, date(2025, 4, 1), date(2026, 3, 31)),
    ]
    assert [(c.period.fiscal_year, c.period.fiscal_quarter) for c in cols[:3]] == [
        (2027, 1),
        (2026, 4),
        (2026, 1),
    ]
    assert all(c.audited and c.explicit and not c.ambiguous for c in cols)


@pytest.mark.parametrize(
    ("header", "ptype", "months"),
    [
        (["Nine months ended December 31, 2025"], PeriodType.NINE_MONTH, 9),
        (["Six months ended September 30, 2025"], PeriodType.HALF_YEAR, 6),
        (["Half-year ended September 30, 2025"], PeriodType.HALF_YEAR, 6),
        (["Year ended March 31, 2026"], PeriodType.FY, 12),
    ],
)
def test_year_to_date_periods(header, ptype, months):
    (col,) = interpret_columns(header, 1, S.PROFIT_AND_LOSS, 3)
    assert (col.period.period_type, col.period.duration_months) == (ptype, months)


def test_point_in_time_and_ambiguous_headers():
    cols = interpret_columns(["As at March 31,", "2026 2025"], 2, S.BALANCE_SHEET, 3)
    assert [c.period.period_type for c in cols] == [PeriodType.POINT_IN_TIME] * 2
    assert [c.period.period_end for c in cols] == [FY26, FY25]
    # a bare year with no known fiscal year end is never guessed
    (col,) = interpret_columns(["2026"], 1, S.PROFIT_AND_LOSS, None)
    assert col.ambiguous and col.period.period_type == PeriodType.UNKNOWN
    (col,) = interpret_columns(["Year 2026"], 1, S.PROFIT_AND_LOSS, 3)
    assert (col.period.period_type, col.period.period_end, col.explicit) == (
        PeriodType.FY,
        FY26,
        False,
    )


def test_labels_and_explicit_aliases():
    assert normalize_label("  (a) Revenue from operations (refer note 5) ") == (
        "revenue from operations"
    )
    assert normalize_label("Basic (in ` per share)") == "basic"
    assert normalize_label("Depreciation and amortization expenses 2.2, 2.4.2 and") == (
        "depreciation and amortization expenses"
    )
    assert normalize_label("Impact of Labour Codes 2.20.1") == "impact of labour codes"
    assert normalize_label("Revenue 2026") == "revenue 2026"  # a year is not a note
    pl, bs = S.PROFIT_AND_LOSS, S.BALANCE_SHEET
    assert map_filing_label(pl, "Revenue from operations", [], False) == M.REVENUE_FROM_OPERATIONS
    assert map_filing_label(pl, "Basic (₹)", ["earnings per equity share"], False) == M.BASIC_EPS
    assert map_filing_label(pl, "Basic", [], False) is None  # no caption, no guess
    assert map_filing_label(pl, "Owners of the Company", ["profit attributable to"], False) == (
        M.PAT_ATTRIBUTABLE_TO_OWNERS
    )
    assert map_filing_label(pl, "Some brand new line", [], False) is None
    # banks: interest expense is not a finance cost; deposits are not borrowings
    assert map_filing_label(pl, "Finance costs", [], True) is None
    assert map_filing_label(pl, "Interest expended", [], True) == M.INTEREST_EXPENDED
    assert map_filing_label(bs, "Deposits", [], True) == M.DEPOSITS
    assert map_filing_label(bs, "Borrowings", [], True) == M.TOTAL_BORROWINGS


def test_split_numbers_are_rejoined_only_when_unambiguous():
    row = {
        0: ("Profit for the year", False),
        4: ("29,", False),
        5: ("211", False),
        6: ("25,568", False),
    }
    assert _row_values(row, [5, 6]) == ([("29, 211", 4), ("25,568", 6)], "SPLIT_NUMBER_REJOINED")
    shifted = {
        0: ("Employee benefit expenses", False),
        1: ("2.20", False),
        2: ("73,239", False),
        3: ("67,", False),
        4: ("4", False),
        5: ("6", False),
        6: ("6", False),
    }
    assert _row_values(shifted, [5, 6]) == (
        [("73,239", 2), ("67, 4 6 6", 3)],
        "SPLIT_NUMBER_REJOINED",
    )
    clean = {0: ("Finance cost", False), 2: ("416", False), 3: ("416", False)}
    assert _row_values(clean, [2, 3]) == ([("416", 2), ("416", 3)], None)
    unreadable = {0: ("x", False), 1: ("1", False), 2: ("2", False), 3: ("3", False)}
    assert _row_values(unreadable, [2, 3])[1] == "SPLIT_NUMBER_SUSPECT"


# --- fixtures A-K through the real Box 1/2/3 pipeline -------------------------------------


async def test_a_annual_profit_and_loss(tmp_path):
    bundle, catalogue = await filings(
        tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Consolidated")])}
    )
    data = build(bundle=bundle, catalogue=catalogue)
    revenue = one(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26)
    assert (revenue.value, revenue.currency, revenue.scale) == (
        Decimal(178650),
        Currency.INR,
        Scale.CRORES,
    )
    assert revenue.outcome == Outcome.SINGLE_SOURCE and revenue.confidence == Confidence.HIGH
    assert revenue.period.period_type == PeriodType.FY and revenue.period.period_start == date(
        2025, 4, 1
    )
    selected = obs(data, revenue.selected_observation_id)
    assert (selected.raw_value, selected.raw_label, selected.source.source_type) == (
        "1,78,650",
        "Revenue from operations",
        SourceType.FILING,
    )
    assert selected.source.page_number == 1 and selected.source.locator.endswith("/c2")
    assert one(data, M.DEFERRED_TAX, B.CONSOLIDATED, FY26).value == Decimal(-1246)
    eps = one(data, M.BASIC_EPS, B.CONSOLIDATED, FY26)
    assert (eps.value, eps.value_kind, eps.scale) == (
        Decimal("71.58"),
        ValueKind.PER_SHARE,
        Scale.ONES,
    )
    statement = data.statement(S.PROFIT_AND_LOSS, B.CONSOLIDATED, "ANNUAL")
    assert statement is not None
    assert [p.period_end for p in statement.periods] == [FY25, FY26]
    assert statement.lines[0].metric == M.REVENUE_FROM_OPERATIONS
    check = next(
        c for c in data.validations if c.name == "PBT_TAX_PAT" and c.period.period_end == FY26
    )
    assert (check.status, check.expected, check.actual) == ("PASS", Decimal(29474), Decimal(29474))
    assert data.statistics.validation_fail == 0 and data.status == "COMPLETE"


async def test_b_c_k_balance_sheet_over_two_pages_and_cash_flow(tmp_path):
    pages = [BALANCE_ASSETS, BALANCE_LIABILITIES, CASH_FLOW]
    bundle, catalogue = await filings(tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, pages)})
    extraction = bundle.extractions[0]
    assert sum(t.continuation_of is not None for t in extraction.tables) == 1
    data = build(bundle=bundle, catalogue=catalogue)
    assets = one(data, M.TOTAL_ASSETS, B.CONSOLIDATED, FY26)
    total = one(data, M.TOTAL_EQUITY_AND_LIABILITIES, B.CONSOLIDATED, FY26)
    # the continued page inherits periods, unit and basis from its first page
    assert obs(data, assets.selected_observation_id).source.page_number == 1
    assert obs(data, total.selected_observation_id).source.page_number == 2
    assert total.period == assets.period and total.period.period_type == PeriodType.POINT_IN_TIME
    assert one(data, M.TRADE_PAYABLES, B.CONSOLIDATED, FY25).value == Decimal(4164)
    assert one(data, M.CFI, B.CONSOLIDATED, FY25).value == Decimal(-1946)
    checks = {(c.name, c.period.period_end): c.status for c in data.validations}
    assert checks == {
        ("BALANCE_SHEET_EQUATION", FY26): "PASS",
        ("BALANCE_SHEET_EQUATION", FY25): "PASS",
        ("CASH_RECONCILIATION", FY26): "PASS",
        ("CASH_RECONCILIATION", FY25): "PASS",
    }
    assert data.statement(S.BALANCE_SHEET, B.CONSOLIDATED, "POINT_IN_TIME") is not None
    assert data.statement(S.CASH_FLOW, B.CONSOLIDATED, "ANNUAL") is not None


async def test_failed_accounting_checks_warn_and_never_change_values(tmp_path):
    broken = [line.replace("Total assets 77,852", "Total assets 77,952") for line in BALANCE_ASSETS]
    bundle, catalogue = await filings(
        tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, [broken, BALANCE_LIABILITIES])}
    )
    data = build(bundle=bundle, catalogue=catalogue)
    check = next(c for c in data.validations if c.period.period_end == FY26)
    assert (check.status, check.difference) == ("FAIL", Decimal(100))
    assert one(data, M.TOTAL_ASSETS, B.CONSOLIDATED, FY26).value == Decimal(77952)
    assert one(data, M.TOTAL_EQUITY_AND_LIABILITIES, B.CONSOLIDATED, FY26).value == Decimal(77852)
    assert "ACCOUNTING_EQUATION_MISMATCH" in {w.code for w in data.warnings}


async def test_d_quarter_and_year_to_date_never_mix(tmp_path):
    bundle, catalogue = await filings(tmp_path, {"/qr.pdf": (T.QUARTERLY_RESULT, [QUARTERLY])})
    data = build(bundle=bundle, catalogue=catalogue)
    dec25 = date(2025, 12, 31)
    quarter = one(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, dec25, PeriodType.QUARTER)
    ytd = one(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, dec25, PeriodType.NINE_MONTH)
    assert (quarter.value, ytd.value) == (Decimal(41764), Decimal(120000))
    assert quarter.period.period_start == date(2025, 10, 1)
    assert ytd.period.period_start == date(2025, 4, 1)
    assert quarter.reconciliation_record_id != ytd.reconciliation_record_id
    assert obs(data, quarter.selected_observation_id).audited is True
    statement = data.statement(S.QUARTERLY_RESULTS, B.STANDALONE, "QUARTERLY")
    assert statement is not None and len(statement.periods) == 3


async def test_e_consolidated_and_standalone_stay_apart(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)  # Screener standalone page
    pages = [
        profit_and_loss("Consolidated"),
        profit_and_loss("Standalone", revenue="1,48,819 1,36,592", other="5,345 4,782"),
    ]
    bundle, catalogue = await filings(tmp_path / "f", {"/ar.pdf": (T.ANNUAL_REPORT, pages)})
    data = build([screener], bundle, catalogue)
    consolidated = one(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26, PeriodType.FY)
    standalone = one(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, FY26, PeriodType.FY)
    assert (consolidated.value, consolidated.outcome) == (Decimal(178650), Outcome.SINGLE_SOURCE)
    assert (standalone.value, standalone.outcome) == (Decimal(148819), Outcome.MATCH)
    assert {obs(data, i).source.source_type for i in standalone.supporting_observation_ids} == {
        SourceType.FILING,
        SourceType.SCREENER,
    }
    for statement in data.statements:
        assert {
            line_value.reporting_basis for line in statement.lines for line_value in line.values
        } == {statement.reporting_basis}
    assert data.consolidated and data.standalone


async def test_f_restated_comparative(tmp_path):
    old = profit_and_loss("Consolidated", revenue="1,62,990 1,53,670", head=["2025 2024"])
    new = profit_and_loss(
        "Consolidated", revenue="1,78,650 1,63,500", head=["2026 2025", "Restated"]
    )
    bundle, catalogue = await filings(
        tmp_path, {"/ar2025.pdf": (T.ANNUAL_REPORT, [old]), "/ar2026.pdf": (T.ANNUAL_REPORT, [new])}
    )
    assert len(bundle.extractions) == 2
    data = build(bundle=bundle, catalogue=catalogue)
    rec = record(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY25)
    assert rec.outcome == Outcome.RESTATEMENT
    selected = obs(data, rec.selected_observation_id)
    assert selected.restated and selected.normalized_value == Decimal(163500)
    original = [obs(data, i) for i in rec.conflicting_observation_ids]
    assert [o.normalized_value for o in original] == [Decimal(162990)]  # kept, not discarded


async def test_g_unexplained_filing_differences_are_conflicts(tmp_path):
    old = profit_and_loss("Consolidated", revenue="1,62,990 1,53,670", head=["2025 2024"])
    new = profit_and_loss("Consolidated", revenue="1,78,650 1,63,500")
    bundle, catalogue = await filings(
        tmp_path, {"/ar2025.pdf": (T.ANNUAL_REPORT, [old]), "/ar2026.pdf": (T.ANNUAL_REPORT, [new])}
    )
    data = build(bundle=bundle, catalogue=catalogue)
    rec = record(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY25)
    assert (rec.outcome, rec.selected_observation_id, rec.max_difference) == (
        Outcome.CONFLICT,
        None,
        Decimal(510),
    )
    assert len(rec.conflicting_observation_ids) == 2
    assert values(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY25) == []
    codes = {w.code for w in data.warnings}
    assert {"RECONCILIATION_CONFLICT", "POSSIBLE_RESTATEMENT"} <= codes


async def test_g_screener_versus_filing_conflict_and_h_rounding(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    page = profit_and_loss("Standalone", revenue="1,48,819.40 1,36,592", other="6,491 4,782")
    bundle, catalogue = await filings(tmp_path / "f", {"/ar.pdf": (T.ANNUAL_REPORT, [page])})
    data = build([screener], bundle, catalogue)
    other = record(data, M.OTHER_INCOME, B.STANDALONE, FY26, PeriodType.FY)
    assert other.outcome == Outcome.SOURCE_PRECEDENCE
    assert obs(data, other.selected_observation_id).normalized_value == Decimal(6491)
    (lower,) = [obs(data, i) for i in other.conflicting_observation_ids]
    assert (lower.source.source_type, lower.normalized_value) == (
        SourceType.SCREENER,
        Decimal(5345),
    )
    # rounding only: 1,48,819.40 (filing) vs 148,819 (Screener, whole crores)
    revenue = record(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, FY26, PeriodType.FY)
    assert revenue.outcome == Outcome.MATCH_WITHIN_TOLERANCE
    assert revenue.max_difference == Decimal("0.40")
    assert obs(data, revenue.selected_observation_id).raw_value == "1,48,819.40"
    exact = record(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, FY25, PeriodType.FY)
    assert exact.outcome == Outcome.MATCH
    # TTM never reconciles against FY
    ttm = [r for r in data.reconciliation if r.period.period_type == PeriodType.TTM]
    assert ttm and all(r.outcome == Outcome.SINGLE_SOURCE for r in ttm)


async def test_i_unmapped_rows_are_preserved(tmp_path):
    bundle, catalogue = await filings(
        tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Consolidated")])}
    )
    data = build(bundle=bundle, catalogue=catalogue)
    (line,) = [u for u in data.unmapped_lines if u.raw_label == "Widget licensing charges"]
    assert line.statement_type == S.PROFIT_AND_LOSS and line.reporting_basis == B.CONSOLIDATED
    kept = [obs(data, i) for i in line.observation_ids]
    assert sorted(o.normalized_value for o in kept) == [Decimal(1100), Decimal(1234)]
    assert all(o.metric is None and "UNMAPPED_LABEL" in o.warnings for o in kept)
    assert data.statistics.unmapped_lines == len(data.unmapped_lines)


async def test_j_bank_statements(tmp_path, fixture_dir):
    screener = await dataset(tmp_path, fixture_dir, YESBANK)
    data = build([screener])
    assert data.industry == Industry.BANK
    metrics = {v.metric for v in data.canonical_values}
    assert M.EBITDA not in metrics and M.OPERATING_PROFIT not in metrics
    assert M.FINANCE_COST not in metrics  # a bank's interest is its cost of funds
    assert one(data, M.DEPOSITS, B.CONSOLIDATED, FY26).value == Decimal(318969)
    assert one(data, M.TOTAL_BORROWINGS, B.CONSOLIDATED, FY26).value == Decimal(65451)
    assert one(data, M.INTEREST_EXPENDED, B.CONSOLIDATED, FY26, PeriodType.FY).value == Decimal(
        20450
    )
    assert one(data, M.BANK_FINANCING_PROFIT, B.CONSOLIDATED, FY26, PeriodType.FY).value == Decimal(
        -1863
    )


async def test_screener_only_dataset(tmp_path, fixture_dir):
    screener = await dataset(tmp_path, fixture_dir, INFY)
    data = build([screener])
    assert (data.industry, data.fiscal_year_end_month) == (Industry.GENERAL, 3)
    quarter = one(
        data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, date(2026, 6, 30), PeriodType.QUARTER
    )
    assert quarter.value == Decimal(39957) and quarter.period.fiscal_quarter == 1
    ttm = [v for v in data.canonical_values if v.period.period_type == PeriodType.TTM]
    assert {v.period.label for v in ttm} == {"TTM"}
    eps = one(data, M.EPS, B.STANDALONE, FY26, PeriodType.FY)
    assert eps.value_kind == ValueKind.PER_SHARE and eps.value == Decimal("72.03")
    percent = [o for o in data.observations if o.value_kind == ValueKind.PERCENT]
    assert percent and all(o.metric is None for o in percent)  # ratios are preserved, not mapped
    assert {u.raw_label for u in data.unmapped_lines} >= {"OPM %", "Tax %", "Free Cash Flow"}
    sources = {obs(data, v.selected_observation_id).source for v in data.canonical_values}
    assert {s.raw_artifact_id for s in sources} == {screener.source.raw_artifact_id}
    assert verify_financial_provenance(data, [screener], None) == []


# --- uncertainty, ambiguity, bounds -----------------------------------------------------------


async def test_unknown_unit_is_kept_but_never_canonical(tmp_path):
    page = [line for line in profit_and_loss("Consolidated") if not line.startswith("(In Rs.")]
    bundle, catalogue = await filings(tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, [page])})
    data = build(bundle=bundle, catalogue=catalogue)
    revenue = [o for o in data.observations if o.metric == M.REVENUE_FROM_OPERATIONS]
    assert revenue and all(
        o.confidence == Confidence.LOW and "UNKNOWN_UNIT" in o.warnings for o in revenue
    )
    assert all(o.normalized_value is None for o in revenue)
    assert (
        record(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26).outcome
        == Outcome.NOT_COMPARABLE
    )
    # only the EPS lines, which state their own currency ('Basic (Rs.)'), stay canonical
    assert {v.value_kind for v in data.canonical_values} == {ValueKind.PER_SHARE}
    assert {v.currency for v in data.canonical_values} == {Currency.INR}


async def test_same_document_disagreement_is_ambiguous(tmp_path):
    page = profit_and_loss("Consolidated")
    page.insert(5, "Revenue from operations 1,78,000 1,62,990")
    bundle, catalogue = await filings(tmp_path, {"/ar.pdf": (T.ANNUAL_REPORT, [page])})
    data = build(bundle=bundle, catalogue=catalogue)
    rec = record(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26)
    assert (rec.outcome, rec.selected_observation_id) == (Outcome.AMBIGUOUS, None)
    # identical duplicates in the same document still match
    assert record(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY25).outcome == Outcome.MATCH


async def test_resource_limits_make_the_dataset_partial(tmp_path, fixture_dir):
    screener = await dataset(tmp_path, fixture_dir, INFY)
    data = build([screener], limits=FinancialLimits(max_observations=10))
    assert data.status == "PARTIAL" and data.statistics.total_observations == 10
    assert "OBSERVATION_LIMIT_REACHED" in {w.code for w in data.warnings}


async def test_deterministic_serialization_and_provenance(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    pages = [profit_and_loss("Standalone", revenue="1,48,819 1,36,592"), BALANCE_ASSETS]
    first_bundle, first_catalogue = await filings(
        tmp_path / "a", {"/ar.pdf": (T.ANNUAL_REPORT, pages)}
    )
    second_bundle, second_catalogue = await filings(
        tmp_path / "b", {"/ar.pdf": (T.ANNUAL_REPORT, pages)}
    )
    first = build([screener], first_bundle, first_catalogue)
    second = build([screener], second_bundle, second_catalogue)
    encoded = canonical_dataset_json(first)
    assert encoded == canonical_dataset_json(second)
    assert CanonicalFinancialDataset.model_validate_json(encoded) == first
    assert json.loads(encoded)["schema_version"] == "CANONICAL_FINANCIAL_DATASET_V1"
    assert verify_financial_provenance(first, [screener], first_bundle) == []
    # a tampered filing cell is caught
    extraction = first_bundle.extractions[0]
    table = extraction.tables[0]
    cells = tuple(
        c.model_copy(update={"raw_text": "9,99,999"}) if (c.row, c.column) == (2, 2) else c
        for c in table.cells
    )
    forged = extraction.model_copy(
        update={"tables": (table.model_copy(update={"cells": cells}), *extraction.tables[1:])}
    )
    tampered = first_bundle.model_copy(update={"extractions": (forged,)})
    assert verify_financial_provenance(first, [screener], tampered)


def test_contract_is_strict():
    with pytest.raises(ValidationError):
        CanonicalFinancialDataset.model_validate(
            {"schema_version": "CANONICAL_FINANCIAL_DATASET_V1"}
        )
    data = build()
    assert data.status == "COMPLETE" and data.observations == ()
    assert {w.code for w in data.warnings} == {"SOURCE_MISSING"}
    with pytest.raises(ValidationError):
        CanonicalFinancialDataset.model_validate({**data.model_dump(), "surprise": 1})


def test_no_ai_or_network_dependencies_in_reconciliation():
    forbidden = ("httpx", "anthropic", "aicredits", "openai", "summary", "socket", "adapters")
    for module in (financial_parsing, financial_reconciliation):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not any(word in name for word in forbidden), (module.__name__, name)


# --- non-financial year headers (HUDCO live failure, 2026-10-15) ----------------------------


def test_hudco_statute_year_header_is_never_a_fiscal_period():
    """The live HUDCO filing table: the 1961 (Income Tax Act) header used to raise a
    FinancialPeriod ValidationError and abort Box 7. It is now an ambiguous UNKNOWN column."""
    header = ["1961", "1961", "1961", "TRACES Demand TDS 0.04 - 0.04 year ended March 31, 2007"]
    cols = interpret_columns(header, 4, S.QUARTERLY_RESULTS, 3)
    assert len(cols) == 4
    for col in cols:
        assert col.period.fiscal_year is None or (
            MIN_FISCAL_YEAR <= col.period.fiscal_year <= MAX_FISCAL_YEAR)
        assert col.period.period_end is None or col.period.period_end.year >= MIN_FISCAL_YEAR - 1
    assert all(c.period.fiscal_year != 1961 for c in cols)


@pytest.mark.parametrize("header", [
    ["Year ended March 31, 1961"], ["Quarter ended June 30, 1956"], ["As at March 31, 1961"],
    ["1961"], ["Year ended March 31, 1989"],
])
def test_pre_range_years_are_unknown_and_ambiguous(header):
    for statement, fy_end in ((S.PROFIT_AND_LOSS, 3), (S.QUARTERLY_RESULTS, 3),
                              (S.BALANCE_SHEET, 3), (S.PROFIT_AND_LOSS, None)):
        (col,) = interpret_columns(header, 1, statement, fy_end)
        assert col.ambiguous and not col.explicit
        assert col.period.period_type == PeriodType.UNKNOWN
        assert (col.period.period_end, col.period.period_start, col.period.fiscal_year,
                col.period.fiscal_quarter, col.period.duration_months) == (
                    None, None, None, None, None)


def test_range_boundaries_still_parse_exactly_as_before():
    (col,) = interpret_columns(["Year ended March 31, 1990"], 1, S.PROFIT_AND_LOSS, 3)
    assert (col.period.period_type, col.period.fiscal_year, col.ambiguous) == (
        PeriodType.FY, 1990, False)
    (col,) = interpret_columns(["Quarter ended December 31, 1989"], 1, S.QUARTERLY_RESULTS, 3)
    assert (col.period.period_type, col.period.fiscal_year, col.period.fiscal_quarter) == (
        PeriodType.QUARTER, 1990, 3)  # FY1990 (Apr 1989 - Mar 1990) is in range
    (col,) = interpret_columns(["Year ended March 31, 2026"], 1, S.PROFIT_AND_LOSS, 3)
    assert (col.period.period_type, col.period.period_end, col.period.fiscal_year,
            col.explicit, col.ambiguous) == (PeriodType.FY, FY26, 2026, True, False)


def test_period_from_guard_covers_every_caller():
    for end, fy_end in ((date(1961, 3, 31), 3), (date(1961, 3, 31), None),
                        (date(2100, 12, 31), 3), (date(1989, 3, 31), 3)):
        period = period_from(PeriodType.FY, 12, end, "x", fy_end)  # used to raise
        assert (period.period_type, period.period_end, period.fiscal_year) == (
            PeriodType.UNKNOWN, None, None)
    normal = period_from(PeriodType.QUARTER, 3, date(2026, 6, 30), "Q1", 3)
    assert (normal.period_type, normal.period_start, normal.period_end, normal.fiscal_year,
            normal.fiscal_quarter) == (PeriodType.QUARTER, date(2026, 4, 1),
                                       date(2026, 6, 30), 2027, 1)
    undated = period_from(PeriodType.UNKNOWN, None, None, "unlabelled column", 3)
    assert (undated.period_type, undated.period_end, undated.fiscal_year) == (
        PeriodType.UNKNOWN, None, None)


def test_fiscal_range_matches_the_financial_period_model():
    from annotated_types import Ge, Le

    from stock_research_ai.domain.financial_canonical import FinancialPeriod

    meta = FinancialPeriod.model_fields["fiscal_year"].metadata
    assert next(m.ge for m in meta if isinstance(m, Ge)) == MIN_FISCAL_YEAR
    assert next(m.le for m in meta if isinstance(m, Le)) == MAX_FISCAL_YEAR
