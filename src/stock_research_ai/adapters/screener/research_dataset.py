"""DETAILED_RESEARCH_V1 Box 1B: parse the complete structured Screener dataset.

Re-reads the SAME hash-verified page artifact as the frozen extraction (no second page
fetch) and keeps everything structured the page shows: all snapshot metrics, every row
and period of every statement table, both shareholding tables, the reported growth
tables, company profile/classification/index membership, and Screener's own pros/cons.
Peers come from the separately fetched fragment (peers_api) when supplied.

Units are stated only when the page states them: a cell's own marker (%, Rs, Cr.) first,
then the row label, then the section caption - and the ratios table never inherits the
'Figures in Rs. Crores' caption, so no unit is invented for e.g. 'Cash Conversion Cycle'.
"""

import hashlib
from datetime import date
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

from stock_research_ai.adapters.screener.html import (
    MAX_SCREENER_NODES,
    Node,
    Page,
    data_tables,
    owned,
    rows,
)
from stock_research_ai.adapters.screener.metrics import OutputBudget, clean_label
from stock_research_ai.adapters.screener.peers_api import PeerFetch, ScreenerPeerClient
from stock_research_ai.adapters.screener.values import parse_value
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.financial import FinancialUnit, ReportingScope, UnitKind, ValueStatus
from stock_research_ai.domain.screener_dataset import (
    ClassificationLevel,
    DatasetIssue,
    DetailedScreenerDataset,
    DisplayedValue,
    GrowthEntry,
    GrowthTable,
    MachineGeneratedNotes,
    NamedLink,
    PeerCell,
    PeerComparison,
    PeerRow,
    PeerStatus,
    PriceQuote,
    ScreenerCompanyProfile,
    ScreenerSourceRef,
    ScreenerTable,
    ScreenerTableKind,
    SnapshotComponent,
    SnapshotMetric,
    TableCell,
    TablePeriod,
    TableRow,
)

K = ScreenerTableKind
CRORE = Decimal("10000000")
_STATEMENT_SECTIONS = {
    K.QUARTERLY_RESULTS: "quarters",
    K.PROFIT_LOSS: "profit-loss",
    K.BALANCE_SHEET: "balance-sheet",
    K.CASH_FLOW: "cash-flow",
    K.RATIOS: "ratios",
}
_SHAREHOLDING_TABLES = {
    K.SHAREHOLDING_QUARTERLY: "quarterly-shp",
    K.SHAREHOLDING_YEARLY: "yearly-shp",
}
_MONEY_TABLES = {K.QUARTERLY_RESULTS, K.PROFIT_LOSS, K.BALANCE_SHEET, K.CASH_FLOW}
_CLASSIFICATION_LEVELS = ("Broad Sector", "Sector", "Broad Industry", "Industry")


def _classes(node: Node) -> list[str]:
    return node.attrs.get("class", "").split()


def _displayed(text: str, unit: FinancialUnit | None = None) -> DisplayedValue:
    parsed = parse_value(text, unit or FinancialUnit(kind=UnitKind.UNKNOWN))
    return DisplayedValue(
        displayed=" ".join(text.split())[:5000],
        value=parsed.value,
        status=parsed.status,
        unit=None if parsed.unit.kind == UnitKind.UNKNOWN else parsed.unit,
    )


def _row_unit(kind: ScreenerTableKind, label: str, caption: str) -> FinancialUnit | None:
    words = label.lower().split()
    if "%" in label:
        return FinancialUnit(kind=UnitKind.PERCENT)
    if "days" in words:
        return FinancialUnit(kind=UnitKind.DAYS)
    if label.lower().startswith("no. of"):
        return FinancialUnit(kind=UnitKind.NUMBER)
    if label.endswith(" in Rs"):
        return FinancialUnit(kind=UnitKind.MONEY, currency="INR")
    if label == "Raw PDF":
        return None
    if kind in _MONEY_TABLES and "Figures in Rs. Crores" in caption:
        return FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=CRORE)
    if kind in _SHAREHOLDING_TABLES and "Numbers in percentages" in caption:
        return FinancialUnit(kind=UnitKind.PERCENT)
    return None  # e.g. ratios: the crores caption is not a ratio unit


