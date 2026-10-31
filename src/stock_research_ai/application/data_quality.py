"""DETAILED_RESEARCH_V1 Box 13: data limitations, source disagreements, diligence items, risk map.

DATA_LIMITATION is never COMPANY_RISK: a blocked source, a missing concall or an unresolved
disagreement says something about the data, not the company. Disagreements are surfaced once
each (deduplicated), with both values and the canonical selection only where an authority
policy made one. Open diligence items state the question and the source that would answer it;
the answer is never filled in here.
"""

from collections import Counter
from datetime import date
from uuid import UUID, uuid5

from stock_research_ai.application.diligence_inputs import fid
from stock_research_ai.application.governance_risk import holding_points, years_before
from stock_research_ai.application.period_names import period_name
from stock_research_ai.application.red_flags import NS, RULES, Ctx
from stock_research_ai.domain.document_intelligence import EvidenceBundle, ExtractionStatus
from stock_research_ai.domain.due_diligence import (
    DataQualityItem,
    DataQualitySummary,
    DisagreeingValue,
    DisagreementEntry,
    DisagreementGroup,
    HolderCategory,
    Layer,
    Level,
    OpenDiligenceItem,
    OrderBookQuality,
    RiskMapEntry,
    RiskSignal,
    Severity,
    SignalClass,
    SignalStatus,
    SourceDisagreements,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    Outcome,
    ReportingBasis,
)
from stock_research_ai.domain.forecasting import ForecastDataset
from stock_research_ai.domain.fundamental_valuation import ValuationDataset, ValuationStatus
from stock_research_ai.domain.management_intelligence import ManagementIntelligenceDataset
from stock_research_ai.domain.market_intelligence import (
    ChainStatus,
    CoverageStatus,
    DisagreementStatus,
    FetchStatus,
    FinancialBasis,
    Freshness,
    L1Resolution,
    MarketIntelligenceDataset,
    MetricStatus,
)

MAX_DISAGREEMENTS = 20_000
STALE_FINANCIALS_DAYS = 550  # latest audited year older than ~18 months

SUBJECT_GROUP = {
    "PROMOTER_HOLDING_PCT": DisagreementGroup.SHAREHOLDING,
    "PUBLIC_HOLDING_PCT": DisagreementGroup.SHAREHOLDING,
    "DIVIDEND_PER_SHARE": DisagreementGroup.EVENT,
    "ORDER_BOOK": DisagreementGroup.ORDER_BOOK,
    "CREDIT_RATING": DisagreementGroup.RATING,
    "LAST_PRICE": DisagreementGroup.MARKET,
    "MARKET_CAP": DisagreementGroup.MARKET,
}


# --- source disagreements ---------------------------------------------------------------------



def _endpoint(url: str) -> str:
    """Host, path and query of a source URL (Box 12 URLs carry no credential), bounded.
    Plain string handling: Box 13 imports no network or URL library."""
    text = url.split("://", 1)[-1].split("#", 1)[0]
    return text.split("@", 1)[-1][:160] or "endpoint not stated"


