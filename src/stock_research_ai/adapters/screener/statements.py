"""Read source-labelled statement tables without inferring missing period boundaries."""

import re
from decimal import Decimal

from stock_research_ai.adapters.screener.html import Node, data_tables, rows
from stock_research_ai.adapters.screener.metrics import ParseContext, clean_label
from stock_research_ai.adapters.screener.values import parse_period
from stock_research_ai.domain.extraction import Section
from stock_research_ai.domain.financial import (
    FinancialObservation,
    FinancialUnit,
    PeriodFrequency,
    ReportingScope,
    UnitKind,
)


def parse_statements(
    node: Node, section: Section, context: ParseContext
) -> tuple[FinancialObservation, ...]:
    result: list[FinancialObservation] = []
    frequency = (
        PeriodFrequency.QUARTERLY if section == Section.QUARTERLY else PeriodFrequency.ANNUAL
    )
    instant = section in {Section.BALANCE_SHEET, Section.SHAREHOLDING}
    if instant:
        frequency = PeriodFrequency.POINT_IN_TIME
    scope = context.scope
    modes = re.findall(r"(Standalone|Consolidated) Figures", node.text)
    if modes:
        scope = ReportingScope(modes[0].lower())
    default = FinancialUnit(kind=UnitKind.UNKNOWN)
    if "Figures in Rs. Crores" in node.text:
        default = FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=Decimal("10000000"))
    if section == Section.SHAREHOLDING or (section == Section.RATIOS and not modes):
        scope = ReportingScope.UNKNOWN
    for table in data_tables(node):
        table_rows = rows(table)
        if not table_rows or len(table_rows[0]) < 2:
            context.issue(section, "MALFORMED_TABLE", "Missing period headers", table, error=True)
            continue
        headers = table_rows[0][1:]
        if not all(h.text and h.tag == "th" for h in headers):
            context.issue(
                section, "MALFORMED_TABLE", "Invalid period header cells", table, error=True
            )
            continue
        for row in table_rows[1:]:
            if not row:
                continue
            label = row[0].text
            if clean_label(label) == "Raw PDF":
                # References, not numeric financial observations - but evidence discovery
                # reads each link (with its column header), so each is charged.
                for header, cell in zip(headers, row[1:], strict=False):
                    for anchor in (a for a in cell.all("a") if "href" in a.attrs):
                        context.charge(header.text, anchor.attrs["href"])
                continue
            if len(row) != len(headers) + 1 or not label:
                context.issue(
                    section,
                    "MALFORMED_TABLE",
                    "Row/header width mismatch or blank label",
                    row[0],
                    label,
                    error=True,
                )
                continue
            unit = default
            if "%" in label or section == Section.SHAREHOLDING:
                unit = FinancialUnit(kind=UnitKind.PERCENT)
            if clean_label(label).endswith(" Days"):
                unit = FinancialUnit(kind=UnitKind.DAYS)
            if clean_label(label) == "No. of Shareholders":
                unit = FinancialUnit(kind=UnitKind.NUMBER)
            if clean_label(label) in {"EPS in Rs", "Book Value Per Share in Rs"}:
                unit = FinancialUnit(kind=UnitKind.MONEY, currency="INR")
            for header, cell in zip(headers, row[1:], strict=True):
                period = parse_period(
                    header.text, header.attrs.get("data-date-key"), frequency, instant
                )
                if period.end_date is None and not re.fullmatch(
                    r"[A-Za-z]{3} \d{4}|TTM", header.text
                ):
                    context.issue(
                        section,
                        "UNRESOLVED_PERIOD",
                        "Unsupported period label retained",
                        header,
                        header.text,
                        error=True,
                    )
                if (
                    header.attrs.get("data-date-key") not in {None, "", "TTM"}
                    and period.end_date is None
                ):
                    context.issue(
                        section,
                        "INVALID_DATE_ATTRIBUTE",
                        "Invalid source date retained",
                        header,
                        header.text,
                        header.attrs["data-date-key"],
                        error=True,
                    )
                result.append(context.observation(section, label, cell, period, unit, scope))
    return tuple(result)
