"""Small explicit source-label mapping; unrecognized rows remain source observations."""

from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, UUID, uuid5

from stock_research_ai.adapters.screener.html import Node, owned
from stock_research_ai.adapters.screener.values import parse_period, parse_value
from stock_research_ai.domain.extraction import CoverageIssue, Section
from stock_research_ai.domain.financial import (
    FinancialObservation,
    FinancialPeriod,
    FinancialUnit,
    PeriodFrequency,
    ReportingScope,
    UnitKind,
    ValueStatus,
)
from stock_research_ai.domain.metrics import MetricDefinition, MetricMappingStatus
from stock_research_ai.domain.provenance import ObservationProvenance, RawArtifact

# Only source-displayed concepts, not calculated or cross-provider equivalences.
LABELS = {
    "Market Cap": "market_cap",
    "Current Price": "current_price",
    "High": "displayed_high",
    "Low": "displayed_low",
    "Stock P/E": "price_earnings",
    "Book Value": "book_value",
    "Dividend Yield": "dividend_yield",
    "ROCE": "roce",
    "ROE": "roe",
    "Face Value": "face_value",
    "Revenue": "revenue",
    "Sales": "sales",
    "Interest": "interest",
    "Expenses": "expenses",
    "Financing Profit": "financing_profit",
    "Financing Margin %": "financing_margin",
    "Operating Profit": "operating_profit",
    "OPM %": "operating_margin",
    "Other Income": "other_income",
    "Depreciation": "depreciation",
    "Profit before tax": "profit_before_tax",
    "Tax %": "tax_percent",
    "Net Profit": "net_profit",
    "EPS in Rs": "eps",
    "Book Value Per Share in Rs": "bvps",
    "Gross NPA %": "gross_npa_percent",
    "Net NPA %": "net_npa_percent",
    "Dividend Payout %": "dividend_payout_percent",
    "Equity Capital": "equity_capital",
    "Reserves": "reserves",
    "Deposits": "deposits",
    "Borrowings": "borrowings",
    "Borrowing": "borrowings",
    "Other Liabilities": "other_liabilities",
    "Total Liabilities": "total_liabilities",
    "Fixed Assets": "fixed_assets",
    "CWIP": "cwip",
    "Investments": "investments",
    "Other Assets": "other_assets",
    "Total Assets": "total_assets",
    "Cash from Operating Activity": "cash_operating",
    "Cash from Investing Activity": "cash_investing",
    "Cash from Financing Activity": "cash_financing",
    "Net Cash Flow": "net_cashflow",
    "Free Cash Flow": "source_free_cashflow",
    "CFO/OP %": "source_cfo_op_percent",
    "CFO/OP": "source_cfo_op_percent",
    "ROE %": "roe",
    "ROCE %": "roce",
    "Debtor Days": "debtor_days",
    "Inventory Days": "inventory_days",
    "Days Payable": "days_payable",
    "Cash Conversion Cycle": "cash_conversion_cycle",
    "Working Capital Days": "working_capital_days",
    "Promoters": "holding_promoters",
    "FIIs": "holding_fii",
    "DIIs": "holding_dii",
    "Government": "holding_government",
    "Public": "holding_public",
    "Others": "holding_others",
    "No. of Shareholders": "shareholder_count",
}


#: The node cap does not bound output: one cell is one observation, one row label or
#: column header is copied into every observation of its row or column, and nested
#: elements repeat their inner text. So every record kept from a page - observation,
#: coverage issue, document or source link examined, peer row and peer value - is charged
#: to one budget, with the source text it copies, before it is built. The largest of the
#: 38 distinct real pages (fixtures and every stored artifact) charges 864 records (563
#: statement observations); a full peers table adds ~110 and a layout change leaving every
#: label unmapped would add an issue per observation. 4,000 is ~4.6x the largest page.
MAX_EXTRACTION_RECORDS = 4_000
#: Characters of source text (labels, headers, values, link text and targets) all records
#: of one page may copy. The largest real page charges 77,160 (most of it the whitespace
#: around cell values); 400,000 is ~5.2x that and keeps the stored extraction to MBs.
MAX_EXTRACTION_TEXT = 400_000


class ExtractionLimitExceeded(ValueError):
    """The page yields more output than any real page could; nothing is kept from it."""

    def __init__(self) -> None:
        super().__init__(
            f"Extraction output limit exceeded: over {MAX_EXTRACTION_RECORDS} records or "
            f"{MAX_EXTRACTION_TEXT} characters of source text"
        )


@dataclass
class OutputBudget:
    """One page's record and copied-text budget, charged before each record is built."""

    records: int = 0
    text: int = 0

    def charge(self, *texts: str | None) -> None:
        self.records += 1
        self.text += sum(len(t) for t in texts if t)
        if self.records > MAX_EXTRACTION_RECORDS or self.text > MAX_EXTRACTION_TEXT:
            raise ExtractionLimitExceeded()


