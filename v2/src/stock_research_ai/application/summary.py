"""Pure SUMMARY_V1 projection: CompanyExtractionResult -> bounded SummaryInput.

Fixed metric allowlists and period caps only. Values, labels, units, periods and scopes are
copied from existing normalized observations; no growth, margin, debt or cash is derived.
No I/O: the caller already holds the extraction.
"""

from collections.abc import Sequence

from stock_research_ai.domain.extraction import CompanyExtractionResult, Section
from stock_research_ai.domain.financial import FinancialObservation, PeriodFrequency
from stock_research_ai.domain.summary import (
    SUMMARY_INPUT_MAX_BYTES,
    SummaryBlock,
    SummaryBlockName,
    SummaryCompany,
    SummaryInput,
    SummaryInputBudgetExceeded,
    SummaryInputUnavailable,
    SummaryLimitation,
    SummaryListing,
    SummaryMetric,
    SummaryRow,
    SummaryValue,
)

# (alternative source metric ids, required). Alternatives are distinct source rows that are
# each kept under their own label - e.g. a bank's Financing Profit is never Operating Profit.
MetricGroup = tuple[tuple[str, ...], bool]

SNAPSHOT: tuple[MetricGroup, ...] = tuple(
    ((metric,), True)
    for metric in (
        "market_cap",
        "current_price",
        "price_earnings",
        "book_value",
        "dividend_yield",
        "roce",
        "roe",
    )
)
_RESULTS: tuple[MetricGroup, ...] = (
    (("sales", "revenue"), True),
    (("operating_profit", "financing_profit"), True),
    (("net_profit",), True),
    (("eps",), True),
)
ANNUAL = _RESULTS
QUARTERLY = (*_RESULTS, (("gross_npa_percent",), False), (("net_npa_percent",), False))
BALANCE_SHEET: tuple[MetricGroup, ...] = (
    (("equity_capital",), True),
    (("reserves",), True),
    (("borrowings",), True),
    (("deposits",), False),
    (("total_assets",), True),
)
CASH_FLOW: tuple[MetricGroup, ...] = (
    (("cash_operating",), True),
    (("cash_investing",), True),
    (("cash_financing",), True),
    (("source_free_cashflow",), False),
)

# block -> (source section, allowlist, completed-period frequency, period cap)
BLOCKS: tuple[
    tuple[SummaryBlockName, Section, tuple[MetricGroup, ...], PeriodFrequency, int], ...
] = (
    ("annual", Section.PROFIT_LOSS, ANNUAL, PeriodFrequency.ANNUAL, 4),
    ("quarterly", Section.QUARTERLY, QUARTERLY, PeriodFrequency.QUARTERLY, 4),
    ("balance_sheet", Section.BALANCE_SHEET, BALANCE_SHEET, PeriodFrequency.POINT_IN_TIME, 2),
    ("cash_flow", Section.CASH_FLOW, CASH_FLOW, PeriodFrequency.ANNUAL, 2),
)
_OBSERVATIONS = {
    Section.PROFIT_LOSS: "profit_loss",
    Section.QUARTERLY: "quarterly",
    Section.BALANCE_SHEET: "balance_sheet",
    Section.CASH_FLOW: "cashflow",
}


def canonical_summary_json(summary: SummaryInput) -> bytes:
    """The single canonical encoding (Pydantic JSON, as in application.hashing)."""
    return summary.model_dump_json().encode("utf-8")


def ensure_within_budget(summary: SummaryInput, max_bytes: int = SUMMARY_INPUT_MAX_BYTES) -> bytes:
    encoded = canonical_summary_json(summary)
    if len(encoded) > max_bytes:
        raise SummaryInputBudgetExceeded(len(encoded), max_bytes)
    return encoded