def source_disagreements(
    ctx: Ctx, box4: CanonicalFinancialDataset, box12: MarketIntelligenceDataset | None
) -> SourceDisagreements:
    entries: dict[tuple[str, ...], DisagreementEntry] = {}
    observations = {o.observation_id: o for o in box4.observations}

    def add(key: tuple[str, ...], entry: DisagreementEntry) -> None:
        if key not in entries:
            entries[key] = entry

    for r in box4.reconciliation:
        if r.outcome not in (Outcome.CONFLICT, Outcome.AMBIGUOUS, Outcome.SOURCE_PRECEDENCE,
                             Outcome.RESTATEMENT):
            continue
        ids = [r.selected_observation_id] if r.selected_observation_id else []
        ids += [i for i in r.conflicting_observation_ids if i not in ids]
        ids += [i for i in r.supporting_observation_ids if i not in ids][:2]
        values = []
        for oid in ids:
            o = observations.get(oid)
            if o is None:
                continue
            values.append(DisagreeingValue(
                source=f"{o.source.source_type.value}:{o.source.locator}"[:1000],
                authority=o.authority.value, value=o.normalized_value, record_id=oid))
        if len({v.value for v in values}) < 2:
            continue
        resolved = r.outcome in (Outcome.SOURCE_PRECEDENCE, Outcome.RESTATEMENT)
        selected = observations.get(r.selected_observation_id) if r.selected_observation_id \
            else None
        key = ("BOX4", r.metric.value, r.period.key(), r.reporting_basis.value,
               *sorted(str(v.value) for v in values))
        add(key, DisagreementEntry(
            entry_id=uuid5(NS, f"{ctx.dd_key}|disagreement|{r.record_id}"),
            group=DisagreementGroup.FINANCIAL, origin=Layer.BOX4, subject=r.metric.value,
            period=period_name(r.period, box4.fiscal_year_end_month),
            basis=r.reporting_basis.value, values=tuple(values),
            status="RESOLVED" if resolved and selected else "OPEN",
            canonical_source=(f"{selected.source.source_type.value} ({selected.authority.value})"
                              if resolved and selected else None),
            reason=r.reason[:1000]))
    if box12 is not None:
        for d in box12.disagreements:
            group = SUBJECT_GROUP.get(d.subject, DisagreementGroup.MARKET)
            resolved = d.status == DisagreementStatus.RESOLVED_BY_AUTHORITY_POLICY
            key = ("BOX12", d.subject, str(d.period), d.basis.value,
                   *sorted((str(d.value_a), str(d.value_b))))
            add(key, DisagreementEntry(
                entry_id=uuid5(NS, f"{ctx.dd_key}|disagreement|{d.disagreement_id}"),
                group=group, origin=Layer.BOX12, subject=d.subject, period=str(d.period),
                basis=d.basis.value,
                values=(DisagreeingValue(source=d.source_a.reference[:1000],
                                         authority=d.source_a.authority.value, value=d.value_a,
                                         record_id=d.disagreement_id),
                        DisagreeingValue(source=d.source_b.reference[:1000],
                                         authority=d.source_b.authority.value, value=d.value_b,
                                         record_id=d.disagreement_id)),
                status="RESOLVED" if resolved else "OPEN",
                canonical_source=d.canonical_selection.value if resolved and
                d.canonical_selection else None,
                reason=(d.reason or d.selection_rationale)[:1000]))
    # Box 13's own selections: one quarter's holding across the exchange filings and the cited
    # inputs, and every explicit input family (the shared input policy)
    for category in (HolderCategory.PROMOTER, HolderCategory.PUBLIC):
        _, disputes = holding_points(ctx, box12, category)
        for day, resolution in disputes:
            subject = f"{category.value}_HOLDING_PCT"
            held = tuple(DisagreeingValue(
                source=c.item.source_id[:1000], authority=c.item.authority, value=c.item.pct,
                record_id=c.item.record_id) for c in resolution.candidates)
            winner = resolution.winner.item if resolution.winner else None
            add(("BOX13", subject, str(day)), DisagreementEntry(
                entry_id=uuid5(NS, f"{ctx.dd_key}|disagreement|{subject}|{day}"),
                group=DisagreementGroup.SHAREHOLDING, origin=Layer.BOX13, subject=subject,
                period=str(day), basis=FinancialBasis.NOT_APPLICABLE.value, values=held,
                status="OPEN" if winner is None else "RESOLVED",
                canonical_source=None if winner is None else
                f"{winner.authority}: {winner.source_id}"[:1000],
                reason=("equal authority, different values: no selection, no grade"
                        if winner is None else "authority policy: the higher-ranked source is "
                        "canonical; every value preserved")))
    for conflict in (ctx.inputs.conflicts if ctx.inputs is not None else []):
        f = conflict.family
        winner = conflict.resolution.winner
        first = conflict.first
        stated = tuple(DisagreeingValue(
            source=" | ".join(x for x in (f.describe(r), r.source.reference) if x)[:1000],
            authority=r.source.authority.value, value=f.value(r),
            record_id=fid(f.record, r)) for r in conflict.records)
        key = ("REQUEST", f.field, *(str(k) for k in conflict.key))
        add(key, DisagreementEntry(
            entry_id=uuid5(NS, f"{ctx.dd_key}|disagreement|{'|'.join(key)}"),
            group=f.group, origin=Layer.REQUEST, subject=f.subject, period=f.period(first),
            basis=f.basis(first), values=stated,
            status="OPEN" if winner is None else "RESOLVED",
            canonical_source=None if winner is None else
            f"{winner.item.source.authority.value}: {winner.item.source.reference}"[:1000],
            reason=("cited inputs of equal authority state different values: no selection; "
                    "rules needing it are UNAVAILABLE (INPUT_CONFLICT)" if winner is None else
                    "authority policy: the higher-ranked source is canonical; every value "
                    "preserved")))
    ordered = sorted(entries.values(), key=lambda e: (e.group.value, e.subject, e.period,
                                                      str(e.entry_id)))
    truncated = len(ordered) > MAX_DISAGREEMENTS
    ordered = ordered[:MAX_DISAGREEMENTS]
    groups = Counter(e.group for e in ordered)
    return SourceDisagreements(
        entries=tuple(ordered),
        open_count=sum(1 for e in ordered if e.status == "OPEN"),
        resolved_count=sum(1 for e in ordered if e.status == "RESOLVED"),
        by_group=tuple(sorted(groups.items(), key=lambda i: i[0].value)), truncated=truncated)


