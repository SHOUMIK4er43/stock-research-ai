"""Exact HUDCO cell evidence and adversarial structural/canonical integrity controls."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
from test_financial_analytics import B, M, V, fy, make, pit
from test_financial_reconciliation import build, filings, profit_and_loss

from stock_research_ai.adapters.documents.document_parsers import parse_html, raw_document_from_json
from stock_research_ai.application import financial_reconciliation as fr
from stock_research_ai.application.document_structure import DocumentContext, analyse_document
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_parsing import parse_financial_number
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.peer_financials import values_of
from stock_research_ai.application.peer_registry import BENCHMARK_METRICS
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain.document_intelligence import (
    ExtractionLimits,
    IssuerBindingMethod,
    IssuerBindingStatus,
)
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.evidence import EvidenceDocumentType as T
from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    FinancialLimits,
    Industry,
    Outcome,
    SourceType,
    TolerancePolicy,
)

NOW = datetime(2026, 10, 18, 5, 36, 11, 603930, tzinfo=UTC)
KEY = "screener:1274074"
NAME = "Housing & Urban Development Corporation Ltd"
FIXTURE = Path(__file__).parent / "fixtures/financial_integrity/hudco_statement_cells.json"


def statement_extraction(raw=None, fixture_path=FIXTURE):
    fixture = json.loads(fixture_path.read_text())
    if raw is None:
        raw = raw_document_from_json({
            "format": "PDF", "status": "SUCCESS", "parser": fixture["parser"],
            "parser_version": fixture["parser"], "page_count": 521,
            "warnings": [], "pages": fixture["pages"],
        })
    context = DocumentContext(
        document_id=UUID(fixture["document_id"]), company_key=fixture["company_key"],
        document_type=T.ANNUAL_REPORT, authority=SourceAuthority.PRIMARY_REGULATORY,
        artifact_sha256=fixture["source_sha256"], artifact_reference=fixture["source_url"],
        retrieval_id=UUID(int=0), extracted_at=NOW,
    )
    extraction = analyse_document(raw, context, ExtractionLimits())
    # Explicit test boundary: this fixture is the verified filing's statement pages,
    # not the cover/issuer-binding fixture. Never relax the production identity gate.
    extraction = extraction.model_copy(update={
        "issuer_binding_status": IssuerBindingStatus.VERIFIED,
        "issuer_binding_target_company_key": fixture["company_key"],
        "issuer_binding_method": next(iter(IssuerBindingMethod)),
    })
    return extraction


def filing_observations(extraction=None):
    ex = extraction or statement_extraction()
    key = ex.company_key
    collector = fr._Collector(key, FinancialLimits())
    fr.filing_observations(
        SimpleNamespace(company_key=key, extractions=(ex,)),
        SimpleNamespace(company=SimpleNamespace(company_key=key), documents=[
            SimpleNamespace(document_id=ex.document_id, source_url=ex.artifact_reference,
                            company_key=key)]),
        collector, Industry.UNKNOWN, 3,
    )
    return collector.observations


def reconcile(observations, *, industry=Industry.UNKNOWN):
    collector = fr._Collector(KEY, FinancialLimits())
    records = fr.reconcile(observations, TolerancePolicy(), collector)
    values = fr.canonical_values(records, {o.observation_id: o for o in observations})
    base = make([], industry=industry)
    return base.model_copy(update={
        "company_key": KEY, "company_name": NAME, "generated_at": NOW,
        "observations": tuple(observations), "reconciliation": tuple(records),
        "canonical_values": tuple(values), "statements": tuple(fr.build_statements(values)),
        "warnings": tuple(collector.warnings),
    })


def secondary_observations():
    rows = []
    for year, revenue, pat, assets, reserves in (
        (2026, 13150, 4034, 166838, 19974), (2025, 10345, 2709, 128496, 15966),
    ):
        rows += [V(M.REVENUE, fy(year), revenue), V(M.PAT, fy(year), pat),
                 V(M.TOTAL_ASSETS, pit(year), assets), V(M.RESERVES, pit(year), reserves),
                 V(M.EQUITY_SHARE_CAPITAL, pit(year), 2002)]
    rows.append(V(M.EQUITY_SHARE_CAPITAL, pit(2023), 2002))
    base = make(rows)
    return [o.model_copy(update={
        "company_key": KEY, "authority": SourceAuthority.SECONDARY_AGGREGATOR,
        "source": o.source.model_copy(update={
            "source_type": SourceType.SCREENER,
            "source_url": "https://www.screener.in/company/HUDCO/consolidated/",
            "raw_artifact_id": UUID(int=22),
            "locator": f"retained-live-input/{o.metric}/{o.period.label}",
        }),
    }) for o in base.observations]


def hudco_dataset():
    return reconcile([*filing_observations(), *secondary_observations()])


def metric(analytics, code, year=2026):
    return next(m for m in analytics.all_metrics()
                if m.code == code and m.period.period_end == date(year, 3, 31)
                and m.reporting_basis == B.CONSOLIDATED)


def corrupt(o, raw, value, *, warnings=("SPLIT_NUMBER_REJOINED",)):
    return o.model_copy(update={
        "raw_value": raw, "parsed": parse_financial_number(value),
        "normalized_value": D(value), "confidence": Confidence.HIGH,
        "extraction_method": "box3-table/text-rows/right-aligned-numbers",
        "warnings": warnings,
    })


@pytest.mark.parametrize("code", ["EQUITY_SHARE_CAPITAL", "FINANCE_COST", "EMPLOYEE_COST",
                                  "CASH_AND_CASH_EQUIVALENTS"])
def test_exact_filing_cells_and_periods(code):
    fixture = json.loads(FIXTURE.read_text())
    data = reconcile(filing_observations())
    for year, truth in zip((2026, 2025), fixture["source_truth_crore"][code], strict=True):
        value = next(v for v in data.canonical_values
                     if v.metric.value == code and v.period.period_end == date(year, 3, 31))
        assert value.value == D(truth)
        obs = next(o for o in data.observations
                   if o.observation_id == value.selected_observation_id)
        assert obs.source.artifact_sha256 == fixture["source_sha256"]
        assert obs.source.page_number in (363, 364)
        assert obs.raw_value.replace(",", "") == truth
        assert fr.extraction_integrity_issue(obs) is None


@pytest.mark.parametrize("note,amount", [(23, "2,001.90"), (29, "8,930.69"),
                                       (31, "268.08"), (103, "5.55")])
def test_distinct_note_column_is_never_joined(note, amount):
    row = {0: ("Expense", False), 1: (str(note), False),
           2: (amount, False), 3: ("1.25", False)}
    assert fr._row_values(row, [2, 3], note_columns=(1,)) == (
        [(amount, 2), ("1.25", 3)], None)
    assert fr._row_values(row, [2, 3])[1] == "SPLIT_NUMBER_SUSPECT"


@pytest.mark.parametrize("raw,value", [("2, 001.90", "2001.90"),
                                      ("2,001 .90", "2001.90"),
                                      ("12 345.67", "12345.67"),
                                      ("(2,\n001.90)", "-2001.90")])
def test_only_verified_single_cell_can_reconstruct_whitespace(raw, value):
    row = {0: ("Amount", False), 1: (raw, False), 2: ("1.25", False)}
    assert fr._row_values(row, [1, 2])[1] == "SPLIT_NUMBER_SUSPECT"
    values, warning = fr._row_values(row, [1, 2], cell_groups=[[1], [2]])
    assert warning == "SPLIT_NUMBER_REJOINED"
    assert parse_financial_number("".join(values[0][0].split())).value == D(value)
    assert values[1] == ("1.25", 2)


@pytest.mark.parametrize("basis", ["Standalone", "Consolidated"])
async def test_notes_and_split_pdf_fragments_have_structural_boundaries(tmp_path, basis):
    pages = profit_and_loss(basis)
    pages[4] = "Revenue from operations  23  2, 001.90  1,900.00"
    pages[7] = "Employee benefit expenses  31  268.08  230.04"
    pages[8] = "Finance cost  29  8,930.69  6,747.45"
    bundle, catalogue = await filings(tmp_path, {"/source.pdf": (T.ANNUAL_REPORT, [pages])})
    data = build(bundle=bundle, catalogue=catalogue)
    for code, truth in [(M.REVENUE_FROM_OPERATIONS, "2001.90"), (M.EMPLOYEE_COST, "268.08"),
                        (M.FINANCE_COST, "8930.69")]:
        v = next(v for v in data.canonical_values if v.metric == code
                 and v.period.period_end == date(2026, 3, 31))
        assert v.value == D(truth)
        assert v.reporting_basis.value == basis.upper()
    assert fr.verify_financial_provenance(data, [], bundle) == []


@pytest.mark.parametrize("basis", ["Standalone", "Consolidated"])
async def test_adjacent_quarter_columns_never_join(tmp_path, basis):
    page = [f"{basis} Financial Results", "(In Rs. crore)",
            "Particulars Note Quarter ended June 30, 2026 Quarter ended March 31, 2026",
            "Revenue from operations 23 2,001.90 1,900.00",
            "Finance cost 29 8,930.69 6,747.45", "Employee benefit expense 31 268.08 230.04"]
    bundle, catalogue = await filings(tmp_path, {"/quarter.pdf": (T.QUARTERLY_RESULT, [page])})
    data = build(bundle=bundle, catalogue=catalogue)
    assert {(v.period.period_end, v.value) for v in data.canonical_values
            if v.metric == M.EMPLOYEE_COST} == {(date(2026, 6, 30), D("268.08")),
                                               (date(2026, 3, 31), D("230.04"))}


@pytest.mark.parametrize("fallback", [True, False])
def test_malformed_primary_excluded_before_authority(fallback):
    primary = next(o for o in filing_observations() if o.metric == M.EQUITY_SHARE_CAPITAL
                   and o.period.period_end == date(2026, 3, 31))
    bad = corrupt(primary, "23 2,001.90", "232001.90")
    others = [o for o in secondary_observations() if o.metric == primary.metric
              and o.period.period_end == primary.period.period_end] if fallback else []
    data = reconcile([bad, *others])
    record, = data.reconciliation
    assert record.excluded_observation_ids == (bad.observation_id,)
    assert "extraction" in record.reason
    assert any(w.code == "EXTRACTION_INTEGRITY_REJECTED"
               and bad.observation_id in w.observation_ids for w in data.warnings)
    if fallback:
        assert data.canonical_values[0].value == D(2002)
        assert data.canonical_values[0].selected_observation_id == others[0].observation_id
    else:
        assert record.outcome == Outcome.NOT_COMPARABLE
        assert not data.canonical_values


def test_valid_primary_still_wins_disagreement():
    primary = next(o for o in filing_observations() if o.metric == M.EQUITY_SHARE_CAPITAL
                   and o.period.period_end == date(2026, 3, 31))
    secondary = primary.model_copy(update={
        "observation_id": uuid5(primary.observation_id, "secondary"),
        "authority": SourceAuthority.SECONDARY_AGGREGATOR,
        "raw_value": "1900", "parsed": parse_financial_number("1900"),
        "normalized_value": D(1900),
    })
    data = reconcile([secondary, primary])
    assert data.reconciliation[0].outcome == Outcome.SOURCE_PRECEDENCE
    assert data.canonical_values[0].value == D("2001.90")


@pytest.mark.parametrize("code,raw,badvalue,dependent", [
    (M.EQUITY_SHARE_CAPITAL, "23 2,001.90", "232001.90", "ROE"),
    (M.EMPLOYEE_COST, "31 268.08", "31268.08", "EMPLOYEE_COST_TO_REVENUE"),
    (M.FINANCE_COST, "29 8,930.69", "298930.69", "INTEREST_COVERAGE"),
    (M.TOTAL_ASSETS, "6 166,838", "6166838", "ROA"),
])
def test_rejected_input_cannot_feed_calculations_or_reports(code, raw, badvalue, dependent):
    # GENERAL here exercises otherwise-applicable finance-cost ratios. This is not a
    # HUDCO classification change: actual HUDCO integration below stays UNKNOWN.
    all_obs = [*filing_observations(), *secondary_observations()]
    target = next(o for o in all_obs if o.metric == code
                  and o.period.period_end == date(2026, 3, 31))
    bad = corrupt(target, raw, badvalue)
    remaining = [o for o in all_obs if not (o.metric == code
                 and o.period.period_end == target.period.period_end)]
    data = reconcile([*remaining, bad], industry=Industry.GENERAL)
    box5 = build_financial_analytics(data, generated_at=NOW)
    result = metric(box5, dependent)
    assert result.status.value != "CALCULATED" and result.value is None
    if code == M.EQUITY_SHARE_CAPITAL:
        assert metric(box5, "SHAREHOLDERS_EQUITY").value is None
        assert metric(box5, "SHARE_CAPITAL_GROWTH_YOY").value is None
    rejected_values = {r.record_id for r in data.reconciliation
                       if bad.observation_id in r.excluded_observation_ids}
    assert rejected_values
    box10 = build_forecast(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=KEY), generated_at=NOW, box10=box10)
    for m in box13.financial_forensics.metrics:
        if m.status == "AVAILABLE":
            assert bad.observation_id not in m.record_ids
        if (code == M.EQUITY_SHARE_CAPITAL and m.code == "SHARE_CAPITAL_GROWTH_3Y"
                or code == M.FINANCE_COST and m.code == "FINANCE_COST_TO_AVERAGE_DEBT"):
            assert m.status != "AVAILABLE" and m.value is None
    report = build_final_report(request=FinalReportRequest(as_of=NOW.date(), company_key=KEY),
                               generated_at=NOW, box4=data, box5=box5, box13=box13, box10=box10)
    assert not any(c.value == D(badvalue) and c.claim_type.value != "DATA_LIMITATION"
                   for c in report.claims)
    assert not any(bad.observation_id in c.evidence_ids and c.claim_type.value in
                   {"FACT", "CALCULATION", "SCENARIO"} for c in report.claims)
    peer_metric = next(m for m in BENCHMARK_METRICS if m.code == "ROE")
    peers = values_of(CompanyLayers(KEY, box4=data, box5=box5), peer_metric)
    if dependent == "ROE":
        assert not any(v.calculated for v in peers if v.period_end == date(2026, 3, 31))


def test_exact_hudco_valid_dependencies_and_peer_input():
    data = hudco_dataset()
    box5 = build_financial_analytics(data, generated_at=NOW)
    assert metric(box5, "SHAREHOLDERS_EQUITY").value == D("21975.90")
    expected_roe = (D(4034) / ((D("21975.90") + D("17967.90")) / 2) * 100).quantize(D("0.000001"))
    assert metric(box5, "ROE").value == expected_roe
    assert metric(box5, "EMPLOYEE_COST_TO_REVENUE").value == D("2.038631")
    assert metric(box5, "SHARE_CAPITAL_GROWTH_YOY").value == 0
    roe = next(m for m in BENCHMARK_METRICS if m.code == "ROE")
    assert any(v.value == expected_roe and v.calculated for v in values_of(
        CompanyLayers(KEY, box4=data, box5=box5), roe))
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=KEY), generated_at=NOW)
    growth_3y = next(m for m in box13.financial_forensics.metrics
                     if m.code == "SHARE_CAPITAL_GROWTH_3Y")
    assert growth_3y.value == D("-0.004995")  # exact capital vs rounded FY2023 secondary
    text = box13.model_dump_json()
    for old in ["0acdfa5b-a399-505f-ba28-8b0ec8c80e73", "ceb5008d-fedb-59d1-b900-864d5d0e3b27",
                "7b438cdb-9818-59be-a54a-9d2d6ac55743", "41fd0af2-abc5-5c7c-b961-c7f8d1f767a6"]:
        assert old not in text


def test_stale_selection_cannot_bypass_calculation_guard():
    data = hudco_dataset()
    target = next(o for o in data.observations if o.metric == M.EMPLOYEE_COST
                  and o.period.period_end == date(2026, 3, 31))
    bad = corrupt(target, "31 268.08", "31268.08")
    stale = data.model_copy(update={"observations": tuple(
        bad if o.observation_id == target.observation_id else o for o in data.observations)})
    analytics = build_financial_analytics(stale, generated_at=NOW)
    result = metric(analytics, "EMPLOYEE_COST_TO_REVENUE")
    assert result.status.value == "INPUT_MISSING" and result.value is None
    assert any("extraction integrity rejected" in note for note in result.missing_inputs)


def test_structural_warning_rejected_even_if_confidence_was_raised():
    target = filing_observations()[0]
    bad = target.model_copy(update={"warnings": ("SPLIT_NUMBER_SUSPECT",),
                                   "confidence": Confidence.HIGH})
    assert not reconcile([bad]).canonical_values


@pytest.mark.parametrize("header", ["Note", "Notes", "Note No.", "Ref.", "Reference"])
@pytest.mark.parametrize("amount", ["2, 001.90", "2,001 .90", "12 345.67", "(2,\n001.90)"])
def test_explicit_html_cells_keep_notes_and_wrapped_amount_separate(header, amount):
    html = f"""<h1>Consolidated Statement of Profit and Loss</h1>
    <p>(In Rs. crore)</p><table><tr><th>Description</th><th>{header}</th>
    <th>Year ended March 31, 2026</th><th>Year ended March 31, 2025</th></tr>
    <tr><td>Finance cost</td><td>29</td><td>{amount}</td><td>6,747.45</td></tr>
    <tr><td>Employee benefit expenses</td><td>31</td><td>268.08</td><td>230.04</td></tr>
    </table>"""
    raw = parse_html(html.encode(), ExtractionLimits())
    ex = statement_extraction(raw)
    data = reconcile(filing_observations(ex))
    result = next(v for v in data.canonical_values if v.metric == M.FINANCE_COST
                  and v.period.period_end == date(2026, 3, 31))
    assert result.value == parse_financial_number("".join(amount.split())).value
    assert fr.verify_financial_provenance(data, [], SimpleNamespace(extractions=(ex,))) == []


@pytest.mark.parametrize("row", [
    "Finance cost 29  8,930.69",  # Notes + one amount: don't shift the note into FY2026.
    "Finance cost  2, 001.90 1,900.00",  # no column separators to disambiguate the split
    "Finance cost  31 268.08  230.04",  # no separate reference cell
])
async def test_ambiguous_pdf_rows_are_retained_but_not_canonical(tmp_path, row):
    page = profit_and_loss("Consolidated")
    page[8] = row
    bundle, catalogue = await filings(tmp_path, {"/ambiguous.pdf": (T.ANNUAL_REPORT, [page])})
    data = build(bundle=bundle, catalogue=catalogue)
    assert any(o.metric == M.FINANCE_COST for o in data.observations)
    assert not any(v.metric == M.FINANCE_COST for v in data.canonical_values)
    assert any(w.code == "EXTRACTION_INTEGRITY_REJECTED" for w in data.warnings)


@pytest.mark.parametrize("field,value", [("raw_value", "23 2,001.90"),
                                        ("normalized_value", D("232001.90"))])
def test_scalar_integrity_cannot_be_overridden_by_source_authority(field, value):
    primary = next(o for o in filing_observations() if o.metric == M.EQUITY_SHARE_CAPITAL)
    assert not reconcile([primary.model_copy(update={field: value})]).canonical_values


def test_retained_infosys_legitimate_splits_still_reconstruct():
    path = FIXTURE.with_name("infosys_split_cells.json")
    fixture = json.loads(path.read_text())
    ex = statement_extraction(fixture_path=path)
    observations = filing_observations(ex)
    collector = fr._Collector(ex.company_key, FinancialLimits())
    records = fr.reconcile(observations, TolerancePolicy(), collector)
    values = fr.canonical_values(records, {o.observation_id: o for o in observations})
    for code, truths in fixture["source_truth_crore"].items():
        for year, truth in zip((2026, 2025), truths, strict=True):
            value = next(v for v in values if v.metric.value == code
                         and v.period.period_end == date(year, 3, 31))
            assert value.value == D(truth)
            assert value.reporting_basis == B.STANDALONE
            obs = next(o for o in observations
                       if o.observation_id == value.selected_observation_id)
            assert obs.source.artifact_sha256 == fixture["source_sha256"]
            assert fr.extraction_integrity_issue(obs) is None


def test_legacy_note_as_cash_is_rejected_even_when_raw_decimal_agrees():
    cash = next(o for o in filing_observations() if o.metric == M.CASH_AND_CASH_EQUIVALENTS)
    bad = corrupt(cash, "6", "6", warnings=())
    data = reconcile([bad])
    assert not data.canonical_values
    assert data.reconciliation[0].excluded_observation_ids == (bad.observation_id,)
