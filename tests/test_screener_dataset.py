"""DETAILED_RESEARCH_V1 Box 1B: complete Screener dataset (offline fixtures only)."""

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_summary_input import INFY, YESBANK, replay

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.peers_api import PeerFetch, ScreenerPeerClient, peers_url
from stock_research_ai.adapters.screener.research_dataset import (
    acquire_screener_dataset,
    build_screener_dataset,
)
from stock_research_ai.application.screener_dataset import canonical_dataset_json
from stock_research_ai.domain.financial import ReportingScope, UnitKind, ValueStatus
from stock_research_ai.domain.screener_dataset import (
    DetailedScreenerDataset,
    ScreenerTable,
    ScreenerTableKind,
)

K = ScreenerTableKind
FIXTURES = Path(__file__).parent / "fixtures" / "screener"
PEER_TIME = datetime(2026, 10, 6, 6, 12, 31, tzinfo=UTC)
PEERS = {"1143": "peers_infy.html", "6599833": "peers_yesbank.html"}


async def page(tmp_path, fixture_dir, case, html=None):
    extraction = await replay(tmp_path, fixture_dir, case, html)
    content = await FileRawArtifactStore(tmp_path / "evidence").read(extraction.raw_artifacts[0])
    return extraction, content


def peer_fetch(warehouse_id: str) -> PeerFetch:
    return PeerFetch(
        "RETRIEVED",
        None,
        peers_url(warehouse_id),
        PEER_TIME,
        (FIXTURES / PEERS[warehouse_id]).read_bytes(),
    )


async def dataset(tmp_path, fixture_dir, case, warehouse_id=None, html=None):
    extraction, content = await page(tmp_path, fixture_dir, case, html)
    fetch = peer_fetch(warehouse_id) if warehouse_id else None
    return build_screener_dataset(extraction, content, fetch)


def table(data: DetailedScreenerDataset, kind: ScreenerTableKind) -> ScreenerTable:
    found = data.table(kind)
    assert found is not None, kind
    return found


def rows_of(data, kind):
    return [r.label for r in table(data, kind).rows]


def cell(data, kind, label, period):
    row = next(r for r in table(data, kind).rows if r.label == label)
    return next(c for c in row.cells if c.period == period)


# --- INFY: every section, every row, every period -------------------------------------