# --- data quality -----------------------------------------------------------------------------


def data_quality(
    ctx: Ctx,
    *,
    box12: MarketIntelligenceDataset | None,
    box8: ManagementIntelligenceDataset | None,
    box10: ForecastDataset | None,
    box11: ValuationDataset | None,
    bundle: EvidenceBundle | None,
    disagreements: SourceDisagreements,
    unverified_evidence: int,
    order_book: OrderBookQuality | None = None,
) -> DataQualitySummary:
    items: list[DataQualityItem] = []

    def add(kind: str, layer: Layer, detail: str, records: tuple[UUID, ...] = (),
            affects: tuple[str, ...] = ()) -> None:
        items.append(DataQualityItem(
            item_id=uuid5(NS, f"{ctx.dd_key}|dq|{kind}|{layer.value}|{detail}"), kind=kind,
            layer=layer, detail=detail[:1000], record_ids=records[:200], affects=affects))

    if ctx.analytical == ReportingBasis.STANDALONE:
        add("MISSING_CONSOLIDATED_BASIS", Layer.BOX4,
            "no consolidated statements in Box 4: standalone is the analytical basis")
    if ctx.control is None and ctx.analytical == ReportingBasis.CONSOLIDATED:
        add("MISSING_STANDALONE_BASIS", Layer.BOX4,
            "no standalone statements: the forensic / control layer is unavailable")
    unknown = sum(1 for v in ctx.box4.canonical_values
                  if v.reporting_basis == ReportingBasis.UNKNOWN)
    if unknown:
        add("BASIS_UNCLEAR", Layer.BOX4, f"{unknown} canonical values have no stated basis and "
            "are excluded")
    latest = ctx.latest.get(ctx.analytical)
    if latest is None:
        add("MISSING_FINANCIALS", Layer.BOX4, "no fiscal year on the analytical basis")
    elif (ctx.request.as_of - latest).days > STALE_FINANCIALS_DAYS:
        add("STALE_DATA", Layer.BOX4, f"latest fiscal year ends {latest}, "
            f"{(ctx.request.as_of - latest).days} days before as_of")
    if ctx.box4.status == "PARTIAL":
        add("PARTIAL_UPSTREAM", Layer.BOX4, "Box 4 dataset is PARTIAL")
    if ctx.box5.status == "PARTIAL":
        add("PARTIAL_UPSTREAM", Layer.BOX5, "Box 5 dataset is PARTIAL")
    if box12 is None:
        add("MISSING_UPSTREAM", Layer.BOX12, "no Box 12 market / exchange dataset supplied")
    else:
        if box12.identity.status.value != "RESOLVED":
            add("IDENTITY_PARTIAL", Layer.BOX12, f"Box 12 identity is "
                f"{box12.identity.status.value}: some listings unverified")
        for p in box12.payloads:
            # which endpoint and window: two calls to one provider are never anonymous twins
            where = _endpoint(p.url) + (
                f" for {p.coverage_window_start} to {p.coverage_window_end}"
                if p.coverage_window_start and p.coverage_window_end else "")
            if p.status in (FetchStatus.BLOCKED, FetchStatus.AUTH_REQUIRED,
                            FetchStatus.RATE_LIMITED):
                add("BLOCKED_SOURCE", Layer.BOX12, f"{p.kind.value} ({where}): {p.status.value} "
                    f"({p.error_code}); never bypassed", (p.payload_id,))
            elif p.status != FetchStatus.OK:
                add("SOURCE_FAILED", Layer.BOX12, f"{p.kind.value} ({where}): {p.status.value} "
                    f"({p.error_code})", (p.payload_id,))
        for c in box12.coverage.items:
            if c.status == CoverageStatus.STALE:
                add("STALE_DATA", Layer.BOX12, f"{c.category.value}: latest {c.latest_date}")
            elif c.expected and c.status in (CoverageStatus.MISSING,
                                             CoverageStatus.SOURCE_UNAVAILABLE):
                kind = {"CONCALL_TRANSCRIPTS": "MISSING_CONCALLS",
                        "CREDIT_RATINGS": "MISSING_RATING_NOTES"}.get(
                    c.category.value, "MISSING_REQUIRED_DOCUMENT")
                add(kind, Layer.BOX12, f"{c.category.value}: {c.status.value}")
            absence_window_start = years_before(ctx.request.as_of, 3)
            window_proven = (
                c.required_sources_complete and c.processing_succeeded
                and c.coverage_window_complete and c.coverage_window_start is not None
                and c.coverage_window_end is not None
                and c.coverage_window_start <= absence_window_start
                and c.coverage_window_end >= ctx.request.as_of
                and not any(gap_start <= ctx.request.as_of and gap_end >= absence_window_start
                            for gap_start, gap_end in c.coverage_window_gaps)
            )
            if c.category.value in ("MATERIAL_ANNOUNCEMENTS", "CORPORATE_ACTIONS",
                                    "CREDIT_RATINGS") and not window_proven:
                signals = {
                    "MATERIAL_ANNOUNCEMENTS": ("EXCHANGE_PENALTIES", "PROMOTER_SELLING",
                                               "CHEAP_INSIDER_ALLOTMENT"),
                    "CORPORATE_ACTIONS": ("PROMOTER_SELLING", "EQUITY_DILUTION"),
                    "CREDIT_RATINGS": ("CREDIT_DETERIORATION",),
                }[c.category.value]
                add("INCOMPLETE_COVERAGE_WINDOW", Layer.BOX12,
                    f"{c.category.value}: source window, acquisition, or processing completeness "
                    "is not verified; absence-based conclusions are unavailable",
                    affects=signals)
        if box12.snapshot is not None and box12.snapshot.freshness == Freshness.STALE:
            add("STALE_DATA", Layer.BOX12, "market quote is stale")
        pending = [c for c in box12.order_chains if c.status == ChainStatus.PENDING_L1 and
                   c.l1_linkage is not None and c.l1_linkage.resolution in (
                       L1Resolution.UNRESOLVED_LINKAGE, L1Resolution.SOURCE_INCOMPLETE)]
        if pending:
            add("INCOMPLETE_ORDER_LINKAGE", Layer.BOX12, f"{len(pending)} L1 bids whose award "
                "linkage cannot be resolved from the evidence",
                tuple(c.chain_id for c in pending), ("ORDER_CANCELLATION_RATE",))
        unlinked = [c for c in box12.order_chains
                    if c.status in (ChainStatus.UNLINKED_CANCELLATION,
                                    ChainStatus.CANCELLATION_UNRESOLVED)]
        if unlinked:
            add("INCOMPLETE_ORDER_LINKAGE", Layer.BOX12, f"{len(unlinked)} cancellations not "
                "linked to an order, or of unresolved scope", tuple(c.chain_id for c in unlinked),
                ("ORDER_CANCELLATION_RATE",))
        if box12.order_book is not None:
            for m in box12.order_book.metrics:
                if m.status == MetricStatus.UNAVAILABLE and m.reason and any(
                        w in m.reason for w in ("TAX", "CURRENCY", "BASIS")):
                    add("VALUE_BASIS_DISAGREEMENT", Layer.BOX12,
                        f"{m.kind.value}: {m.reason}")
        unknown_basis = sum(1 for f in box12.filings if f.category.value == "FINANCIAL_RESULT"
                            and f.basis == FinancialBasis.UNKNOWN)
        if unknown_basis:
            add("BASIS_UNCLEAR", Layer.BOX12, f"{unknown_basis} result filings without a "
                "stated basis")
    if box8 is None:
        add("MISSING_UPSTREAM", Layer.BOX8, "no Box 8 management dataset (concalls) supplied",
            affects=("UNRELIABLE_GUIDANCE",))
    elif not box8.events:
        add("MISSING_CONCALLS", Layer.BOX8, "Box 8 holds no management event")
    if box10 is None:
        add("FORECAST_INPUTS_UNAVAILABLE", Layer.BOX10, "no Box 10 forecast supplied")
    elif box10.status.value != "COMPLETE":
        add("FORECAST_INPUTS_UNAVAILABLE", Layer.BOX10, f"Box 10 forecast is "
            f"{box10.status.value} ({len(box10.issues)} issues)")
    if box11 is None:
        add("VALUATION_INPUTS_UNAVAILABLE", Layer.BOX11, "no Box 11 valuation supplied")
    elif box11.status != ValuationStatus.COMPLETE:
        reasons = sorted({r.reason for r in box11.results if r.reason})
        add("VALUATION_INPUTS_UNAVAILABLE", Layer.BOX11, f"Box 11 valuation is "
            f"{box11.status.value}: {', '.join(reasons)[:600]}")
    if bundle is None:
        add("MISSING_UPSTREAM", Layer.BOX3, "no Box 3 evidence bundle supplied: explicit "
            "evidence IDs cannot be verified")
    else:
        for x in bundle.extractions:
            if x.status == ExtractionStatus.NO_TEXT:
                add("SCAN_ONLY_DOCUMENT", Layer.BOX3, f"{x.document_type.value} "
                    f"{x.artifact_reference}: no text layer (OCR not attempted)",
                    (x.document_id,))
            elif x.status == ExtractionStatus.UNSUPPORTED:
                add("UNSUPPORTED_DOCUMENT", Layer.BOX3, f"{x.document_type.value} "
                    f"{x.artifact_reference}: unsupported format (e.g. legacy XLS)",
                    (x.document_id,))
            elif x.status in (ExtractionStatus.FAILED, ExtractionStatus.MALFORMED,
                              ExtractionStatus.ENCRYPTED, ExtractionStatus.TOO_LARGE):
                add("DOCUMENT_NOT_EXTRACTED", Layer.BOX3, f"{x.document_type.value} "
                    f"{x.artifact_reference}: {x.status.value}", (x.document_id,))
        for skipped in bundle.documents_skipped:
            add("DOCUMENT_SKIPPED", Layer.BOX3, f"{skipped.key}: {skipped.count}")
    if order_book is not None and order_book.applicable and \
            order_book.reported_order_book_crore is None:
        add("MISSING_REPORTED_ORDER_BOOK", Layer.BOX13, "order-book-driven company without a "
            "company-reported order book (Box 7 fact or cited input)",
            affects=("LOW_EXECUTION_RATE",))
    if unverified_evidence:
        add("EVIDENCE_NOT_VERIFIED", Layer.REQUEST, f"{unverified_evidence} cited evidence IDs "
            "could not be checked against a Box 3 bundle")
    if disagreements.open_count:
        add("UNRESOLVED_DISAGREEMENT", Layer.BOX13, f"{disagreements.open_count} source "
            "disagreements without an authority selection")
    if ctx.inputs is not None:
        for conflict in ctx.inputs.conflicts:
            if conflict.open:
                add("INPUT_CONFLICT", Layer.REQUEST, f"{conflict.family.subject} "
                    f"{conflict.family.period(conflict.first)}: {len(conflict.records)} cited "
                    "inputs disagree (equal authority); withheld from every rule",
                    conflict.record_ids())
    unavailable = [s for s in ctx.signals if s.status in (
        SignalStatus.UNAVAILABLE, SignalStatus.UNSUPPORTED, SignalStatus.INSUFFICIENT_SAMPLE)]
    for code, n in sorted(Counter(s.code for s in unavailable).items()):
        add("RULE_INPUTS_UNAVAILABLE", Layer.BOX13, f"{code}: {n} signal(s) not evaluable",
            affects=(code,))
    counts = Counter(i.kind for i in items)
    return DataQualitySummary(
        items=tuple(sorted(items, key=lambda i: (i.kind, i.layer.value, i.detail))[:20_000]),
        counts=tuple(sorted(counts.items())),
        rules_unavailable=len(unavailable),
        rules_evaluated=sum(1 for s in ctx.signals if s.status == SignalStatus.EVALUATED),
    )


