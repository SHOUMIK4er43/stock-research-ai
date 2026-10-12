"""DETAILED_RESEARCH_V1 Box 13: order-book quality for EPC / capital-goods / defence companies.

Consumes the Box 12 order ledger and order-book analytics as they are (no ledger math is redone)
and Box 7 reported order-book facts. Pending L1 bids are a pipeline: never confirmed backlog.
Government / PSU exposure is concentration information, never a severity by itself.
"""

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from stock_research_ai.application.governance_risk import years_before
from stock_research_ai.application.red_flags import (
    RULES,
    Ctx,
    DueDiligenceInputError,
    Ev,
    Trace,
    grade,
    q,
    unavailable,
)
from stock_research_ai.domain.business_intelligence import BusinessIntelligenceDataset
from stock_research_ai.domain.due_diligence import (
    BusinessModel,
    Layer,
    OrderBookQuality,
    OrderMetricView,
    PendingL1Summary,
    SignalBasis,
    SignalStatus,
)
from stock_research_ai.domain.financial_canonical import Confidence
from stock_research_ai.domain.market_intelligence import (
    ChainStatus,
    L1Resolution,
    MarketIntelligenceDataset,
    MetricStatus,
    OrderBookMetricKind,
    OrderChain,
)

S = SignalStatus
NA = SignalBasis.NOT_APPLICABLE
D = Decimal
K = OrderBookMetricKind
HUNDRED = D(100)
MIN_CONFIRMED_CHAINS = 5  # ledger evidence of an order-book-driven business
MAX_CITED_CHAINS = 200
STALE_YEARS = 5


def applicability(ctx: Ctx, box12: MarketIntelligenceDataset | None,
                  box7: BusinessIntelligenceDataset | None) -> tuple[bool, list[str]]:
    basis: list[str] = []
    if ctx.request.business_model == BusinessModel.ORDER_BOOK_DRIVEN:
        basis.append("DECLARED_ORDER_BOOK_DRIVEN")
    if box12 is not None:
        confirmed = sum(1 for c in box12.order_chains if c.confirmed_on is not None)
        if confirmed >= MIN_CONFIRMED_CHAINS:
            basis.append("BOX12_CONFIRMED_ORDER_CHAINS")
    if box7 is not None and any(f.kind in ("ORDER_BOOK", "BACKLOG") for f in box7.order_book_facts):
        basis.append("BOX7_REPORTED_ORDER_BOOK")
    return bool(basis), basis


def reported_book(ctx: Ctx, box7: BusinessIntelligenceDataset | None) -> dict[str, Any]:
    """The latest company-reported order book: a Box 7 extracted fact, else a cited input."""
    if box7 is not None:
        books = [f for f in box7.order_book_facts
                 if f.kind in ("ORDER_BOOK", "BACKLOG") and f.normalized_value is not None
                 and f.as_of is not None and f.scope.upper() == "COMPANY"]
        if books:
            fact = max(books, key=lambda f: (f.as_of or date.min, str(f.fact_id)))
            return {"reported_order_book_crore": fact.normalized_value,
                    "reported_order_book_as_of": fact.as_of,
                    "reported_order_book_tax_basis": "NOT_STATED",
                    "reported_order_book_basis": fact.scope,
                    "reported_order_book_definition": fact.definition,
                    "reported_order_book_layer": Layer.BOX7}
    reports = [r for r in ctx.request.order_book_reports if r.currency == ctx.currency]
    if reports:
        r = max(reports, key=lambda x: (x.as_of, x.basis == ctx.analytical.value, x.value_crore))
        return {"reported_order_book_crore": q(r.value_crore),
                "reported_order_book_as_of": r.as_of,
                "reported_order_book_tax_basis": r.tax_basis,
                "reported_order_book_basis": r.basis,
                "reported_order_book_definition": r.definition,
                "reported_order_book_layer": Layer.REQUEST}
    return {"reported_order_book_crore": None, "reported_order_book_as_of": None,
            "reported_order_book_tax_basis": None}


def _metric(box12: MarketIntelligenceDataset, kind: OrderBookMetricKind):  # type: ignore[no-untyped-def]
    if box12.order_book is None:
        return None
    return next((m for m in box12.order_book.metrics if m.kind == kind), None)


