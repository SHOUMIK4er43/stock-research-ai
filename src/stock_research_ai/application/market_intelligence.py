"""DETAILED_RESEARCH_V1 Box 12: RawPayloads + explicit inputs -> MarketIntelligenceDataset.

Pipeline (pure; `build_market_intelligence` makes no network call and reads no clock):
  payloads (sorted, de-duplicated by content identity) -> identity (fail closed: an ambiguous
  or unconfirmed identity yields a dataset with no market data and no events) -> Kite quote
  and daily candles (company and benchmarks) -> internally calculated returns, statistics,
  benchmark comparisons and beta -> exchange announcements -> canonical events (classified,
  merged across exchanges, superseded only by explicit reference) -> order records and
  chains -> order-book analytics -> corporate actions, shareholding, insider trades, filing
  documents -> source disagreements -> coverage and timeline.

`generated_at` is injected and excluded from every ID and from the canonical JSON; equal
payloads and requests always give byte-identical canonical output. Box 12's only
networked entry point is `adapters.exchange.source.acquire` (bounded Box 12 adapters).
"""

import asyncio
import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID

from stock_research_ai.application.corporate_events import (
    build_actions,
    build_events,
    build_timeline,
)
from stock_research_ai.application.exchange_intelligence import (
    ActionRow,
    Budget,
    FilingRow,
    IdentityInputs,
    bse_announcements,
    bse_corporate_actions,
    by_kind,
    csv_rows,
    issue,
    nse_announcements,
    nse_corporate_actions,
    nse_documents,
    nse_insider_trades,
    nse_shareholding,
    payload_id,
    payload_record,
    resolve_identity,
    shp_declarations,
    source_ref,
    uid,
)
from stock_research_ai.application.market_data import (
    beta,
    compare,
    parse_history,
    parse_quote,
    period_returns,
    statistics,
)
from stock_research_ai.application.order_ledger import LedgerCoverage, build_order_ledger
from stock_research_ai.application.source_coverage import (
    build_coverage,
    disagreements,
    exchange_facts,
)
from stock_research_ai.domain.market_intelligence import (
    IST,
    MAX_ISSUES,
    MAX_MASTER_ROWS,
    MAX_PAYLOADS,
    BenchmarkComparison,
    BenchmarkRole,
    BenchmarkSpec,
    BetaEstimate,
    CompanyIdentity,
    CorporateAction,
    CorporateEvent,
    CoverageCategory,
    FetchStatus,
    FilingDocument,
    InsiderTrade,
    InstrumentRef,
    Issue,
    MarketIntelligenceDataset,
    MarketIntelligenceRequest,
    MarketStatistic,
    OrderBookAnalytics,
    OrderChain,
    OrderRecord,
    PayloadKind,
    PeriodReturn,
    PriceHistory,
    RawPayload,
    ShareholdingFiling,
)

_HISTORY_TOKEN = re.compile(r"/instruments/historical/([0-9]{1,12})/day")


class MarketIntelligenceInputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sorted_unique(payloads: Sequence[RawPayload]) -> list[tuple[RawPayload, UUID]]:
    if len(payloads) > MAX_PAYLOADS:
        raise MarketIntelligenceInputError("TOO_MANY_PAYLOADS")
    unique: dict[UUID, RawPayload] = {}
    for payload in payloads:
        if not isinstance(payload, RawPayload):
            raise MarketIntelligenceInputError("NOT_A_RAW_PAYLOAD")
        unique.setdefault(payload_id(payload), payload)
    return sorted(((p, i) for i, p in unique.items()),
                  key=lambda pi: (pi[0].kind.value, pi[0].url, str(pi[1])))


