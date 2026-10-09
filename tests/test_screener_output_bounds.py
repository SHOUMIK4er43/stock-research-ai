"""P1 output amplification: a Screener page accepted by the structural HTML bounds must not
yield unbounded output. Two causes are closed here:

- section ownership: statement sections nested around one table each used to read it
  (five sections -> five copies of every cell); an element now belongs to its nearest
  enclosing Screener section only, and a duplicated section id fails closed;
- a direct record/text budget: every record kept from a page is charged, with the source
  text it copies, before it is built, so one big table, a long label or header, or a link
  flood fails closed instead of producing tens of thousands of records.

Offline, synthetic pages plus the checked-in Screener fixtures; temporary databases."""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener import metrics
from stock_research_ai.adapters.screener.evidence import ScreenerEvidenceSource
from stock_research_ai.adapters.screener.html import Page, data_tables, owned
from stock_research_ai.adapters.screener.metrics import (
    MAX_EXTRACTION_RECORDS,
    MAX_EXTRACTION_TEXT,
    ExtractionLimitExceeded,
)
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.peers_api import PeerFetch
from stock_research_ai.adapters.screener.research_dataset import (
    build_screener_dataset,
    parse_peer_fragment,
)
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ExtractionServices, ResearchEngine
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import (
    CompanyExtractionResult,
    ExtractionRequest,
    Section,
)
from stock_research_ai.domain.provenance import RawArtifact, RetrievalStatus, SourceRetrieval

URL = "https://www.screener.in/company/HOST/"
STATEMENTS = {
    Section.QUARTERLY: "quarters",
    Section.PROFIT_LOSS: "profit-loss",
    Section.BALANCE_SHEET: "balance-sheet",
    Section.CASH_FLOW: "cash-flow",
    Section.RATIOS: "ratios",
}
HEAD = (
    '<html><body><div id="company-info" data-company-id="1" data-consolidated="true"></div>'
    '<div id="top"><h1>Hostile Ltd</h1><a href="https://www.nseindia.com/x">NSE: HOST</a></div>'
)


def table(rows: int, columns: int, label: str = "Sales", header: str = "Mar") -> str:
    """`rows` x `columns` data cells; a mapped label and dated headers raise no issues."""
    head = "<tr><th></th>" + "".join(
        f'<th data-date-key="{2000 + i}-03-31">{header} {2000 + i}</th>' for i in range(columns)
    )
    body = (f"<tr><td>{label}</td>" + "<td>1,234</td>" * columns + "</tr>") * rows
    return f'<table class="data-table">{head}</tr>{body}</table>'


def page(body: str) -> bytes:
    return (HEAD + body + "</body></html>").encode()


def nest(ids: list[str], inner: str) -> str:
    return "".join(f'<section id="{i}">' for i in ids) + inner + "</section>" * len(ids)


def inputs(content: bytes) -> tuple[RawArtifact, SourceRetrieval]:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    retrieval = SourceRetrieval(
        id=UUID(int=2),
        provider="screener",
        requested_url=URL,
        final_url=URL,
        retrieved_at=at,
        status=RetrievalStatus.SUCCESS,
        mode="replay",
    )
    artifact = RawArtifact(
        id=UUID(int=1),
        source_url=URL,
        source_type="screener_html",
        retrieved_at=at,
        content_reference="memory",
        content_sha256=hashlib.sha256(content).hexdigest(),
        collector_version="1",
        retrieval_id=retrieval.id,
        size_bytes=len(content),
    )
    return artifact, retrieval


def normalize(content: bytes) -> CompanyExtractionResult:
    artifact, retrieval = inputs(content)
    return ScreenerNormalizer().normalize(content, artifact, retrieval, UUID(int=3))


def identify(content: bytes) -> object:
    artifact, retrieval = inputs(content)
    return ScreenerNormalizer().identify(content, artifact, retrieval, UUID(int=3))


def records(result: CompanyExtractionResult) -> dict[Section, int]:
    return {s.section: s.records for s in result.coverage.sections}


def codes(result: CompanyExtractionResult, section: Section | None = None) -> set[str]:
    return {
        i.code
        for s in result.coverage.sections
        if section is None or s.section == section
        for i in s.issues
    }


def assert_failed_closed(result: CompanyExtractionResult, code: str) -> None:
    assert result.company is None and result.coverage.overall == "FAILED"
    assert result.quarterly == result.profit_loss == result.balance_sheet == ()
    assert result.cashflow == result.ratios == result.top_metrics == result.documents == ()
    assert result.peers.entries == () and result.shareholding.holdings == ()
    assert codes(result) == {code}
    assert result.raw_artifacts and result.retrievals  # the source evidence stays linked


# --- section ownership -------------------------------------------------------------------


