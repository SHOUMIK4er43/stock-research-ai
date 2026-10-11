"""Box 12 order / project ledger and order-book analytics.

Stages are never conflated: L1 (lowest bidder) is not an order; a letter of award/acceptance
is an awarded order; a signed contract is the same order at a later stage; revenue is never
an order stage. Each disclosure becomes an OrderRecord; records of one project are linked
into one OrderChain by fixed rules, in this order:
  LETTER_REFERENCE  the filing says "in continuation to our letter dated D" and exactly one
                    earlier chain of the same client was announced on D
  PROJECT_TEXT      same client and identical normalized project description
  EXACT_VALUE       same client and identical stated value (to the rupee)
A chain counts its value once, at its latest confirmed stage; an L1-only chain is reported
separately as pending and never as confirmed; a cancellation removes the chain from the
confirmed value; a revision replaces the value and records the delta; a JV order counts its
attributable share only when the share is explicitly stated. Order details come from the
filing's title/summary and, when supplied, the already-extracted text of its attachment -
read only by anchored labels from the SEBI disclosure annexure. Ambiguity leaves a field
unknown with an issue.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from stock_research_ai.application.corporate_events import (
    StatedMoney,
    letter_dates,
    norm_key,
    single_amount,
)
from stock_research_ai.application.exchange_intelligence import issue, uid
from stock_research_ai.domain.market_intelligence import (
    STAGE_ORDER,
    AwardMode,
    ChainStatus,
    ClientCategory,
    CorporateEvent,
    DocumentText,
    EventType,
    Geography,
    Issue,
    MarketIntelligenceRequest,
    MetricStatus,
    OrderBookAnalytics,
    OrderBookMetric,
    OrderBookMetricKind,
    OrderBookReconciliation,
    OrderChain,
    OrderFact,
    OrderRecord,
    OrderStage,
    PeriodRevenue,
    ReportedOrderBook,
    TaxBasis,
)

_I = re.IGNORECASE | re.DOTALL
_SIGNED = re.compile(r"sign(?:ed|ing) (?:of )?(?:the )?contract|contract agreement", _I)
_VALUE_ANCHOR = re.compile(
    r"broad consideration or size of|size of (?:the )?order|order value|contract value|"
    r"value of (?:the )?(?:order|contract|work)", _I
)
_NEXT_ITEM = re.compile(r"\n\s*[a-i]\.\s|whether the promoter|whether the order", _I)
_GEOGRAPHY = re.compile(r"whether domestic or international;?\s*(domestic|international)", _I)
_GEOGRAPHY_ENTITY = re.compile(
    r"awarded by domestic\s*/\s*international\s+entit[yv];?\s*(domestic|international)", _I
)
_PERIOD_ANCHOR = re.compile(r"time period by which the", _I)
_PERIOD = re.compile(r"\b(\d{1,4})\s*(months?|days?|years?)\b", _I)
_PROJECT = re.compile(r"\bfor\s+[“\"](?P<p>[^”\"]{5,1500})[”\"]", _I)
_CLIENT = re.compile(
    r"(?:received|receipt of)\s+(?:an?\s+)?(?:letter of (?:award|acceptance)(?: of contract)?|"
    r"LOA|LoA|work order|purchase order|order|contract)\s+from\s+(?P<c>[^\n|]{2,200}?)"
    r"\s+(?:for|towards)\b"
    r"|lowest bidder\s*\(?L-?1\)?\s+(?:from|in|for|by)\s+(?P<c2>[^\n|]{2,200}?)\s+(?:for\b|-|–)"
    r"|contract agreement with\s+(?P<c3>[^\n|]{2,200}?)\s+(?:for|towards)\b",
    _I,
)
_JV = re.compile(r"\bjoint venture\b|\bJV\b|\bconsortium\b", re.I)
_JV_SHARE = re.compile(
    r"(?:RVNL|company|our)['’]?s?\s+share\s*(?:is|of|:)?\s*(\d{1,3}(?:\.\d{1,4})?)\s*%", re.I
)
_NOMINATION = re.compile(r"\bon nomination\b|\bnomination basis\b", re.I)
_TENDER = re.compile(r"\be-?tender\b|\btender no\b|lowest bidder", re.I)

STAGE_BY_EVENT = {
    EventType.L1_BIDDER: OrderStage.L1_BIDDER,
    EventType.LOA: OrderStage.LOA,
    EventType.ORDER_AWARD: OrderStage.LOA,
    EventType.ORDER_CANCELLATION: OrderStage.CANCELLATION,
    EventType.ORDER_REVISION: OrderStage.REVISION,
}


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split()).strip(" .,;-–")
    return cleaned[:limit] or None


def _window(text: str, anchor: re.Pattern[str], size: int) -> str | None:
    match = anchor.search(text)
    if match is None:
        return None
    tail = text[match.end(): match.end() + size]
    stop = _NEXT_ITEM.search(tail)
    return tail[: stop.start()] if stop else tail


def extract_order(
    event: CorporateEvent,
    document: DocumentText | None,
    fact: OrderFact | None,
) -> OrderRecord:
    stage = STAGE_BY_EVENT[event.event_type]
    rules: list[str] = [f"STAGE_FROM_{event.classification_rule}"]
    issues: list[Issue] = []
    head = f"{event.title}\n{event.summary or ''}"
    body = document.text if document else ""
    if stage == OrderStage.LOA and event.event_type == EventType.ORDER_AWARD and _SIGNED.search(
        head + body[:4000]
    ):
        stage = OrderStage.SIGNED_CONTRACT
        rules.append("SIGNED_CONTRACT_WORDING")
    combined = f"{head}\n{body}"

    client = event.counterparty
    match = _CLIENT.search(combined)
    if match:
        client = _clean(match.group("c") or match.group("c2") or match.group("c3"), 300) or client
        rules.append("CLIENT_FROM_COVERING_LETTER")
    project_match = _PROJECT.search(combined)
    project = _clean(project_match.group("p"), 1500) if project_match else None

    value: StatedMoney | None = None
    segment = _window(body, _VALUE_ANCHOR, 600) if body else None
    if segment is not None:
        value, ambiguous = single_amount(segment)
        if value is not None:
            rules.append("VALUE_FROM_ANNEXURE_LABEL")
        elif ambiguous:
            issues.append(issue("AMBIGUOUS_ORDER_VALUE", "Several values under the value label"))
    if value is None and event.financial_value_crore is not None:
        value = StatedMoney(
            crore=event.financial_value_crore, currency=event.value_currency or "INR",
            text=event.value_text or "", tax=event.value_tax_basis or TaxBasis.UNKNOWN,
        )
        rules.append("VALUE_FROM_FILING_SUBJECT")

    geography = Geography.UNKNOWN
    found = {m.lower() for m in _GEOGRAPHY.findall(body)} | {
        m.lower() for m in _GEOGRAPHY_ENTITY.findall(body)
    }
    if len(found) == 1:
        geography = Geography.DOMESTIC if found == {"domestic"} else Geography.INTERNATIONAL
        rules.append("GEOGRAPHY_FROM_ANNEXURE")
    elif len(found) > 1:
        issues.append(issue("CONFLICTING_GEOGRAPHY", "Annexure states both domestic and "
                            "international"))

    period_text = months = None
    period_segment = _window(body, _PERIOD_ANCHOR, 160) if body else None
    if period_segment is not None:
        periods = {(int(n), u.lower().rstrip("s")) for n, u in _PERIOD.findall(period_segment)}
        if len(periods) == 1:
            n, unit = next(iter(periods))
            period_text = f"{n} {unit}{'s' if n != 1 else ''}"
            months = n if unit == "month" else n * 12 if unit == "year" else None
            rules.append("EXECUTION_PERIOD_FROM_ANNEXURE")

    joint = True if _JV.search(combined) else None
    share = None
    shares = {Decimal(s) for s in _JV_SHARE.findall(combined)}
    if joint and len(shares) == 1:
        share = next(iter(shares))
    if fact is not None and fact.jv_share_pct is not None:
        joint, share = True, fact.jv_share_pct
        rules.append("JV_SHARE_FROM_CITED_FACT")

    mode = AwardMode.UNKNOWN
    if _NOMINATION.search(combined):
        mode = AwardMode.NOMINATION
    elif stage == OrderStage.L1_BIDDER or _TENDER.search(combined):
        mode = AwardMode.COMPETITIVE_BIDDING
    if fact is not None and fact.award_mode is not None:
        mode = fact.award_mode

    gross = value.crore if value else None
    attributable = None
    if gross is not None:
        if joint is None:
            attributable = gross  # no JV is disclosed: the whole value is the company's
        elif share is not None and 0 < share <= 100:
            attributable = (gross * share / 100).quantize(Decimal("0.000001"))
        else:
            issues.append(issue("JV_SHARE_NOT_STATED", "JV order without a stated share: "
                                "attributable value unknown"))
    category = fact.client_category if fact and fact.client_category else ClientCategory.UNKNOWN
    return OrderRecord(
        order_id=uid("order", event.event_id),
        event_id=event.event_id,
        stage=stage,
        announcement_date=event.event_date,
        client=client,
        client_key=norm_key(client) if client else None,
        project=project,
        gross_value_crore=gross,
        value_currency=value.currency if value else None,  # type: ignore[arg-type]
        value_tax_basis=value.tax if value else TaxBasis.UNKNOWN,
        value_text=value.text[:1000] if value else None,
        joint_venture=joint,
        jv_share_pct=share,
        jv_partner=fact.jv_partner if fact else None,
        attributable_value_crore=attributable,
        geography=geography,
        segment=fact.segment if fact else None,
        execution_period=period_text,
        execution_months=months,
        expected_completion=fact.expected_completion if fact else None,
        award_mode=mode,
        client_category=category,
        referenced_letter_dates=letter_dates(combined),
        source_url=event.source_url,
        document_sha256=document.document_sha256 if document else None,
        extraction_rules=tuple(rules),
        issues=tuple(issues),
    )


# --- chains -----------------------------------------------------------------------------------


@dataclass
class _Chain:
    records: list[OrderRecord]
    rules: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    duplicates: list[OrderRecord] = field(default_factory=list)

    @property
    def client_key(self) -> str | None:
        return next((r.client_key for r in self.records if r.client_key), None)


def _link(record: OrderRecord, chains: list[_Chain]) -> tuple[_Chain | None, str | None, bool]:
    """(chain, rule, ambiguous)."""
    if record.stage == OrderStage.L1_BIDDER and not record.referenced_letter_dates:
        return None, None, False
    pool = [
        c for c in chains
        if record.client_key is not None and c.client_key == record.client_key
        and c.records[-1].announcement_date <= record.announcement_date
    ]
    tests: list[tuple[str, Any]] = [
        ("LETTER_REFERENCE", lambda c: any(r.announcement_date in record.referenced_letter_dates
                                           for r in c.records)),
        ("PROJECT_TEXT", lambda c: record.project is not None and any(
            r.project and norm_key(r.project) == norm_key(record.project) for r in c.records)),
        ("EXACT_VALUE", lambda c: record.gross_value_crore is not None and any(
            r.gross_value_crore == record.gross_value_crore for r in c.records)),
    ]
    for rule, test in tests:
        hits = [c for c in pool if test(c)]
        if len(hits) == 1:
            return hits[0], rule, False
        if len(hits) > 1:
            # narrow by the remaining tests, in order
            for _, narrower in tests:
                narrowed = [c for c in hits if narrower(c)]
                if len(narrowed) == 1:
                    return narrowed[0], f"{rule}_NARROWED", False
            return None, None, True
    return None, None, False


def build_chains(records: Sequence[OrderRecord]) -> list[OrderChain]:
    chains: list[_Chain] = []
    for record in sorted(records, key=lambda r: (r.announcement_date, str(r.event_id))):
        chain, rule, ambiguous = _link(record, chains)
        if chain is None:
            new = _Chain(records=[record])
            if ambiguous:
                new.issues.append(issue("AMBIGUOUS_ORDER_LINK", "Several earlier orders match; "
                                        "not linked (counted separately, flagged)"))
            chains.append(new)
            continue
        assert rule is not None
        stages = [r.stage for r in chain.records]
        if record.stage in STAGE_ORDER:
            reached = max((STAGE_ORDER[s] for s in stages if s in STAGE_ORDER), default=-1)
            if STAGE_ORDER[record.stage] <= reached and OrderStage.CANCELLATION not in stages:
                # A repeat of a stage already disclosed: a duplicate announcement, never a
                # second order (e.g. the same LOA filed again).
                chain.duplicates.append(record)
                chain.rules.append(f"DUPLICATE_STAGE_{rule}")
                chain.records.append(record)
                continue
        chain.records.append(record)
        chain.rules.append(rule)
    return [_finish(c) for c in chains]


def _finish(chain: _Chain) -> OrderChain:
    records = chain.records
    stages = tuple(r.stage for r in records)
    first = records[0]
    issues = list(chain.issues)
    duplicates = {r.order_id for r in chain.duplicates}
    live = [r for r in records if r.order_id not in duplicates]
    confirmed = [r for r in live if r.stage in (OrderStage.LOA, OrderStage.SIGNED_CONTRACT)]
    l1 = [r for r in live if r.stage == OrderStage.L1_BIDDER]
    cancelled = any(r.stage == OrderStage.CANCELLATION for r in live)
    revisions = [r for r in live if r.stage == OrderStage.REVISION]
    if not confirmed and not l1:
        status = (ChainStatus.UNLINKED_CANCELLATION if cancelled
                  else ChainStatus.UNLINKED_REVISION)
        issues.append(issue("UNLINKED_ORDER_CHANGE", "Change to an order not found in the "
                            "ledger window; no value counted"))
    elif cancelled:
        status = ChainStatus.CANCELLED
    elif confirmed:
        status = ChainStatus.CONFIRMED
    else:
        status = ChainStatus.PENDING_L1
    valued = [r for r in confirmed if r.gross_value_crore is not None]
    awarded = valued[0] if valued else None
    current = valued[-1] if valued else None
    revision_delta = None
    for revision in revisions:
        if revision.gross_value_crore is not None and current is not None:
            revision_delta = (revision_delta or Decimal(0)) + (
                revision.gross_value_crore - (current.gross_value_crore or Decimal(0))
            )
            current = revision
    base: OrderRecord | None = current
    gross = base.gross_value_crore if base else None
    attributable = base.attributable_value_crore if base else None
    currencies = {r.value_currency for r in live if r.value_currency}
    if len(currencies) > 1:
        issues.append(issue("MIXED_ORDER_CURRENCY", "Stages state different currencies"))
    values_l1 = [r.gross_value_crore for r in l1 if r.gross_value_crore is not None]
    if confirmed and values_l1 and awarded and values_l1[-1] != awarded.gross_value_crore:
        issues.append(issue("L1_VALUE_DIFFERS_FROM_AWARD", "Award value differs from the L1 "
                            "value; the award value is used", severity="INFO"))
    if status == ChainStatus.CONFIRMED and first.client_key is None:
        issues.append(issue("CLIENT_NOT_IDENTIFIED", "No client stated: this order cannot be "
                            "linked to its other stages and may be counted with them"))
    if status == ChainStatus.CONFIRMED and gross is None:
        issues.append(issue("ORDER_VALUE_NOT_DISCLOSED", "Confirmed order without a stated value",
                            severity="INFO"))
    geos = {r.geography for r in live if r.geography != Geography.UNKNOWN}
    modes = {r.award_mode for r in live if r.award_mode != AwardMode.UNKNOWN}
    cats = {r.client_category for r in live if r.client_category != ClientCategory.UNKNOWN}
    if l1 and AwardMode.NOMINATION not in modes:
        modes.add(AwardMode.COMPETITIVE_BIDDING)
    counted = status == ChainStatus.CONFIRMED
    note = {
        ChainStatus.CONFIRMED: "COUNTED_ONCE_AT_LATEST_CONFIRMED_STAGE",
        ChainStatus.PENDING_L1: "L1_NOT_AN_ORDER_REPORTED_SEPARATELY",
        ChainStatus.CANCELLED: "CANCELLED_EXCLUDED",
        ChainStatus.UNLINKED_CANCELLATION: "UNLINKED_CHANGE_NOT_COUNTED",
        ChainStatus.UNLINKED_REVISION: "UNLINKED_CHANGE_NOT_COUNTED",
    }[status]
    tax = {r.value_tax_basis for r in valued}
    return OrderChain(
        chain_id=uid("chain", first.order_id),
        client=next((r.client for r in records if r.client), None),
        project=next((r.project for r in records if r.project), None),
        order_ids=tuple(r.order_id for r in records),
        stages=stages,
        link_rules=tuple(chain.rules),
        status=status,
        revised=bool(revisions),
        first_announced=first.announcement_date,
        confirmed_on=confirmed[0].announcement_date if confirmed else None,
        last_updated=records[-1].announcement_date,
        value_currency=next(iter(currencies)) if len(currencies) == 1 else None,
        value_tax_basis=next(iter(tax)) if len(tax) == 1 else TaxBasis.UNKNOWN,
        pending_l1_value_crore=values_l1[-1] if status == ChainStatus.PENDING_L1 and values_l1
        else None,
        confirmed_gross_value_crore=gross if counted else None,
        confirmed_attributable_value_crore=attributable if counted else None,
        cancelled_value_crore=gross if status == ChainStatus.CANCELLED else None,
        revision_delta_crore=revision_delta,
        geography=next(iter(geos)) if len(geos) == 1 else Geography.UNKNOWN,
        award_mode=next(iter(modes)) if len(modes) == 1 else AwardMode.UNKNOWN,
        client_category=next(iter(cats)) if len(cats) == 1 else ClientCategory.UNKNOWN,
        counted_in_confirmed=counted,
        counting_note=note,
        issues=tuple(issues),
    )


def awarded_value(chain: OrderChain, orders: dict[Any, OrderRecord]) -> Decimal | None:
    """Value at award (first confirmed stage with a value) - the inflow of the period."""
    for oid in chain.order_ids:
        r = orders[oid]
        if r.stage in (OrderStage.LOA, OrderStage.SIGNED_CONTRACT) and r.gross_value_crore:
            return r.gross_value_crore
    return None


# --- analytics --------------------------------------------------------------------------------

K = OrderBookMetricKind
Completeness = Literal["DISCLOSED_ORDERS_ONLY", "REPORTED", "MIXED", "NOT_APPLICABLE"]


def _metric(kind: OrderBookMetricKind, unit: str, method: str, completeness: Completeness,
            *, value: Decimal | None = None, reason: str | None = None,
            start: date | None = None, end: date | None = None, chains: int = 0,
            issues: Sequence[str] = ()) -> OrderBookMetric:
    return OrderBookMetric.model_validate({
        "kind": kind, "status": "AVAILABLE" if reason is None else "UNAVAILABLE",
        "value": value.quantize(Decimal("0.000001")) if value is not None and reason is None
        else None,
        "unit": unit, "method": method, "completeness": completeness, "reason": reason,
        "period_start": start, "period_end": end, "chains": chains, "issues": tuple(issues),
    })


def fiscal_bounds(fy: int) -> tuple[date, date]:
    return date(fy - 1, 4, 1), date(fy, 3, 31)


def _tax_ok(bases: set[TaxBasis], allow: bool) -> tuple[bool, list[str]]:
    if bases <= {TaxBasis.EXCLUSIVE_OF_TAX}:
        return True, []
    if allow:
        return True, ["TAX_INCLUSIVE_OR_UNKNOWN_VALUES_PERMITTED_BY_POLICY"]
    return False, []


def _ledger_inflow(
    chains: Sequence[OrderChain], orders: dict[Any, OrderRecord], start: date, end: date,
    ledger_start: date | None,
) -> tuple[Decimal | None, set[TaxBasis], int, str | None]:
    if ledger_start is None or ledger_start > start:
        return None, set(), 0, "LEDGER_DOES_NOT_COVER_PERIOD"
    picked = [c for c in chains if c.confirmed_on is not None and start <= c.confirmed_on <= end]
    values = [awarded_value(c, orders) for c in picked]
    if any(v is None for v in values):
        return None, set(), len(picked), "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
    if any(c.value_currency not in (None, "INR") for c in picked):
        return None, set(), len(picked), "NON_INR_ORDER_VALUES"
    total = sum((v for v in values if v is not None), Decimal(0))
    return total, {c.value_tax_basis for c in picked}, len(picked), None


def order_book_analytics(
    chains: Sequence[OrderChain],
    orders: Sequence[OrderRecord],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
) -> OrderBookAnalytics:
    by_id = {o.order_id: o for o in orders}
    allow = request.allow_tax_inclusive_order_values
    today = request.as_of.date()
    t_start = today - timedelta(days=364)
    metrics: list[OrderBookMetric] = []

    inflow, inflow_tax, n, reason = _ledger_inflow(chains, by_id, t_start, today, ledger_start)
    mixed = len(inflow_tax) > 1
    metrics.append(_metric(
        K.DISCLOSED_CONFIRMED_INFLOW, "INR_CRORE",
        "SUM_AWARD_VALUE_OF_CHAINS_CONFIRMED_IN_LAST_365D", "DISCLOSED_ORDERS_ONLY",
        value=inflow, reason=reason or ("MIXED_TAX_BASIS" if mixed else None),
        start=t_start, end=today, chains=n,
        issues=[f"TAX_BASIS_{b.value}" for b in sorted(inflow_tax)],
    ))
    pending = [c for c in chains if c.status == ChainStatus.PENDING_L1
               and c.first_announced >= t_start]
    pending_values = [c.pending_l1_value_crore for c in pending]
    pending_reason = (
        "LEDGER_DOES_NOT_COVER_PERIOD" if ledger_start is None or ledger_start > t_start
        else "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS" if any(v is None for v in pending_values)
        else None
    )
    metrics.append(_metric(
        K.DISCLOSED_PENDING_L1_VALUE, "INR_CRORE", "SUM_L1_VALUE_OF_OPEN_L1_CHAINS_LAST_365D",
        "DISCLOSED_ORDERS_ONLY",
        value=sum((v for v in pending_values if v is not None), Decimal(0)),
        reason=pending_reason, start=t_start, end=today, chains=len(pending),
        issues=["L1_IS_NOT_CONFIRMED_ORDER_BOOK"],
    ))

    annual = sorted(
        (r for r in request.period_revenues if 360 <= (r.period_end - r.period_start).days <= 370),
        key=lambda r: r.period_end,
    )
    latest = annual[-1] if annual else None
    books = sorted(request.reported_order_books, key=lambda b: b.as_of)

    # order book / revenue
    book = next((b for b in reversed(books) if b.as_of <= today), None)
    method = "LATEST_REPORTED_ORDER_BOOK / LATEST_ANNUAL_REVENUE"
    if book is None or latest is None:
        metrics.append(_metric(K.ORDER_BOOK_TO_REVENUE, "RATIO", method, "REPORTED",
                               reason="INPUT_NOT_SUPPLIED"))
    else:
        reason, notes = _comparable(book, latest, allow)
        metrics.append(_metric(K.ORDER_BOOK_TO_REVENUE, "RATIO", method, "REPORTED",
                               value=book.value_crore / latest.value_crore
                               if latest.value_crore else None,
                               reason=reason or (None if latest.value_crore else "ZERO_REVENUE"),
                               start=latest.period_start, end=book.as_of, issues=notes))

    # book-to-bill, inflow growth
    def inflow_for(start: date, end: date) -> tuple[Decimal | None, set[TaxBasis], str,
                                                    str | None]:
        reported = next((i for i in request.reported_inflows
                         if (i.period_start, i.period_end) == (start, end)), None)
        if reported is not None:
            return reported.value_crore, {reported.tax_basis}, "REPORTED", None
        value, bases, _, why = _ledger_inflow(chains, by_id, start, end, ledger_start)
        if why is None and len(bases) > 1:
            why = "MIXED_TAX_BASIS"
        return value, bases, "DISCLOSED_ORDERS_ONLY", why

    if latest is None:
        metrics.append(_metric(K.BOOK_TO_BILL, "RATIO", "INFLOW / REVENUE", "NOT_APPLICABLE",
                               reason="INPUT_NOT_SUPPLIED"))
    else:
        value, bases, source, why = inflow_for(latest.period_start, latest.period_end)
        ok, notes = _tax_ok(bases, allow)
        if why is None and not ok:
            why = "TAX_BASIS_NOT_COMPARABLE"
        if why is None and latest.currency != "INR":
            why = "CURRENCY_MISMATCH"
        metrics.append(_metric(
            K.BOOK_TO_BILL, "RATIO", "PERIOD_INFLOW / PERIOD_REVENUE", source,  # type: ignore[arg-type]
            value=value / latest.value_crore if value is not None and latest.value_crore else None,
            reason=why, start=latest.period_start, end=latest.period_end, issues=notes,
        ))
    growth_reason: str | None = "INPUT_NOT_SUPPLIED"
    growth_value = None
    g_start = g_end = None
    if latest is not None:
        cur_start, cur_end = latest.period_start, latest.period_end
        prev_end = cur_start - timedelta(days=1)
        prev_start = date(prev_end.year - 1, prev_end.month, prev_end.day) + timedelta(days=1) \
            if not (prev_end.month == 2 and prev_end.day == 29) else date(prev_end.year - 1, 3, 1)
        now_v, now_b, now_s, why_now = inflow_for(cur_start, cur_end)
        old_v, old_b, old_s, why_old = inflow_for(prev_start, prev_end)
        growth_reason = why_now or why_old
        if growth_reason is None and (now_s != old_s or now_b != old_b):
            growth_reason = "PERIODS_NOT_COMPARABLE"
        if growth_reason is None and not old_v:
            growth_reason = "ZERO_BASE_PERIOD"
        if growth_reason is None and now_v is not None and old_v:
            growth_value = (now_v / old_v - 1) * 100
        g_start, g_end = prev_start, cur_end
    metrics.append(_metric(K.ORDER_INFLOW_GROWTH, "PERCENT",
                           "INFLOW(PERIOD) / INFLOW(PRIOR EQUAL PERIOD) - 1", "MIXED",
                           value=growth_value, reason=growth_reason, start=g_start, end=g_end))

    # execution rate
    opening = None
    if latest is not None:
        opening = next((b for b in books if b.as_of == latest.period_start - timedelta(days=1)),
                       None)
    if latest is None or opening is None:
        metrics.append(_metric(K.EXECUTION_RATE, "PERCENT", "PERIOD_REVENUE / OPENING_ORDER_BOOK",
                               "REPORTED", reason="INPUT_NOT_SUPPLIED"))
    else:
        reason, notes = _comparable(opening, latest, allow)
        metrics.append(_metric(
            K.EXECUTION_RATE, "PERCENT", "PERIOD_REVENUE / OPENING_ORDER_BOOK", "REPORTED",
            value=latest.value_crore / opening.value_crore * 100 if opening.value_crore else None,
            reason=reason or (None if opening.value_crore else "ZERO_OPENING_ORDER_BOOK"),
            start=latest.period_start, end=latest.period_end, issues=notes,
        ))

    # ledger-only shares over chains confirmed in the last 365 days
    population = [c for c in chains if c.confirmed_on is not None and c.confirmed_on >= t_start]
    awarded = {c.chain_id: awarded_value(c, by_id) for c in population}
    complete = all(v is not None for v in awarded.values()) and bool(population)
    total = sum((v for v in awarded.values() if v is not None), Decimal(0))
    base_reason = None if complete and total > 0 else (
        "NO_CONFIRMED_ORDERS_IN_WINDOW" if not population else "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
    )
    taxes = {c.value_tax_basis for c in population}
    if base_reason is None and len(taxes) > 1:
        base_reason = "MIXED_TAX_BASIS"

    def share(kind: OrderBookMetricKind, attr: str, wanted: set[Any], method: str) -> None:
        reason = base_reason
        if reason is None and any(getattr(c, attr).value == "UNKNOWN" for c in population):
            reason = f"{attr.upper()}_NOT_DISCLOSED_FOR_SOME_ORDERS"
        part = sum((awarded[c.chain_id] or Decimal(0) for c in population
                    if getattr(c, attr) in wanted), Decimal(0))
        metrics.append(_metric(kind, "PERCENT", method, "DISCLOSED_ORDERS_ONLY",
                               value=part / total * 100 if total else None, reason=reason,
                               start=t_start, end=today, chains=len(population)))

    share(K.DOMESTIC_SHARE, "geography", {Geography.DOMESTIC}, "DOMESTIC_AWARD_VALUE / TOTAL")
    share(K.INTERNATIONAL_SHARE, "geography", {Geography.INTERNATIONAL},
          "INTERNATIONAL_AWARD_VALUE / TOTAL")
    share(K.GOVERNMENT_SHARE, "client_category", {ClientCategory.GOVERNMENT, ClientCategory.PSU},
          "GOVERNMENT_AND_PSU_AWARD_VALUE / TOTAL (CITED CATEGORIES ONLY)")
    share(K.COMPETITIVE_BIDDING_SHARE, "award_mode", {AwardMode.COMPETITIVE_BIDDING},
          "COMPETITIVE_AWARD_VALUE / TOTAL")
    share(K.NOMINATION_SHARE, "award_mode", {AwardMode.NOMINATION},
          "NOMINATION_AWARD_VALUE / TOTAL")
    clients: dict[str, Decimal] = {}
    missing_client = False
    for c in population:
        key = norm_key(c.client) if c.client else None
        if key is None:
            missing_client = True
            continue
        clients[key] = clients.get(key, Decimal(0)) + (awarded[c.chain_id] or Decimal(0))
    ranked = sorted(clients.values(), reverse=True)
    for kind, top in ((K.TOP_CLIENT_SHARE, 1), (K.TOP5_CLIENT_SHARE, 5)):
        reason = base_reason or ("CLIENT_NOT_DISCLOSED_FOR_SOME_ORDERS" if missing_client
                                 else None)
        metrics.append(_metric(kind, "PERCENT", f"TOP_{top}_CLIENTS_AWARD_VALUE / TOTAL",
                               "DISCLOSED_ORDERS_ONLY",
                               value=sum(ranked[:top], Decimal(0)) / total * 100 if total
                               else None, reason=reason, start=t_start, end=today,
                               chains=len(population)))
    attributable = [c.confirmed_attributable_value_crore for c in population
                    if c.status == ChainStatus.CONFIRMED]
    confirmed_now = [c for c in population if c.status == ChainStatus.CONFIRMED]
    metrics.append(_metric(
        K.JV_ATTRIBUTABLE_CONFIRMED_VALUE, "INR_CRORE",
        "SUM_ATTRIBUTABLE_VALUE_OF_CONFIRMED_CHAINS_LAST_365D", "DISCLOSED_ORDERS_ONLY",
        value=sum((v for v in attributable if v is not None), Decimal(0)),
        reason=None if confirmed_now and all(v is not None for v in attributable) and len(
            {c.value_tax_basis for c in confirmed_now}) == 1
        else ("NO_CONFIRMED_ORDERS_IN_WINDOW" if not confirmed_now
              else "ATTRIBUTABLE_VALUE_UNKNOWN_FOR_SOME_ORDERS"
              if any(v is None for v in attributable) else "MIXED_TAX_BASIS"),
        start=t_start, end=today, chains=len(confirmed_now),
    ))
    # cancellation rate over the whole ledger
    awarded_all = [c for c in chains if c.confirmed_on is not None]
    values_all = [awarded_value(c, by_id) for c in awarded_all]
    cancelled = [awarded_value(c, by_id) for c in awarded_all if c.status == ChainStatus.CANCELLED]
    c_reason = None
    if not awarded_all:
        c_reason = "NO_CONFIRMED_ORDERS"
    elif any(v is None for v in values_all):
        c_reason = "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
    elif len({c.value_tax_basis for c in awarded_all}) > 1:
        c_reason = "MIXED_TAX_BASIS"
    denominator = sum((v for v in values_all if v is not None), Decimal(0))
    metrics.append(_metric(
        K.CANCELLATION_RATE, "PERCENT", "CANCELLED_AWARD_VALUE / ALL_AWARD_VALUE (LEDGER)",
        "DISCLOSED_ORDERS_ONLY",
        value=sum((v for v in cancelled if v is not None), Decimal(0)) / denominator * 100
        if denominator else None,
        reason=c_reason or (None if denominator else "ZERO_AWARD_VALUE"),
        start=ledger_start, end=today, chains=len(awarded_all),
    ))
    metrics.sort(key=lambda m: list(K).index(m.kind))
    return OrderBookAnalytics(
        metrics=tuple(metrics),
        reconciliation=reconcile(chains, by_id, request, ledger_start, books, annual),
        tax_inclusive_values_allowed=allow,
    )


def _comparable(book: ReportedOrderBook, revenue: PeriodRevenue, allow: bool) -> tuple[
    str | None, list[str]
]:
    if book.currency != revenue.currency:
        return "CURRENCY_MISMATCH", []
    if book.basis != revenue.basis:
        return "BASIS_MISMATCH", []
    ok, notes = _tax_ok({book.tax_basis, revenue.tax_basis}, allow)
    return (None, notes) if ok else ("TAX_BASIS_NOT_COMPARABLE", [])


def reconcile(
    chains: Sequence[OrderChain],
    orders: dict[Any, OrderRecord],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
    books: Sequence[ReportedOrderBook],
    annual: Sequence[PeriodRevenue],
) -> OrderBookReconciliation | None:
    if not annual:
        return None
    revenue = annual[-1]
    start, end = revenue.period_start, revenue.period_end
    base: dict[str, Any] = {"period_start": start, "period_end": end}
    opening = next((b for b in books if b.as_of == start - timedelta(days=1)), None)
    closing = next((b for b in books if b.as_of == end), None)
    if opening is None:
        return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE,
                                       reason="NO_OPENING_ORDER_BOOK")
    allow = request.allow_tax_inclusive_order_values
    reason, notes = _comparable(opening, revenue, allow)
    if reason:
        return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE, reason=reason,
                                       opening_crore=opening.value_crore)
    reported = next((i for i in request.reported_inflows
                     if (i.period_start, i.period_end) == (start, end)), None)
    source: Literal["REPORTED", "LEDGER_DISCLOSED_ORDERS_ONLY"]
    inflow: Decimal | None
    if reported is not None:
        inflow, bases, source = reported.value_crore, {reported.tax_basis}, "REPORTED"
    else:
        inflow, bases, _, why = _ledger_inflow(chains, orders, start, end, ledger_start)
        if why:
            return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE, reason=why,
                                           opening_crore=opening.value_crore,
                                           executed_revenue_crore=revenue.value_crore)
        source = "LEDGER_DISCLOSED_ORDERS_ONLY"
        notes.append("INFLOW_IS_A_LOWER_BOUND_UNDISCLOSED_ORDERS_EXCLUDED")
    if ledger_start is None or ledger_start > start:
        return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE,
                                       reason="LEDGER_DOES_NOT_COVER_PERIOD",
                                       opening_crore=opening.value_crore)
    ok, more = _tax_ok(bases | {opening.tax_basis}, allow)
    if not ok:
        return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE,
                                       reason="TAX_BASIS_NOT_COMPARABLE",
                                       opening_crore=opening.value_crore)
    in_period = [c for c in chains if start <= c.last_updated <= end]
    cancellations = Decimal(0)
    revisions = Decimal(0)
    for chain in in_period:
        if chain.status == ChainStatus.CANCELLED:
            value = awarded_value(chain, orders)
            if value is None:
                return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE,
                                               reason="CANCELLED_VALUE_NOT_DISCLOSED",
                                               opening_crore=opening.value_crore)
            cancellations += value
        if chain.revision_delta_crore is not None:
            revisions += chain.revision_delta_crore
    assert inflow is not None
    implied = opening.value_crore + inflow - revenue.value_crore - cancellations + revisions
    return OrderBookReconciliation(
        **base, status=MetricStatus.AVAILABLE, opening_crore=opening.value_crore,
        inflow_crore=inflow, inflow_source=source, executed_revenue_crore=revenue.value_crore,
        cancellations_crore=cancellations, revisions_crore=revisions,
        implied_closing_crore=implied,
        reported_closing_crore=closing.value_crore if closing else None,
        difference_crore=(closing.value_crore - implied) if closing else None,
        issues=tuple([*notes, *more]),
    )


def build_order_ledger(
    events: Sequence[CorporateEvent],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
) -> tuple[list[OrderRecord], list[OrderChain], OrderBookAnalytics]:
    documents = {d.source_url: d for d in sorted(request.document_texts,
                                                 key=lambda d: d.document_sha256)}
    facts = {f.source_url: f for f in request.order_facts}
    records = [
        extract_order(e, documents.get(e.source_url or ""), facts.get(e.source_url or ""))
        for e in events
        if e.event_type in STAGE_BY_EVENT
    ]
    chains = sorted(build_chains(records), key=lambda c: (c.first_announced, str(c.chain_id)))
    return records, chains, order_book_analytics(chains, records, request, ledger_start)
