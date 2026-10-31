"""Box 12 order / project ledger and order-book analytics.

Stages are never conflated: L1 (lowest bidder) is not an order; a letter of award/acceptance
is an awarded order; a signed contract is the same order at a later stage; revenue is never
an order stage. Each disclosure becomes an OrderRecord; records of one project are linked
into one OrderChain by fixed rules, in this order:
  LETTER_REFERENCE  the filing says "in continuation to our letter dated D" and exactly one
                    earlier chain of the same client was announced on D
  TENDER_REFERENCE  the same explicit tender / contract / LOA / work-order number
  PROJECT_TEXT      same client and identical normalized project description
A monetary value never links two records: equal values are common and prove nothing.
Before any rule, a conservative project fingerprint excludes a chain: a different explicit
project description (unless the same explicit number joins them), or different numbers of
the same kind (two tenders, two packages), keep two projects apart whatever their client,
value or date.

Each chain is then replayed chronologically as a state machine (PENDING_L1 -> CONFIRMED ->
PARTIALLY_CANCELLED / CANCELLED / CANCELLATION_UNRESOLVED -> re-awarded CONFIRMED ...), and
every step is kept as an OrderTransition with its provenance:
  award           the award value enters the book once; a later stage (signed contract)
                  restates the current value, never adds to it
  revision        the revised value replaces the active value; delta = new - previous
  cancellation    FULL removes the whole active value only when the filing says the whole
                  order / LOA / contract is cancelled (or states exactly the active value);
                  PARTIAL removes only an explicitly stated amount or percentage; anything
                  else leaves the remaining value unknown (CANCELLATION_UNRESOLVED), never
                  assumed. A stated amount is subtracted only when it is comparable
                  (currency, tax basis, JV attribution) with the chain's value
  re-award        a later award after a full cancellation reactivates the chain
A JV order counts its attributable share only when the share is explicitly stated.

An L1 chain is reported as truly pending only when the announcement feed is complete for the
whole period after the bid and no later award could belong to it; otherwise its linkage is
UNRESOLVED_LINKAGE or SOURCE_INCOMPLETE and the pending-L1 total is not presented as complete.

Order details come from the filing's title/summary and, when supplied, the already-extracted
text of its attachment - read only by anchored labels from the SEBI disclosure annexure.
Ambiguity leaves a field unknown with an issue. Every ratio, sum and reconciliation passes
`order_comparability.check_comparable` first.
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
    scan_amounts,
    single_amount,
)
from stock_research_ai.application.exchange_intelligence import issue, uid
from stock_research_ai.application.order_comparability import (
    ALL,
    ORDER_VALUES,
    Comparability,
    Dimension,
    Quantity,
    as_of_equals,
    check_comparable,
    same_period,
)
from stock_research_ai.domain.market_intelligence import (
    COUNTED_CHAIN_STATUSES,
    STAGE_ORDER,
    AwardMode,
    CancellationScope,
    ChainStatus,
    ClientCategory,
    CorporateEvent,
    DocumentText,
    EventType,
    Geography,
    Issue,
    L1Linkage,
    L1Resolution,
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
    OrderTransition,
    PeriodRevenue,
    ReportedInflow,
    ReportedOrderBook,
    TaxBasis,
    TransitionKind,
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
#: An explicit identifier: '<kind> No./Number/Ref./ID[:] <id>'. The id must carry a digit.
_REFERENCE = re.compile(
    r"\b(?P<kind>e-?tender|tender|NIT|RFP|bid|contract|agreement|LOA|letter of "
    r"(?:award|acceptance)|work order|package)\s*(?:no|number|ref(?:erence)?|id)\b\.?\s*"
    r"[:.\-–]?\s*(?P<id>[A-Za-z0-9][A-Za-z0-9/._\-]{0,79})",
    _I,
)
_REFERENCE_KIND = {
    "etender": "TENDER", "e-tender": "TENDER", "tender": "TENDER", "nit": "TENDER",
    "rfp": "TENDER", "bid": "TENDER", "contract": "CONTRACT", "agreement": "CONTRACT",
    "loa": "LOA", "letter of award": "LOA", "letter of acceptance": "LOA",
    "work order": "WORK_ORDER", "package": "PACKAGE",
}
#: Explicit scope of a cancellation. PARTIAL wins over FULL wording in the same filing.
_PARTIAL = re.compile(
    r"\bpartial(?:ly)?\b|\bpart of (?:the )?(?:order|contract|work|scope|loa)\b|\bportion of\b"
    r"|\bde-?scop|\bshort[- ]?clos|\breduc(?:tion|ed)\s+(?:in\s+|of\s+)?(?:the\s+)?scope",
    _I,
)
_FULL = re.compile(
    r"cancell?ation of (?:the )?(?:entire |whole )?(?:letter of (?:award|acceptance)|loa|"
    r"work order|order|contract)\b"
    r"|\b(?:order|contract|loa|letter of (?:award|acceptance)|work order)\b[^|]{0,300}?\bhas "
    r"been (?:cancell?ed|terminated)\b"
    r"|termination of (?:the )?(?:entire |whole )?(?:contract|order|loa)\b"
    r"|\b(?:entire|whole|complete|full)\b[^|.]{0,60}\bcancell?",
    _I,
)
_CANCEL_PCT = re.compile(
    r"(\d{1,2}(?:\.\d{1,4})?)\s*%\s*of\s+(?:the\s+)?(?:order|contract|scope|work|loa|value)", _I
)
SIX = Decimal("0.000001")

STAGE_BY_EVENT = {
    EventType.L1_BIDDER: OrderStage.L1_BIDDER,
    EventType.LOA: OrderStage.LOA,
    EventType.ORDER_AWARD: OrderStage.LOA,
    EventType.ORDER_CANCELLATION: OrderStage.CANCELLATION,
    EventType.ORDER_REVISION: OrderStage.REVISION,
}
_AWARD_STAGES = (OrderStage.LOA, OrderStage.SIGNED_CONTRACT)


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


def reference_ids(text: str) -> tuple[str, ...]:
    """Explicit identifiers as 'KIND:ID'. Only safe normalization (case, surrounding
    punctuation); every digit, letter, slash and dash of the id is kept."""
    out = set()
    for match in _REFERENCE.finditer(text[:200_000]):
        kind = _REFERENCE_KIND[" ".join(match.group("kind").lower().split())]
        ident = match.group("id").upper().rstrip("./-_")
        if not any(c.isdigit() for c in ident) or (kind != "PACKAGE" and len(ident) < 4):
            continue
        out.add(f"{kind}:{ident}")
    return tuple(sorted(out))


def _cancellation(text: str) -> tuple[CancellationScope, Decimal | None]:
    if _PARTIAL.search(text):
        shares = {Decimal(p) for p in _CANCEL_PCT.findall(text)}
        share = next(iter(shares)) if len(shares) == 1 else None
        return CancellationScope.PARTIAL, share if share and 0 < share < 100 else None
    if _FULL.search(text):
        return CancellationScope.FULL, None
    return CancellationScope.UNSPECIFIED, None


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
            if scan_amounts(segment)[1]:
                issues.append(issue("MALFORMED_AMOUNT", "An amount under the value label cannot "
                                    "be read as one complete number; no value is taken"))
            else:
                issues.append(issue("AMBIGUOUS_ORDER_VALUE", "Several values under the value "
                                    "label"))
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
            attributable = (gross * share / 100).quantize(SIX)
        else:
            issues.append(issue("JV_SHARE_NOT_STATED", "JV order without a stated share: "
                                "attributable value unknown"))
    scope = pct = None
    if stage == OrderStage.CANCELLATION:
        scope, pct = _cancellation(combined)
        rules.append(f"CANCELLATION_SCOPE_{scope.value}")
    references = reference_ids(combined)
    if references:
        rules.append("REFERENCE_NUMBER_STATED")
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
        reference_ids=references,
        cancellation_scope=scope,
        cancellation_pct=pct,
        source_url=event.source_url,
        document_sha256=document.document_sha256 if document else None,
        extraction_rules=tuple(rules),
        issues=tuple(issues),
    )


# --- linking ----------------------------------------------------------------------------------


def _refs(ids: Sequence[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for ref in ids:
        kind, _, ident = ref.partition(":")
        out.setdefault(kind, set()).add(ident)
    return out


def _linking_ids(refs: dict[str, set[str]]) -> set[str]:
    """Ids that may identify a project. A package number alone is too short to: it only
    separates two packages of one tender."""
    return {i for kind, ids in refs.items() if kind != "PACKAGE" for i in ids}


@dataclass
class _Chain:
    records: list[OrderRecord]
    rules: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    duplicates: list[OrderRecord] = field(default_factory=list)
    projects: set[str] = field(default_factory=set)
    refs: dict[str, set[str]] = field(default_factory=dict)
    dates: set[date] = field(default_factory=set)

    def __post_init__(self) -> None:
        for record in list(self.records):
            self._index(record)

    def add(self, record: OrderRecord) -> None:
        self.records.append(record)
        self._index(record)

    def _index(self, record: OrderRecord) -> None:
        self.dates.add(record.announcement_date)
        if record.project:
            self.projects.add(norm_key(record.project))
        for kind, ids in _refs(record.reference_ids).items():
            self.refs.setdefault(kind, set()).update(ids)

    @property
    def client_key(self) -> str | None:
        return next((r.client_key for r in self.records if r.client_key), None)

    @property
    def live(self) -> list[OrderRecord]:
        duplicates = {r.order_id for r in self.duplicates}
        return [r for r in self.records if r.order_id not in duplicates]


def fingerprint_conflict(
    project: str | None, refs: dict[str, set[str]],
    other_projects: set[str], other_refs: dict[str, set[str]],
) -> str | None:
    """Why two sets of disclosures cannot be the same project (None = no contradiction).
    Different explicit numbers of one kind always separate; different explicit project
    texts separate unless an explicit number joins them."""
    for kind, ids in refs.items():
        theirs = other_refs.get(kind)
        if theirs and ids.isdisjoint(theirs):
            return f"{kind}_REFERENCE_CONFLICT"
    joined = bool(_linking_ids(refs) & _linking_ids(other_refs))
    if project and other_projects and norm_key(project) not in other_projects and not joined:
        return "PROJECT_TEXT_CONFLICT"
    return None


def _conflict(record: OrderRecord, chain: _Chain) -> str | None:
    return fingerprint_conflict(record.project, _refs(record.reference_ids), chain.projects,
                                chain.refs)


def _link(
    record: OrderRecord, pool: Sequence[_Chain]
) -> tuple[_Chain | None, str | None, bool, list[str]]:
    """(chain, rule, ambiguous, blocked-by codes). Value never links."""
    if record.stage == OrderStage.L1_BIDDER and not record.referenced_letter_dates:
        return None, None, False, []
    record_ids = _linking_ids(_refs(record.reference_ids))
    tests: list[tuple[str, Any]] = [
        ("LETTER_REFERENCE", lambda c: any(d in c.dates for d in record.referenced_letter_dates)),
        ("TENDER_REFERENCE", lambda c: bool(record_ids & _linking_ids(c.refs))),
        ("PROJECT_TEXT", lambda c: record.project is not None
         and norm_key(record.project) in c.projects),
    ]
    candidates = [c for c in pool if c.records[-1].announcement_date <= record.announcement_date]
    open_pool: list[_Chain] = []
    blocked: list[str] = []
    for chain in candidates:
        why = _conflict(record, chain)
        if why is None:
            open_pool.append(chain)
        elif any(test(chain) for _, test in tests):
            blocked.append(why)  # a reference points at a contradicting project
    for rule, test in tests:
        hits = [c for c in open_pool if test(c)]
        if len(hits) == 1:
            return hits[0], rule, False, blocked
        if len(hits) > 1:
            # narrow by the remaining strong tests, in order (never by value)
            for _, narrower in tests:
                narrowed = [c for c in hits if narrower(c)]
                if len(narrowed) == 1:
                    return narrowed[0], f"{rule}_NARROWED", False, blocked
            return None, None, True, blocked
    return None, None, False, blocked


def _link_all(records: Sequence[OrderRecord]) -> list[_Chain]:
    chains: list[_Chain] = []
    by_client: dict[str, list[_Chain]] = {}
    for record in sorted(records, key=lambda r: (r.announcement_date, str(r.event_id))):
        pool = by_client.get(record.client_key, []) if record.client_key else []
        chain, rule, ambiguous, blocked = _link(record, pool)
        if chain is None:
            new = _Chain(records=[record])
            if ambiguous:
                new.issues.append(issue("AMBIGUOUS_ORDER_LINK", "Several earlier orders match; "
                                        "not linked (counted separately, flagged)"))
            for code in sorted(set(blocked)):
                new.issues.append(issue("ORDER_LINK_BLOCKED", f"A referenced earlier disclosure "
                                        f"describes a different project ({code}); not linked"))
            chains.append(new)
            if record.client_key:
                by_client.setdefault(record.client_key, []).append(new)
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
                chain.add(record)
                continue
        chain.add(record)
        chain.rules.append(rule)
    return chains


# --- chronological state machine ----------------------------------------------------------------


@dataclass
class _Machine:
    jv: bool
    jv_share: Decimal | None
    status: ChainStatus | None = None
    active: Decimal | None = None
    currency: str | None = None
    tax: TaxBasis = TaxBasis.UNKNOWN
    l1_value: Decimal | None = None
    l1_currency: str | None = None
    l1_tax: TaxBasis = TaxBasis.UNKNOWN
    cancelled: Decimal = Decimal(0)
    cancelled_known: bool = True
    cancel_amounts: int = 0
    revision_delta: Decimal | None = None
    revision_unknown: bool = False
    revised: bool = False
    had_award: bool = False
    reawarded: bool = False
    episode: int | None = None  # index of the current AWARD / REAWARD transition
    stated: set[Decimal] = field(default_factory=set)  # award / revision values seen
    steps: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def emit(self, r: OrderRecord, kind: TransitionKind, rule: str, *, before: Decimal | None,
             after: Decimal | None, amount: Decimal | None, currency: str | None = None,
             tax: TaxBasis | None = None) -> None:
        self.steps.append({
            "order_id": r.order_id, "effective_date": r.announcement_date, "stage": r.stage,
            "kind": kind, "status_after": self.status, "value_before_crore": before,
            "value_after_crore": after, "amount_crore": amount,
            "currency": currency if currency is not None else self.currency,
            "tax_basis": tax if tax is not None else self.tax, "rule": rule,
        })

    def note(self, code: str, message: str, severity: str = "WARNING") -> None:
        self.issues.append(issue(code, message, severity=severity))

    # -- stages --
    def l1(self, r: OrderRecord) -> None:
        if self.status in (None, ChainStatus.CANCELLED):
            self.status = ChainStatus.PENDING_L1
            self.active = None
            if r.gross_value_crore is not None:
                self.l1_value, self.l1_currency = r.gross_value_crore, r.value_currency
                self.l1_tax = r.value_tax_basis
            self.emit(r, TransitionKind.L1, "LOWEST_BIDDER", before=None, after=None,
                      amount=r.gross_value_crore, currency=r.value_currency,
                      tax=r.value_tax_basis)
        elif self.status == ChainStatus.PENDING_L1:
            if r.gross_value_crore is not None:
                self.l1_value, self.l1_currency = r.gross_value_crore, r.value_currency
                self.l1_tax = r.value_tax_basis
            self.emit(r, TransitionKind.L1, "LOWEST_BIDDER_RESTATED", before=None, after=None,
                      amount=r.gross_value_crore, currency=r.value_currency,
                      tax=r.value_tax_basis)
        else:
            self.emit(r, TransitionKind.IGNORED, "L1_AFTER_AWARD", before=self.active,
                      after=self.active, amount=None)

    def award(self, r: OrderRecord) -> None:
        value = r.gross_value_crore
        if self.status in (None, ChainStatus.PENDING_L1, ChainStatus.CANCELLED):
            kind = TransitionKind.REAWARD if self.had_award else TransitionKind.AWARD
            self.status = ChainStatus.CONFIRMED
            self.active, self.currency, self.tax = value, r.value_currency, r.value_tax_basis
            self.had_award = True
            self.reawarded = self.reawarded or kind == TransitionKind.REAWARD
            if value is not None:
                self.stated.add(value)
            self.episode = len(self.steps)
            self.emit(r, kind, "REAWARD_AFTER_CANCELLATION" if kind == TransitionKind.REAWARD
                      else "AWARD", before=None, after=value, amount=value)
            return
        if value is None:
            self.emit(r, TransitionKind.STAGE, "STAGE_WITHOUT_VALUE", before=self.active,
                      after=self.active, amount=None)
            return
        if self.status != ChainStatus.CONFIRMED:
            self.note("STAGE_VALUE_AFTER_CANCELLATION_NOT_APPLIED", "A later stage states a "
                      "value after a cancellation; the remaining value is not replaced")
            self.emit(r, TransitionKind.IGNORED, "STAGE_VALUE_AFTER_CANCELLATION_NOT_APPLIED",
                      before=self.active, after=self.active, amount=None)
            return
        self._fill_award(r)
        before = self.active
        self.active, self.currency, self.tax = value, r.value_currency, r.value_tax_basis
        self.stated.add(value)
        self.emit(r, TransitionKind.STAGE, "VALUE_AT_LATEST_CONFIRMED_STAGE", before=before,
                  after=value, amount=None)

    def _fill_award(self, r: OrderRecord) -> None:
        """The award value is the first value stated for the award (LOA or later stage)."""
        if self.episode is not None and self.steps[self.episode]["amount_crore"] is None:
            self.steps[self.episode].update({
                "amount_crore": r.gross_value_crore, "value_after_crore": r.gross_value_crore,
                "currency": r.value_currency, "tax_basis": r.value_tax_basis,
            })

    def duplicate(self, r: OrderRecord) -> None:
        if (r.stage in _AWARD_STAGES and self.status == ChainStatus.CONFIRMED
                and self.active is None and r.gross_value_crore is not None):
            # the same award restated with its value: the value becomes known, once
            self._fill_award(r)
            self.active, self.currency, self.tax = (r.gross_value_crore, r.value_currency,
                                                    r.value_tax_basis)
            self.stated.add(r.gross_value_crore)
        self.emit(r, TransitionKind.DUPLICATE, "DUPLICATE_STAGE", before=self.active,
                  after=self.active, amount=None)

    def _comparable(self, r: OrderRecord) -> Comparability:
        check = check_comparable(
            [Quantity("chain", self.active, self.currency, self.tax),
             Quantity("stated", r.gross_value_crore, r.value_currency, r.value_tax_basis)],
            ORDER_VALUES, unknown_matches_unknown=True,
        )
        if r.stage == OrderStage.CANCELLATION and r.joint_venture and (
            not self.jv or (r.jv_share_pct is not None and r.jv_share_pct != self.jv_share)
        ):
            return Comparability(reasons=(*check.reasons, "JV_ATTRIBUTION_MISMATCH"))
        return check

    def cancel(self, r: OrderRecord) -> None:
        scope = r.cancellation_scope or CancellationScope.UNSPECIFIED
        if self.status is None:
            self.emit(r, TransitionKind.IGNORED, "CANCELLATION_OUTSIDE_LEDGER", before=None,
                      after=None, amount=None)
            return
        if self.status == ChainStatus.CANCELLED:
            self.emit(r, TransitionKind.IGNORED, "ALREADY_CANCELLED", before=None, after=None,
                      amount=None)
            return
        if self.status == ChainStatus.PENDING_L1:
            if scope == CancellationScope.FULL:
                self.status = ChainStatus.CANCELLED
                self.emit(r, TransitionKind.FULL_CANCELLATION, "BID_CANCELLED", before=None,
                          after=None, amount=None)
            else:
                self.status = ChainStatus.CANCELLATION_UNRESOLVED
                self.emit(r, TransitionKind.UNRESOLVED_CANCELLATION, "BID_CANCELLATION_SCOPE_"
                          "NOT_STATED", before=None, after=None, amount=None)
            return
        active = self.active
        stated = r.gross_value_crore
        amount: Decimal | None = None
        if stated is not None:
            check = self._comparable(r)
            if check.ok:
                amount = stated
            elif active is not None:
                self.note("CANCELLATION_NOT_COMPARABLE", "The cancelled amount is not "
                          f"comparable with the order ({', '.join(check.reasons)}); not "
                          "subtracted")
        if scope == CancellationScope.PARTIAL:
            part = None
            if r.cancellation_pct is not None and active is not None:
                part = (active * r.cancellation_pct / 100).quantize(SIX)
            elif amount is not None and active is not None:
                if amount >= active:
                    return self._unresolved(r, "PARTIAL_CANCELLATION_NOT_BELOW_ORDER_VALUE")
                part = amount
            if part is None:
                self.status = ChainStatus.PARTIALLY_CANCELLED
                self.active, self.cancelled_known = None, False
                self.note("PARTIAL_CANCELLATION_VALUE_UNKNOWN", "A part of the order is "
                          "cancelled but the part is not stated comparably: remaining value "
                          "unknown")
                self.emit(r, TransitionKind.PARTIAL_CANCELLATION, "PARTIAL_AMOUNT_NOT_STATED",
                          before=active, after=None, amount=None)
                return
            return self._partial(r, part, "PARTIAL_PERCENT_STATED"
                                 if r.cancellation_pct is not None else "PARTIAL_AMOUNT_STATED")
        if scope == CancellationScope.FULL:
            if (amount is not None and active is not None and amount != active
                    and amount not in self.stated):
                return self._unresolved(r, "CANCELLED_VALUE_CONFLICTS_WITH_ORDER")
            if amount is not None and active is not None and amount != active:
                self.note("CANCELLATION_RESTATES_EARLIER_VALUE", "The whole order is cancelled; "
                          "the filing restates an earlier value of it", severity="INFO")
            return self._full(r, "WHOLE_ORDER_CANCELLED")
        # UNSPECIFIED: only an explicit comparable amount decides
        if amount is not None and active is not None:
            if amount == active:
                return self._full(r, "STATED_AMOUNT_EQUALS_ORDER_VALUE")
            if amount < active:
                return self._partial(r, amount, "STATED_AMOUNT_BELOW_ORDER_VALUE")
            return self._unresolved(r, "CANCELLED_VALUE_CONFLICTS_WITH_ORDER")
        return self._unresolved(r, "CANCELLATION_SCOPE_NOT_STATED")

    def _partial(self, r: OrderRecord, part: Decimal, rule: str) -> None:
        before = self.active
        assert before is not None
        self.status = ChainStatus.PARTIALLY_CANCELLED
        self.active = before - part
        self.cancelled += part
        self.cancel_amounts += 1
        self.emit(r, TransitionKind.PARTIAL_CANCELLATION, rule, before=before,
                  after=self.active, amount=part)

    def _full(self, r: OrderRecord, rule: str) -> None:
        before = self.active
        self.status = ChainStatus.CANCELLED
        if before is None:
            self.cancelled_known = False
        else:
            self.cancelled += before
            self.cancel_amounts += 1
        self.active = None
        self.emit(r, TransitionKind.FULL_CANCELLATION, rule, before=before,
                  after=Decimal(0) if before is not None else None, amount=before)

    def _unresolved(self, r: OrderRecord, rule: str) -> None:
        before = self.active
        self.status = ChainStatus.CANCELLATION_UNRESOLVED
        self.active, self.cancelled_known = None, False
        self.note("CANCELLATION_UNRESOLVED", f"Cancellation scope or amount cannot be "
                  f"determined ({rule}); remaining value unknown, never assumed")
        self.emit(r, TransitionKind.UNRESOLVED_CANCELLATION, rule, before=before, after=None,
                  amount=None)

    def revise(self, r: OrderRecord) -> None:
        self.revised = True
        if self.status is None:
            self.emit(r, TransitionKind.IGNORED, "REVISION_OUTSIDE_LEDGER", before=None,
                      after=None, amount=None)
            return
        if self.status not in COUNTED_CHAIN_STATUSES:
            rule = ("REVISION_AFTER_FULL_CANCELLATION" if self.status == ChainStatus.CANCELLED
                    else "REVISION_NOT_APPLICABLE")
            self.note(rule, "A revision of an order that is not active; no value applied")
            self.emit(r, TransitionKind.IGNORED, rule, before=self.active, after=self.active,
                      amount=None)
            return
        value = r.gross_value_crore
        if value is None:
            self.emit(r, TransitionKind.REVISION, "REVISION_VALUE_NOT_STATED",
                      before=self.active, after=self.active, amount=None)
            return
        before = self.active
        check = self._comparable(r)
        delta = None
        if before is not None and check.ok:
            delta = value - before
            self.revision_delta = (self.revision_delta or Decimal(0)) + delta
            rule = "REVISED_VALUE_REPLACES_PREVIOUS"
        else:
            self.revision_unknown = True
            rule = "REVISION_NOT_COMPARABLE"
            self.note(rule, "The revised value cannot be compared with the previous value "
                      f"({', '.join(check.reasons)}); it replaces it, delta unknown")
        self.active, self.currency, self.tax = value, r.value_currency, r.value_tax_basis
        self.stated.add(value)
        self.emit(r, TransitionKind.REVISION, rule, before=before, after=value, amount=delta)


def _run(chain: _Chain) -> _Machine:
    awards = [r for r in chain.live if r.stage in _AWARD_STAGES]
    shares = {r.jv_share_pct for r in awards if r.jv_share_pct is not None}
    machine = _Machine(jv=any(r.joint_venture for r in awards),
                       jv_share=next(iter(shares)) if len(shares) == 1 else None)
    duplicates = {r.order_id for r in chain.duplicates}
    for r in chain.records:
        if r.order_id in duplicates:
            machine.duplicate(r)
        elif r.stage == OrderStage.L1_BIDDER:
            machine.l1(r)
        elif r.stage in _AWARD_STAGES:
            machine.award(r)
        elif r.stage == OrderStage.CANCELLATION:
            machine.cancel(r)
        else:
            machine.revise(r)
    return machine


# --- L1 linkage -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerCoverage:
    """Whether the announcement feed can show every later award: some exchange on which the
    listing is verified answered completely (SEBI LODR Reg. 30 requires every disclosure on
    each exchange where the company is listed) and nothing was truncated."""

    complete: bool
    ledger_start: date | None
    reasons: tuple[str, ...] = ()


def _strong_key(record: OrderRecord) -> bool:
    return record.project is not None or bool(_linking_ids(_refs(record.reference_ids)))


@dataclass(frozen=True)
class _Print:
    """A record's project identity, computed once: normalized project text and explicit
    numbers by kind (PACKAGE included) plus the numbers that may identify a project."""

    project: str | None
    refs: dict[str, set[str]]
    ids: frozenset[str]


def _print(record: OrderRecord) -> _Print:
    refs = _refs(record.reference_ids)
    return _Print(project=norm_key(record.project) if record.project else None, refs=refs,
                  ids=frozenset(_linking_ids(refs)))


def _may_be_award_of(bid: OrderRecord, award: OrderRecord, bp: _Print, ap: _Print) -> bool:
    """True unless explicit project-identity evidence shows the award belongs to another
    project: a different project text or explicit number (the `fingerprint_conflict` rules),
    or an explicit continuation of another filing. A parsed client name is noisy text
    ('M. P' vs 'Madhya Pradesh Paschim Kshetra ...') and never rules an award out by itself.
    An award that explicitly continues the bid's own letter stays possible even when
    extracted text or numbers disagree: the disagreement leaves the bid unresolved, never
    pending."""
    if award.announcement_date < bid.announcement_date:
        return False
    if bid.announcement_date in award.referenced_letter_dates:
        return True
    for kind, ids in bp.refs.items():
        theirs = ap.refs.get(kind)
        if theirs and ids.isdisjoint(theirs):
            return False  # different explicit numbers of one kind: another project
    joined = bool(bp.ids & ap.ids)
    if bp.project and ap.project and bp.project != ap.project and not joined:
        return False  # different explicit project texts: another project
    if joined or (bp.project is not None and bp.project == ap.project):
        return True
    # A different client string alone proves nothing; only an explicit continuation of
    # another filing rules the award out here.
    return not award.referenced_letter_dates


def _l1_linkage(
    chain: _Chain, machine: _Machine, unexplained: Sequence[tuple[OrderRecord, _Print]],
    coverage: LedgerCoverage,
) -> L1Linkage | None:
    bids = [r for r in chain.live if r.stage == OrderStage.L1_BIDDER]
    if not bids:
        return None
    bid = bids[-1]
    searched = coverage.ledger_start is not None
    document = bid.document_sha256 is not None
    strong = _strong_key(bid)
    if machine.had_award:
        return L1Linkage(resolution=L1Resolution.LINKED_TO_AWARD,
                         follow_up_search_performed=searched,
                         event_coverage_complete=coverage.complete,
                         document_text_available=document, strong_link_key=strong,
                         reasons=("AWARD_LINKED",))
    if machine.status != ChainStatus.PENDING_L1:
        return None
    covered = (coverage.complete and coverage.ledger_start is not None
               and coverage.ledger_start <= bid.announcement_date)
    bp = _print(bid)
    candidates = [a for a, ap in unexplained if _may_be_award_of(bid, a, bp, ap)]
    reasons: list[str] = []
    if not covered:
        reasons += list(coverage.reasons) or ["LEDGER_DOES_NOT_COVER_BID"]
    if not document:
        reasons.append("L1_DOCUMENT_TEXT_ABSENT")
    if not strong:
        reasons.append("NO_STRONG_LINK_KEY")
    if bid.client_key is None:
        reasons.append("CLIENT_NOT_IDENTIFIED")
    if candidates:
        reasons.append("LATER_AWARD_NEITHER_LINKED_NOR_RULED_OUT")
    resolution = (L1Resolution.SOURCE_INCOMPLETE if not covered
                  else L1Resolution.UNRESOLVED_LINKAGE if candidates
                  else L1Resolution.TRUE_PENDING)
    if resolution == L1Resolution.TRUE_PENDING:
        reasons.append("NO_LATER_AWARD_FOUND")
    return L1Linkage(
        resolution=resolution, follow_up_search_performed=searched,
        event_coverage_complete=covered, document_text_available=document,
        strong_link_key=strong, unresolved_award_candidates=len(candidates),
        candidate_order_ids=tuple(a.order_id for a in candidates[:50]),
        reasons=tuple(dict.fromkeys(reasons)),
    )


# --- chains -----------------------------------------------------------------------------------


def build_chains(
    records: Sequence[OrderRecord], coverage: LedgerCoverage | None = None
) -> list[OrderChain]:
    coverage = coverage or LedgerCoverage(complete=False, ledger_start=None,
                                          reasons=("COVERAGE_NOT_ASSESSED",))
    chains = _link_all(records)
    machines = [_run(c) for c in chains]
    # awards whose chain has no bid: a pending bid may be one of these, unless ruled out
    unexplained = [
        (c.live[0], _print(c.live[0])) for c in chains
        if c.live and c.live[0].stage in _AWARD_STAGES
        and not any(r.stage == OrderStage.L1_BIDDER for r in c.live)
    ]
    return [_finish(c, m, _l1_linkage(c, m, unexplained, coverage))
            for c, m in zip(chains, machines, strict=True)]


def _finish(chain: _Chain, machine: _Machine, linkage: L1Linkage | None) -> OrderChain:
    records = chain.records
    live = chain.live
    first = records[0]
    issues = [*chain.issues, *machine.issues]
    confirmed = [r for r in live if r.stage in _AWARD_STAGES]
    status = machine.status
    if status is None:
        status = (ChainStatus.UNLINKED_CANCELLATION
                  if any(r.stage == OrderStage.CANCELLATION for r in live)
                  else ChainStatus.UNLINKED_REVISION)
        issues.append(issue("UNLINKED_ORDER_CHANGE", "Change to an order not found in the "
                            "ledger window; no value counted"))
    counted = status in COUNTED_CHAIN_STATUSES
    gross = machine.active if counted else None
    attributable = None
    if gross is not None:
        if not machine.jv:
            attributable = gross
        elif machine.jv_share is not None:
            attributable = (gross * machine.jv_share / 100).quantize(SIX)
    valued = [r for r in confirmed if r.gross_value_crore is not None]
    currencies = {r.value_currency for r in live if r.value_currency}
    if len(currencies) > 1:
        issues.append(issue("MIXED_ORDER_CURRENCY", "Stages state different currencies"))
    l1_values = [r.gross_value_crore for r in live
                 if r.stage == OrderStage.L1_BIDDER and r.gross_value_crore is not None]
    if valued and l1_values and l1_values[-1] != valued[0].gross_value_crore:
        issues.append(issue("L1_VALUE_DIFFERS_FROM_AWARD", "Award value differs from the L1 "
                            "value; the award value is used", severity="INFO"))
    if status == ChainStatus.CONFIRMED and first.client_key is None:
        issues.append(issue("CLIENT_NOT_IDENTIFIED", "No client stated: this order cannot be "
                            "linked to its other stages and may be counted with them"))
    if status == ChainStatus.CONFIRMED and gross is None:
        issues.append(issue("ORDER_VALUE_NOT_DISCLOSED", "Confirmed order without a stated value",
                            severity="INFO"))
    if linkage is not None and linkage.resolution in (L1Resolution.UNRESOLVED_LINKAGE,
                                                      L1Resolution.SOURCE_INCOMPLETE):
        issues.append(issue(f"L1_{linkage.resolution.value}", "Whether this bid was later "
                            "awarded cannot be established; not reported as truly pending"))
    geos = {r.geography for r in live if r.geography != Geography.UNKNOWN}
    modes = {r.award_mode for r in live if r.award_mode != AwardMode.UNKNOWN}
    cats = {r.client_category for r in live if r.client_category != ClientCategory.UNKNOWN}
    if any(r.stage == OrderStage.L1_BIDDER for r in live) and AwardMode.NOMINATION not in modes:
        modes.add(AwardMode.COMPETITIVE_BIDDING)
    note = {
        ChainStatus.CONFIRMED: "COUNTED_ONCE_AT_LATEST_CONFIRMED_STAGE",
        ChainStatus.PARTIALLY_CANCELLED: "COUNTED_REMAINING_AFTER_PARTIAL_CANCELLATION",
        ChainStatus.PENDING_L1: "L1_NOT_AN_ORDER_REPORTED_SEPARATELY",
        ChainStatus.CANCELLED: "CANCELLED_EXCLUDED",
        ChainStatus.CANCELLATION_UNRESOLVED: "CANCELLATION_UNRESOLVED_NOT_COUNTED",
        ChainStatus.UNLINKED_CANCELLATION: "UNLINKED_CHANGE_NOT_COUNTED",
        ChainStatus.UNLINKED_REVISION: "UNLINKED_CHANGE_NOT_COUNTED",
    }[status]
    tax = {r.value_tax_basis for r in valued}
    steps = [{**s, "status_after": s["status_after"] or status} for s in machine.steps]
    return OrderChain(
        chain_id=uid("chain", first.order_id),
        client=next((r.client for r in records if r.client), None),
        project=next((r.project for r in records if r.project), None),
        order_ids=tuple(r.order_id for r in records),
        stages=tuple(r.stage for r in records),
        link_rules=tuple(chain.rules),
        status=status,
        revised=machine.revised,
        reawarded=machine.reawarded,
        first_announced=first.announcement_date,
        confirmed_on=confirmed[0].announcement_date if confirmed else None,
        last_updated=records[-1].announcement_date,
        value_currency=next(iter(currencies)) if len(currencies) == 1 else None,
        value_tax_basis=next(iter(tax)) if len(tax) == 1 else TaxBasis.UNKNOWN,
        pending_l1_value_crore=machine.l1_value if status == ChainStatus.PENDING_L1 else None,
        confirmed_gross_value_crore=gross,
        confirmed_attributable_value_crore=attributable,
        cancelled_value_crore=machine.cancelled
        if machine.cancel_amounts and machine.cancelled_known else None,
        revision_delta_crore=None if machine.revision_unknown else machine.revision_delta,
        geography=next(iter(geos)) if len(geos) == 1 else Geography.UNKNOWN,
        award_mode=next(iter(modes)) if len(modes) == 1 else AwardMode.UNKNOWN,
        client_category=next(iter(cats)) if len(cats) == 1 else ClientCategory.UNKNOWN,
        counted_in_confirmed=counted,
        counting_note=note,
        transitions=tuple(OrderTransition.model_validate(s) for s in steps),
        l1_linkage=linkage,
        issues=tuple(issues),
    )


# --- analytics --------------------------------------------------------------------------------

K = OrderBookMetricKind
Completeness = Literal["DISCLOSED_ORDERS_ONLY", "REPORTED", "MIXED", "NOT_APPLICABLE"]
_AWARD_KINDS = (TransitionKind.AWARD, TransitionKind.REAWARD)
_CANCEL_KINDS = (TransitionKind.PARTIAL_CANCELLATION, TransitionKind.FULL_CANCELLATION)


def _metric(kind: OrderBookMetricKind, unit: str, method: str, completeness: Completeness,
            *, value: Decimal | None = None, reason: str | None = None,
            start: date | None = None, end: date | None = None, chains: int = 0,
            issues: Sequence[str] = ()) -> OrderBookMetric:
    return OrderBookMetric.model_validate({
        "kind": kind, "status": "AVAILABLE" if reason is None else "UNAVAILABLE",
        "value": value.quantize(SIX) if value is not None and reason is None else None,
        "unit": unit, "method": method, "completeness": completeness, "reason": reason,
        "period_start": start, "period_end": end, "chains": chains,
        "issues": tuple(dict.fromkeys(issues)),
    })


def fiscal_bounds(fy: int) -> tuple[date, date]:
    return date(fy - 1, 4, 1), date(fy, 3, 31)


def _book(b: ReportedOrderBook) -> Quantity:
    return Quantity("order_book", b.value_crore, b.currency, b.tax_basis, b.basis)


def _revenue(r: PeriodRevenue) -> Quantity:
    return Quantity("revenue", r.value_crore, r.currency, r.tax_basis, r.basis)


def _reported(i: ReportedInflow) -> Quantity:
    return Quantity("reported_inflow", i.value_crore, i.currency, i.tax_basis)


def _step(t: OrderTransition) -> Quantity:
    return Quantity(t.kind.value.lower(), t.amount_crore, t.currency, t.tax_basis)


def _sum_check(quantities: Sequence[Quantity]) -> str | None:
    """A sum of disclosed order values: one currency and one tax basis (all-unknown is a
    'disclosed orders only' sum, flagged by the caller)."""
    check = check_comparable(quantities, (Dimension.CURRENCY, Dimension.TAX, Dimension.UNIT),
                             unknown_matches_unknown=True)
    if not check.ok:
        return "MIXED_TAX_BASIS" if check.reason == "TAX_BASIS_NOT_COMPARABLE" else check.reason
    return None


def _awards(chain: OrderChain, start: date, end: date) -> list[OrderTransition]:
    return [t for t in chain.transitions
            if t.kind in _AWARD_KINDS and start <= t.effective_date <= end]


@dataclass(frozen=True)
class _Inflow:
    value: Decimal | None
    quantity: Quantity | None
    source: Literal["REPORTED", "DISCLOSED_ORDERS_ONLY"]
    chains: int
    reason: str | None
    notes: tuple[str, ...] = ()

    @property
    def taxes(self) -> set[TaxBasis]:
        return {self.quantity.tax} if self.quantity is not None else set()


def _ledger_inflow(
    chains: Sequence[OrderChain], start: date, end: date, ledger_start: date | None,
) -> _Inflow:
    """Award (and re-award) values whose award falls in [start, end], from the ledger."""
    if ledger_start is None or ledger_start > start:
        return _Inflow(None, None, "DISCLOSED_ORDERS_ONLY", 0, "LEDGER_DOES_NOT_COVER_PERIOD")
    picked = [(c, t) for c in chains for t in _awards(c, start, end)]
    n = len({c.chain_id for c, _ in picked})
    if any(t.amount_crore is None for _, t in picked):
        return _Inflow(None, None, "DISCLOSED_ORDERS_ONLY", n,
                       "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS")
    if any(t.currency != "INR" for _, t in picked):
        return _Inflow(None, None, "DISCLOSED_ORDERS_ONLY", n, "NON_INR_ORDER_VALUES")
    taxes = {t.tax_basis for _, t in picked}
    notes = tuple(f"TAX_BASIS_{b.value}" for b in sorted(taxes))
    if _sum_check([_step(t) for _, t in picked]):
        return _Inflow(None, None, "DISCLOSED_ORDERS_ONLY", n, "MIXED_TAX_BASIS", notes)
    total = sum((t.amount_crore for _, t in picked if t.amount_crore is not None), Decimal(0))
    tax = next(iter(taxes)) if taxes else TaxBasis.EXCLUSIVE_OF_TAX  # nothing awarded: zero
    return _Inflow(total, Quantity("ledger_inflow", total, "INR", tax), "DISCLOSED_ORDERS_ONLY",
                   n, None, notes)


def _inflow(
    chains: Sequence[OrderChain], request: MarketIntelligenceRequest, start: date, end: date,
    ledger_start: date | None, target: Quantity | None,
) -> _Inflow:
    """A reported inflow for exactly [start, end] wins only when it is comparable with the
    calculation's target; otherwise the ledger's disclosed inflow is used, flagged."""
    reported = next((i for i in request.reported_inflows
                     if same_period((i.period_start, i.period_end), (start, end)) is None), None)
    notes: tuple[str, ...] = ()
    if reported is not None:
        quantity = _reported(reported)
        check = check_comparable([quantity, target], (Dimension.CURRENCY, Dimension.TAX),
                                 allow_tax_policy=request.allow_tax_inclusive_order_values) \
            if target is not None else None
        if check is None or check.ok:
            return _Inflow(reported.value_crore, quantity, "REPORTED", 0, None,
                           check.notes if check else ())
        notes = ("REPORTED_INFLOW_NOT_COMPARABLE", *(f"REPORTED_INFLOW_{r}"
                                                     for r in check.reasons))
    ledger = _ledger_inflow(chains, start, end, ledger_start)
    return _Inflow(ledger.value, ledger.quantity, ledger.source, ledger.chains, ledger.reason,
                   (*notes, *ledger.notes))


def _pending_metrics(
    chains: Sequence[OrderChain], t_start: date, today: date, covered: bool,
) -> list[OrderBookMetric]:
    pending = [c for c in chains if c.status == ChainStatus.PENDING_L1
               and c.first_announced >= t_start]
    true = [c for c in pending if c.l1_linkage is not None
            and c.l1_linkage.resolution == L1Resolution.TRUE_PENDING]
    unresolved = [c for c in pending if c not in true]

    def total(group: list[OrderChain]) -> tuple[Decimal, str | None]:
        if not covered:
            return Decimal(0), "LEDGER_DOES_NOT_COVER_PERIOD"
        if any(c.pending_l1_value_crore is None for c in group):
            return Decimal(0), "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
        steps = [t for c in group for t in c.transitions if t.kind == TransitionKind.L1
                 and t.amount_crore is not None]
        why = _sum_check([_step(t) for t in steps]) if steps else None
        return sum((c.pending_l1_value_crore or Decimal(0) for c in group), Decimal(0)), why

    value, reason = total(pending)
    if reason is None and unresolved:
        reason = "PENDING_L1_LINKAGE_UNRESOLVED"
    counts = [f"UNRESOLVED_L1_CHAINS_{len(unresolved)}"] if unresolved else []
    true_value, true_reason = total(true)
    open_value, open_reason = total(unresolved)
    return [
        _metric(K.DISCLOSED_PENDING_L1_VALUE, "INR_CRORE",
                "SUM_L1_VALUE_OF_OPEN_L1_CHAINS_LAST_365D (ONLY WHEN EVERY BID'S LINKAGE IS "
                "RESOLVED)", "DISCLOSED_ORDERS_ONLY", value=value, reason=reason,
                start=t_start, end=today, chains=len(pending),
                issues=["L1_IS_NOT_CONFIRMED_ORDER_BOOK", *counts]),
        _metric(K.PENDING_L1_TRUE_PENDING_VALUE, "INR_CRORE",
                "SUM_L1_VALUE_OF_BIDS_WITH_COMPLETE_COVERAGE_AND_NO_LATER_AWARD_LAST_365D",
                "DISCLOSED_ORDERS_ONLY", value=true_value, reason=true_reason, start=t_start,
                end=today, chains=len(true),
                issues=["L1_IS_NOT_CONFIRMED_ORDER_BOOK",
                        *(["EXCLUDES_UNRESOLVED_L1_CHAINS"] if unresolved else [])]),
        _metric(K.PENDING_L1_UNRESOLVED_VALUE, "INR_CRORE",
                "SUM_L1_VALUE_OF_BIDS_WHOSE_AWARD_STATUS_IS_UNKNOWN_LAST_365D",
                "DISCLOSED_ORDERS_ONLY", value=open_value, reason=open_reason, start=t_start,
                end=today, chains=len(unresolved),
                issues=["AWARD_STATUS_UNKNOWN_MAY_ALREADY_BE_IN_CONFIRMED_INFLOW"]),
    ]


def order_book_analytics(
    chains: Sequence[OrderChain],
    orders: Sequence[OrderRecord],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
) -> OrderBookAnalytics:
    allow = request.allow_tax_inclusive_order_values
    today = request.as_of.date()
    t_start = today - timedelta(days=364)
    metrics: list[OrderBookMetric] = []

    inflow = _ledger_inflow(chains, t_start, today, ledger_start)
    metrics.append(_metric(
        K.DISCLOSED_CONFIRMED_INFLOW, "INR_CRORE",
        "SUM_AWARD_VALUE_OF_AWARDS_IN_LAST_365D", "DISCLOSED_ORDERS_ONLY",
        value=inflow.value, reason=inflow.reason, start=t_start, end=today, chains=inflow.chains,
        issues=inflow.notes,
    ))
    covered = ledger_start is not None and ledger_start <= t_start
    metrics += _pending_metrics(chains, t_start, today, covered)

    annual = sorted(
        (r for r in request.period_revenues if 360 <= (r.period_end - r.period_start).days <= 370),
        key=lambda r: r.period_end,
    )
    latest = annual[-1] if annual else None
    books = sorted(request.reported_order_books, key=lambda b: b.as_of)

    # order book / revenue: the book at exactly the revenue period's end (never the closest)
    method = "ORDER_BOOK_AT_REVENUE_PERIOD_END / LATEST_ANNUAL_REVENUE"
    if not books or latest is None:
        metrics.append(_metric(K.ORDER_BOOK_TO_REVENUE, "RATIO", method, "REPORTED",
                               reason="INPUT_NOT_SUPPLIED"))
    else:
        book = next((b for b in books if as_of_equals(b.as_of, latest.period_end) is None), None)
        if book is None:
            metrics.append(_metric(K.ORDER_BOOK_TO_REVENUE, "RATIO", method, "REPORTED",
                                   reason="PERIOD_MISMATCH", start=latest.period_start,
                                   end=latest.period_end,
                                   issues=["NO_ORDER_BOOK_AT_REVENUE_PERIOD_END"]))
        else:
            check = check_comparable([_book(book), _revenue(latest)], ALL,
                                     allow_tax_policy=allow)
            metrics.append(_metric(K.ORDER_BOOK_TO_REVENUE, "RATIO", method, "REPORTED",
                                   value=book.value_crore / latest.value_crore
                                   if latest.value_crore else None,
                                   reason=check.reason
                                   or (None if latest.value_crore else "ZERO_REVENUE"),
                                   start=latest.period_start, end=book.as_of,
                                   issues=check.notes))

    # book-to-bill: inflow and revenue over the identical period
    if latest is None:
        metrics.append(_metric(K.BOOK_TO_BILL, "RATIO", "INFLOW / REVENUE", "NOT_APPLICABLE",
                               reason="INPUT_NOT_SUPPLIED"))
    else:
        flow = _inflow(chains, request, latest.period_start, latest.period_end, ledger_start,
                       _revenue(latest))
        why, notes = flow.reason, list(flow.notes)
        if why is None and flow.quantity is not None:
            check = check_comparable([flow.quantity, _revenue(latest)],
                                     (Dimension.CURRENCY, Dimension.BASIS, Dimension.TAX,
                                      Dimension.UNIT), allow_tax_policy=allow)
            why = check.reason
            notes += check.notes
        metrics.append(_metric(
            K.BOOK_TO_BILL, "RATIO", "PERIOD_INFLOW / PERIOD_REVENUE (SAME PERIOD)", flow.source,
            value=flow.value / latest.value_crore
            if flow.value is not None and latest.value_crore else None,
            reason=why, start=latest.period_start, end=latest.period_end,
            issues=[n for n in notes if not n.startswith("TAX_BASIS_")],
        ))
    growth_reason: str | None = "INPUT_NOT_SUPPLIED"
    growth_value = None
    g_start = g_end = None
    if latest is not None:
        cur_start, cur_end = latest.period_start, latest.period_end
        prev_end = cur_start - timedelta(days=1)
        prev_start = date(prev_end.year - 1, prev_end.month, prev_end.day) + timedelta(days=1) \
            if not (prev_end.month == 2 and prev_end.day == 29) else date(prev_end.year - 1, 3, 1)
        now = _inflow(chains, request, cur_start, cur_end, ledger_start, None)
        old = _inflow(chains, request, prev_start, prev_end, ledger_start, None)
        growth_reason = now.reason or old.reason
        if growth_reason is None and (now.source != old.source or now.taxes != old.taxes):
            growth_reason = "PERIODS_NOT_COMPARABLE"
        if growth_reason is None and now.quantity is not None and old.quantity is not None:
            growth_reason = check_comparable(
                [now.quantity, old.quantity], (Dimension.CURRENCY, Dimension.UNIT)).reason
        if growth_reason is None and not old.value:
            growth_reason = "ZERO_BASE_PERIOD"
        if growth_reason is None and now.value is not None and old.value:
            growth_value = (now.value / old.value - 1) * 100
        g_start, g_end = prev_start, cur_end
    metrics.append(_metric(K.ORDER_INFLOW_GROWTH, "PERCENT",
                           "INFLOW(PERIOD) / INFLOW(PRIOR EQUAL PERIOD) - 1", "MIXED",
                           value=growth_value, reason=growth_reason, start=g_start, end=g_end))

    # execution rate: revenue of the period over the book on the day before it starts
    opening = None
    if latest is not None:
        opening = next((b for b in books
                        if as_of_equals(b.as_of, latest.period_start - timedelta(days=1)) is None),
                       None)
    if latest is None or opening is None:
        metrics.append(_metric(K.EXECUTION_RATE, "PERCENT", "PERIOD_REVENUE / OPENING_ORDER_BOOK",
                               "REPORTED", reason="INPUT_NOT_SUPPLIED"))
    else:
        check = check_comparable([_book(opening), _revenue(latest)], ALL, allow_tax_policy=allow)
        metrics.append(_metric(
            K.EXECUTION_RATE, "PERCENT", "PERIOD_REVENUE / OPENING_ORDER_BOOK", "REPORTED",
            value=latest.value_crore / opening.value_crore * 100 if opening.value_crore else None,
            reason=check.reason or (None if opening.value_crore else "ZERO_OPENING_ORDER_BOOK"),
            start=latest.period_start, end=latest.period_end, issues=check.notes,
        ))

    # ledger-only shares over the awards of the last 365 days
    windowed = {c.chain_id: _awards(c, t_start, today) for c in chains}
    population = [c for c in chains if windowed[c.chain_id]]
    awarded: dict[Any, Decimal | None] = {}
    for c in population:
        amounts = [t.amount_crore for t in windowed[c.chain_id]]
        awarded[c.chain_id] = None if any(a is None for a in amounts) else sum(
            (a for a in amounts if a is not None), Decimal(0))
    complete = all(v is not None for v in awarded.values()) and bool(population)
    total = sum((v for v in awarded.values() if v is not None), Decimal(0))
    base_reason = None if complete and total > 0 else (
        "NO_CONFIRMED_ORDERS_IN_WINDOW" if not population else "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
    )
    if base_reason is None:
        base_reason = _sum_check([_step(t) for c in population for t in windowed[c.chain_id]])

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
    confirmed_now = [c for c in population if c.counted_in_confirmed]
    attributable = [c.confirmed_attributable_value_crore for c in confirmed_now]
    metrics.append(_metric(
        K.JV_ATTRIBUTABLE_CONFIRMED_VALUE, "INR_CRORE",
        "SUM_ATTRIBUTABLE_REMAINING_VALUE_OF_CONFIRMED_CHAINS_AWARDED_LAST_365D",
        "DISCLOSED_ORDERS_ONLY",
        value=sum((v for v in attributable if v is not None), Decimal(0)),
        reason=None if confirmed_now and all(v is not None for v in attributable) and len(
            {c.value_tax_basis for c in confirmed_now}) == 1
        else ("NO_CONFIRMED_ORDERS_IN_WINDOW" if not confirmed_now
              else "ATTRIBUTABLE_VALUE_UNKNOWN_FOR_SOME_ORDERS"
              if any(v is None for v in attributable) else "MIXED_TAX_BASIS"),
        start=t_start, end=today, chains=len(confirmed_now),
    ))
    metrics.append(_cancellation_rate(chains, ledger_start, today))
    metrics.sort(key=lambda m: list(K).index(m.kind))
    return OrderBookAnalytics(
        metrics=tuple(metrics),
        reconciliation=reconcile(chains, request, ledger_start, books, annual),
        tax_inclusive_values_allowed=allow,
    )


def _cancellation_rate(
    chains: Sequence[OrderChain], ledger_start: date | None, today: date
) -> OrderBookMetric:
    """Cancelled value over the value booked (awards + revision deltas), whole ledger."""
    steps = [t for c in chains for t in c.transitions]
    awards = [t for t in steps if t.kind in _AWARD_KINDS]
    cancels = [t for t in steps if t.kind in _CANCEL_KINDS and t.rule != "BID_CANCELLED"]
    revisions = [t for t in steps if t.kind == TransitionKind.REVISION
                 and t.rule != "REVISION_VALUE_NOT_STATED"]
    reason = None
    if not awards:
        reason = "NO_CONFIRMED_ORDERS"
    elif any(t.amount_crore is None for t in awards):
        reason = "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS"
    elif any(t.kind == TransitionKind.UNRESOLVED_CANCELLATION for t in steps):
        reason = "CANCELLATION_UNRESOLVED"
    elif any(t.amount_crore is None for t in cancels):
        reason = "CANCELLED_VALUE_NOT_DISCLOSED"
    elif any(t.amount_crore is None for t in revisions):
        reason = "REVISION_NOT_COMPARABLE"
    else:
        reason = _sum_check([_step(t) for t in (*awards, *cancels, *revisions)])
    booked = sum((t.amount_crore or Decimal(0) for t in (*awards, *revisions)), Decimal(0))
    cancelled = sum((t.amount_crore or Decimal(0) for t in cancels), Decimal(0))
    return _metric(
        K.CANCELLATION_RATE, "PERCENT",
        "CANCELLED_VALUE / (AWARDED_VALUE + REVISION_DELTAS) (LEDGER)", "DISCLOSED_ORDERS_ONLY",
        value=cancelled / booked * 100 if booked else None,
        reason=reason or (None if booked else "ZERO_AWARD_VALUE"),
        start=ledger_start, end=today,
        chains=len({c.chain_id for c in chains if any(t.kind in _AWARD_KINDS
                                                      for t in c.transitions)}),
    )


def reconcile(
    chains: Sequence[OrderChain],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
    books: Sequence[ReportedOrderBook],
    annual: Sequence[PeriodRevenue],
) -> OrderBookReconciliation | None:
    """opening (day before the period) + inflow (the period) - revenue (the period) -
    cancellations (dated in the period) +/- revision deltas (dated in the period) = implied
    closing (last day of the period): one contiguous period, every term comparable."""
    if not annual:
        return None
    revenue = annual[-1]
    start, end = revenue.period_start, revenue.period_end
    base: dict[str, Any] = {"period_start": start, "period_end": end}
    opening = next((b for b in books if as_of_equals(b.as_of, start - timedelta(days=1)) is None),
                   None)
    closing = next((b for b in books if as_of_equals(b.as_of, end) is None), None)
    if opening is None:
        return OrderBookReconciliation(**base, status=MetricStatus.UNAVAILABLE,
                                       reason="NO_OPENING_ORDER_BOOK")
    allow = request.allow_tax_inclusive_order_values
    unavailable: dict[str, Any] = {**base, "status": MetricStatus.UNAVAILABLE,
                                   "opening_crore": opening.value_crore}
    check = check_comparable([_book(opening), _revenue(revenue)], ALL, allow_tax_policy=allow)
    if not check.ok:
        return OrderBookReconciliation(**unavailable, reason=check.reason)
    notes = list(check.notes)
    flow = _inflow(chains, request, start, end, ledger_start, _book(opening))
    notes += [n for n in flow.notes if not n.startswith("TAX_BASIS_")]
    if flow.reason:
        return OrderBookReconciliation(**unavailable, reason=flow.reason,
                                       executed_revenue_crore=revenue.value_crore,
                                       issues=tuple(dict.fromkeys(notes)))
    source: Literal["REPORTED", "LEDGER_DISCLOSED_ORDERS_ONLY"] = (
        "REPORTED" if flow.source == "REPORTED" else "LEDGER_DISCLOSED_ORDERS_ONLY"
    )
    if source == "LEDGER_DISCLOSED_ORDERS_ONLY":
        notes.append("INFLOW_IS_A_LOWER_BOUND_UNDISCLOSED_ORDERS_EXCLUDED")
    if ledger_start is None or ledger_start > start:
        return OrderBookReconciliation(**unavailable, reason="LEDGER_DOES_NOT_COVER_PERIOD")
    assert flow.quantity is not None and flow.value is not None
    check = check_comparable([_book(opening), flow.quantity, _revenue(revenue)],
                             (Dimension.CURRENCY, Dimension.TAX, Dimension.UNIT),
                             allow_tax_policy=allow)
    if not check.ok:
        return OrderBookReconciliation(**unavailable, reason=check.reason)
    notes += check.notes
    steps = [t for c in chains for t in c.transitions if start <= t.effective_date <= end]
    if any(t.kind == TransitionKind.UNRESOLVED_CANCELLATION for t in steps):
        return OrderBookReconciliation(**unavailable, reason="CANCELLATION_UNRESOLVED")
    cancels = [t for t in steps if t.kind in _CANCEL_KINDS and t.rule != "BID_CANCELLED"]
    revisions = [t for t in steps if t.kind == TransitionKind.REVISION
                 and t.rule != "REVISION_VALUE_NOT_STATED"]
    if any(t.amount_crore is None for t in cancels):
        return OrderBookReconciliation(**unavailable, reason="CANCELLED_VALUE_NOT_DISCLOSED")
    if any(t.amount_crore is None for t in revisions):
        return OrderBookReconciliation(**unavailable, reason="REVISION_NOT_COMPARABLE")
    for group, code in ((cancels, "CANCELLATION_NOT_COMPARABLE"),
                        (revisions, "REVISION_NOT_COMPARABLE")):
        if group:
            check = check_comparable([_book(opening), *(_step(t) for t in group)],
                                     (Dimension.CURRENCY, Dimension.TAX, Dimension.UNIT),
                                     allow_tax_policy=allow)
            if not check.ok:
                return OrderBookReconciliation(**unavailable, reason=code,
                                               issues=check.reasons)
            notes += check.notes
    cancellations = sum((t.amount_crore or Decimal(0) for t in cancels), Decimal(0))
    revision_total = sum((t.amount_crore or Decimal(0) for t in revisions), Decimal(0))
    implied = opening.value_crore + flow.value - revenue.value_crore - cancellations + \
        revision_total
    reported_closing = difference = None
    if closing is not None:
        # a closing book is compared only on the opening's exact currency, basis and tax
        check = check_comparable([_book(opening), _book(closing)], ALL)
        if check.ok:
            reported_closing = closing.value_crore
            difference = closing.value_crore - implied
        else:
            notes += ["CLOSING_BOOK_NOT_COMPARABLE", *(f"CLOSING_{r}" for r in check.reasons)]
    return OrderBookReconciliation(
        **base, status=MetricStatus.AVAILABLE, opening_crore=opening.value_crore,
        inflow_crore=flow.value, inflow_source=source,
        executed_revenue_crore=revenue.value_crore, cancellations_crore=cancellations,
        revisions_crore=revision_total, implied_closing_crore=implied,
        reported_closing_crore=reported_closing, difference_crore=difference,
        issues=tuple(dict.fromkeys(notes)),
    )


def build_order_ledger(
    events: Sequence[CorporateEvent],
    request: MarketIntelligenceRequest,
    ledger_start: date | None,
    coverage: LedgerCoverage | None = None,
) -> tuple[list[OrderRecord], list[OrderChain], OrderBookAnalytics]:
    documents = {d.source_url: d for d in sorted(request.document_texts,
                                                 key=lambda d: d.document_sha256)}
    facts = {f.source_url: f for f in request.order_facts}
    records = [
        extract_order(e, documents.get(e.source_url or ""), facts.get(e.source_url or ""))
        for e in events
        if e.event_type in STAGE_BY_EVENT
    ]
    if coverage is None:
        coverage = LedgerCoverage(complete=False, ledger_start=ledger_start,
                                  reasons=("COVERAGE_NOT_ASSESSED",))
    chains = sorted(build_chains(records, coverage),
                    key=lambda c: (c.first_announced, str(c.chain_id)))
    return records, chains, order_book_analytics(chains, records, request, ledger_start)
