"""P1 re-audit R1: the shared HTML tree (Screener pages, Summary/research extraction,
document HTML) is linear and bounded. Sibling indexing does not rescan siblings, walks
are iterative, locators are derived from the parent chain, and node count / nesting depth
/ attributes per element are capped with a typed failure. Offline, synthetic inputs and
the checked-in Screener fixtures only; timing bounds are generous (the old quadratic
code took many seconds at these sizes)."""

import asyncio
import hashlib
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from stock_research_ai.adapters.documents.document_parsers import (
    LocalDocumentParser,
    parse_document,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.html import (
    MAX_ATTRIBUTES,
    MAX_DEPTH,
    MAX_LOCATOR_LENGTH,
    MAX_NODES,
    MAX_SCREENER_NODES,
    MAX_TAG_NAME_LENGTH,
    HtmlLimitExceeded,
    Node,
    Page,
    data_tables,
    rows,
)
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.sector import extract_broad_industry
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ExtractionServices, ResearchEngine
from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.document_intelligence import ExtractionLimits, ExtractionStatus
from stock_research_ai.domain.evidence_retrieval import DetectedContentType
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest

YESBANK_URL = "https://www.screener.in/company/YESBANK/consolidated/"


def nested(depth: int, tag: str = "div") -> str:
    return f"<{tag}>" * depth + "x" + f"</{tag}>" * depth


# --- unchanged output on legitimate pages ------------------------------------------------


def test_real_screener_fixture_tree_is_unchanged(fixture_dir):
    # Reference values recorded from the previous (pre-remediation) parser.
    root = Page((fixture_dir / "yesbank_consolidated.html").read_text()).root
    assert (len(root.all()), len(root.all("a")), len(root.raw_text), len(root.text)) == (
        2017,
        195,
        102891,
        13684,
    )
    locators = "\n".join(n.locator for n in root.all()).encode()
    assert (
        hashlib.sha256(locators).hexdigest()
        == "dbb338dc80bd0ddc93c04595d47be203d26b0f0d82de65cc7372dc788acbdb86"
    )
    quarters = root.by_id("quarters")
    assert quarters is not None and quarters.locator == "#quarters"
    table = data_tables(quarters)[0]
    contents = rows(table)
    assert table.locator == "#quarters/div[3]/table[1]" and len(contents) == 15
    assert contents[1][0].locator == "#quarters/div[3]/table[1]/tbody[1]/tr[1]/td[1]"
    assert contents[1][0].text == "Revenue +"
    assert contents[-1][-1].locator == "#quarters/div[3]/table[1]/tbody[1]/tr[14]/td[14]"
    anchors = root.all("a")
    assert anchors[0].locator == "document/html[1]/body[1]/nav[1]/ul[1]/li[1]/a[1]"
    assert anchors[-1].locator == "#large-footer/div[1]/div[4]/p[3]/a[2]"
    ratios = root.by_class("company-ratios")
    assert ratios is not None and ratios.locator == "#top/div[3]/div[2]"


def test_document_order_locators_and_text_semantics():
    root = Page(
        "<div id='x'><i>1</i><p>a <b>b</b></p><i>2<br><i>3</i></i></div>"
        "<script>if (a<b) x</script><style>p{}</style><p>  t \n u </p>"
    ).root
    assert [n.tag for n in root.all()] == ["div", "i", "p", "b", "i", "br", "i", "script"] + [
        "style",
        "p",
    ]
    assert [n.locator for n in root.all("i")] == ["#x/i[1]", "#x/i[2]", "#x/i[2]/i[1]"]
    assert root.all("br")[0].locator == "#x/i[2]/br[1]"
    assert root.all("p")[1].locator == "document/p[1]"  # script/style are counted siblings
    assert root.raw_text == "1a b23  t \n u "  # script and style text dropped
    assert root.all("p")[1].text == "t u"
    assert root.by_id("x") is root.all()[0] and root.by_class("missing") is None


def test_malformed_html_is_tolerated_as_before():
    root = Page("</div></b>lead<div><p>a<b>b</p>c</div></b><ul><li>1<li>2</ul></table>").root
    assert [(n.tag, n.locator) for n in root.all()] == [
        ("div", "document/div[1]"),
        ("p", "document/div[1]/p[1]"),
        ("b", "document/div[1]/p[1]/b[1]"),
        ("ul", "document/ul[1]"),
        ("li", "document/ul[1]/li[1]"),
        ("li", "document/ul[1]/li[1]/li[1]"),  # unclosed <li> nests, as before
    ]
    assert root.raw_text == "leadabc12"


def test_table_helpers_are_unchanged():
    root = Page(
        "<table class='data-table x'><tr><th>h</th><td>1</td></tr><tr><td>2</td></tr></table>"
        "<table><tr><td>ignored</td></tr></table>"
    ).root
    (table,) = data_tables(root)
    assert [[c.text for c in row] for row in rows(table)] == [["h", "1"], ["2"]]


# --- linear construction and iterative walks --------------------------------------------


def test_many_siblings_build_linearly():
    count = MAX_NODES - 10
    started = time.monotonic()
    root = Page("<div>" + "<br>" * count + "</div>").root
    breaks = root.all("br")
    elapsed = time.monotonic() - started
    assert len(breaks) == count
    assert breaks[-1].locator == f"document/div[1]/br[{count}]"
    assert elapsed < 5.0  # the old sibling rescan needed minutes here


def test_deepest_accepted_nesting_walks_without_recursion():
    root = Page(nested(MAX_DEPTH, "b")).root
    spans = root.all("b")
    assert len(spans) == MAX_DEPTH
    assert spans[-1].locator == "document" + "/b[1]" * MAX_DEPTH
    assert root.raw_text == "x" and spans[-1].text == "x"
    assert root.by_id("none") is None


def test_many_deep_chains_stay_fast():
    chain = "<a href='/x'>" * 100 + "t" + "</a>" * 100
    started = time.monotonic()
    root = Page(chain * 900).root  # 90,000 nodes, 100 deep
    assert len(root.all("a")) == 90_000 and len(root.raw_text) == 900
    assert time.monotonic() - started < 5.0


# --- hard structural bounds ---------------------------------------------------------------


def test_node_limit_fails_typed_and_fast():
    started = time.monotonic()
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page("<br>" * (MAX_NODES + 1))
    assert raised.value.limit == "nodes"
    assert time.monotonic() - started < 5.0


@pytest.mark.parametrize("depth", [MAX_DEPTH + 1, 5_000, 100_000])
def test_depth_limit_fails_typed_without_recursion_error(depth):
    started = time.monotonic()
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(nested(depth))
    assert raised.value.limit == "depth"
    assert time.monotonic() - started < 2.0


def test_attribute_limit_fails_typed():
    attributes = " ".join(f"a{i}=1" for i in range(MAX_ATTRIBUTES + 1))
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(f"<div {attributes}></div>")
    assert raised.value.limit == "attributes"
    assert "/" not in str(raised.value)  # no paths or internals
    Page("<div " + " ".join(f"a{i}=1" for i in range(MAX_ATTRIBUTES)) + "></div>")


def test_stray_end_tags_do_not_rescan_the_open_stack():
    started = time.monotonic()
    root = Page("<span>" * (MAX_DEPTH - 1) + "</nope>" * 500_000 + "x").root
    assert root.raw_text == "x"
    assert time.monotonic() - started < 5.0


# --- every caller fails closed --------------------------------------------------------------


def test_document_html_over_limit_is_too_large_in_and_out_of_process():
    content = b"<html><body>" + nested(5_000).encode() + b"</body></html>"
    limits = ExtractionLimits()
    for document in (
        parse_document(content, DetectedContentType.HTML, limits),
        LocalDocumentParser().parse(content, DetectedContentType.HTML, limits),
    ):
        assert document.status is ExtractionStatus.TOO_LARGE
        assert [code for code, _, _ in document.warnings] == ["HTML_STRUCTURE_LIMIT_REACHED"]


def test_sector_reader_fails_closed(fixture_dir):
    page = (fixture_dir / "yesbank_consolidated.html").read_bytes()
    assert extract_broad_industry(page, "utf-8") is not None
    hostile = page.replace(b"</body>", b"<div>" * (MAX_DEPTH + 1) + b"</body>")
    assert extract_broad_industry(hostile, "utf-8") is None


async def _extract(tmp_path, html: str):
    path = tmp_path / "page.html"
    path.write_text(html)
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "evidence")
    await initialize_database(settings)
    return await extract_snapshot(
        settings,
        ExtractionRequest(provider="screener", url=YESBANK_URL, replay_path=path),
        tmp_path / "out",
    )


