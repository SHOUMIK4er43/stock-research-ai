"""Box 12 source authority, source disagreements and due-diligence source coverage.

Disagreement policy (fixed): facts are grouped by (metric, period, basis, unit); within a group
the highest-authority fact is the reference and every fact whose value differs from it becomes
a SourceDisagreement holding both values. A source is selected only when the two authorities
differ and neither is an explicit analyst input (PRIMARY_REGULATORY > PRIMARY_COMPANY >
MARKET_PROVIDER > THIRD_PARTY_STRUCTURED); equal authorities stay OPEN. Nothing is overwritten.

Coverage reports, per category: expected or not (and why), available count, latest date, age,
stale bound and the resulting state. There is deliberately no composite coverage score.
"""

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from stock_research_ai.application.exchange_intelligence import ActionRow, uid
from stock_research_ai.domain.market_intelligence import (
    AUTHORITY_RANK,
    Authority,
    Citation,
    ComparableFact,
    CorporateAction,
    CorporateEvent,
    CoverageCategory,
    CoverageItem,
    CoverageStatus,
    DisagreementStatus,
    EventType,
    FetchStatus,
    FilingCategory,
    FilingDocument,
    FinancialBasis,
    Freshness,
    InsiderTrade,
    MarketSnapshot,
    PayloadKind,
    PriceHistory,
    Provider,
    ShareholdingFiling,
    SourceCoverageDataset,
    SourceDisagreement,
    SourcePayloadRecord,
)
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset, ScreenerTableKind

# --- disagreements ---------------------------------------------------------------------------


def exchange_facts(
    shareholding: Iterable[ShareholdingFiling],
    action_conflicts: Iterable[tuple[ActionRow, ActionRow]],
) -> list[ComparableFact]:
    """Facts Box 12 itself observed on an exchange, for comparison with other sources."""
    facts: list[ComparableFact] = []
    latest: dict[date, ShareholdingFiling] = {}
    for filing in shareholding:  # ordered: a later filing for the period supersedes
        latest[filing.period_end] = filing
    for period, filing in sorted(latest.items()):
        cite = Citation(provider=Provider.NSE, authority=Authority.PRIMARY_REGULATORY,
                        reference=f"NSE shareholding pattern for {period.isoformat()}",
                        url=filing.xbrl_url)
        for metric, value in (("PROMOTER_HOLDING_PCT", filing.promoter_pct),
                              ("PUBLIC_HOLDING_PCT", filing.public_pct)):
            if value is not None:
                facts.append(ComparableFact(metric=metric, period=period, value=value,
                                            unit="PERCENT", citation=cite))
    for a, b in action_conflicts:
        for row in (a, b):
            source = row.source
            if row.ex_date is None or row.amount is None:
                continue
            facts.append(ComparableFact(
                metric="DIVIDEND_PER_SHARE", period=row.ex_date,
                value=row.amount, unit="INR_PER_SHARE",
                citation=Citation(provider=source.provider, authority=source.authority,
                                  reference=f"{source.provider.value} corporate actions "
                                  f"{source.locator}"),
            ))
    return facts


def screener_shareholding_facts(dataset: DetailedScreenerDataset) -> list[ComparableFact]:
    """Read-only bridge from Box 1B: Screener's quarterly promoter / public percentages."""
    table = dataset.table(ScreenerTableKind.SHAREHOLDING_QUARTERLY)
    if table is None:
        return []
    labels = {"promoters": "PROMOTER_HOLDING_PCT", "public": "PUBLIC_HOLDING_PCT"}
    facts = []
    for row in table.rows:
        metric = labels.get(row.label.strip().casefold())
        if metric is None:
            continue
        for period, cell in zip(table.periods, row.cells, strict=True):
            if period.end_date is None or cell.value.value is None:
                continue
            facts.append(ComparableFact(
                metric=metric, period=period.end_date, value=cell.value.value, unit="PERCENT",
                basis=FinancialBasis.NOT_APPLICABLE,
                citation=Citation(provider=Provider.SCREENER,
                                  authority=Authority.THIRD_PARTY_STRUCTURED,
                                  reference=f"Screener {cell.locator}"[:1000],
                                  url=dataset.source.page_url),
            ))
    return facts


