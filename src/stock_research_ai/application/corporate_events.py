"""Box 12 corporate event ledger, corporate actions and filing timeline.

Classification is an ordered table of fixed rules over the exchange's own category and
subject text; the first rule that matches wins and its code is recorded on the event. No AI
decides an event's type, date or value. Values are taken only where the filing states an
amount; materiality is only a stated value over an explicit, cited annual revenue, never a
reading of words such as "major" or "significant" (those stay in management_wording).

Duplicates: the same exchange record seen twice is one record; the same filing on NSE and
BSE (same day, same type, same normalized subject - or, for orders, same counterparty and
stated value) becomes one event that keeps both sources. A filing that says it revises an
earlier one, and names that filing's date, supersedes it; the earlier event is kept.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from stock_research_ai.application.exchange_intelligence import ActionRow, FilingRow, issue, uid
from stock_research_ai.domain.market_intelligence import (
    AUTHORITY_RANK,
    CRORE,
    LAKH,
    MAX_MONEY,
    ORDER_EVENT_TYPES,
    CorporateAction,
    CorporateActionKind,
    CorporateEvent,
    EventStatus,
    EventType,
    FilingDocument,
    FilingTimeline,
    FinancialBasis,
    Issue,
    Materiality,
    MetricStatus,
    ReferenceRevenue,
    TaxBasis,
    TimelineEntry,
    TimelineWindow,
)

E = EventType
_I = re.IGNORECASE

#: (rule code, event type, pattern over "category | subject | body"). Order matters.
RULES: tuple[tuple[str, EventType, re.Pattern[str]], ...] = tuple(
    (code, kind, re.compile(pattern, _I))
    for code, kind, pattern in (
        # Exchange queries and replies quote other filings: never an event of that type.
        ("EXCHANGE_CLARIFICATION", E.OTHER_MATERIAL_EVENT,
         r"sought clarification|reply to clarification|clarification sought|news verification"),
        ("TRADING_WINDOW", E.OTHER_MATERIAL_EVENT, r"trading window"),
        ("IEPF_TRANSFER", E.OTHER_MATERIAL_EVENT, r"\bIEPF\b|investor education and protection"),
        ("ORDER_CANCELLED", E.ORDER_CANCELLATION,
         r"cancell?ation of (?:the )?(?:letter of (?:award|acceptance)|loa|work order|order|"
         r"contract)|(?:order|contract|loa|letter of (?:award|acceptance))\b[^|]{0,300}?\bhas "
         r"been (?:cancell?ed|terminated|foreclosed)|termination of (?:the )?(?:contract|order)"),
        ("ORDER_REVISED", E.ORDER_REVISION,
         r"\b(?:revision|revised|modification|amendment|variation)\b[^|]{0,40}\b(?:of|in|to)\b"
         r"[^|]{0,40}\b(?:order|contract|loa|letter of (?:award|acceptance)|work)\b"),
        ("L1_LOWEST_BIDDER", E.L1_BIDDER, r"lowest bidder|\bL-?1\b|\bL1 bidder"),
        ("LETTER_OF_AWARD", E.LOA,
         r"letter of (?:award|acceptance|intent)|\bLOA\b|\bLoA\b|\bLOI\b"),
        ("ORDER_RECEIVED", E.ORDER_AWARD,
         r"bagging|receiving of orders|receipt of (?:an? )?(?:work )?order|\bwork order\b|"
         r"purchase order|order (?:received|win|inflow)|contract (?:agreement|signed)|"
         r"signing of (?:the )?contract|awarded (?:a|the) contract"),
        ("EXCHANGE_FINE", E.EXCHANGE_PENALTY,
         r"(?:fine|penalt(?:y|ies))\b[^|]{0,80}\b(?:BSE|NSE|stock exchange)|"
         r"(?:BSE|NSE)\b[^|]{0,40}\b(?:fine|penalt)"),
        ("AUDITOR", E.AUDITOR_CHANGE, r"\bauditors?\b"),
        ("KMP", E.KMP_CHANGE,
         r"chief financial officer|\bCFO\b|company secretary|\bKMP\b|key managerial|"
         r"compliance officer|change in management|senior management"),
        ("BOARD", E.BOARD_CHANGE,
         r"change in directors?|\bdirectors?\b[^|]{0,60}(?:appoint|cessation|resign)|"
         r"(?:appoint|cessation|resign)[^|]{0,60}\bdirectors?\b|chairman|managing director|"
         r"\bdirector\s*\("),
        ("CREDIT_RATING", E.CREDIT_RATING, r"credit rating|\brating\b"),
        ("BUYBACK", E.BUYBACK, r"buy[\s-]?back"),
        ("BONUS", E.BONUS, r"\bbonus\b"),
        ("SPLIT", E.SPLIT, r"sub-?division|stock split|split of (?:equity )?shares"),
        ("RIGHTS", E.RIGHTS, r"rights issue"),
        ("DIVIDEND", E.DIVIDEND, r"dividend"),
        ("RESULTS", E.RESULTS,
         r"financial results?|result updates|quarterly results|audited results|"
         r"clarification - financial"),
        ("ANNUAL_REPORT", E.ANNUAL_REPORT, r"annual report"),
        ("INVESTOR_PRESENTATION", E.INVESTOR_PRESENTATION, r"investor presentation|presentation"),
        ("CONCALL", E.CONCALL,
         r"con\.? ?call|conference call|earnings call|analysts?/institutional investor meet|"
         r"investor meet"),
        ("POSTAL_BALLOT", E.POSTAL_BALLOT, r"postal ballot"),
        ("SHAREHOLDER_MEETING", E.AGM_EGM,
         r"shareholders meeting|annual general meeting|\bAGM\b|\bEGM\b|"
         r"extra-?ordinary general meeting"),
        ("BULK_DEAL", E.BULK_DEAL, r"bulk deal"),
        ("BLOCK_DEAL", E.BLOCK_DEAL, r"block deal"),
        ("INSIDER", E.INSIDER_TRADE,
         r"insider trading|prohibition of insider|SEBI \(PIT\)|regulation 7\(2\)"),
        ("SHAREHOLDING", E.SHAREHOLDING,
         r"shareholding pattern|takeover regulations|substantial acquisition|offer for sale|"
         r"\bOFS\b|pledge"),
        ("MOU", E.MOU, r"memorandum of understanding|\bMOU'?s?\b|\bMoU\b"),
        ("JOINT_VENTURE", E.JV, r"joint venture|\bJV\b"),
        ("ACQUISITION", E.ACQUISITION, r"\bacqui(?:sition|re|red)\b"),
        ("DIVESTMENT", E.DIVESTMENT, r"divest|disinvest|sale of (?:stake|subsidiary|undertaking)"),
        ("FUND_RAISE", E.FUND_RAISE,
         r"fund rais|qualified institutions? placement|\bQIP\b|preferential (?:issue|allotment)|"
         r"debentures?|\bNCDs?\b|commercial paper|\bbonds?\b"),
        ("LITIGATION", E.LITIGATION,
         r"litigation|arbitra|\bdispute|court|tribunal|\bNCLT\b|legal proceeding"),
        ("REGULATORY", E.REGULATORY,
         r"action\(s\) taken or orders passed|show cause|\bSEBI\b order|regulatory action|"
         r"\bGST\b[^|]{0,40}(?:demand|order)|tax demand|income tax"),
    )
)
FALLBACK_RULE = "UNMATCHED"

_REVISION = re.compile(r"\b(?:revised|revision|corrigendum|addendum|erratum)\b", _I)
_LETTER_DATE = re.compile(r"letter dated\s+(\d{1,2})[./-](\d{1,2})[./-](\d{4})", _I)
_BASIS_BOTH = re.compile(r"standalone\s*(?:&|and|/)\s*consolidated|"
                         r"consolidated\s*(?:&|and|/)\s*standalone", _I)
_BASIS_CONS = re.compile(r"\bconsolidated\b", _I)
_BASIS_STAND = re.compile(r"\bstandalone\b|non-consolidated", _I)
FINANCIAL_TYPES = frozenset({E.RESULTS, E.ANNUAL_REPORT, E.INVESTOR_PRESENTATION, E.CONCALL})

# --- money ------------------------------------------------------------------------------------

#: A currency marker, never the tail of a word ('Hrs', 'MINR').
_CURRENCY = re.compile(r"(?<![A-Za-z])(?P<cur>Rs\.?|INR|₹|USD|US\$|EUR|€)\s{0,4}", _I)
#: The WHOLE numeric token after a marker: digits, commas and dots, plus one or two blanks only
#: where they touch a comma (text extraction writes 'Rs 148,26,89 ,066'). It is validated as
#: one token; a token that cannot be read whole is malformed, never cut back to a prefix.
_TOKEN = re.compile(r"[\d,](?:[\d,.]|[ \t]{1,2}(?=,)|(?<=,)[ \t]{1,2}(?=\d))*")
_UNIT = re.compile(
    r"\s*(?:/-)?\s*(?P<unit>crores?|cr\b\.?|lakhs?|lacs?|millions?|mn\b|billions?|bn\b)?", _I
)
_GROUPED = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")
#: Text extraction also splits paise off a grouped amount: 'Rs. 72,73,45,018. 74/-'.
_SPLIT_PAISE = re.compile(r"[ \t]{1,2}(\d{2})(?=[ \t]{0,2}/)")  # only before the '/-' terminator
MAX_AMOUNT_DIGITS = 20
_TAX_INCLUSIVE = re.compile(r"incl(?:\.|uding|usive)?\s*(?:of\s*)?(?:GST|taxes)", _I)
_TAX_EXCLUSIVE = re.compile(r"excl(?:\.|uding|usive)?\s*(?:of\s*)?(?:GST|taxes)|plus GST|"
                            r"\+\s*GST|without GST", _I)


@dataclass(frozen=True)
class StatedMoney:
    crore: Decimal
    currency: str
    text: str
    tax: TaxBasis


def _to_crore(number: Decimal, unit: str | None) -> Decimal | None:
    u = (unit or "").lower().rstrip(".")
    if u.startswith("cr"):
        value = number
    elif u.startswith(("lakh", "lac")):
        value = number * LAKH / CRORE
    elif u.startswith(("million", "mn")):
        value = number * Decimal(1_000_000) / CRORE
    elif u.startswith(("billion", "bn")):
        value = number * Decimal(1_000_000_000) / CRORE
    else:
        value = number / CRORE  # an amount without a unit is in ones
    return value if 0 < value <= MAX_MONEY else None


def _grouping_ok(token: str) -> bool:
    """Plain digits, or comma groups: Indian (1,23,45,678 / 1134,11,42,182), Western
    (1,234,567) or the two mixed (1,134,11,42,182). The leading group may exceed three digits
    (amounts of Rs 1,000 crore and more are written '1134,11,42,182'); every later group is
    two or three digits and the last is exactly three."""
    if not _GROUPED.fullmatch(token):
        return False
    head, *rest = token.split(".")[0].split(",")
    if not rest:
        return True
    return (len(head) <= 7 and len(rest[-1]) == 3
            and all(len(group) in (2, 3) for group in rest[:-1]))


def _indian(token: str) -> bool:
    groups = token.split(".")[0].split(",")
    return (len(groups) >= 3 and len(groups[-1]) == 3
            and all(len(group) == 2 for group in groups[1:-1]))


def amount_token(token: str, following: str = "") -> str | None:
    """The complete number a raw token states (commas kept), or None when it cannot be read
    whole - never a prefix of a number. `following` is the text right after the token.

    A full stop ending the token, or a comma ending it before a blank / the end of the text,
    is punctuation. A token cut short by an attached letter or digit ('Rs. 1,lQ,86,49,140'),
    or followed by a blank and more digits ('Rs. 1134 11 42 182'), is unreadable. Blanks
    touching a comma are an extraction artefact only when the joined
    token is one Indian-grouped number of three or more groups ('148,26,89 ,066'); anything
    else ('Rs. 100, 200') is unreadable rather than guessed."""
    if token.endswith(",") and following[:1] not in ("", " ", "\t", "\n", "\r"):
        return None
    if following[:1].isalnum() or re.match(r"[ \t]{1,2}\d", following):
        return None
    token = token.rstrip(".")
    if not re.search(r"\s", token):
        candidate = token[:-1] if token.endswith(",") else token
        return candidate if _grouping_ok(candidate) else None
    joined = re.sub(r"\s+", "", token)
    return joined if _grouping_ok(joined) and _indian(joined) else None


def scan_amounts(segment: str) -> tuple[list[StatedMoney], int]:
    """(amounts, malformed): every currency-marked amount, each read as one complete token.
    A marker followed by a number that cannot be read whole counts as malformed and yields no
    value."""
    out: list[StatedMoney] = []
    malformed = 0
    text = segment[:20_000]
    for marker in _CURRENCY.finditer(text):
        found = _TOKEN.match(text, marker.end())
        if found is None:
            continue  # 'Rs.' with no number after it
        token, token_end = found.group(0), found.end()
        paise = _SPLIT_PAISE.match(text, token_end)
        if paise is not None and token.endswith(".") and "," in token:
            token, token_end = token + paise.group(1), paise.end()
        unit = _UNIT.match(text, token_end)
        assert unit is not None  # every part of the pattern is optional
        following = "" if unit.group("unit") else text[token_end: token_end + 3]
        number_text = amount_token(token, following)
        if number_text is None:
            malformed += 1
            continue
        digits = number_text.replace(",", "")
        if len(digits) > MAX_AMOUNT_DIGITS:
            continue
        crore = _to_crore(Decimal(digits), unit.group("unit"))
        if crore is None:
            continue
        cur = marker.group("cur").upper()
        currency = "USD" if "US" in cur else "EUR" if cur in ("EUR", "€") else "INR"
        text_end = unit.end()  # the verbatim amount includes its unit and any '/-'
        tail = text[text_end: text_end + 250]
        tax = (TaxBasis.INCLUSIVE_OF_TAX if _TAX_INCLUSIVE.search(tail)
               else TaxBasis.EXCLUSIVE_OF_TAX if _TAX_EXCLUSIVE.search(tail) else TaxBasis.UNKNOWN)
        out.append(StatedMoney(crore=crore, currency=currency,
                               text=text[marker.start():text_end].strip()[:200], tax=tax))
    return out, malformed


def stated_amounts(segment: str) -> list[StatedMoney]:
    return scan_amounts(segment)[0]


def single_amount(segment: str) -> tuple[StatedMoney | None, bool]:
    """(value, ambiguous): one distinct (currency, value) or nothing. A malformed amount in
    the segment makes it ambiguous: the unreadable one might be the value."""
    found, malformed = scan_amounts(segment)
    distinct = {(m.currency, m.crore) for m in found}
    if len(distinct) == 1 and not malformed:
        return found[0], False
    return None, len(distinct) > 1 or malformed > 0


# --- classification ---------------------------------------------------------------------------


def classify(category: str | None, subject: str, body: str | None) -> tuple[EventType, str]:
    haystack = " | ".join(x for x in (category, subject, body) if x)
    for code, kind, pattern in RULES:
        if pattern.search(haystack):
            return kind, code
    return E.OTHER_MATERIAL_EVENT, FALLBACK_RULE


def detect_basis(kind: EventType, haystack: str) -> FinancialBasis:
    if kind not in FINANCIAL_TYPES:
        return FinancialBasis.NOT_APPLICABLE
    if _BASIS_BOTH.search(haystack):
        return FinancialBasis.BOTH
    cons, stand = bool(_BASIS_CONS.search(haystack)), bool(_BASIS_STAND.search(haystack))
    if cons and stand:
        return FinancialBasis.BOTH
    if cons:
        return FinancialBasis.CONSOLIDATED
    if stand:
        return FinancialBasis.STANDALONE
    return FinancialBasis.UNKNOWN


def letter_dates(haystack: str) -> tuple[date, ...]:
    out = set()
    for d, m, y in _LETTER_DATE.findall(haystack):
        try:
            out.add(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return tuple(sorted(out))


_COUNTERPARTY = re.compile(
    r"\b(?:from|agreement with)\s+(?:M/s\.?\s*)?(?P<name>[A-Z][A-Za-z0-9&.,()' -]{2,160}?)"
    r"(?=\s+(?:for|towards|worth|amounting|valued|in connection)\b|\s*[-–:;]|\s*$|\.\s)"
)


def counterparty(subject: str, body: str | None) -> str | None:
    for source in (subject, body or ""):
        match = _COUNTERPARTY.search(source)
        if match:
            name = " ".join(match.group("name").split()).strip(" .,-'\"‘’“”")
            if name and not name.lower().startswith(("the exchange", "our letter")):
                return name[:300]
    return None


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()[:300]


# --- ledger -----------------------------------------------------------------------------------


@dataclass
class _Draft:
    rows: list[FilingRow]
    kind: EventType
    rule: str
    value: StatedMoney | None
    value_ambiguous: bool
    party: str | None
    basis: FinancialBasis
    malformed_amounts: int = 0

    @property
    def primary(self) -> FilingRow:
        return self.rows[0]


def _row_key(row: FilingRow) -> tuple[object, ...]:
    return (
        row.published_on, row.published_at.isoformat() if row.published_at else "",
        AUTHORITY_RANK[row.authority], row.provider.value, row.record_id or "", row.locator,
        str(row.payload_id),
    )


def build_events(
    rows: Sequence[FilingRow],
    identity_id: UUID,
    reference: ReferenceRevenue | None,
    issues: list[Issue],
) -> list[CorporateEvent]:
    # 1. the same exchange record seen more than once (pagination overlap, repeated fetch)
    unique: dict[tuple[str, str], FilingRow] = {}
    loose: list[FilingRow] = []
    for row in sorted(rows, key=lambda r: tuple(map(str, _row_key(r)))):
        if row.record_id is None:
            loose.append(row)
            continue
        key = (row.provider.value, row.record_id)
        prior = unique.get(key)
        if prior is None:
            unique[key] = row
        elif (prior.subject, prior.body, prior.published_on) != (row.subject, row.body,
                                                                 row.published_on):
            issues.append(issue("CONFLICTING_DUPLICATE_RECORD",
                                f"{row.provider.value} record {row.record_id} differs between "
                                "payloads; the first is kept", payload=row.payload_id,
                                locator=row.locator))
    # 2. classify and draft
    drafts: list[_Draft] = []
    for row in sorted([*unique.values(), *loose], key=lambda r: tuple(map(str, _row_key(r)))):
        kind, rule = classify(row.category, row.subject, row.body)
        haystack = " | ".join(x for x in (row.category, row.subject, row.body) if x)
        stated = f"{row.subject} {row.body or ''}"
        value, ambiguous = single_amount(stated)
        drafts.append(_Draft(
            rows=[row], kind=kind, rule=rule, value=value, value_ambiguous=ambiguous,
            malformed_amounts=scan_amounts(stated)[1],
            party=counterparty(row.subject, row.body) if kind in ORDER_EVENT_TYPES else None,
            basis=detect_basis(kind, haystack),
        ))
    # 3. merge the same filing across exchanges
    merged: list[_Draft] = []
    for draft in drafts:
        row = draft.primary
        target = next(
            (
                m for m in merged
                if m.primary.published_on == row.published_on
                and m.kind == draft.kind
                and row.provider not in {r.provider for r in m.rows}
                and (
                    norm_key(m.primary.subject) == norm_key(row.subject)
                    or (
                        draft.kind in ORDER_EVENT_TYPES
                        and draft.party is not None and m.party is not None
                        and norm_key(draft.party) == norm_key(m.party)
                        and draft.value is not None and m.value is not None
                        and draft.value.crore == m.value.crore
                    )
                )
            ),
            None,
        )
        if target is None:
            merged.append(draft)
            continue
        target.rows.append(row)
        if target.value is None and draft.value is not None:
            target.value = draft.value
        elif (target.value is not None and draft.value is not None
              and target.value.crore != draft.value.crore):
            issues.append(issue("EXCHANGE_VALUE_DISAGREEMENT", "NSE and BSE state different values "
                                "for one filing; no value selected", payload=row.payload_id))
            target.value, target.value_ambiguous = None, True
    # 4. events
    events: list[CorporateEvent] = []
    for draft in merged:
        draft.rows.sort(key=lambda r: tuple(map(str, _row_key(r))))
        row = draft.primary
        own: list[Issue] = []
        if draft.malformed_amounts:
            own.append(issue("MALFORMED_AMOUNT", f"{draft.malformed_amounts} stated amount(s) "
                             "cannot be read as one complete number; no value is taken from "
                             "them", payload=row.payload_id, locator=row.locator))
        if draft.value_ambiguous:
            own.append(issue("AMBIGUOUS_VALUE", "Several different amounts are stated; none "
                             "selected", payload=row.payload_id, locator=row.locator))
        event_id = uid("event", identity_id, row.provider.value, row.record_id or row.locator,
                       row.payload_id if row.record_id is None else "-", row.published_on,
                       draft.kind.value)
        value = draft.value
        events.append(CorporateEvent(
            event_id=event_id,
            identity_id=identity_id,
            event_date=row.published_on,
            event_time=row.published_at,
            exchanges=tuple(sorted({"NSE" if r.exchange == "NSE" else "BSE" for r in draft.rows})),
            event_type=draft.kind,
            classification_rule=draft.rule,
            title=row.subject[:1000],
            summary=row.body,
            management_wording=row.body or row.subject,
            financial_value_crore=value.crore if value else None,
            value_currency=value.currency if value else None,  # type: ignore[arg-type]
            value_tax_basis=value.tax if value else None,
            value_text=value.text if value else None,
            counterparty=draft.party,
            source_url=next((r.url for r in draft.rows if r.url), None),
            document_reference=row.record_id,
            authority=min((r.authority for r in draft.rows), key=AUTHORITY_RANK.__getitem__),
            basis=draft.basis,
            sources=tuple(r.source for r in draft.rows),
            materiality=materiality(value, reference) if value else None,
            issues=tuple(own),
        ))
    events.sort(key=event_key)
    return supersede(events, issues)


def event_key(e: CorporateEvent) -> tuple[date, bool, str, str]:
    return (e.event_date, e.event_time is None,
            e.event_time.isoformat() if e.event_time else "", str(e.event_id))


def supersede(events: list[CorporateEvent], issues: list[Issue]) -> list[CorporateEvent]:
    """A revision that names the date of the filing it revises supersedes that filing."""
    by_id = {e.event_id: e for e in events}
    for event in events:
        if event.event_type in ORDER_EVENT_TYPES or not _REVISION.search(
            f"{event.title} {event.summary or ''}"
        ):
            continue
        dates = letter_dates(f"{event.title} {event.summary or ''}")
        candidates = [
            e for e in events
            if e.event_type == event.event_type and e.event_id != event.event_id
            and e.event_date in dates and e.event_date <= event.event_date
            and by_id[e.event_id].status == EventStatus.ACTIVE
        ]
        if len(candidates) == 1:
            old = candidates[0]
            by_id[old.event_id] = by_id[old.event_id].model_copy(
                update={"status": EventStatus.SUPERSEDED,
                        "related_event_ids": tuple(sorted({*old.related_event_ids,
                                                           event.event_id}, key=str))}
            )
            by_id[event.event_id] = by_id[event.event_id].model_copy(
                update={"supersedes_event_id": old.event_id}
            )
        elif len(candidates) > 1:
            issues.append(issue("AMBIGUOUS_SUPERSESSION", "A revision matches several filings; "
                                "none superseded", severity="INFO"))
    return [by_id[e.event_id] for e in events]


def materiality(value: StatedMoney, reference: ReferenceRevenue | None) -> Materiality:
    if reference is None:
        return Materiality(status=MetricStatus.UNAVAILABLE, numerator_crore=value.crore,
                           reason="NO_REFERENCE_REVENUE")
    if value.currency != reference.currency:
        return Materiality(status=MetricStatus.UNAVAILABLE, numerator_crore=value.crore,
                           reason="CURRENCY_MISMATCH")
    notes = ("NUMERATOR_INCLUSIVE_OF_TAX",) if value.tax == TaxBasis.INCLUSIVE_OF_TAX else (
        ("NUMERATOR_TAX_BASIS_UNKNOWN",) if value.tax == TaxBasis.UNKNOWN else ()
    )
    ratio = (value.crore / reference.value_crore * 100).quantize(Decimal("0.000001"))
    return Materiality(
        status=MetricStatus.AVAILABLE, ratio_pct=ratio, numerator_crore=value.crore,
        denominator_crore=reference.value_crore, denominator_fiscal_year=reference.fiscal_year,
        denominator_basis=reference.basis, issues=notes,
    )


# --- corporate actions ------------------------------------------------------------------------


def build_actions(rows: Sequence[ActionRow], identity_id: UUID) -> tuple[
    list[CorporateAction], list[tuple[ActionRow, ActionRow]]
]:
    """Merge NSE/BSE rows for one action; return conflicting pairs for disagreement records."""
    groups: list[list[ActionRow]] = []
    conflicts: list[tuple[ActionRow, ActionRow]] = []
    ordered = sorted(rows, key=lambda r: (str(r.ex_date), r.kind.value, r.source.provider.value,
                                          r.source.locator, str(r.source.payload_id)))
    for row in ordered:
        # The same action on the other exchange: same kind and ex-date, preferring a group
        # that states the same amount; a different amount is a disagreement, never a pick.
        candidates = [
            g for g in groups
            if g[0].kind == row.kind and g[0].ex_date == row.ex_date
            and row.ex_date is not None
            and row.source.provider not in {r.source.provider for r in g}
        ]
        group = next((g for g in candidates if g[0].amount == row.amount),
                     candidates[0] if candidates else None)
        if group is None:
            # the same row repeated in one provider's feed
            same = next((g for g in groups if g[0].source.provider == row.source.provider
                         and (g[0].kind, g[0].ex_date, g[0].subject, g[0].record_date)
                         == (row.kind, row.ex_date, row.subject, row.record_date)), None)
            if same is not None:
                continue
            groups.append([row])
            continue
        if (group[0].amount is not None and row.amount is not None
                and group[0].amount != row.amount):
            conflicts.append((group[0], row))
        group.append(row)
    actions = []
    for group in groups:
        head = group[0]

        def pick(attr: str, g: list[ActionRow] = group) -> object:
            values = {getattr(r, attr) for r in g if getattr(r, attr) is not None}
            return next(iter(values)) if len(values) == 1 else None

        issues = [i for r in group for i in r.issues]
        amount = pick("amount")
        if head.kind == CorporateActionKind.DIVIDEND and amount is None and any(
            r.amount is not None for r in group
        ):
            issues.append(issue("SOURCE_DISAGREEMENT", "Exchanges state different amounts; see "
                                "disagreements"))
        actions.append(CorporateAction(
            action_id=uid("action", identity_id, head.kind.value, head.ex_date, head.subject),
            kind=head.kind, subject=head.subject,
            announcement_date=pick("announcement_date"),  # type: ignore[arg-type]
            record_date=pick("record_date"),  # type: ignore[arg-type]
            ex_date=head.ex_date,
            book_closure_start=pick("bc_start"),  # type: ignore[arg-type]
            book_closure_end=pick("bc_end"),  # type: ignore[arg-type]
            payment_date=pick("payment_date"),  # type: ignore[arg-type]
            amount_per_share=amount,  # type: ignore[arg-type]
            currency="INR" if amount is not None else None,
            ratio=pick("ratio"),  # type: ignore[arg-type]
            face_value_from=pick("face_from"),  # type: ignore[arg-type]
            face_value_to=pick("face_to"),  # type: ignore[arg-type]
            sources=tuple(r.source for r in group),
            issues=tuple(issues),
        ))
    actions.sort(key=lambda a: (a.ex_date or date.min, a.kind.value, str(a.action_id)))
    return actions, conflicts


# --- timeline --------------------------------------------------------------------------------

WINDOWS: tuple[int, ...] = (30, 90, 365)


def build_timeline(
    as_of: date,
    events: Iterable[CorporateEvent],
    actions: Iterable[CorporateAction],
    filings: Iterable[FilingDocument],
) -> FilingTimeline:
    entries: list[TimelineEntry] = []
    for e in events:
        entries.append(TimelineEntry(
            entry_id=uid("timeline", "EVENT", e.event_id), entry_date=e.event_date,
            entry_time=e.event_time, kind="EVENT", category=e.event_type.value, title=e.title,
            reference_id=e.event_id, source_url=e.source_url,
        ))
    for a in actions:
        day = a.ex_date or a.record_date or a.announcement_date
        if day is None:
            continue
        entries.append(TimelineEntry(
            entry_id=uid("timeline", "ACTION", a.action_id), entry_date=day,
            kind="CORPORATE_ACTION", category=a.kind.value, title=a.subject,
            reference_id=a.action_id,
            source_url=next((s.source_url for s in a.sources if s.source_url), None),
        ))
    for f in filings:
        if f.published_on is None:
            continue
        entries.append(TimelineEntry(
            entry_id=uid("timeline", "FILING", f.document_id), entry_date=f.published_on,
            kind="FILING", category=f.category.value, title=f.title, reference_id=f.document_id,
            source_url=f.url,
        ))
    entries.sort(key=lambda t: (t.entry_date, t.entry_time is None,
                                t.entry_time.isoformat() if t.entry_time else "", t.kind,
                                t.category, str(t.entry_id)))
    windows = tuple(window(entries, as_of, days) for days in WINDOWS)
    return FilingTimeline(as_of=as_of, entries=tuple(entries), windows=windows)


def window(entries: Sequence[TimelineEntry], as_of: date, days: int) -> TimelineWindow:
    start = as_of - timedelta(days=days - 1)
    picked = filter_timeline(entries, start, as_of)
    counts: dict[str, int] = {}
    for entry in picked:
        counts[entry.category] = counts.get(entry.category, 0) + 1
    return TimelineWindow.model_validate({
        "days": days, "start_date": start, "end_date": as_of, "total": len(picked),
        "counts": tuple(sorted(counts.items())),
    })


def filter_timeline(
    entries: Sequence[TimelineEntry],
    start: date,
    end: date,
    categories: Iterable[str] | None = None,
) -> list[TimelineEntry]:
    """Deterministic 'what changed between start and end' (inclusive), optionally by category."""
    wanted = set(categories) if categories is not None else None
    return [
        e for e in entries
        if start <= e.entry_date <= end and (wanted is None or e.category in wanted)
    ]