async def test_over_limit_page_yields_no_company_dataset(fixture_dir, tmp_path):
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    hostile = html.replace("</body>", "<br>" * MAX_SCREENER_NODES + "</body>")
    result = await _extract(tmp_path, hostile)
    assert result.company is None
    assert result.coverage.overall == "FAILED"
    assert result.quarterly == () and result.documents == ()
    messages = {i.message for s in result.coverage.sections for i in s.issues}
    assert messages == {"HTML structure limit exceeded: nodes"}


def _with_cells(html: str, table_rows: int) -> str:
    """One extra 12-period quarterly table: every cell becomes a financial observation."""
    header = "<tr><th></th>" + "".join(f"<th>Mar {2000 + i}</th>" for i in range(12)) + "</tr>"
    body = "".join(f"<tr><td>L{r}</td>" + "<td>1</td>" * 12 + "</tr>" for r in range(table_rows))
    marker = html.index(">", html.index('<section id="quarters"')) + 1
    table = f'<table class="data-table">{header}{body}</table>'
    return html[:marker] + table + html[marker:]


async def test_event_loop_stays_responsive_on_the_worst_accepted_page(fixture_dir, tmp_path):
    # Observations, not markup, dominate cost: the extraction record budget, not the node
    # cap, now bounds them (test_screener_output_bounds.py), so fill that budget instead -
    # 1,500 unmapped cells (an observation and an issue each) on top of the real page.
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    hostile = _with_cells(html, 125)
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    path = tmp_path / "page.html"
    path.write_text(hostile)
    database = SQLiteDatabase(tmp_path / "db.sqlite3")
    await initialize_database(Settings(database_path=tmp_path / "db.sqlite3"))
    # The engine path the HTTP API runs (the CLI's JSON export is not on a shared loop).
    engine = ResearchEngine(
        database.repository,
        ExtractionServices(
            source=ScreenerSource(),
            artifacts=FileRawArtifactStore(tmp_path / "evidence"),
            normalizer=ScreenerNormalizer(),
            repository=database.extractions,
        ),
    )
    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        request = ExtractionRequest(provider="screener", url=YESBANK_URL, replay_path=path)
        result = await engine.extract_company(request)
    finally:
        beat.cancel()
        await database.close()
    assert result.company is not None  # accepted: the bound is not hit
    assert len(result.quarterly) == 169 + 125 * 12
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.5  # the old in-loop parse stalled for seconds