def disagreements(facts: Sequence[ComparableFact]) -> list[SourceDisagreement]:
    groups: dict[tuple[str, date, str, str], list[ComparableFact]] = {}
    for fact in facts:
        groups.setdefault((fact.metric, fact.period, fact.basis.value, fact.unit), []).append(fact)
    out: list[SourceDisagreement] = []
    for (metric, period, basis, unit), group in sorted(groups.items(), key=lambda kv: (
            kv[0][0], kv[0][1], kv[0][2], kv[0][3])):
        group.sort(key=lambda f: (AUTHORITY_RANK[f.citation.authority], f.citation.provider.value,
                                  f.citation.reference, str(f.value)))
        reference = group[0]
        seen: set[tuple[str, str, Decimal]] = set()
        for other in group[1:]:
            if other.value == reference.value:
                continue
            key = (other.citation.provider.value, other.citation.reference, other.value)
            if key in seen:
                continue
            seen.add(key)
            a, b = reference.citation.authority, other.citation.authority
            resolvable = (AUTHORITY_RANK[a] != AUTHORITY_RANK[b]
                          and Authority.EXPLICIT_INPUT not in (a, b))
            out.append(SourceDisagreement(
                disagreement_id=uid("disagreement", metric, period, basis, unit,
                                    reference.citation.provider.value, reference.citation.reference,
                                    reference.value, *key),
                subject=metric, period=period, basis=FinancialBasis(basis), unit=unit,
                source_a=reference.citation, value_a=reference.value,
                source_b=other.citation, value_b=other.value,
                difference=reference.value - other.value,
                canonical_selection=reference.citation.provider if resolvable else None,
                selection_rationale=(
                    f"AUTHORITY_POLICY: {a.value} outranks {b.value} for a filed fact; both "
                    "values preserved" if resolvable
                    else "NO_AUTOMATIC_SELECTION: equal authority or explicit input"
                ),
                status=DisagreementStatus.RESOLVED_BY_AUTHORITY_POLICY if resolvable
                else DisagreementStatus.OPEN,
            ))
    return out


# --- coverage ---------------------------------------------------------------------------------