@pytest.mark.parametrize("section", list(STATEMENTS))
def test_one_table_in_each_normal_statement_section(section):
    result = normalize(page(nest([STATEMENTS[section]], table(3, 4))))
    assert result.company is not None
    counts = records(result)
    assert counts[section] == 12
    assert sum(counts[s] for s in STATEMENTS) == 12
    observations = {
        Section.QUARTERLY: result.quarterly,
        Section.PROFIT_LOSS: result.profit_loss,
        Section.BALANCE_SHEET: result.balance_sheet,
        Section.CASH_FLOW: result.cashflow,
        Section.RATIOS: result.ratios,
    }[section]
    assert len(observations) == 12
    assert all(o.provenance.source_location.section == section.value for o in observations)


def test_nested_quarters_and_profit_loss_read_the_table_once():
    result = normalize(page(nest(["quarters", "profit-loss"], table(3, 4))))
    counts = records(result)
    assert counts[Section.PROFIT_LOSS] == 12 and counts[Section.QUARTERLY] == 0
    assert len(result.profit_loss) == 12 and result.quarterly == ()
    assert "EMPTY_SECTION" in codes(result, Section.QUARTERLY)


def test_all_five_statement_sections_nested_around_one_table_read_it_once():
    result = normalize(page(nest(list(STATEMENTS.values()), table(12, 12))))
    counts = records(result)
    # The nearest enclosing section (the innermost) owns the table; the others hold none.
    assert counts[Section.RATIOS] == 144
    assert sum(counts[s] for s in STATEMENTS) == 144
    locators = [o.provenance.source_location.locator for o in result.ratios]
    assert len(set(locators)) == 144
    for section in list(STATEMENTS)[:-1]:
        assert "EMPTY_SECTION" in codes(result, section)


def test_the_original_nested_attack_fails_closed_quickly():
    # The audited page: five sections nested around one ~285 x 60 table (85k observations
    # and ~1 GB RSS before). Read once it is still 16,756 cells: past the record budget.
    content = page(nest(list(STATEMENTS.values()), table(284, 59)))
    assert len(content) < 400_000
    started = time.monotonic()
    result = normalize(content)
    assert time.monotonic() - started < 5.0
    assert_failed_closed(result, "EXTRACTION_LIMIT_EXCEEDED")
    assert identify(content) is None


def test_a_table_belongs_to_its_own_section_through_plain_wrappers():
    body = (
        '<section id="quarters"><div><div class="responsive"><div>'
        + table(2, 3)
        + '</div></div></div></section><section id="profit-loss"><p>none</p></section>'
    )
    result = normalize(page(body))
    counts = records(result)
    assert counts[Section.QUARTERLY] == 6 and counts[Section.PROFIT_LOSS] == 0
    root = Page(HEAD + body).root
    quarters, profit_loss = root.by_id("quarters"), root.by_id("profit-loss")
    assert quarters is not None and profit_loss is not None
    assert len(data_tables(quarters)) == 1 and data_tables(profit_loss) == []


def test_owned_walk_stops_only_at_other_screener_sections():
    root = Page(
        '<section id="shareholding"><div id="quarterly-shp"><table class="data-table">'
        '</table></div><section id="peers"><table class="data-table"></table></section>'
        "</section>"
    ).root
    holding = root.by_id("shareholding")
    assert holding is not None
    assert [n.attrs.get("id") for n in owned(holding, "div")] == ["quarterly-shp"]
    assert len(data_tables(holding)) == 1  # the nested #peers table is not shareholding's


@pytest.mark.parametrize("duplicated", [*STATEMENTS.values(), "documents", "company-info"])
def test_duplicate_section_id_fails_closed(duplicated):
    # HEAD already holds #company-info and the page below #quarters: one more makes two.
    copies = 1 if duplicated in {"quarters", "company-info"} else 2
    content = page(nest(["quarters"], table(2, 2)) + nest([duplicated], table(2, 2)) * copies)
    result = normalize(content)
    assert_failed_closed(result, "AMBIGUOUS_SECTION")
    assert {i.message for s in result.coverage.sections for i in s.issues} == {
        f"Duplicate section id: {duplicated}"
    }
    assert identify(content) is None


def test_two_distinct_tables_in_one_section_stay_distinct():
    result = normalize(page(nest(["quarters"], table(2, 3) + table(2, 3, label="Expenses"))))
    assert len(result.quarterly) == 12
    assert len({o.provenance.source_location.locator for o in result.quarterly}) == 12
    assert {o.original_label for o in result.quarterly} == {"Sales", "Expenses"}


def test_identical_tables_in_different_sections_are_not_deduplicated_by_content():
    same = table(2, 3)
    result = normalize(page(nest(["quarters"], same) + nest(["profit-loss"], same)))
    assert len(result.quarterly) == len(result.profit_loss) == 6
    assert [o.value for o in result.quarterly] == [o.value for o in result.profit_loss]