def _benchmark_tokens(
    kite: list[tuple[RawPayload, UUID]], specs: Sequence[BenchmarkSpec], issues: list[Issue],
    budget: Budget,
) -> dict[tuple[str, str], str]:
    tokens: dict[tuple[str, str], set[str]] = {(s.exchange, s.symbol): set() for s in specs}
    for payload, pid in kite:
        for _, row in csv_rows(payload, pid, [], budget):
            key = (row.get("EXCHANGE", ""), row.get("TRADINGSYMBOL", ""))
            if key in tokens and row.get("SEGMENT") == "INDICES":
                token = row.get("INSTRUMENT_TOKEN", "")
                if re.fullmatch(r"[0-9]{1,12}", token):
                    tokens[key].add(token)
    out = {}
    for key, found in tokens.items():
        if len(found) == 1:
            out[key] = next(iter(found))
        elif found:
            issues.append(issue("AMBIGUOUS_BENCHMARK", f"Several tokens for {key[1]}",
                                severity="ERROR"))
        else:
            issues.append(issue("BENCHMARK_NOT_MAPPED", f"No Kite index row for {key[1]}"))
    return out


def _ledger_coverage(
    identity: CompanyIdentity, ordered: list[tuple[RawPayload, UUID]],
    ledger_start: date | None, truncated: bool,
) -> LedgerCoverage:
    """Complete when the announcement feed of some exchange on which the listing is VERIFIED
    answered every request and nothing was cut by a bound."""
    verified = {PayloadKind.NSE_ANNOUNCEMENTS: identity.nse_symbol is not None,
                PayloadKind.BSE_ANNOUNCEMENTS: identity.bse_code is not None}
    complete_feed = False
    reasons: list[str] = []
    for kind, ok in verified.items():
        feed = by_kind(ordered, kind)
        if not feed:
            continue
        if not ok:
            reasons.append(f"{kind.value}_IDENTITY_UNVERIFIED")
        elif all(p.status == FetchStatus.OK for p, _ in feed):
            complete_feed = True
        else:
            reasons.append(f"{kind.value}_SOURCE_FAILED")
    if not complete_feed:
        reasons.append("NO_COMPLETE_VERIFIED_ANNOUNCEMENT_FEED")
    if truncated:
        reasons.append("ANNOUNCEMENT_FEED_TRUNCATED")
    complete = complete_feed and not truncated and ledger_start is not None
    return LedgerCoverage(complete=complete, ledger_start=ledger_start,
                          reasons=() if complete else tuple(reasons))


def _absence_coverage_requirements(
    identity: CompanyIdentity,
) -> dict[CoverageCategory, tuple[PayloadKind, ...]]:
    announcement_feeds = tuple(
        kind for enabled, kind in (
            (identity.nse_symbol is not None, PayloadKind.NSE_ANNOUNCEMENTS),
            (identity.bse_code is not None, PayloadKind.BSE_ANNOUNCEMENTS),
        ) if enabled
    )
    action_feeds = tuple(
        kind for enabled, kind in (
            (identity.nse_symbol is not None, PayloadKind.NSE_CORPORATE_ACTIONS),
            (identity.bse_code is not None, PayloadKind.BSE_CORPORATE_ACTIONS),
        ) if enabled
    )
    requirements: dict[CoverageCategory, tuple[PayloadKind, ...]] = {}
    if announcement_feeds:
        requirements[CoverageCategory.MATERIAL_ANNOUNCEMENTS] = announcement_feeds
        requirements[CoverageCategory.CORPORATE_ACTIONS] = action_feeds
    if identity.nse_symbol is not None:
        requirements[CoverageCategory.CREDIT_RATINGS] = (
            PayloadKind.NSE_CREDIT_RATINGS, PayloadKind.NSE_ANNOUNCEMENTS,
        )
    return requirements