C = CoverageCategory
#: (expected, expectation basis, stale bound in days, source payload kinds)
POLICY: dict[CoverageCategory, tuple[bool, str, int | None, tuple[PayloadKind, ...]]] = {
    C.ANNUAL_REPORTS: (True, "LODR_REG_34", 548,
                       (PayloadKind.NSE_ANNUAL_REPORTS, PayloadKind.NSE_ANNOUNCEMENTS,
                        PayloadKind.BSE_ANNOUNCEMENTS)),
    C.QUARTERLY_RESULTS: (True, "LODR_REG_33", 140,
                          (PayloadKind.NSE_FINANCIAL_RESULTS, PayloadKind.NSE_ANNOUNCEMENTS,
                           PayloadKind.BSE_ANNOUNCEMENTS)),
    C.INVESTOR_PRESENTATIONS: (False, "CUSTOMARY_NOT_MANDATED", 200,
                               (PayloadKind.NSE_ANNOUNCEMENTS, PayloadKind.BSE_ANNOUNCEMENTS)),
    C.CONCALL_TRANSCRIPTS: (False, "ONLY_IF_A_CALL_WAS_HELD", 200,
                            (PayloadKind.NSE_ANNOUNCEMENTS, PayloadKind.BSE_ANNOUNCEMENTS)),
    C.CONCALL_RECORDINGS: (False, "ONLY_IF_A_CALL_WAS_HELD", 200,
                           (PayloadKind.NSE_ANNOUNCEMENTS, PayloadKind.BSE_ANNOUNCEMENTS)),
    C.CREDIT_RATINGS: (False, "ONLY_IF_RATED_INSTRUMENTS", 400,
                       (PayloadKind.NSE_CREDIT_RATINGS, PayloadKind.NSE_ANNOUNCEMENTS,
                        PayloadKind.BSE_ANNOUNCEMENTS)),
    C.AGM_EGM_MATERIAL: (True, "COMPANIES_ACT_ANNUAL_AGM", 450,
                         (PayloadKind.NSE_ANNOUNCEMENTS, PayloadKind.BSE_ANNOUNCEMENTS)),
    C.BRSR: (False, "APPLICABILITY_NOT_DETERMINED", 450, (PayloadKind.NSE_BRSR,)),
    C.SHAREHOLDING_FILINGS: (True, "LODR_REG_31_QUARTERLY", 120,
                             (PayloadKind.NSE_SHAREHOLDING,)),
    C.INSIDER_FILINGS: (False, "ONLY_IF_INSIDERS_TRADED", None,
                        (PayloadKind.NSE_INSIDER_TRADING,)),
    C.MATERIAL_ANNOUNCEMENTS: (True, "LODR_REG_30", 120,
                               (PayloadKind.NSE_ANNOUNCEMENTS, PayloadKind.BSE_ANNOUNCEMENTS)),
    C.CORPORATE_ACTIONS: (False, "ONLY_IF_ACTIONS_DECLARED", None,
                          (PayloadKind.NSE_CORPORATE_ACTIONS, PayloadKind.BSE_CORPORATE_ACTIONS)),
    C.MARKET_QUOTE: (True, "LISTED_SECURITY", None, (PayloadKind.KITE_QUOTE,)),
    C.MARKET_HISTORY: (True, "LISTED_SECURITY", None, (PayloadKind.KITE_HISTORICAL,)),
}