def _period_end(header: Node) -> date | None:
    try:
        return date.fromisoformat(header.attrs.get("data-date-key", ""))
    except ValueError:
        return None


class _Builder:
    def __init__(self, extraction: CompanyExtractionResult, content: bytes) -> None:
        identity = extraction.company
        if identity is None or len(extraction.raw_artifacts) != 1:
            raise ValueError("A detailed dataset requires one verified Screener extraction")
        artifact = extraction.raw_artifacts[0]
        if hashlib.sha256(content).hexdigest() != artifact.content_sha256:
            raise ValueError("Content does not match the extraction's preserved artifact")
        retrieval = next(r for r in extraction.retrievals if r.id == artifact.retrieval_id)
        self.identity = identity
        self.page_url = retrieval.final_url
        self.source = ScreenerSourceRef(
            page_url=retrieval.final_url,
            retrieved_at=retrieval.retrieved_at,
            retrieval_id=retrieval.id,
            raw_artifact_id=artifact.id,
            content_sha256=artifact.content_sha256,
            size_bytes=artifact.size_bytes,
        )
        self.root = Page(
            content.decode(artifact.encoding, errors="strict"), MAX_SCREENER_NODES
        ).root
        self.issues: list[DatasetIssue] = []
        # The extraction's output budget, charged per kept record (with the source text it
        # copies) before it is built: past it, the whole dataset fails closed.
        self.budget = OutputBudget()

    def charged(self, *texts: str | None) -> bool:
        self.budget.charge(*texts)
        return True

    def issue(self, code: str, section: str, node: Node | None = None, text: str = "") -> None:
        displayed = " ".join(text.split())[:300]
        self.budget.charge(displayed)
        self.issues.append(
            DatasetIssue(
                code=code,
                section=section,
                locator=node.locator if node is not None else None,
                displayed=displayed or None,
            )
        )

    def url(self, node: Node) -> str:
        return urljoin(self.page_url, node.attrs["href"])

    # --- profile ---------------------------------------------------------------------

    def warehouse_id(self) -> str | None:
        info = self.root.by_id("company-info")
        value = info.attrs.get("data-warehouse-id", "") if info else ""
        return value if value.isdigit() and len(value) <= 12 else None

    def profile(self) -> ScreenerCompanyProfile:
        top = self.root.by_id("top")
        listings = {listing.exchange: listing for listing in self.identity.listings}
        website = next(
            (self.url(a) for a in self.root.all("a") if a.text == "Website" and "href" in a.attrs),
            None,
        )
        about = top.by_class("about") if top else None
        key_points = top.by_class("commentary") if top else None
        peers = self.root.by_id("peers")
        classification = tuple(
            ClassificationLevel(level=a.attrs["title"], name=a.text, url=self.url(a))
            for a in (peers.all("a") if peers else [])
            if a.attrs.get("title") in _CLASSIFICATION_LEVELS
            and "href" in a.attrs
            and self.charged(a.text, a.attrs["href"])
        )
        benchmarks = self.root.by_id("benchmarks")
        memberships = tuple(
            NamedLink(name=a.text, url=self.url(a))
            for a in (benchmarks.all("a") if benchmarks else [])
            if a.text and "href" in a.attrs and self.charged(a.text, a.attrs["href"])
        )
        return ScreenerCompanyProfile(
            name=self.identity.name,
            page_url=self.page_url,
            source_company_key=self.identity.source_company_key,
            warehouse_id=self.warehouse_id(),
            reporting_basis=self.identity.reporting_scope,
            bse_code=listings["BSE"].exchange_identifier if "BSE" in listings else None,
            nse_symbol=listings["NSE"].symbol if "NSE" in listings else None,
            website_url=website,
            about=about.text or None if about else None,
            key_points=key_points.text or None if key_points else None,
            price=self.price(top),
            classification=classification,
            index_memberships=memberships,
        )

    def price(self, top: Node | None) -> PriceQuote | None:
        block = next(
            (n for n in (top.all("div") if top else []) if "font-size-18" in _classes(n)), None
        )
        spans = block.all("span") if block else []
        if not block or not spans:
            self.issue("PRICE_BLOCK_MISSING", "top", top)
            return None
        change = next(
            (s for s in spans if {"up", "down"} & set(_classes(s)) or "%" in s.text), None
        )
        as_of = next((d for d in block.all("div") if "font-size-11" in _classes(d)), None)
        return PriceQuote(
            price=_displayed(spans[0].text),
            change=_displayed(change.text) if change is not None else None,
            as_of_text=as_of.text or None if as_of else None,
        )

    # --- snapshot ----------------------------------------------------------------------

    def snapshot(self) -> tuple[SnapshotMetric, ...]:
        ratios = self.root.by_id("top-ratios")
        if ratios is None:
            self.issue("SECTION_MISSING", "top-ratios")
            return ()
        metrics: list[SnapshotMetric] = []
        for item in owned(ratios, "li"):
            name, value = item.by_class("name"), item.by_class("value")
            if name is None or value is None:
                self.issue("MALFORMED_METRIC", "top-ratios", item, item.text)
                continue
            numbers = [n for n in value.all("span") if "number" in _classes(n)]
            self.budget.charge(name.text, value.raw_text)
            for number in numbers if len(numbers) > 1 else ():
                self.budget.charge(number.text)
            components: tuple[SnapshotComponent, ...] = ()
            main = _displayed(value.raw_text)
            if len(numbers) > 1:
                parts = [p.strip() for p in name.text.split("/")]
                currency = "₹ " if "₹" in value.text else ""
                components = tuple(
                    SnapshotComponent(
                        label=parts[i] if len(parts) == len(numbers) else None,
                        value=_displayed(currency + n.text),
                    )
                    for i, n in enumerate(numbers)
                )
                main = DisplayedValue(
                    displayed=value.text, value=None, status=ValueStatus.NOT_APPLICABLE, unit=None
                )
            elif main.status == ValueStatus.PARSE_ERROR:
                self.issue("PARSE_ERROR", "top-ratios", value, value.text)
            metrics.append(
                SnapshotMetric(
                    label=name.text, value=main, components=components, locator=item.locator
                )
            )
        return tuple(metrics)

    # --- tables ------------------------------------------------------------------------

    def tables(self) -> tuple[ScreenerTable, ...]:
        result: list[ScreenerTable] = []
        for kind, section_id in _STATEMENT_SECTIONS.items():
            section = self.root.by_id(section_id)
            if section is None:
                self.issue("SECTION_MISSING", section_id)
                continue
            tables = [t for t in data_tables(section) if "blur" not in _classes(t)]
            if not tables:
                self.issue("SECTION_UNAVAILABLE", section_id, section, section.text[:120])
                continue
            if len(tables) > 1:
                self.issue("EXTRA_TABLE_IGNORED", section_id, tables[1])
            table = self.table(kind, section, tables[0], section_id)
            if table is not None:
                result.append(table)
        holding = self.root.by_id("shareholding")
        for kind, holder_id in _SHAREHOLDING_TABLES.items():
            container = next(
                (n for n in (owned(holding) if holding else []) if n.attrs.get("id") == holder_id),
                None,
            )
            found = data_tables(container) if container else []
            if not found or holding is None:
                self.issue("SECTION_MISSING", holder_id)
                continue
            table = self.table(kind, holding, found[0], holder_id)
            if table is not None:
                result.append(table)
        return tuple(result)

    def table(
        self, kind: ScreenerTableKind, section: Node, table: Node, section_id: str
    ) -> ScreenerTable | None:
        caption_node = next(
            (p for p in section.all("p") if "Figures" in p.text or "percentages" in p.text), None
        )
        caption = caption_node.text if caption_node else ""
        if kind in _SHAREHOLDING_TABLES:
            basis = ReportingScope.UNKNOWN  # company-level, not a standalone/consolidated figure
        elif "Consolidated Figures" in caption:
            basis = ReportingScope.CONSOLIDATED
        elif "Standalone Figures" in caption:
            basis = ReportingScope.STANDALONE
        else:
            basis = ReportingScope.UNKNOWN
        contents = rows(table)
        if not contents or len(contents[0]) < 2:
            self.issue("MALFORMED_TABLE", section_id, table)
            return None
        headers = contents[0][1:]
        periods = tuple(TablePeriod(label=h.text, end_date=_period_end(h)) for h in headers)
        table_rows: list[TableRow] = []
        for row in contents[1:]:
            if not row:
                continue
            if len(row) != len(headers) + 1 or not row[0].text:
                self.issue("MALFORMED_ROW", section_id, row[0], row[0].text)
                continue
            displayed_label = row[0].text
            self.budget.charge(displayed_label)
            label = clean_label(displayed_label)
            unit = _row_unit(kind, label, caption)
            cells: list[TableCell] = []
            for period, cell in zip(periods, row[1:], strict=True):
                links = [a for a in cell.all("a") if "href" in a.attrs]
                self.budget.charge(
                    period.label, cell.raw_text, links[0].attrs["href"] if links else None
                )
                value = _displayed(cell.raw_text, unit)
                if value.status == ValueStatus.PARSE_ERROR:
                    self.issue("PARSE_ERROR", section_id, cell, cell.text)
                cells.append(
                    TableCell(
                        period=period.label,
                        value=value,
                        link=self.url(links[0]) if links else None,
                        locator=cell.locator,
                    )
                )
            shown = [c.value.unit for c in cells if c.value.status == ValueStatus.AVAILABLE]
            if shown and all(u is not None and u.kind == UnitKind.PERCENT for u in shown):
                unit = FinancialUnit(kind=UnitKind.PERCENT)  # every cell displays '%'
            table_rows.append(
                TableRow(
                    label=label,
                    displayed_label=displayed_label,
                    expandable=displayed_label.rstrip().endswith("+"),
                    unit=unit,
                    cells=tuple(cells),
                )
            )
        return ScreenerTable(
            kind=kind,
            reporting_basis=basis,
            caption=caption or None,
            periods=periods,
            rows=tuple(table_rows),
            locator=table.locator,
        )

    def growth(self) -> tuple[GrowthTable, ...]:
        section = self.root.by_id("profit-loss")
        found = [
            t for t in (owned(section, "table") if section else []) if "ranges-table" in _classes(t)
        ]
        result: list[GrowthTable] = []
        for table in found:
            contents = rows(table)
            if not contents or len(contents[0]) != 1 or not contents[0][0].text:
                self.issue("MALFORMED_GROWTH_TABLE", "profit-loss", table)
                continue
            self.budget.charge(contents[0][0].text)
            entries: list[GrowthEntry] = []
            for row in contents[1:]:
                if len(row) != 2 or not row[0].text:
                    self.issue(
                        "MALFORMED_GROWTH_ROW", "profit-loss", table, row[0].text if row else ""
                    )
                    continue
                self.budget.charge(row[0].text, row[1].raw_text)
                value = _displayed(row[1].raw_text)
                if value.status == ValueStatus.PARSE_ERROR:
                    self.issue("PARSE_ERROR", "profit-loss", row[1], row[1].text)
                entries.append(
                    GrowthEntry(
                        horizon=row[0].text.rstrip(":").strip(),
                        displayed_horizon=row[0].text,
                        value=value,
                    )
                )
            result.append(
                GrowthTable(
                    title=contents[0][0].text, entries=tuple(entries), locator=table.locator
                )
            )
        if section is not None and not found:
            self.issue("GROWTH_TABLES_MISSING", "profit-loss", section)
        return tuple(result)

    def notes(self) -> MachineGeneratedNotes | None:
        analysis = self.root.by_id("analysis")
        if analysis is None:
            return None
        pros, cons = analysis.by_class("pros"), analysis.by_class("cons")
        return MachineGeneratedNotes(
            pros=tuple(li.text for li in pros.all("li") if li.text and self.charged(li.text))
            if pros
            else (),
            cons=tuple(li.text for li in cons.all("li") if li.text and self.charged(li.text))
            if cons
            else (),
        )

    def gated_sections(self) -> None:
        insights = self.root.by_id("insights")
        if insights is not None:
            # Premium-gated: values are masked ('xxx') on the public page.
            self.issue("INSIGHTS_NOT_CAPTURED", "insights", insights)