# --- open diligence items ---------------------------------------------------------------------

#: rule code -> (question, the source that would answer it)
NEEDS: dict[str, tuple[str, str]] = {
    "PROMOTER_PLEDGE": ("What share of promoter holding is pledged or encumbered?",
                        "Shareholding pattern (Table II) / SAST Reg 31 encumbrance disclosures"),
    "PROMOTER_SELLING": ("Did promoter shares fall through sales or through dilution?",
                         "Shareholding patterns with share counts; allotment filings"),
    "EQUITY_DILUTION": ("How many shares were outstanding 3 years ago, adjusted for bonus / "
                        "split?", "Annual reports (share capital note) / corporate actions"),
    "CHEAP_INSIDER_ALLOTMENT": ("At what price, and to whom, were shares or warrants issued?",
                                "Preferential-issue notices and allotment filings"),
    "RELATED_PARTY_DEPENDENCE": ("What are related-party revenues and purchases?",
                                 "Annual report related-party note (Ind AS 24)"),
    "AUDITOR_SIGNAL": ("What did the statutory auditor report (opinion, EoM, CARO, IFC)?",
                       "Independent auditor's reports (standalone and consolidated)"),
    "CONTINGENT_LIABILITY": ("What contingent liabilities are disclosed, by type and case?",
                             "Annual report contingent-liabilities note"),
    "STALLED_CWIP": ("How old is capital work-in-progress?",
                     "Annual report CWIP ageing schedule (Schedule III)"),
    "CREDIT_DETERIORATION": ("What are the current ratings and outlooks by instrument?",
                             "Rating-agency press releases / rationales"),
    "MANAGEMENT_CHURN": ("Which CEO / CFO / auditor changes occurred, and were they planned?",
                         "Regulation 30 KMP / auditor change filings"),
    "UNRELIABLE_GUIDANCE": ("How did quantitative guidance compare with outcomes?",
                            "Earnings-call transcripts (Box 8) for completed periods"),
    "EXECUTIVE_PAY_VS_PROFIT": ("How did KMP remuneration move against profit?",
                                "Annual report remuneration disclosures / MGT-7"),
    "BOARD_INDEPENDENCE": ("How many directors are independent and is the chair executive?",
                           "Corporate-governance report"),
    "RECURRING_EXCEPTIONALS": ("Were exceptional items reported in each year?",
                               "Audited results (exceptional items line)"),
    "GOODWILL_HEAVY": ("What goodwill and intangibles are carried?",
                       "Balance sheet / intangible-assets note"),
    "LEVERAGE": ("What are net debt and operating EBITDA?", "Audited balance sheet and P&L"),
    "WEAK_CASH_CONVERSION": ("What were CFO and PAT over 5 years?", "Audited cash-flow statements"),
    "PROFIT_WITHOUT_CASH": ("What were CFO and PAT in each of the last 5 years?",
                            "Audited cash-flow statements"),
    "HIGH_ACCRUALS": ("What were PAT, CFO and total assets?", "Audited financial statements"),
    "OTHER_INCOME_DEPENDENCE": ("What were other income and PBT?", "Audited P&L"),
    "RECEIVABLES_OUTRUNNING_SALES": ("What were trade receivables at each year end?",
                                     "Annual report balance sheet / trade-receivables note"),
    "RISING_DEBTOR_DAYS": ("What were trade receivables at each year end (and their ageing)?",
                           "Annual report trade-receivables note and ageing schedule"),
    "INVENTORY_BUILDUP": ("What were inventory and cost of goods sold?",
                          "Annual report inventory note / P&L"),
    "WEAK_INTEREST_COVER": ("What were EBIT and finance cost?", "Audited P&L"),
    "REFINANCING_RISK": ("How is debt split between short and long term, and when does it "
                         "mature?", "Borrowings note / maturity profile"),
    "RELATED_PARTY_LOANS": ("Were loans, advances or guarantees given to promoter / KMP "
                            "entities?", "Annual report related-party note"),
    "EXCHANGE_PENALTIES": ("Were exchange or regulator penalties levied?",
                           "Exchange announcements / contingent-liability note"),
    "ORDER_CANCELLATION_RATE": ("What value of awarded orders was cancelled?",
                                "Order filings with values / company order-book disclosures"),
    "LOW_EXECUTION_RATE": ("What were the opening order book and the period revenue?",
                           "Company order-book disclosures (opening / closing)"),
    "CLIENT_CONCENTRATION": ("How are awarded orders split across clients?",
                             "Order filings with values and clients / investor presentation"),
}