# --- direct output budget ----------------------------------------------------------------


def _at_budget(extra: str = "") -> bytes:
    # 40 x 100 mapped, dated cells: exactly MAX_EXTRACTION_RECORDS observations, no issues.
    assert MAX_EXTRACTION_RECORDS == 4_000
    return page(nest(["quarters"], table(40, 100)) + extra)


def test_record_budget_exact_boundary_is_accepted():
    result = normalize(_at_budget())
    assert result.company is not None and len(result.quarterly) == MAX_EXTRACTION_RECORDS
    assert result.coverage.overall == "PARTIAL"  # only the absent sections are flagged


def test_record_budget_plus_one_fails_closed():
    link = '<section id="documents"><a href="https://www.bseindia.com/a.pdf">AR</a></section>'
    content = _at_budget(link)
    assert_failed_closed(normalize(content), "EXTRACTION_LIMIT_EXCEEDED")
    assert identify(content) is None


def test_budget_is_enforced_before_records_are_built(monkeypatch):
    built = 0
    real = metrics.FinancialObservation

    def counting(*args, **kwargs):
        nonlocal built
        built += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(metrics, "FinancialObservation", counting)
    result = normalize(page(nest(["quarters"], table(280, 60))))  # 16,800 cells
    assert result.coverage.overall == "FAILED"
    assert built == MAX_EXTRACTION_RECORDS  # never more than the budget, then it stops


@pytest.mark.parametrize(
    "body",
    [
        nest(["quarters"], table(1, 1_000, label="S" * 10_000)),  # one label, every cell
        nest(["quarters"], table(1_000, 1, header="M" * 10_000)),  # one header, every row
        nest(["quarters"], table(1, 1).replace("1,234", "9" * (MAX_EXTRACTION_TEXT + 1))),
        nest(["documents"], '<a href="https://www.bseindia.com/a.pdf">AR</a>' * 4_001),
        nest(
            ["quarters"],
            '<table class="data-table"><tr><th></th><th>Mar 2024</th></tr>'
            + "<tr><td></td></tr>" * 4_001
            + "</table>",
        ),
        '<ul id="top-ratios">'
        + "<li>" * 100
        + '<span class="name">Market Cap</span><span class="value">'
        + "x" * 5_000
        + "</span>"
        + "</li>" * 100
        + "</ul>",
    ],
    ids=["long-label", "long-header", "long-value", "link-flood", "malformed-rows", "nested-li"],
)
def test_text_and_record_amplification_fails_closed(body):
    started = time.monotonic()
    result = normalize(page(body))
    assert time.monotonic() - started < 5.0
    assert_failed_closed(result, "EXTRACTION_LIMIT_EXCEEDED")


def test_peers_are_charged_to_the_same_budget():
    head = "<tr><th>S.No.</th><th>Name</th><th>P/E</th></tr>"
    row = '<tr><td>1</td><td><a href="/company/X/">X</a></td><td>1</td></tr>'
    small = normalize(page(nest(["peers"], f'<table class="data-table">{head}{row * 3}</table>')))
    assert len(small.peers.entries) == 3
    big = page(nest(["peers"], f'<table class="data-table">{head}{row * 2_001}</table>'))
    assert_failed_closed(normalize(big), "EXTRACTION_LIMIT_EXCEEDED")


async def _engine(tmp_path):
    database = SQLiteDatabase(tmp_path / "db.sqlite3")
    await initialize_database(Settings(database_path=tmp_path / "db.sqlite3"))
    store = FileRawArtifactStore(tmp_path / "evidence")
    engine = ResearchEngine(
        database.repository,
        ExtractionServices(
            source=ScreenerSource(),
            artifacts=store,
            normalizer=ScreenerNormalizer(),
            repository=database.extractions,
        ),
    )
    return database, engine