async def test_infy_complete_dataset(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, INFY, "1143")
    profile = data.profile
    assert data.company_key == "screener:1489"
    assert (profile.name, profile.source_company_key, profile.warehouse_id) == (
        "Infosys Ltd",
        "1489",
        "1143",
    )
    assert profile.reporting_basis is ReportingScope.STANDALONE
    assert (profile.bse_code, profile.nse_symbol) == ("500209", "INFY")
    assert profile.website_url == "http://www.infosys.com"
    assert profile.about and profile.about.startswith("Infosys Ltd provides consulting")
    assert profile.key_points
    assert profile.price is not None
    assert (profile.price.price.value, profile.price.change.displayed) == (
        Decimal("1087"),
        "-3.81%",
    )
    assert profile.price.as_of_text == "07 Sep 4:01 p.m."  # no year shown, none inferred
    assert [(c.level, c.name) for c in profile.classification] == [
        ("Broad Sector", "Information Technology"),
        ("Sector", "Information Technology"),
        ("Broad Industry", "IT - Software"),
        ("Industry", "Computers - Software & Consulting"),
    ]
    assert len(profile.index_memberships) == 57
    assert profile.index_memberships[0].name == "BSE Sensex"

    assert [m.label for m in data.snapshot] == [
        "Market Cap",
        "Current Price",
        "High / Low",
        "Stock P/E",
        "Book Value",
        "Dividend Yield",
        "ROCE",
        "ROE",
        "Face Value",
    ]
    market_cap = data.snapshot[0].value
    assert (market_cap.displayed, market_cap.value) == ("₹ 4,41,130 Cr.", Decimal("441130"))
    assert market_cap.unit is not None and market_cap.unit.scale == Decimal("10000000")
    high_low = data.snapshot[2]
    assert high_low.value.status is ValueStatus.NOT_APPLICABLE
    assert [(c.label, c.value.value) for c in high_low.components] == [
        ("High", Decimal("1728")),
        ("Low", Decimal("982")),
    ]
    assert data.snapshot[3].value.unit is None  # 'Stock P/E': the page states no unit

    expected = {
        K.QUARTERLY_RESULTS: (12, 13, "Jun 2023", "Jun 2026"),
        K.PROFIT_LOSS: (12, 13, "Mar 2015", "TTM"),
        K.BALANCE_SHEET: (10, 12, "Mar 2015", "Mar 2026"),
        K.CASH_FLOW: (6, 12, "Mar 2015", "Mar 2026"),
        K.RATIOS: (6, 12, "Mar 2015", "Mar 2026"),
        K.SHAREHOLDING_QUARTERLY: (7, 12, "Sep 2023", "Jun 2026"),
        K.SHAREHOLDING_YEARLY: (7, 11, "Mar 2017", "Jun 2026"),
    }
    for kind, (row_count, period_count, first, last) in expected.items():
        found = table(data, kind)
        assert (len(found.rows), len(found.periods)) == (row_count, period_count), kind
        assert (found.periods[0].label, found.periods[-1].label) == (first, last), kind
        assert all(len(r.cells) == period_count for r in found.rows)
    assert rows_of(data, K.QUARTERLY_RESULTS)[-1] == "Raw PDF"
    assert rows_of(data, K.SHAREHOLDING_QUARTERLY) == [
        "Promoters",
        "FIIs",
        "DIIs",
        "Government",
        "Public",
        "Others",
        "No. of Shareholders",
    ]
    assert table(data, K.PROFIT_LOSS).periods[-1].end_date is None  # TTM: no date invented
    assert str(table(data, K.BALANCE_SHEET).periods[-1].end_date) == "2026-03-31"

    assert [(g.title, [e.horizon for e in g.entries]) for g in data.growth] == [
        ("Compounded Sales Growth", ["10 Years", "5 Years", "3 Years", "TTM"]),
        ("Compounded Profit Growth", ["10 Years", "5 Years", "3 Years", "TTM"]),
        ("Stock Price CAGR", ["10 Years", "5 Years", "3 Years", "1 Year"]),
        ("Return on Equity", ["10 Years", "5 Years", "3 Years", "Last Year"]),
    ]
    first = data.growth[0].entries[0]
    assert (first.displayed_horizon, first.value.displayed, first.value.value) == (
        "10 Years:",
        "11%",
        Decimal("11"),
    )
    assert first.value.unit is not None and first.value.unit.kind is UnitKind.PERCENT

    assert data.notes is not None and len(data.notes.pros) == 4 and len(data.notes.cons) == 3
    assert data.notes.origin == "SCREENER_MACHINE_GENERATED"
    assert [i.code for i in data.issues] == ["INSIGHTS_NOT_CAPTURED"]


async def test_yesbank_bank_rows_and_no_fabricated_rows(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, YESBANK, "6599833")
    assert data.profile.reporting_basis is ReportingScope.CONSOLIDATED
    assert (data.profile.bse_code, data.profile.nse_symbol) == ("532648", "YESBANK")
    assert table(data, K.QUARTERLY_RESULTS).reporting_basis is ReportingScope.CONSOLIDATED
    assert rows_of(data, K.QUARTERLY_RESULTS) == [
        "Revenue",
        "Interest",
        "Expenses",
        "Financing Profit",
        "Financing Margin %",
        "Other Income",
        "Depreciation",
        "Profit before tax",
        "Tax %",
        "Net Profit",
        "EPS in Rs",
        "Gross NPA %",
        "Net NPA %",
        "Raw PDF",
    ]
    assert "Deposits" in rows_of(data, K.BALANCE_SHEET)
    assert rows_of(data, K.RATIOS) == ["ROE %"]
    # The quarterly holding table shows no Promoters row; none is invented.
    assert "Promoters" not in rows_of(data, K.SHAREHOLDING_QUARTERLY)
    assert "Promoters" in rows_of(data, K.SHAREHOLDING_YEARLY)
    assert len(data.growth) == 4 and all(len(g.entries) == 4 for g in data.growth)
    assert data.profile.classification[-1].name == "Private Sector Bank"


# --- Cross-check against the frozen extraction: nothing lost, nothing changed ----------


@pytest.mark.parametrize("case", [INFY, YESBANK])
async def test_every_frozen_observation_is_preserved_with_the_same_value(
    tmp_path, fixture_dir, case
):
    extraction, content = await page(tmp_path, fixture_dir, case)
    data = build_screener_dataset(extraction, content)
    sections = {
        "quarterly": K.QUARTERLY_RESULTS,
        "profit_loss": K.PROFIT_LOSS,
        "balance_sheet": K.BALANCE_SHEET,
        "cashflow": K.CASH_FLOW,
        "ratios": K.RATIOS,
    }
    checked = 0
    for attribute, kind in sections.items():
        for observation in getattr(extraction, attribute):
            label = observation.metric.removeprefix("source:")
            found = cell(data, kind, label, observation.period.fiscal_label)
            assert found.value.value == observation.value, (kind, label)
            assert found.value.status == observation.value_status, (kind, label)
            checked += 1
    assert checked > 400