def build_coverage(
    as_of: date,
    payloads: Sequence[SourcePayloadRecord],
    events: Sequence[CorporateEvent],
    filings: Sequence[FilingDocument],
    shareholding: Sequence[ShareholdingFiling],
    insider: Sequence[InsiderTrade],
    actions: Sequence[CorporateAction],
    snapshot: MarketSnapshot | None,
    history: PriceHistory | None,
    *,
    identity_usable: bool,
    truncated: bool = False,
    required_payload_kinds: dict[CoverageCategory, tuple[PayloadKind, ...]] | None = None,
) -> SourceCoverageDataset:
    def ev(kind: EventType, word: str | None = None) -> list[date]:
        return [e.event_date for e in events if e.event_type == kind
                and (word is None or word in f"{e.title} {e.summary or ''}".lower())]

    def docs(category: FilingCategory) -> list[date]:
        return [f.published_on for f in filings if f.category == category and f.published_on]

    found: dict[CoverageCategory, list[date]] = {
        C.ANNUAL_REPORTS: docs(FilingCategory.ANNUAL_REPORT) + ev(EventType.ANNUAL_REPORT),
        C.QUARTERLY_RESULTS: docs(FilingCategory.FINANCIAL_RESULT) + ev(EventType.RESULTS),
        C.INVESTOR_PRESENTATIONS: ev(EventType.INVESTOR_PRESENTATION),
        C.CONCALL_TRANSCRIPTS: ev(EventType.CONCALL, "transcript"),
        C.CONCALL_RECORDINGS: ev(EventType.CONCALL, "recording") + ev(EventType.CONCALL, "audio"),
        C.CREDIT_RATINGS: docs(FilingCategory.CREDIT_RATING) + ev(EventType.CREDIT_RATING),
        C.AGM_EGM_MATERIAL: ev(EventType.AGM_EGM),
        C.BRSR: docs(FilingCategory.BRSR),
        C.SHAREHOLDING_FILINGS: [s.submitted_on or s.period_end for s in shareholding],
        C.INSIDER_FILINGS: [d for t in insider
                            if (d := (t.disclosed_at.date() if t.disclosed_at
                                      else t.intimation_date)) is not None],
        C.MATERIAL_ANNOUNCEMENTS: [e.event_date for e in events],
        C.CORPORATE_ACTIONS: [d for a in actions
                              if (d := a.ex_date or a.record_date or a.announcement_date)],
        C.MARKET_QUOTE: [snapshot.market_date] if snapshot and snapshot.market_date else [],
        C.MARKET_HISTORY: [history.latest] if history and history.latest else [],
    }
    by_kind: dict[PayloadKind, list[SourcePayloadRecord]] = {}
    for record in payloads:
        by_kind.setdefault(record.kind, []).append(record)
    items = []
    for category, (expected, basis, stale, kinds) in POLICY.items():
        records = [r for k in kinds for r in by_kind.get(k, [])]
        required_kinds = (required_payload_kinds or {}).get(category, ())
        required_records = [r for kind in required_kinds for r in by_kind.get(kind, [])]
        required_sources_complete = identity_usable and bool(required_kinds) and all(
            any(r.kind == kind for r in required_records)
            and all(r.status == FetchStatus.OK and r.processing_succeeded
                    for r in required_records if r.kind == kind)
            for kind in required_kinds
        )
        processing_succeeded = bool(records) and all(
            r.status == FetchStatus.OK and r.processing_succeeded for r in records
        )
        windows = [r for r in required_records if r.coverage_window_complete
                   and r.coverage_window_start is not None
                   and r.coverage_window_end is not None]
        coverage_window_complete = (
            required_sources_complete
            and len(windows) == len(required_records)
            and bool(required_records)
        )
        starts = [r.coverage_window_start for r in windows if r.coverage_window_start is not None]
        ends = [r.coverage_window_end for r in windows if r.coverage_window_end is not None]
        window_start = max(starts) if coverage_window_complete and starts else None
        window_end = min(ends) if coverage_window_complete and ends else None
        source_retrieved = min((r.retrieved_at.date() for r in required_records), default=None) \
            if required_sources_complete else None
        ok = [r for r in records if r.status == FetchStatus.OK]
        failures = sorted({r.error_code or r.status.value for r in records
                           if r.status != FetchStatus.OK})
        if not records:
            failures = ["NOT_ACQUIRED"]
        if not identity_usable:
            failures = sorted({*failures, "IDENTITY_NOT_RESOLVED"})
        if truncated:
            failures = sorted({*failures, "SOURCE_RECORD_LIMIT"})
        dates = sorted(found[category]) if identity_usable else []
        latest = dates[-1] if dates else None
        age = (as_of - latest).days if latest else None
        if category == C.MARKET_QUOTE and snapshot is not None:
            fresh_bad = snapshot.freshness in (Freshness.STALE, Freshness.UNKNOWN)
        elif category == C.MARKET_HISTORY and history is not None:
            fresh_bad = history.freshness != Freshness.CURRENT
        else:
            fresh_bad = stale is not None and age is not None and age > stale
        if dates:
            status = CoverageStatus.STALE if fresh_bad else CoverageStatus.AVAILABLE
        elif not ok or not identity_usable or truncated:
            status = CoverageStatus.SOURCE_UNAVAILABLE
        else:
            status = CoverageStatus.MISSING if expected else CoverageStatus.NOT_FOUND
        items.append(CoverageItem(
            category=category, expected=expected, expectation_basis=basis, status=status,
            count=len(dates), latest_date=latest, age_days=age, stale_after_days=stale,
            providers=tuple(sorted({r.provider for r in ok}, key=lambda p: p.value)),
            source_failures=tuple(failures),
            coverage_window_start=window_start,
            coverage_window_end=window_end,
            coverage_window_complete=coverage_window_complete,
            coverage_window_gaps=tuple(sorted({
                gap for r in required_records for gap in r.coverage_window_gaps
            })),
            required_sources_complete=required_sources_complete,
            processing_succeeded=processing_succeeded,
            required_sources_retrieved=source_retrieved,
        ))
    return SourceCoverageDataset(as_of=as_of, items=tuple(items))