def open_items(
    ctx: Ctx,
    *,
    box12: MarketIntelligenceDataset | None,
    extra: list[tuple[str, str, str, tuple[UUID, ...]]],
) -> list[OpenDiligenceItem]:
    out: dict[str, OpenDiligenceItem] = {}
    by_id = {s.risk_id: s for s in ctx.signals}

    def importance(related: tuple[UUID, ...], core: bool) -> Level:
        severities = {by_id[i].severity for i in related if i in by_id}
        if Severity.RED in severities:
            return Level.HIGH
        if Severity.YELLOW in severities or core:
            return Level.MEDIUM
        return Level.LOW

    def add(question: str, reason: str, needed: str, related: tuple[UUID, ...] = (),
            core: bool = False) -> None:
        key = f"{question}|{reason}"
        if key in out:
            return
        out[key] = OpenDiligenceItem(
            item_id=uuid5(NS, f"{ctx.dd_key}|diligence|{key}"), question=question[:1000],
            reason=reason[:1000], importance=importance(related, core),
            needed_source=needed[:1000], related_risk_ids=related)

    for s in ctx.signals:
        if s.status not in (SignalStatus.UNAVAILABLE, SignalStatus.UNSUPPORTED,
                            SignalStatus.INSUFFICIENT_SAMPLE) or s.code not in NEEDS:
            continue
        question, needed = NEEDS[s.code]
        add(question, f"{s.code} is {s.status.value} ({s.reason})", needed, (s.risk_id,),
            core=RULES[s.code].impact == Level.HIGH)
    for s in ctx.signals:
        if s.code == "PROMOTER_SELLING" and s.reason == "DILUTION_NOT_SEPARABLE":
            add("Was the promoter holding decline a sale or dilution?", "decline above the "
                "threshold with a share issue in the window", "Share counts at both quarter "
                "ends; allotment filings", (s.risk_id,), core=True)
        if "RATING_WITHDRAWN" in s.issues:
            add("Why was a credit rating withdrawn?", "a rating was withdrawn", "Rating-agency "
                "withdrawal press release", (s.risk_id,))
        if "COUNTERPARTY_RELATION_UNCLEAR" in s.issues:
            add("Which related parties are promoter / KMP entities?", "a counterparty's "
                "relation is unclear", "Annual report related-party note", (s.risk_id,))
        if "MORE_EXCHANGE_FILINGS_THAN_SUPPLIED_CHANGES" in s.issues:
            add("Which KMP / auditor filings are departures of the CEO, CFO or auditor?",
                "more change filings than classified changes", "Regulation 30 filings",
                (s.risk_id,))
    if ctx.inputs is not None:
        for c in ctx.inputs.conflicts:
            if c.open:
                f = c.family
                add(f"Which source states {f.subject.lower().replace('_', ' ')} for "
                    f"{f.period(c.first)} correctly?", f"{len(c.records)} cited inputs of equal "
                    "authority disagree; nothing was selected", "The original filing or "
                    "document both inputs cite")
    for r in ctx.request.auditor_reports:
        for kam in r.key_audit_matters:
            add(f"Key audit matter FY{r.fiscal_year} ({r.basis.lower()}): {kam}",
                "a key audit matter is a diligence item, not a flag",
                "Auditor's report KAM section and the related note")
    if box12 is not None:
        ratings = [f for f in box12.filings if f.category.value == "CREDIT_RATING"]
        if ratings and not ctx.request.ratings:
            add("What instrument does each filed rating cover?", f"{len(ratings)} exchange "
                "rating filings state no instrument", "Rating rationales by instrument")
    for question, reason, needed, related in extra:
        add(question, reason, needed, related)
    return sorted(out.values(), key=lambda i: ({Level.HIGH: 0, Level.MEDIUM: 1, Level.LOW: 2,
                                                 Level.UNKNOWN: 3}[i.importance], i.question))