def clean_label(label: str) -> str:
    return label.rstrip(" +\xa0").strip()


@dataclass
class ParseContext:
    artifact: RawArtifact
    company_id: UUID
    scope: ReportingScope
    issues: list[CoverageIssue] = field(default_factory=list)
    definitions: dict[str, MetricDefinition] = field(default_factory=dict)
    budget: OutputBudget = field(default_factory=OutputBudget)

    def charge(self, *texts: str | None) -> None:
        """Count one more record, and the source text it copies, before it is built;
        past the budget, raise instead."""
        self.budget.charge(*texts)

    def issue(
        self,
        section: Section,
        code: str,
        message: str,
        node: Node,
        label: str | None = None,
        value: str | None = None,
        error: bool = False,
    ) -> None:
        self.charge(label, value)
        self.issues.append(
            CoverageIssue(
                code=code,
                severity="error" if error else "warning",
                section=section,
                message=message,
                original_label=label,
                original_value=value,
                raw_artifact_id=self.artifact.id,
                retrieval_id=self.artifact.retrieval_id,
                source_location=node.location(section.value),
            )
        )

    def observation(
        self,
        section: Section,
        label: str,
        cell: Node,
        period: FinancialPeriod,
        unit: FinancialUnit,
        scope: ReportingScope | None = None,
        text: str | None = None,
        component: str = "",
    ) -> FinancialObservation:
        source = cell.raw_text if text is None else text
        self.charge(label, source, period.fiscal_label, period.original_date_key)
        cleaned = clean_label(label)
        metric = LABELS.get(cleaned)
        parsed = parse_value(source, unit)
        locator = cell.location(section.value)
        if component:
            locator = locator.model_copy(update={"locator": locator.locator + "/" + component})
        if metric is None:
            self.issue(section, "UNMAPPED_METRIC", "Unmapped source label retained", cell, label)
        else:
            self.definitions[metric] = MetricDefinition(
                id=metric,
                label=cleaned,
                definition=f"Source-displayed {cleaned}; no calculation performed.",
            )
        if parsed.status == ValueStatus.PARSE_ERROR:
            self.issue(
                section,
                "PARSE_ERROR",
                parsed.reason or "Invalid number",
                cell,
                label,
                parsed.original,
                error=True,
            )
        series = (
            f"{self.company_id}:{section}:{scope or self.scope}:{cleaned}:{period.fiscal_label}"
        )
        return FinancialObservation(
            id=uuid5(
                NAMESPACE_URL,
                f"{self.artifact.id}:{self.artifact.content_sha256}:1:{series}:{locator.locator}",
            ),
            company_id=self.company_id,
            metric=f"source:{cleaned}",
            metric_id=metric,
            mapping_status=MetricMappingStatus.MAPPED if metric else MetricMappingStatus.UNMAPPED,
            original_label=label,
            series_key=series,
            value=parsed.value,
            value_status=parsed.status,
            unit=parsed.unit,
            original_unit=parsed.unit,
            original_value=parsed.original,
            period=period,
            reporting_scope=scope or self.scope,
            provenance=ObservationProvenance(
                raw_artifact_id=self.artifact.id, source_location=locator, normalizer_version="1"
            ),
        )


def parse_top(node: Node, context: ParseContext) -> tuple[FinancialObservation, ...]:
    result: list[FinancialObservation] = []
    period = parse_period(
        "Current snapshot; observation date undisclosed",
        None,
        PeriodFrequency.POINT_IN_TIME,
        instant=True,
    )
    for item in owned(node, "li"):
        label, value = item.by_class("name"), item.by_class("value")
        if label is None or value is None:
            context.issue(
                Section.TOP_METRICS, "MALFORMED_METRIC", "Missing label or value", item, error=True
            )
            continue
        unit = FinancialUnit(kind=UnitKind.UNKNOWN)
        if label.text == "Stock P/E":
            unit = FinancialUnit(kind=UnitKind.RATIO)
        if label.text == "High / Low":
            numbers = [n for n in value.all("span") if "number" in n.attrs.get("class", "").split()]
            if len(numbers) != 2:
                context.issue(
                    Section.TOP_METRICS,
                    "MALFORMED_METRIC",
                    "High/Low needs two values",
                    value,
                    error=True,
                )
                continue
            for name, number in zip(("High", "Low"), numbers, strict=True):
                currency = "₹ " if "₹" in value.text else ""
                result.append(
                    context.observation(
                        Section.TOP_METRICS,
                        name,
                        number,
                        period,
                        unit,
                        ReportingScope.UNKNOWN,
                        currency + number.text,
                    )
                )
        else:
            result.append(
                context.observation(
                    Section.TOP_METRICS, label.text, value, period, unit, ReportingScope.UNKNOWN
                )
            )
    return tuple(result)