def _chain_trace(ctx: Ctx, box12: MarketIntelligenceDataset, chains: list[OrderChain],
                 role: str, value: Decimal | None, unit: str) -> Trace:
    trace = Trace()
    events = {e.event_id: e for e in box12.events}
    orders = {o.order_id: o for o in box12.orders}
    ctx.record(trace, role, value, unit, Layer.BOX12, box12.dataset_id,
               f"BOX12:{box12.dataset_id}:order_book", confidence=Confidence.MEDIUM,
               detail="Box 12 order-book analytics (disclosed orders only)")
    for c in chains[:MAX_CITED_CHAINS]:
        trace.record_ids.add(c.chain_id)
        for oid in c.order_ids:
            order = orders.get(oid)
            if order is None:
                continue
            trace.evidence_ids.add(order.event_id)
            event = events.get(order.event_id)
            url = order.source_url or (event.source_url if event else None)
            if url:
                trace.source_ids.add(url[:1000])
    return trace


def _from_metric(ctx: Ctx, code: str, box12: MarketIntelligenceDataset, kind: OrderBookMetricKind,
                 chains: list[OrderChain]) -> Ev:
    m = _metric(box12, kind)
    if m is None:
        return unavailable(NA, "BOX12_METRIC_ABSENT")
    trace = _chain_trace(ctx, box12, chains, kind.value, m.value, m.unit)
    if m.status != MetricStatus.AVAILABLE or m.value is None:
        return unavailable(NA, m.reason or "UNAVAILABLE", trace=trace,
                           explanation=f"Box 12 {kind.value}: {m.reason}")
    value = q(m.value)
    return Ev(S.EVALUATED, NA, grade(value, RULES[code].thresholds), value,
              period_label=f"{m.period_start} to {m.period_end}" if m.period_start else None,
              period_end=m.period_end, trace=trace, issues=list(m.issues),
              calculation=f"{m.method} = {value} ({m.completeness})",
              explanation=f"{kind.value.lower().replace('_', ' ')} {value} {m.unit.lower()} "
              f"({m.completeness.lower().replace('_', ' ')})")


