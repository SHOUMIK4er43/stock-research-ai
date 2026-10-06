"""Parse an actually supplied peers table; never guess a lazy-loading endpoint."""

from decimal import Decimal

from stock_research_ai.adapters.screener.html import Node, data_tables, rows
from stock_research_ai.adapters.screener.metrics import ParseContext
from stock_research_ai.adapters.screener.values import parse_value
from stock_research_ai.domain.extraction import Section
from stock_research_ai.domain.financial import FinancialUnit, UnitKind, ValueStatus
from stock_research_ai.domain.peers import PeerEntry, PeerMetric, PeerSnapshot
from stock_research_ai.domain.provenance import ObservationProvenance


def parse_peers(node: Node, context: ParseContext, final_url: str) -> PeerSnapshot:
    entries: list[PeerEntry] = []
    for table in data_tables(node):
        contents = rows(table)
        if not contents:
            continue
        for cells in contents[1:]:
            links = [a for c in cells for a in c.all("a") if "href" in a.attrs]
            if not links or len(cells) != len(contents[0]):
                context.issue(
                    Section.PEERS,
                    "MALFORMED_PEER",
                    "Peer row has no link or wrong width",
                    table,
                    error=True,
                )
                continue
            metrics: list[PeerMetric] = []
            for header, cell in zip(contents[0], cells, strict=True):
                if cell.all("a") or header.text in {"S.No.", "Name"}:
                    continue
                unit = FinancialUnit(kind=UnitKind.UNKNOWN)
                if header.text == "P/E":
                    unit = FinancialUnit(kind=UnitKind.RATIO)
                elif "%" in header.text:
                    unit = FinancialUnit(kind=UnitKind.PERCENT)
                elif "Rs.Cr." in header.text:
                    unit = FinancialUnit(
                        kind=UnitKind.MONEY, currency="INR", scale=Decimal("10000000")
                    )
                parsed = parse_value(cell.text, unit)
                if parsed.status == ValueStatus.PARSE_ERROR:
                    context.issue(
                        Section.PEERS,
                        "PARSE_ERROR",
                        "Malformed peer value retained",
                        cell,
                        header.text,
                        cell.text,
                        error=True,
                    )
                metrics.append(
                    PeerMetric(
                        label=header.text,
                        parsed=parsed,
                        provenance=ObservationProvenance(
                            raw_artifact_id=context.artifact.id,
                            source_location=cell.location("PEERS"),
                            normalizer_version="1",
                        ),
                    )
                )
            entries.append(
                PeerEntry(
                    name=links[0].text,
                    link=links[0].link(final_url, "PEERS"),
                    metrics=tuple(metrics),
                )
            )
    return PeerSnapshot(entries=tuple(entries))