async def test_screener_cell_flood_past_the_screener_cap_saves_nothing(fixture_dir, tmp_path):
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    result = await _extract(tmp_path, _with_cells(html, 1_400))  # ~21,600 nodes
    assert result.company is None and result.quarterly == ()
    messages = {i.message for s in result.coverage.sections for i in s.issues}
    assert messages == {"HTML structure limit exceeded: nodes"}
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM companies").fetchone() == (0,)


def test_screener_cap_applies_to_screener_pages_only():
    page = "<div>" + "<br>" * MAX_SCREENER_NODES + "</div>"
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(page, MAX_SCREENER_NODES)
    assert raised.value.limit == "nodes"
    assert extract_broad_industry(page.encode(), "utf-8") is None
    assert len(Page(page).root.all("br")) == MAX_SCREENER_NODES  # documents keep MAX_NODES
    document = parse_document(
        f"<html><body>{page}</body></html>".encode(), DetectedContentType.HTML, ExtractionLimits()
    )
    assert document.status is not ExtractionStatus.TOO_LARGE


# --- locator length: tag names and ids cannot amplify provenance --------------------------
#
# Every observation copies its cell's locator, so a page wrapping its tables in one huge
# tag name (or an element with a huge id, which restarts the locator) used to repeat that
# string per observation: RSS and event-loop stalls grew with name length x observations.


def test_tag_names_up_to_the_limit_are_kept_and_longer_ones_rejected():
    tag = "x" * MAX_TAG_NAME_LENGTH
    root = Page(f"<{tag}><p>a</p></{tag}><ytd-masthead>b</ytd-masthead>").root
    assert root.all("p")[0].locator == f"document/{tag}[1]/p[1]"
    assert root.all("ytd-masthead")[0].locator == "document/ytd-masthead[1]"
    for name in ("x" * (MAX_TAG_NAME_LENGTH + 1), "x" * 2_000_000):
        started = time.monotonic()
        with pytest.raises(HtmlLimitExceeded) as raised:
            Page(f"<div><{name}><p>a</p></div>")
        assert raised.value.limit == "tag_name"
        assert str(raised.value) == "HTML structure limit exceeded: tag_name"
        assert time.monotonic() - started < 2.0
    # End tags are never stored: a long stray end tag is still just ignored.
    assert Page("<p>a</" + "x" * 10_000 + "></p>").root.text == "a"


def test_locator_up_to_the_bound_is_kept_and_longer_ones_rejected():
    fill = MAX_LOCATOR_LENGTH - len("#") - len("/p[1]")
    root = Page(f"<div id='{'i' * fill}'><p>a</p></div>").root
    assert len(root.all("p")[0].locator) == MAX_LOCATOR_LENGTH
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(f"<div id='{'i' * (fill + 1)}'><p>a</p></div>")
    assert raised.value.limit == "locator"