# --- Units -------------------------------------------------------------------------------


async def test_units_only_where_the_page_states_them(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, INFY)
    units = {r.label: r.unit for r in table(data, K.RATIOS).rows}
    assert units["Cash Conversion Cycle"] is None  # no stated unit: unknown, not crores
    for label in ("Debtor Days", "Inventory Days", "Days Payable", "Working Capital Days"):
        assert units[label] is not None and units[label].kind is UnitKind.DAYS
    assert units["ROCE %"] is not None and units["ROCE %"].kind is UnitKind.PERCENT
    ccc = cell(data, K.RATIOS, "Cash Conversion Cycle", "Mar 2026").value
    assert (ccc.displayed, ccc.value, ccc.unit) == ("74", Decimal("74"), None)

    sales = cell(data, K.QUARTERLY_RESULTS, "Sales", "Jun 2026").value
    assert (sales.displayed, sales.value) == ("39,957", Decimal("39957"))
    assert sales.unit is not None and sales.unit.scale == Decimal("10000000")
    eps = next(r for r in table(data, K.QUARTERLY_RESULTS).rows if r.label == "EPS in Rs")
    assert eps.unit is not None and (eps.unit.kind, eps.unit.scale) == (UnitKind.MONEY, 1)
    cfo = next(r for r in table(data, K.CASH_FLOW).rows if r.label == "CFO/OP")
    assert cfo.unit is not None and cfo.unit.kind is UnitKind.PERCENT  # every cell shows '%'
    holders = next(
        r for r in table(data, K.SHAREHOLDING_YEARLY).rows if r.label == "No. of Shareholders"
    )
    assert holders.unit is not None and holders.unit.kind is UnitKind.NUMBER


async def test_raw_pdf_row_keeps_per_quarter_source_links(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, INFY)
    raw = next(r for r in table(data, K.QUARTERLY_RESULTS).rows if r.label == "Raw PDF")
    assert raw.unit is None
    assert raw.cells[0].link == "https://www.screener.in/company/source/quarter/1489/6/2023/"
    assert all(c.link for c in raw.cells)
    assert all(c.value.status is ValueStatus.BLANK for c in raw.cells)


# --- Missing and malformed data ------------------------------------------------------------


def remove_section(html: str, section_id: str) -> str:
    start = html.index(f'<section id="{section_id}"')
    end = html.index("</section>", start) + len("</section>")
    return html[:start] + html[end:]


async def test_missing_sections_are_reported_and_the_rest_survives(tmp_path, fixture_dir):
    html = remove_section((fixture_dir / INFY[0]).read_text(), "cash-flow")
    data = await dataset(tmp_path, fixture_dir, INFY, html=html)
    assert data.table(K.CASH_FLOW) is None
    assert ("SECTION_MISSING", "cash-flow") in {(i.code, i.section) for i in data.issues}
    assert data.table(K.PROFIT_LOSS) is not None and len(data.growth) == 4


async def test_loading_placeholder_and_missing_growth_tables(tmp_path, fixture_dir):
    html = (fixture_dir / INFY[0]).read_text()
    html = re.sub(
        r'<section id="quarters".*?</section>',
        '<section id="quarters">Loading results table ...</section>',
        html,
        count=1,
        flags=re.S,
    )
    html = re.sub(r'<table class="ranges-table">.*?</table>', "", html, flags=re.S)
    data = await dataset(tmp_path, fixture_dir, INFY, html=html)
    codes = {(i.code, i.section) for i in data.issues}
    assert ("SECTION_UNAVAILABLE", "quarters") in codes
    assert ("GROWTH_TABLES_MISSING", "profit-loss") in codes
    assert data.table(K.QUARTERLY_RESULTS) is None and data.growth == ()