async def test_over_budget_page_persists_no_company_and_keeps_the_raw_artifact(tmp_path):
    content = page(nest(list(STATEMENTS.values()), table(284, 59)))
    path = tmp_path / "page.html"
    path.write_bytes(content)
    database, engine = await _engine(tmp_path)
    try:
        result = await engine.extract_company(
            ExtractionRequest(provider="screener", url=URL, replay_path=path)
        )
        stored = await database.extractions.get_extraction(result.metadata.id)
    finally:
        await database.close()
    assert_failed_closed(result, "EXTRACTION_LIMIT_EXCEEDED")
    assert stored == result
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM companies").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM listings").fetchone() == (0,)
        assert db.execute("SELECT status, company_id FROM extractions").fetchall() == [
            ("FAILED", None)
        ]
    (artifact,) = result.raw_artifacts
    preserved = list((tmp_path / "evidence").rglob(f"{artifact.content_sha256}*"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == content


# --- legitimate pages are unchanged ------------------------------------------------------

# Recorded at 477de10 (before ownership and the budget) from the checked-in fixtures.
LEGITIMATE = {
    "yesbank_consolidated.html": (169, 156, 132, 72, 12, 10, 126, 86),
    "infy_standalone.html": (143, 156, 120, 72, 72, 10, 161, 76),
}


@pytest.mark.parametrize("name", list(LEGITIMATE))
def test_legitimate_fixture_output_is_unchanged_and_well_inside_the_budget(
    name, fixture_dir, monkeypatch
):
    content = (fixture_dir / name).read_bytes()
    result = normalize(content)
    holdings = len(result.shareholding.holdings) + len(result.shareholding.shareholder_counts)
    assert (
        len(result.quarterly),
        len(result.profit_loss),
        len(result.balance_sheet),
        len(result.cashflow),
        len(result.ratios),
        len(result.top_metrics),
        holdings,
        len(result.documents),
    ) == LEGITIMATE[name]
    assert identify(content) == result.company
    charged: list[tuple[int, int]] = []
    real = metrics.OutputBudget.charge

    def spy(self, *texts):
        real(self, *texts)
        charged.append((self.records, self.text))

    monkeypatch.setattr(metrics.OutputBudget, "charge", spy)
    normalize(content)
    monkeypatch.undo()
    records_used, text_used = max(charged)
    assert records_used < MAX_EXTRACTION_RECORDS / 4
    assert text_used < MAX_EXTRACTION_TEXT / 5
    dataset = build_screener_dataset(result, content)
    assert len(dataset.tables) == 7
    assert result.company is not None
    source = ScreenerEvidenceSource(result, content)
    discovery = asyncio.run(source.discover(source.company()))
    assert len(discovery.candidates) > len(result.documents)


# --- Box 1 / 1B builders re-read the same page -------------------------------------------


def test_dataset_builder_reads_a_nested_table_once():
    content = page(nest(list(STATEMENTS.values()), table(3, 4)))
    dataset = build_screener_dataset(normalize(content), content)
    assert [t.kind.value for t in dataset.tables] == ["RATIOS"]
    assert {i.code for i in dataset.issues} >= {"SECTION_UNAVAILABLE"}


def test_dataset_peer_fragment_is_budgeted():
    head = "<tr><th>S.No.</th><th>Name</th><th>P/E</th></tr>"
    row = '<tr data-row-company-id="9"><td>1</td><td><a href="/c/X/">X</a></td><td>1</td></tr>'

    def fetch(rows: int) -> PeerFetch:
        body = f'<table class="data-table">{head}{row * rows}</table>'.encode()
        return PeerFetch(
            status="RETRIEVED",
            code=None,
            url="https://www.screener.in/api/company/9/peers/",
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            content=body,
        )

    comparison = parse_peer_fragment(fetch(10), "1")
    assert comparison is not None and len(comparison.peers) == 10
    with pytest.raises(ExtractionLimitExceeded):
        parse_peer_fragment(fetch(2_001), "1")


# --- worst accepted page: bounded resources, exact persistence ----------------------------


def _worst_accepted() -> bytes:
    # Fills the record budget with the longest legal locators (an id restarts the locator,
    # so every cell's is ~1,000 of the 1,024 allowed characters).
    wrapper = '<div id="' + "i" * 960 + '">'
    return page(nest(["quarters"], wrapper + table(65, 60) + "</div>"))


async def _extract_with_heartbeat(tmp_path, jobs: int):
    path = tmp_path / "page.html"
    path.write_bytes(_worst_accepted())
    database, engine = await _engine(tmp_path)
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        request = ExtractionRequest(provider="screener", url=URL, replay_path=path)
        results = await asyncio.gather(*(engine.extract_company(request) for _ in range(jobs)))
        stored = [await database.extractions.get_extraction(r.metadata.id) for r in results]
    finally:
        beat.cancel()
        await database.close()
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    return results, stored, max(gaps)


async def test_worst_accepted_page_is_bounded_and_round_trips(tmp_path):
    (result,), (stored,), gap = await _extract_with_heartbeat(tmp_path, 1)
    assert result.company is not None and len(result.quarterly) == 3_900
    assert gap < 0.5  # 85k observations stalled the loop for ~0.5-0.7 s before
    payload = json.dumps(result.model_dump(mode="json"))
    assert len(payload) < 16 * 2**20  # was ~123 MB for the nested attack
    assert stored == result
    assert json.dumps(stored.model_dump(mode="json")) == payload


async def test_two_concurrent_worst_accepted_extractions(tmp_path):
    results, stored, gap = await _extract_with_heartbeat(tmp_path, 2)
    assert [len(r.quarterly) for r in results] == [3_900, 3_900]
    assert results[0].company is not None and results[1].company is not None
    assert results[0].company.company_id == results[1].company.company_id
    assert stored == results
    assert gap < 0.5