def build_market_intelligence(
    request: MarketIntelligenceRequest,
    payloads: Sequence[RawPayload],
    *,
    generated_at: datetime,
) -> MarketIntelligenceDataset:
    ordered = _sorted_unique(payloads)
    issues: list[Issue] = []
    budget = Budget()
    # Instrument masters are lookup tables, not records: their own bound, so a large master
    # can never starve the filing feeds of the record budget.
    master_budget = Budget(remaining=MAX_MASTER_ROWS)
    identity = resolve_identity(
        request,
        IdentityInputs(
            nse_master=by_kind(ordered, PayloadKind.NSE_EQUITY_MASTER),
            kite_instruments=by_kind(ordered, PayloadKind.KITE_INSTRUMENTS),
            bse_header=by_kind(ordered, PayloadKind.BSE_COMPANY_HEADER),
        ),
        master_budget,
    )
    as_of_day = request.as_of.astimezone(IST).date()
    decoded: dict[UUID, int] = {}
    snapshot = company_history = None
    benchmark_histories: list[PriceHistory] = []
    comparisons: list[BenchmarkComparison] = []
    betas: list[BetaEstimate] = []
    returns: tuple[PeriodReturn, ...] = ()
    stats: tuple[MarketStatistic, ...] = ()
    events: list[CorporateEvent] = []
    actions: list[CorporateAction] = []
    conflicts: list[tuple[ActionRow, ActionRow]] = []
    filings: list[FilingDocument] = []
    shareholding: list[ShareholdingFiling] = []
    insider: list[InsiderTrade] = []
    orders: list[OrderRecord] = []
    chains: list[OrderChain] = []
    order_book: OrderBookAnalytics | None = None
    sector = "NOT_MAPPED"

    if not identity.usable:
        issues.append(issue("IDENTITY_NOT_RESOLVED", "Identity is ambiguous or unconfirmed; no "
                            "market data or events are attributed (fail closed)",
                            severity="ERROR"))
    else:
        # --- market --------------------------------------------------------------------
        company = InstrumentRef(symbol=identity.nse_symbol or identity.bse_code or "UNKNOWN",
                                exchange="NSE" if identity.nse_symbol else "BSE", kind="EQUITY",
                                instrument_token=identity.kite_instrument_token,
                                name=identity.name)
        for payload, pid in by_kind(ordered, PayloadKind.KITE_QUOTE):
            if identity.nse_symbol is None:
                continue
            found = parse_quote(payload, pid, f"NSE:{identity.nse_symbol}", company,
                                as_of=request.as_of,
                                current_seconds=request.quote_current_seconds,
                                stale_days=request.quote_stale_days, issues=issues)
            if found is not None and (snapshot is None or (
                    found.exchange_timestamp or found.observed_at) > (
                    snapshot.exchange_timestamp or snapshot.observed_at)):
                snapshot = found
        kite_master = by_kind(ordered, PayloadKind.KITE_INSTRUMENTS)
        bench_tokens = _benchmark_tokens(kite_master, request.benchmarks, issues,
                                         Budget(remaining=MAX_MASTER_ROWS))
        histories: dict[str, list[tuple[RawPayload, UUID]]] = {}
        for payload, pid in by_kind(ordered, PayloadKind.KITE_HISTORICAL):
            match = _HISTORY_TOKEN.search(payload.url)
            histories.setdefault(match.group(1) if match else "-", []).append((payload, pid))
        if identity.kite_instrument_token and identity.kite_instrument_token in histories:
            company_history = parse_history(histories[identity.kite_instrument_token], company,
                                            as_of=request.as_of,
                                            stale_days=request.history_stale_days)
            returns = period_returns(company_history)
            stats = statistics(company_history)
        else:
            issues.append(issue("NO_COMPANY_HISTORY", "No candle payload attributable to the "
                                "company's Kite token", severity="ERROR"))
        for spec in request.benchmarks:
            token = bench_tokens.get((spec.exchange, spec.symbol))
            if token is None or token not in histories:
                if spec.role == BenchmarkRole.SECTOR:
                    sector = "NOT_MAPPED"
                continue
            ref = InstrumentRef(symbol=spec.symbol, exchange=spec.exchange, kind="INDEX",
                                instrument_token=token)
            history = parse_history(histories[token], ref, as_of=request.as_of,
                                    stale_days=request.history_stale_days)
            benchmark_histories.append(history)
            if spec.role == BenchmarkRole.SECTOR:
                sector = "MAPPED"
            if company_history is not None:
                comparisons.extend(compare(company_history, history, spec.role))
                betas.append(beta(company_history, history))
        known = {identity.kite_instrument_token, *bench_tokens.values()}
        for token, items in histories.items():
            if token not in known:
                issues.append(issue("UNATTRIBUTED_HISTORY_PAYLOAD", "Candle payload for an "
                                    "instrument outside this request; ignored",
                                    payload=items[0][1]))

        # --- exchange ------------------------------------------------------------------
        rows: list[FilingRow] = []
        for payload, pid in by_kind(ordered, PayloadKind.NSE_ANNOUNCEMENTS):
            found_rows = nse_announcements(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_rows)
            rows += found_rows
        for payload, pid in by_kind(ordered, PayloadKind.BSE_ANNOUNCEMENTS):
            found_rows = bse_announcements(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_rows)
            rows += found_rows
        announcements_truncated = budget.exhausted
        events = build_events(rows, identity.identity_id, request.reference_revenue, issues)
        if len(events) > 5_000:
            issues.append(issue("EVENT_LIMIT", "Only the latest 5,000 events kept",
                                severity="ERROR"))
            events = events[-5_000:]
            announcements_truncated = True
        action_rows = []
        for payload, pid in by_kind(ordered, PayloadKind.NSE_CORPORATE_ACTIONS):
            found_actions = nse_corporate_actions(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_actions)
            action_rows += found_actions
        for payload, pid in by_kind(ordered, PayloadKind.BSE_CORPORATE_ACTIONS):
            found_actions = bse_corporate_actions(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_actions)
            action_rows += found_actions
        actions, conflicts = build_actions(action_rows, identity.identity_id)
        for payload, pid in by_kind(ordered, PayloadKind.NSE_SHAREHOLDING):
            found_sh = nse_shareholding(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_sh)
            shareholding += found_sh
        by_xbrl = {f.xbrl_url: i for i, f in enumerate(shareholding) if f.xbrl_url}
        for payload, pid in by_kind(ordered, PayloadKind.NSE_SHAREHOLDING_XBRL):
            found_decl = shp_declarations(payload, pid, identity, issues)
            decoded[pid] = 1 if found_decl else 0
            at = by_xbrl.get(payload.url)
            if found_decl is None or at is None:
                continue
            day, declared = found_decl
            filing = shareholding[at]
            if filing.period_end != day:
                issues.append(issue("SHAREHOLDING_XBRL_PERIOD_MISMATCH", "Shareholding XBRL "
                                    "reports another period than its filing; ignored",
                                    payload=pid))
                continue
            shareholding[at] = filing.model_copy(update={
                **declared,
                "unparsed_categories": tuple(c for c in filing.unparsed_categories
                                             if c != "PROMOTER_PLEDGE"),
                "sources": (*filing.sources, source_ref(payload, pid, "xbrl"))})
        for payload, pid in by_kind(ordered, PayloadKind.NSE_INSIDER_TRADING):
            found_it = nse_insider_trades(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_it)
            insider += found_it
        for payload, pid in by_kind(ordered, PayloadKind.NSE_ANNUAL_REPORTS,
                                    PayloadKind.NSE_FINANCIAL_RESULTS, PayloadKind.NSE_BRSR,
                                    PayloadKind.NSE_CREDIT_RATINGS):
            found_docs = nse_documents(payload, pid, identity, issues, budget)
            decoded[pid] = len(found_docs)
            filings += found_docs
        shareholding.sort(key=lambda s: (s.period_end, s.submitted_on or date.min,
                                         str(s.filing_id)))
        insider = sorted({t.record_id: t for t in insider}.values(),
                         key=lambda t: (t.disclosed_at or datetime.min.replace(tzinfo=IST),
                                        str(t.record_id)))
        filings = sorted({f.document_id: f for f in filings}.values(),
                         key=lambda f: (f.published_on or date.min, f.category.value,
                                        str(f.document_id)))

        # --- orders ----------------------------------------------------------------------
        announcement_ok = any(
            p.status == FetchStatus.OK
            for p, _ in by_kind(ordered, PayloadKind.NSE_ANNOUNCEMENTS,
                                PayloadKind.BSE_ANNOUNCEMENTS)
        )
        ledger_start = min((e.event_date for e in events), default=None) if announcement_ok \
            else None
        orders, chains, order_book = build_order_ledger(
            events, request, ledger_start,
            _ledger_coverage(identity, ordered, ledger_start, announcements_truncated),
        )
        related: dict[UUID, set[UUID]] = {}
        event_of = {o.order_id: o.event_id for o in orders}
        for chain in chains:
            members = {event_of[o] for o in chain.order_ids}
            for member in members:
                related.setdefault(member, set()).update(members - {member})
        events = [
            e.model_copy(update={"related_event_ids": tuple(sorted(
                {*e.related_event_ids, *related.get(e.event_id, set())}, key=str))})
            if e.event_id in related else e
            for e in events
        ]
    if budget.exhausted:
        issues.append(issue("SOURCE_RECORD_LIMIT", "Decoded-row budget reached across payloads",
                            severity="ERROR"))

    records = [payload_record(
        p,
        decoded.get(i, 0),
        processing_succeeded=not any(x.payload_id == i for x in issues),
    ) for p, i in ordered]
    facts = exchange_facts(shareholding, conflicts) + list(request.comparable_facts)
    coverage = build_coverage(
        as_of_day, records, events, filings, shareholding, insider, actions, snapshot,
        company_history, identity_usable=identity.usable,
        truncated=budget.exhausted or master_budget.exhausted,
        required_payload_kinds=_absence_coverage_requirements(identity),
    )
    timeline = build_timeline(as_of_day, events, actions, filings)
    issues = sorted(issues, key=lambda i: (i.code, str(i.payload_id), i.locator or "", i.message))
    if len(issues) > MAX_ISSUES:
        issues = issues[: MAX_ISSUES - 1] + [issue("ISSUE_LIMIT", "Further issues omitted",
                                                   severity="ERROR")]
    dataset_id = uid("dataset", identity.identity_id, request.as_of.isoformat(),
                     *(str(i) for _, i in ordered),
                     request.model_dump_json(exclude={"as_of"}))
    return MarketIntelligenceDataset(
        dataset_id=dataset_id,
        generated_at=generated_at,
        as_of=request.as_of,
        identity=identity,
        payloads=tuple(records),
        snapshot=snapshot,
        company_history=company_history,
        benchmark_histories=tuple(benchmark_histories),
        period_returns=returns,
        statistics=stats,
        benchmark_comparisons=tuple(comparisons),
        betas=tuple(betas),
        sector_index=sector,  # type: ignore[arg-type]
        events=tuple(events),
        corporate_actions=tuple(actions),
        orders=tuple(sorted(orders, key=lambda o: (o.announcement_date, str(o.order_id)))),
        order_chains=tuple(chains),
        order_book=order_book,
        shareholding=tuple(shareholding),
        insider_trades=tuple(insider),
        filings=tuple(filings),
        disagreements=tuple(disagreements(facts)),
        coverage=coverage,
        timeline=timeline,
        issues=tuple(issues),
    )


async def build_market_intelligence_async(
    request: MarketIntelligenceRequest,
    payloads: Sequence[RawPayload],
    *,
    generated_at: datetime,
) -> MarketIntelligenceDataset:
    """CPU-bound work off the event loop."""
    return await asyncio.to_thread(
        build_market_intelligence, request, payloads, generated_at=generated_at
    )


def canonical_market_intelligence_json(dataset: MarketIntelligenceDataset) -> str:
    """Deterministic serialization; generated_at and retrieval times are excluded so equal
    inputs compare equal even when fetched at different moments."""
    data = dataset.model_dump(mode="json", exclude={"generated_at"})
    for record in data["payloads"]:
        record.pop("retrieved_at", None)
    if data.get("snapshot"):
        data["snapshot"].pop("observed_at", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