async def test_malformed_value_keeps_raw_text_without_a_number(tmp_path, fixture_dir):
    html = (fixture_dir / INFY[0]).read_text()
    html, count = re.subn(r"(<td[^>]*>\s*)39,957(\s*</td>)", r"\g<1>39,9x7\g<2>", html, count=1)
    assert count == 1
    data = await dataset(tmp_path, fixture_dir, INFY, html=html)
    bad = cell(data, K.QUARTERLY_RESULTS, "Sales", "Jun 2026").value
    assert (bad.displayed, bad.value, bad.status) == ("39,9x7", None, ValueStatus.PARSE_ERROR)
    issue = next(i for i in data.issues if i.code == "PARSE_ERROR")
    assert (issue.section, issue.displayed) == ("quarters", "39,9x7")


# --- Provenance and serialization ------------------------------------------------------------


async def test_provenance_points_at_the_preserved_page_artifact(tmp_path, fixture_dir):
    extraction, content = await page(tmp_path, fixture_dir, INFY)
    data = build_screener_dataset(extraction, content)
    artifact = extraction.raw_artifacts[0]
    assert data.source.raw_artifact_id == artifact.id
    assert (
        data.source.content_sha256 == artifact.content_sha256 == hashlib.sha256(content).hexdigest()
    )
    assert data.source.retrieval_id == artifact.retrieval_id
    assert all(c.locator for t in data.tables for r in t.rows for c in r.cells)
    with pytest.raises(ValueError, match="does not match"):
        build_screener_dataset(extraction, content + b" ")


async def test_canonical_serialization_is_deterministic_and_round_trips(tmp_path, fixture_dir):
    extraction, content = await page(tmp_path, fixture_dir, YESBANK)
    first = build_screener_dataset(extraction, content, peer_fetch("6599833"))
    second = build_screener_dataset(extraction, content, peer_fetch("6599833"))
    encoded = canonical_dataset_json(first)
    assert encoded == canonical_dataset_json(second)
    restored = DetailedScreenerDataset.model_validate_json(encoded)
    assert restored == first
    assert canonical_dataset_json(restored) == encoded


def test_contract_is_strict(tmp_path):
    with pytest.raises(ValidationError):
        ScreenerTable.model_validate(
            {
                "kind": "RATIOS",
                "reporting_basis": "standalone",
                "caption": None,
                "periods": [{"label": "Mar 2026", "end_date": None}],
                "rows": [
                    {
                        "label": "ROE %",
                        "displayed_label": "ROE %",
                        "expandable": False,
                        "unit": None,
                        "cells": [],
                    }
                ],
                "locator": "#ratios",
            }
        )


# --- Peers -------------------------------------------------------------------------------------


async def test_peer_fragment_is_parsed_completely(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, INFY, "1143")
    peers = data.peers
    assert data.peer_status.status == "RETRIEVED" and peers is not None
    assert peers.columns == (
        "S.No.",
        "Company",
        "CMP Rs.",
        "P/E",
        "Mar Cap Rs.Cr.",
        "Div Yld %",
        "NP Qtr Rs.Cr.",
        "Qtr Profit Var %",
        "Sales Qtr Rs.Cr.",
        "Qtr Sales Var %",
        "ROCE %",
    )
    assert len(peers.peers) == 8
    assert [p.name for p in peers.peers if p.is_subject_company] == ["Infosys"]
    tcs = peers.peers[0]
    assert (tcs.rank, tcs.name, tcs.screener_company_id) == ("1.", "TCS", "3365")
    assert tcs.company_url == "https://www.screener.in/company/TCS/consolidated/"
    values = {c.column: c.value for c in tcs.cells}
    assert len(values) == 9
    assert values["Mar Cap Rs.Cr."].value == Decimal("757916.97")
    assert values["Mar Cap Rs.Cr."].unit.scale == Decimal("10000000")
    assert values["ROCE %"].unit.kind is UnitKind.PERCENT
    assert values["P/E"].unit is None
    assert [r.name for r in peers.summary_rows] == ["Median: 73 Co."]
    fixture = (FIXTURES / "peers_infy.html").read_bytes()
    assert peers.content_sha256 == hashlib.sha256(fixture).hexdigest()
    assert peers.retrieved_at == PEER_TIME  # its own time, not the page's
    assert peers.source_url == "https://www.screener.in/api/company/1143/peers/"


async def test_peers_not_requested_by_default(tmp_path, fixture_dir):
    data = await dataset(tmp_path, fixture_dir, INFY)
    assert (data.peer_status.status, data.peers) == ("NOT_REQUESTED", None)