# --- risk map and mitigants -------------------------------------------------------------------


def risk_map(ctx: Ctx, flags: list[RiskSignal]) -> list[RiskMapEntry]:
    out: list[RiskMapEntry] = []
    for s in flags:
        assert s.severity is not None
        history = ctx.persistence.get(s.risk_id)
        if history is None or any(h is None for h in history[1:]):
            likelihood = Level.UNKNOWN
            why = "PERSISTENCE/1: not a periodic rule, or a prior year cannot be evaluated"
        else:
            breaches = sum(1 for h in history if h in (Severity.YELLOW, Severity.RED))
            likelihood = {3: Level.HIGH, 2: Level.MEDIUM}.get(breaches, Level.LOW)
            why = f"PERSISTENCE/1: threshold crossed in {breaches} of the latest 3 fiscal years"
        impact = RULES[s.code].impact
        out.append(RiskMapEntry(
            risk_id=s.risk_id, code=s.code, category=s.category, severity=s.severity,
            likelihood=likelihood, likelihood_rule=why, impact=impact,
            impact_rule=f"IMPACT/1: registered impact of {RULES[s.code].formula_id}"))
    return out


def attach_mitigants(signals: list[RiskSignal]) -> list[RiskSignal]:
    controls = [s for s in signals if s.signal_class == SignalClass.POSITIVE_CONTROL]
    out: list[RiskSignal] = []
    for s in signals:
        if s.signal_class == SignalClass.COMPANY_RISK and s.severity in (Severity.YELLOW,
                                                                         Severity.RED):
            related = [c for c in controls if c.category == s.category]
            if related:
                s = s.model_copy(update={
                    "mitigating_signal_ids": tuple(c.risk_id for c in related),
                    "mitigant": ("mitigating evidence (does not change the severity): "
                                 + "; ".join(f"{c.code} {c.value}" for c in related))[:1000],
                })
        out.append(s)
    return out


def days_between(a: date, b: date) -> int:
    return (b - a).days