def test_huge_id_is_rejected_but_huge_class_or_other_attributes_are_not():
    huge = "i" * 400_000
    started = time.monotonic()
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(f"<section id='{huge}'><table><tr><td>1</td></tr></table></section>")
    assert raised.value.limit == "locator" and "iii" not in str(raised.value)
    assert time.monotonic() - started < 2.0
    # Only the id enters locators; other values are kept once and never repeated.
    root = Page(f"<div class='data-table {huge}' data-x='{huge}'><p>a</p></div>").root
    assert root.all("p")[0].locator == "document/div[1]/p[1]"
    assert root.all("div")[0].attrs["data-x"] == huge


def test_nesting_long_legal_tag_names_cannot_exceed_the_locator_bound():
    tag = "x" * MAX_TAG_NAME_LENGTH
    with pytest.raises(HtmlLimitExceeded) as raised:
        Page(f"<{tag}>" * 20 + "<p>a</p>")  # 20 levels, far below MAX_DEPTH
    assert raised.value.limit == "locator"


def test_many_descendants_under_a_legal_long_tag_stay_bounded():
    tag = "x" * MAX_TAG_NAME_LENGTH
    count = 20_000
    started = time.monotonic()
    root = Page(f"<{tag}><table><tr>" + "<td>1</td>" * count + "</tr></table></" + tag + ">").root
    cells = root.all("td")
    assert len(cells) == count
    assert max(len(c.locator) for c in cells) <= MAX_LOCATOR_LENGTH
    assert time.monotonic() - started < 5.0


def test_node_locator_fails_closed_for_nodes_built_outside_the_parser():
    node = Node("document")
    for _ in range(MAX_LOCATOR_LENGTH):
        node = Node("b", _parent=node, _step="/b[1]")
    with pytest.raises(HtmlLimitExceeded) as raised:
        _ = node.locator
    assert raised.value.limit == "locator"
    with pytest.raises(HtmlLimitExceeded):
        _ = Node("div", {"id": "i" * MAX_LOCATOR_LENGTH}).locator
    assert Node("div", {"id": "i" * (MAX_LOCATOR_LENGTH - 1)}).locator.startswith("#i")


def _hostile_sections(html: str, wrapper: str) -> str:
    """Wrap every financial table in `wrapper`, below the section id that resets the base."""
    for section in ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"):
        marker = f'<section id="{section}"'
        start = html.index(marker)
        end = html.index(">", start) + 1
        html = html[:end] + wrapper + html[end:]
    return html


@pytest.mark.parametrize(
    ("wrapper", "limit"),
    [("<x" + "a" * 40_000 + ">", "tag_name"), ('<div id="' + "i" * 40_000 + '">', "locator")],
    ids=["huge_tag", "huge_id"],
)
async def test_hostile_wrapper_fails_before_observations_and_saves_nothing(
    fixture_dir, tmp_path, wrapper, limit
):
    html = _hostile_sections((fixture_dir / "yesbank_consolidated.html").read_text(), wrapper)
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    try:
        result = await _extract(tmp_path, html)
    finally:
        beat.cancel()
    assert time.monotonic() - started < 5.0
    assert max(b - a for a, b in zip(ticks, ticks[1:], strict=False)) < 0.25
    assert result.company is None and result.coverage.overall == "FAILED"
    assert result.quarterly == result.profit_loss == result.ratios == result.top_metrics == ()
    assert result.documents == () and result.peers.entries == ()
    messages = {i.message for s in result.coverage.sections for i in s.issues}
    assert messages == {f"HTML structure limit exceeded: {limit}"}
    with sqlite3.connect(tmp_path / "db.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM companies").fetchone() == (0,)
        assert db.execute("SELECT company_id FROM extractions").fetchall() == [(None,)]


async def test_extraction_payload_is_serialized_off_the_event_loop(
    fixture_dir, tmp_path, monkeypatch
):
    result = await _extract(tmp_path, (fixture_dir / "yesbank_consolidated.html").read_text())
    threads: list[threading.Thread] = []
    dump = CompanyExtractionResult.model_dump

    def recording_dump(self, *args, **kwargs):
        threads.append(threading.current_thread())
        return dump(self, *args, **kwargs)

    monkeypatch.setattr(CompanyExtractionResult, "model_dump", recording_dump)
    database = SQLiteDatabase(tmp_path / "db.sqlite3")
    try:
        await database.extractions.save_extraction(
            result.model_copy(
                update={"metadata": result.metadata.model_copy(update={"id": uuid4()})}
            )
        )
        assert await database.extractions.get_extraction(result.metadata.id) == result
    finally:
        await database.close()
    assert threads and threading.main_thread() not in threads