async def test_peer_failures_never_break_the_dataset(tmp_path, fixture_dir):
    extraction, content = await page(tmp_path, fixture_dir, INFY)
    blocked = PeerFetch("BLOCKED", "HTTP_403", peers_url("1143"), PEER_TIME, None)
    data = build_screener_dataset(extraction, content, blocked)
    assert (data.peer_status.status, data.peer_status.code, data.peers) == (
        "BLOCKED",
        "HTTP_403",
        None,
    )
    assert len(data.tables) == 7
    empty = PeerFetch("RETRIEVED", None, peers_url("1143"), PEER_TIME, b"<div>no table</div>")
    assert build_screener_dataset(extraction, content, empty).peer_status.code == (
        "PEER_TABLE_MISSING"
    )
    foreign = PeerFetch("RETRIEVED", None, "https://evil.test/api/peers/", PEER_TIME, b"")
    with pytest.raises(ValueError, match="www.screener.in"):
        build_screener_dataset(extraction, content, foreign)


class Recorder:
    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.respond(request)


def html_response(status=200, content=b"", content_type="text/html; charset=utf-8", **kw):
    return lambda _r: httpx.Response(
        status, content=content, headers={"content-type": content_type, **kw}
    )


async def test_peer_client_makes_exactly_one_bounded_request():
    fixture = (FIXTURES / "peers_infy.html").read_bytes()
    handler = Recorder(html_response(content=fixture))
    fetch = await ScreenerPeerClient(transport=httpx.MockTransport(handler)).fetch("1143")
    (request,) = handler.requests
    assert str(request.url) == "https://www.screener.in/api/company/1143/peers/"
    assert request.method == "GET" and "cookie" not in request.headers
    assert (fetch.status, fetch.code, fetch.content) == ("RETRIEVED", None, fixture)


def raise_(exc):
    def respond(_request):
        raise exc

    return respond


@pytest.mark.parametrize(
    "respond,status,code",
    [
        (html_response(403), "BLOCKED", "HTTP_403"),
        (html_response(429), "BLOCKED", "HTTP_429"),
        (html_response(302, location="/login/"), "FAILED", "REDIRECT_REFUSED"),
        (html_response(500), "FAILED", "HTTP_ERROR"),
        (
            html_response(content=b"{}", content_type="application/json"),
            "FAILED",
            "UNEXPECTED_CONTENT_TYPE",
        ),
        (html_response(content=b"x" * 1_000_001), "FAILED", "RESPONSE_TOO_LARGE"),
        (raise_(httpx.ReadTimeout("t")), "FAILED", "TIMEOUT"),
        (raise_(httpx.ConnectError("c")), "FAILED", "CONNECTION_FAILED"),
    ],
)
async def test_peer_client_failures_are_statuses_with_no_retry(respond, status, code):
    handler = Recorder(respond)
    fetch = await ScreenerPeerClient(transport=httpx.MockTransport(handler)).fetch("1143")
    assert (fetch.status, fetch.code, fetch.content) == (status, code, None)
    assert len(handler.requests) == 1


@pytest.mark.parametrize("warehouse_id", ["", "abc", "1/../2", "1143?x=1", "1" * 13])
def test_peer_url_accepts_only_numeric_warehouse_ids(warehouse_id):
    with pytest.raises(ValueError):
        peers_url(warehouse_id)


async def test_acquire_fetches_peers_once_using_the_pages_own_warehouse_id(tmp_path, fixture_dir):
    extraction, content = await page(tmp_path, fixture_dir, YESBANK)
    handler = Recorder(html_response(content=(FIXTURES / "peers_yesbank.html").read_bytes()))
    data = await acquire_screener_dataset(
        extraction, content, ScreenerPeerClient(transport=httpx.MockTransport(handler))
    )
    assert [str(r.url) for r in handler.requests] == [
        "https://www.screener.in/api/company/6599833/peers/"
    ]
    assert data.peer_status.status == "RETRIEVED"
    assert [p.name for p in data.peers.peers if p.is_subject_company] == ["Yes Bank"]


async def test_acquire_without_warehouse_id_makes_no_request(tmp_path, fixture_dir):
    html = (fixture_dir / INFY[0]).read_text().replace(' data-warehouse-id="1143"', "", 1)
    extraction, content = await page(tmp_path, fixture_dir, INFY, html)
    handler = Recorder(html_response())
    data = await acquire_screener_dataset(
        extraction, content, ScreenerPeerClient(transport=httpx.MockTransport(handler))
    )
    assert handler.requests == []
    assert (data.peer_status.status, data.peer_status.code) == ("UNAVAILABLE", "NO_WAREHOUSE_ID")