def project_summary_input(extraction: CompanyExtractionResult) -> SummaryInput:
    identity = extraction.company
    if identity is None:
        raise SummaryInputUnavailable("Extraction has no verified company identity")
    limitations = [
        *_section_limitations(extraction, "company", Section.COMPANY_IDENTITY),
        *_section_limitations(extraction, "snapshot", Section.TOP_METRICS),
    ]
    snapshot: list[SummaryMetric] = []
    for alternatives, required in SNAPSHOT:
        found = [
            SummaryMetric(
                label=_label(o),
                metric_id=metric_id,
                unit=o.unit,
                reporting_scope=o.reporting_scope,
                observation=_value(o),
            )
            for metric_id in alternatives
            for o in extraction.top_metrics
            if o.metric_id == metric_id
        ]
        if not found and required:
            limitations.append(_not_present("snapshot", alternatives))
        snapshot.extend(found)
    blocks: dict[SummaryBlockName, SummaryBlock] = {}
    for name, section, allowlist, frequency, cap in BLOCKS:
        limitations.extend(_section_limitations(extraction, name, section))
        observations = getattr(extraction, _OBSERVATIONS[section])
        rows: list[SummaryRow] = []
        for alternatives, required in allowlist:
            group_rows: list[SummaryRow] = []
            for metric_id in alternatives:
                for series in _by_source_label(observations, metric_id):
                    row, codes = _row(series, metric_id, frequency, cap)
                    limitations.extend(
                        SummaryLimitation(block=name, code=code, metric_ids=(metric_id,))
                        for code in codes
                    )
                    if row is not None:
                        group_rows.append(row)
            if not group_rows and required:
                limitations.append(_not_present(name, alternatives))
            rows.extend(group_rows)
        blocks[name] = SummaryBlock(rows=tuple(rows))
    retrieved_at = (
        extraction.retrievals[0].retrieved_at if len(extraction.retrievals) == 1 else None
    )
    summary = SummaryInput(
        company=SummaryCompany(
            name=identity.name,
            source_company_key=identity.source_company_key,
            reporting_scope=identity.reporting_scope,
            listings=tuple(
                SummaryListing(
                    exchange=listing.exchange,
                    symbol=listing.symbol,
                    exchange_identifier=listing.exchange_identifier,
                )
                for listing in identity.listings
            ),
            source_url=identity.final_url,
            retrieved_at=retrieved_at,
            coverage_overall=extraction.coverage.overall,
        ),
        snapshot=tuple(snapshot),
        annual=blocks["annual"],
        quarterly=blocks["quarterly"],
        balance_sheet=blocks["balance_sheet"],
        cash_flow=blocks["cash_flow"],
        limitations=tuple(limitations),
    )
    ensure_within_budget(summary)
    return summary


def _label(observation: FinancialObservation) -> str:
    # The normalizer's own cleaned source label ("source:<label>"), not a rename.
    return observation.metric.removeprefix("source:")


def _value(observation: FinancialObservation) -> SummaryValue:
    return SummaryValue(
        period=observation.period.fiscal_label,
        value=observation.value,
        status=observation.value_status,
    )


def _not_present(block: SummaryBlockName, alternatives: tuple[str, ...]) -> SummaryLimitation:
    return SummaryLimitation(block=block, code="NOT_PRESENT", metric_ids=alternatives)


def _by_source_label(
    observations: Sequence[FinancialObservation], metric_id: str
) -> list[list[FinancialObservation]]:
    """Each distinct source row mapped to metric_id, in source order."""
    series: dict[str, list[FinancialObservation]] = {}
    for observation in observations:
        if observation.metric_id == metric_id:
            series.setdefault(observation.metric, []).append(observation)
    return list(series.values())


def _row(
    series: list[FinancialObservation], metric_id: str, frequency: PeriodFrequency, cap: int
) -> tuple[SummaryRow | None, list[str]]:
    """Latest `cap` dated completed periods oldest-first, plus TTM for annual rows.

    Anything excluded is reported as a code, never silently dropped.
    """
    codes: list[str] = []
    completed = [o for o in series if o.period.frequency == frequency]
    if any(o.period.end_date is None for o in completed):
        codes.append("UNRESOLVED_PERIOD")
    dated = sorted(
        ((o.period.end_date, o) for o in completed if o.period.end_date is not None),
        key=lambda pair: pair[0],
    )
    selected = [o for _, o in dated[-cap:]]
    ttm = [o for o in series if o.period.frequency == PeriodFrequency.TTM]
    if frequency != PeriodFrequency.ANNUAL:
        ttm = []
    elif len(ttm) > 1:
        codes.append("AMBIGUOUS_TTM")
        ttm = []
    if not selected and not ttm:
        return None, codes
    first = (selected or ttm)[0]
    if any(
        o.unit != first.unit or o.reporting_scope != first.reporting_scope
        for o in (*selected, *ttm)
    ):
        return None, [*codes, "INCONSISTENT_UNIT_OR_SCOPE"]
    return (
        SummaryRow(
            label=_label(first),
            metric_id=metric_id,
            unit=first.unit,
            reporting_scope=first.reporting_scope,
            values=tuple(_value(o) for o in selected),
            ttm=_value(ttm[0]) if ttm else None,
        ),
        codes,
    )


def _section_limitations(
    extraction: CompanyExtractionResult, block: SummaryBlockName, section: Section
) -> list[SummaryLimitation]:
    """Existing coverage codes for an unsuccessful section; never its message text."""
    coverage = next(s for s in extraction.coverage.sections if s.section == section)
    if coverage.parse_status == "SUCCESS":
        return []
    codes = sorted({i.code for i in coverage.issues if i.severity == "error"})
    return [
        SummaryLimitation(block=block, code=code)
        for code in codes or [f"SECTION_{coverage.parse_status}"]
    ]