def build_order_book(
    ctx: Ctx, box12: MarketIntelligenceDataset | None, box7: BusinessIntelligenceDataset | None
) -> tuple[OrderBookQuality, list[UUID]]:
    applicable, basis = applicability(ctx, box12, box7)
    reported = reported_book(ctx, box7)
    empty = OrderBookQuality(
        applicable=applicable, applicability_basis=tuple(basis), **reported, metrics=(),
        pending_l1=None, confirmed_chains=0, cancelled_chains=0, unlinked_cancellations=0,
        revised_chains=0, signal_ids=())
    if not applicable:
        return empty, []
    ids: list[UUID] = []
    if box12 is None:
        for code in ("ORDER_CANCELLATION_RATE", "LOW_EXECUTION_RATE", "CLIENT_CONCENTRATION",
                     "FREQUENT_ORDER_REVISIONS", "STALE_ORDERS"):
            ids.append(ctx.emit(code, unavailable(NA, "BOX12_NOT_SUPPLIED")))
        return empty.model_copy(update={"signal_ids": ctx.present(ids)}), ids
    chains = list(box12.order_chains)
    pending = [c for c in chains if c.status == ChainStatus.PENDING_L1]
    if any(c.counted_in_confirmed for c in pending):
        raise DueDiligenceInputError("L1_COUNTED_AS_BACKLOG",
                                     "a pending L1 chain is counted in the confirmed book")
    resolutions = Counter(c.l1_linkage.resolution if c.l1_linkage else None for c in pending)
    true_value = _metric(box12, K.PENDING_L1_TRUE_PENDING_VALUE)
    unresolved_value = _metric(box12, K.PENDING_L1_UNRESOLVED_VALUE)
    summary = PendingL1Summary(
        chains=len(pending), true_pending=resolutions[L1Resolution.TRUE_PENDING],
        unresolved_linkage=resolutions[L1Resolution.UNRESOLVED_LINKAGE],
        source_incomplete=resolutions[L1Resolution.SOURCE_INCOMPLETE] + resolutions[None],
        true_pending_value_crore=true_value.value if true_value else None,
        unresolved_value_crore=unresolved_value.value if unresolved_value else None,
        chain_ids=tuple(c.chain_id for c in pending)[:20_000])
    confirmed = [c for c in chains if c.counted_in_confirmed]
    awarded = [c for c in chains if c.confirmed_on is not None]
    ids.append(ctx.emit("ORDER_CANCELLATION_RATE",
                        _from_metric(ctx, "ORDER_CANCELLATION_RATE", box12, K.CANCELLATION_RATE,
                                     awarded)))
    ids.append(ctx.emit("LOW_EXECUTION_RATE",
                        _from_metric(ctx, "LOW_EXECUTION_RATE", box12, K.EXECUTION_RATE,
                                     confirmed)))
    window = [c for c in awarded if c.confirmed_on and c.confirmed_on > years_before(
        ctx.request.as_of, 1)]
    gov = _metric(box12, K.GOVERNMENT_SHARE)
    client = _from_metric(ctx, "CLIENT_CONCENTRATION", box12, K.TOP_CLIENT_SHARE, window)
    if client.status == S.EVALUATED and not (
        gov is not None and gov.status == MetricStatus.AVAILABLE and gov.value is not None
        and gov.value < 50
    ):
        client.status, client.severity = S.INFORMATIONAL, None
        client.reason = "GOVERNMENT_CLIENT_BASE_OR_UNKNOWN"
        client.explanation += ("; reported as information: government / PSU clients are at "
                               "least half of awards or their share is not known")
    ids.append(ctx.emit("CLIENT_CONCENTRATION", client))
    government = _from_metric(ctx, "GOVERNMENT_CONCENTRATION", box12, K.GOVERNMENT_SHARE, window)
    if government.status == S.EVALUATED:
        government.status, government.severity = S.INFORMATIONAL, None
        government.reason = "INFORMATION_ONLY"
    ids.append(ctx.emit("GOVERNMENT_CONCENTRATION", government))
    revised = [c for c in awarded if c.revised]
    if len(awarded) < MIN_CONFIRMED_CHAINS:
        ev = unavailable(NA, "INSUFFICIENT_SAMPLE", status=S.INSUFFICIENT_SAMPLE)
    else:
        share = q(D(len(revised)) * HUNDRED / D(len(awarded)))
        trace = _chain_trace(ctx, box12, revised or awarded, "revised share", share, "PERCENT")
        ev = Ev(S.EVALUATED, NA, grade(share, RULES["FREQUENT_ORDER_REVISIONS"].thresholds),
                share, period_end=ctx.request.as_of, trace=trace,
                calculation=f"{len(revised)} revised / {len(awarded)} awarded chains x 100",
                explanation=f"{len(revised)} of {len(awarded)} awarded orders were revised")
    ids.append(ctx.emit("FREQUENT_ORDER_REVISIONS", ev))
    dated = [c for c in confirmed if c.confirmed_on is not None]
    if len(dated) < MIN_CONFIRMED_CHAINS:
        ev = unavailable(NA, "INSUFFICIENT_SAMPLE", status=S.INSUFFICIENT_SAMPLE)
    else:
        cutoff = years_before(ctx.request.as_of, STALE_YEARS)
        old = [c for c in dated if c.confirmed_on is not None and c.confirmed_on < cutoff]
        share = q(D(len(old)) * HUNDRED / D(len(dated)))
        trace = _chain_trace(ctx, box12, old or dated, "stale share", share, "PERCENT")
        ev = Ev(S.EVALUATED, NA, grade(share, RULES["STALE_ORDERS"].thresholds), share,
                period_end=ctx.request.as_of, trace=trace,
                calculation=f"{len(old)} confirmed before {cutoff} / {len(dated)} x 100",
                explanation=f"{len(old)} of {len(dated)} confirmed orders are older than "
                f"{STALE_YEARS} years (ledger coverage limits how old an order can be seen)")
    ids.append(ctx.emit("STALE_ORDERS", ev))
    views = tuple(
        OrderMetricView(kind=m.kind.value, status=m.status.value,
                        value=q(m.value) if m.value is not None else None, unit=m.unit,
                        completeness=m.completeness, reason=m.reason)
        for m in (box12.order_book.metrics if box12.order_book else ())
    )
    return OrderBookQuality(
        applicable=True, applicability_basis=tuple(basis), **reported, metrics=views,
        pending_l1=summary, confirmed_chains=len(confirmed),
        cancelled_chains=sum(1 for c in chains if c.status == ChainStatus.CANCELLED),
        unlinked_cancellations=sum(1 for c in chains
                                   if c.status == ChainStatus.UNLINKED_CANCELLATION),
        revised_chains=len(revised), signal_ids=ctx.present(ids)), ids