def parse_peer_fragment(fetch: PeerFetch, subject_company_key: str) -> PeerComparison | None:
    """The peer table from a retrieved fragment; None when no table is present."""
    if fetch.content is None:
        return None
    root = Page(fetch.content.decode("utf-8", errors="strict"), MAX_SCREENER_NODES).root
    tables = data_tables(root)
    if not tables:
        return None
    contents = rows(tables[0])
    if not contents or len(contents[0]) < 3:
        return None
    budget = OutputBudget()  # a separate fetch: its own budget, as large as a page's
    columns = tuple(h.text for h in contents[0])
    peers: list[PeerRow] = []
    summaries: list[PeerRow] = []
    trs = [tr for tr in tables[0].all("tr") if tr.all("td")]
    for tr, row in zip(trs, contents[1:], strict=False):
        if len(row) != len(columns):
            continue
        links = [a for a in row[1].all("a") if "href" in a.attrs]
        company_id = tr.attrs.get("data-row-company-id")
        budget.charge(row[0].text, row[1].text, links[0].attrs["href"] if links else None)
        cells: list[PeerCell] = []
        for column, cell in zip(columns[2:], row[2:], strict=True):
            budget.charge(column, cell.raw_text)
            cells.append(
                PeerCell(column=column, value=_displayed(cell.raw_text, _peer_unit(column)))
            )
        entry = PeerRow(
            rank=row[0].text,
            name=row[1].text,
            company_url=urljoin(fetch.url, links[0].attrs["href"]) if links else None,
            screener_company_id=company_id if company_id and company_id.isdigit() else None,
            is_subject_company=company_id == subject_company_key,
            cells=tuple(cells),
        )
        (peers if company_id else summaries).append(entry)
    content = fetch.content
    return PeerComparison(
        source_url=fetch.url,
        retrieved_at=fetch.retrieved_at,
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        columns=columns,
        peers=tuple(peers),
        summary_rows=tuple(summaries),
    )


def _peer_unit(column: str) -> FinancialUnit | None:
    """Units stated in the peer column headers only."""
    if "%" in column:
        return FinancialUnit(kind=UnitKind.PERCENT)
    if "Rs.Cr." in column:
        return FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=CRORE)
    if column.endswith("Rs."):
        return FinancialUnit(kind=UnitKind.MONEY, currency="INR")
    return None


def build_screener_dataset(
    extraction: CompanyExtractionResult,
    content: bytes,
    peer_fetch: PeerFetch | None = None,
) -> DetailedScreenerDataset:
    """Pure: the complete dataset from the preserved page (+ an optional peer fetch)."""
    builder = _Builder(extraction, content)
    if peer_fetch is not None and urlsplit(peer_fetch.url).hostname != "www.screener.in":
        raise ValueError("Peer data must come from www.screener.in")
    peers = None
    if peer_fetch is None:
        status = PeerStatus(status="NOT_REQUESTED")
    elif peer_fetch.status != "RETRIEVED":
        status = PeerStatus(status=peer_fetch.status, code=peer_fetch.code)
    else:
        peers = parse_peer_fragment(peer_fetch, builder.identity.source_company_key)
        status = (
            PeerStatus(status="RETRIEVED")
            if peers is not None
            else PeerStatus(status="FAILED", code="PEER_TABLE_MISSING")
        )
    profile = builder.profile()
    snapshot = builder.snapshot()
    tables = builder.tables()
    growth = builder.growth()
    notes = builder.notes()
    builder.gated_sections()
    return DetailedScreenerDataset(
        company_key=f"{builder.identity.provider}:{builder.identity.source_company_key}",
        source=builder.source,
        profile=profile,
        snapshot=snapshot,
        tables=tables,
        growth=growth,
        notes=notes,
        peer_status=status,
        peers=peers,
        issues=tuple(builder.issues),
    )


async def acquire_screener_dataset(
    extraction: CompanyExtractionResult,
    content: bytes,
    peer_client: ScreenerPeerClient | None = None,
) -> DetailedScreenerDataset:
    """Optionally fetch peers once (the page's own warehouse ID), then build."""
    warehouse_id = _Builder(extraction, content).warehouse_id()
    fetch = None
    if peer_client is not None and warehouse_id is not None:
        fetch = await peer_client.fetch(warehouse_id)
    dataset = build_screener_dataset(extraction, content, fetch)
    if peer_client is not None and warehouse_id is None:
        return dataset.model_copy(
            update={"peer_status": PeerStatus(status="UNAVAILABLE", code="NO_WAREHOUSE_ID")}
        )
    return dataset
