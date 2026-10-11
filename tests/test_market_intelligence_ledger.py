"""Box 12 remediation: provider identity gating, order linking, the chronological order
state machine, the comparability contract and pending-L1 linkage (offline).

Payload factories come from test_market_intelligence; every expected number is worked out
by hand in the comment beside it, never with Box 12 helpers.
"""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_market_intelligence import (
    AS_OF,
    BSE,
    ISIN,
    L1_TEXT,
    L1_URL,
    SHA,
    SYMBOL,
    _loa,
    announcement,
    base_payloads,
    bse_header,
    build,
    docs,
    kite_master,
    nse_announcements,
    nse_master,
    order_payloads,
    raw,
    request,
)

from stock_research_ai.application.corporate_events import scan_amounts, single_amount
from stock_research_ai.application.order_comparability import (
    ALL,
    ORDER_VALUES,
    TAX_POLICY_NOTE,
    TAX_UNKNOWN_NOTE,
    Dimension,
    Quantity,
    as_of_equals,
    check_comparable,
    same_period,
)
from stock_research_ai.application.order_ledger import (
    LedgerCoverage,
    build_chains,
    order_book_analytics,
    reference_ids,
)
from stock_research_ai.domain.market_intelligence import (
    Authority,
    CancellationScope,
    ChainStatus,
    Citation,
    CorporateActionKind,
    DocumentText,
    FetchStatus,
    FinancialBasis,
    IdentityStatus,
    L1Resolution,
    MetricStatus,
    OrderBookMetricKind,
    OrderChain,
    OrderRecord,
    OrderStage,
    PayloadKind,
    PeriodRevenue,
    Provider,
    RawPayload,
    ReferenceRevenue,
    ReportedInflow,
    ReportedOrderBook,
    TaxBasis,
    TransitionKind,
)

IRCON_BSE = "541956"
IRCON_ISIN = "INE962Y01021"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
K = OrderBookMetricKind


# --- factories ---------------------------------------------------------------------------------


def bse_feed(*rows: dict[str, Any], code: str = IRCON_BSE) -> RawPayload:
    return raw(PayloadKind.BSE_ANNOUNCEMENTS, {"Table": list(rows), "Table1": [{"ROWCNT": 1}]},
               f"{BSE_API}/AnnSubCategoryGetData/w?pageno=1&strScrip={code}")


def bse_ann(news: str, subject: str, *, code: str = IRCON_BSE, **extra: Any) -> dict[str, Any]:
    return {"NEWSID": news, "SCRIP_CD": int(code), "NEWSSUB": subject, "HEADLINE": subject,
            "CATEGORYNAME": "Company Update", "SUBCATNAME": "", "NEWS_DT": "2026-10-01T10:00:00",
            "ATTACHMENTNAME": f"{news}.pdf", **extra}


def bse_actions(*rows: dict[str, Any], code: str = IRCON_BSE) -> RawPayload:
    return raw(PayloadKind.BSE_CORPORATE_ACTIONS, {"Table": list(rows)},
               f"{BSE_API}/CorporateAction/w?scripcode={code}")


def header_down(code: str = IRCON_BSE) -> RawPayload:
    return RawPayload(kind=PayloadKind.BSE_COMPANY_HEADER,
                      url=f"{BSE_API}/ComHeadernew/w?scripcode={code}",
                      status=FetchStatus.TIMEOUT, retrieved_at=AS_OF, error_code="TIMEOUT")


def nse_side() -> list[RawPayload]:
    return [nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master()]


IRCON_LOA = "Letter of Award from Northern Railway worth Rs. 250 crore"
IRCON_DIVIDEND = {"scrip_code": IRCON_BSE, "Purpose": "Interim Dividend - Rs. - 2.0000",
                  "Ex_date": "01 Sep 2026"}


def codes(data: Any) -> set[str]:
    return {i.code for i in data.issues}


# --- H-1: verified provider identity ----------------------------------------------------------


def test_correct_verified_bse_code_admits_bse_rows():
    data = build([*nse_side(), bse_header(),
                  bse_feed(bse_ann("r-1", "Letter of Award from NMDC Limited worth Rs. 90 crore",
                                   code=BSE), code=BSE),
                  bse_actions({"scrip_code": BSE, "Purpose": "Final Dividend - Rs. - 1.5000",
                               "Ex_date": "01 Sep 2026"}, code=BSE)])
    assert data.identity.status == IdentityStatus.RESOLVED
    assert data.identity.bse_code == BSE == data.identity.declared_bse_code
    (event,) = data.events
    assert event.exchanges == ("BSE",) and event.financial_value_crore == Decimal(90)
    (action,) = data.corporate_actions
    assert action.amount_per_share == Decimal("1.5000")
    assert "PROVIDER_IDENTITY_UNVERIFIED" not in codes(data)


def test_wrong_declared_bse_code_with_failed_header_admits_nothing():
    # RVNL on NSE, but the caller declares IRCON's BSE code and the BSE header times out.
    data = build([*nse_side(), header_down(),
                  bse_feed(bse_ann("irc-1", IRCON_LOA)), bse_actions(IRCON_DIVIDEND)],
                 bse_code=IRCON_BSE)
    identity = data.identity
    assert identity.status == IdentityStatus.PARTIAL
    assert identity.bse_code is None and identity.declared_bse_code == IRCON_BSE
    assert (identity.nse_symbol, identity.isin) == (SYMBOL, ISIN)  # verified by NSE + Kite
    # IRCON's LOA never becomes an RVNL event, order or confirmed chain
    assert data.events == () and data.orders == () and data.order_chains == ()
    # IRCON's Rs 2 dividend never becomes an RVNL corporate action
    assert data.corporate_actions == ()
    assert "PROVIDER_IDENTITY_UNVERIFIED" in codes(data)
    assert "IDENTITY_FIELD_UNCONFIRMED" in {i.code for i in identity.issues}


def test_correct_but_unverified_bse_code_is_not_canonical():
    # The declared code is right, but nothing verified it: rows keyed only by it stay out.
    data = build([*nse_side(), header_down(BSE),
                  bse_feed(bse_ann("r-1", "Letter of Award from NMDC Limited worth Rs. 90 crore",
                                   code=BSE), code=BSE),
                  bse_actions({"scrip_code": BSE, "Purpose": "Dividend - Rs. - 1.0000",
                               "Ex_date": "01 Sep 2026"}, code=BSE)])
    assert data.identity.bse_code is None and data.identity.declared_bse_code == BSE
    assert data.events == () and data.corporate_actions == ()
    unverified = [i for i in data.issues if i.code == "PROVIDER_IDENTITY_UNVERIFIED"]
    assert len(unverified) == 2 and all(i.payload_id is not None for i in unverified)


def test_row_level_isin_admits_a_bse_row_without_a_verified_code():
    # Policy: a row carrying the VERIFIED ISIN is the company's even when the BSE code is not
    # verified. A row with another ISIN is a mismatch, never 'unverified'.
    data = build([*nse_side(), header_down(BSE), bse_feed(
        bse_ann("r-1", "Letter of Award from NMDC Limited worth Rs. 90 crore", code=BSE,
                ISIN=ISIN),
        bse_ann("r-2", IRCON_LOA, code=BSE, ISIN=IRCON_ISIN), code=BSE)])
    (event,) = data.events
    assert event.financial_value_crore == Decimal(90)
    assert "ROW_IDENTITY_MISMATCH" in codes(data)


def test_row_isin_with_a_contradicting_code_is_rejected():
    data = build([*nse_side(), header_down(BSE), bse_feed(
        bse_ann("r-1", "Dividend Rs 1 per share", code=IRCON_BSE, ISIN=ISIN), code=BSE)])
    assert data.events == () and "ROW_IDENTITY_MISMATCH" in codes(data)


def test_bse_header_conflicting_with_the_declared_code_fails_closed():
    # The header for the declared scripcode names IRCON's ISIN: AMBIGUOUS, nothing attributed.
    ircon_header = bse_header(code=IRCON_BSE, isin=IRCON_ISIN)
    data = build([*nse_side(), ircon_header, bse_feed(bse_ann("irc-1", IRCON_LOA)),
                  nse_announcements(announcement("01-Oct-2026 10:00:00", "Dividend",
                                                 "Dividend Rs 1 per share", seq="1"))],
                 bse_code=IRCON_BSE)
    assert data.identity.status == IdentityStatus.AMBIGUOUS
    assert data.events == () and data.corporate_actions == ()
    # a header stating a different code than declared is a conflict as well
    other = build([*nse_side(), bse_header(code=IRCON_BSE)])
    assert other.identity.status == IdentityStatus.AMBIGUOUS


def test_bse_header_that_only_echoes_the_code_does_not_verify_it():
    echo = raw(PayloadKind.BSE_COMPANY_HEADER, {"SecurityCode": IRCON_BSE},
               f"{BSE_API}/ComHeadernew/w?scripcode={IRCON_BSE}")
    data = build([*nse_side(), echo, bse_feed(bse_ann("irc-1", IRCON_LOA))],
                 bse_code=IRCON_BSE)
    assert data.identity.status == IdentityStatus.PARTIAL
    assert data.identity.bse_code is None
    assert "BSE_HEADER_WITHOUT_ISIN" in {i.code for i in data.identity.issues}
    assert data.events == ()


def test_partial_identity_keeps_verified_nse_data():
    nse_ca = raw(PayloadKind.NSE_CORPORATE_ACTIONS, [
        {"symbol": SYMBOL, "isin": ISIN, "subject": "Dividend - Re 0.71 Per Share",
         "exDate": "18-Aug-2026", "recDate": "18-Aug-2026"}],
        "https://www.nseindia.com/api/corporates-corporateActions?symbol=RVNL")
    data = build([*nse_side(), header_down(), bse_feed(bse_ann("irc-1", IRCON_LOA)),
                  bse_actions(IRCON_DIVIDEND), nse_ca,
                  nse_announcements(announcement("01-Oct-2026 18:54:20",
                                                 "Bagging/Receiving of orders/contracts",
                                                 "Letter of Award from NMDC Limited worth Rs. "
                                                 "500 crore", seq="11"))],
                 bse_code=IRCON_BSE)
    assert data.identity.status == IdentityStatus.PARTIAL
    (event,) = data.events
    assert event.exchanges == ("NSE",) and event.financial_value_crore == Decimal(500)
    (action,) = data.corporate_actions
    assert action.kind == CorporateActionKind.DIVIDEND
    assert action.amount_per_share == Decimal("0.71")
    assert {s.provider for s in action.sources} == {Provider.NSE}


def test_nse_rows_need_a_verified_key_too():
    # NSE master unavailable: the NSE symbol is only declared; the BSE header verifies the
    # ISIN. An NSE row stating the verified ISIN is admitted, a symbol-only row is not.
    master_down = RawPayload(kind=PayloadKind.NSE_EQUITY_MASTER,
                             url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                             status=FetchStatus.BLOCKED, retrieved_at=AS_OF, error_code="HTTP_403")
    symbol_only = announcement("02-Oct-2026 10:00:00", "Dividend", "Dividend Rs 2 per share",
                               seq="2")
    del symbol_only["sm_isin"]
    data = build([master_down, bse_header(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Dividend", "Dividend Rs 1 per share", seq="1"),
        symbol_only)])
    assert data.identity.nse_symbol is None and data.identity.isin == ISIN
    assert [e.document_reference for e in data.events] == ["1"]
    assert "PROVIDER_IDENTITY_UNVERIFIED" in codes(data)


def test_company_scoped_feed_of_another_symbol_is_rejected():
    reports = raw(PayloadKind.NSE_ANNUAL_REPORTS, {"data": [
        {"fromYr": "2025", "toYr": "2026", "disseminationDateTime": "21-AUG-2026 18:47:23",
         "fileName": "https://nsearchives.nseindia.com/annual_reports/AR.pdf"}]},
        "https://www.nseindia.com/api/annual-reports?index=equities&symbol=IRCON")
    data = build([*base_payloads(), reports])
    assert data.filings == () and "ROW_IDENTITY_MISMATCH" in codes(data)
    own = reports.model_copy(update={"url": reports.url.replace("IRCON", SYMBOL)})
    assert len(build([*base_payloads(), own]).filings) == 1
    # without a verified NSE listing the same scoped rows are unverified, not admitted
    unverified = build([header_down(BSE), bse_header(), own], nse_symbol=SYMBOL)
    assert unverified.identity.nse_symbol is None and unverified.filings == ()


# --- M-1: linking ----------------------------------------------------------------------------

D = date(2026, 6, 1)


def rec(n: int, day: date, stage: OrderStage = OrderStage.LOA, *, client: str | None = "NMDC",
        project: str | None = None, value: str | None = None, tax: TaxBasis = TaxBasis.UNKNOWN,
        currency: str = "INR", refs: tuple[str, ...] = (), letters: tuple[date, ...] = (),
        scope: CancellationScope | None = None, pct: str | None = None, doc: bool = True,
        jv: bool | None = None, share: str | None = None) -> OrderRecord:
    if stage == OrderStage.CANCELLATION and scope is None:
        scope = CancellationScope.UNSPECIFIED
    return OrderRecord.model_validate({
        "order_id": uuid5(NAMESPACE_URL, f"o{n}"), "event_id": uuid5(NAMESPACE_URL, f"e{n}"),
        "stage": stage, "announcement_date": day, "client": client,
        "client_key": client.lower() if client else None, "project": project,
        "gross_value_crore": Decimal(value) if value else None,
        "value_currency": currency if value else None, "value_tax_basis": tax,
        "reference_ids": refs, "referenced_letter_dates": letters, "cancellation_scope": scope,
        "cancellation_pct": Decimal(pct) if pct else None,
        "document_sha256": SHA if doc else None, "joint_venture": jv,
        "jv_share_pct": Decimal(share) if share else None,
        "attributable_value_crore": None,
    })


FULL_COVERAGE = LedgerCoverage(complete=True, ledger_start=date(2025, 1, 1))


def chains(*records: OrderRecord, coverage: LedgerCoverage = FULL_COVERAGE) -> list[OrderChain]:
    return sorted(build_chains(records, coverage), key=lambda c: (c.first_announced,
                                                                  str(c.chain_id)))


def test_equal_value_same_client_different_projects_stay_two_orders():
    a = rec(1, D, project="Project Alpha at Kirandul", value="100")
    b = rec(2, date(2026, 7, 1), project="Project Beta at Bacheli", value="100")
    result = chains(a, b)
    assert len(result) == 2
    assert [c.confirmed_gross_value_crore for c in result] == [Decimal(100), Decimal(100)]
    assert all(c.link_rules == () for c in result)


def test_equal_value_same_day_different_projects_stay_two_orders():
    result = chains(rec(1, D, project="Doubling between A and B", value="245.50"),
                    rec(2, D, project="Electrification between C and D", value="245.50"))
    assert len(result) == 2


def test_equal_value_different_clients_stay_two_orders():
    result = chains(rec(1, D, client="Central Railway", value="100"),
                    rec(2, date(2026, 7, 1), client="Western Railway", value="100"))
    assert len(result) == 2


def test_identical_amount_alone_never_links():
    # an L1 and a later LOA of the same client and value, nothing else in common
    bid = rec(1, D, OrderStage.L1_BIDDER, value="100")
    award = rec(2, date(2026, 7, 1), value="100")
    result = chains(bid, award)
    assert len(result) == 2 and all(c.link_rules == () for c in result)
    # two LOAs of one client with the same value and no description: two orders, never one
    assert len(chains(rec(3, D, value="100"), rec(4, date(2026, 7, 1), value="100"))) == 2


def test_explicit_letter_reference_links():
    bid = rec(1, D, OrderStage.L1_BIDDER, value="100")
    award = rec(2, date(2026, 6, 20), value="100", letters=(D,))
    (chain,) = chains(bid, award)
    assert chain.link_rules == ("LETTER_REFERENCE",)
    assert chain.stages == (OrderStage.L1_BIDDER, OrderStage.LOA)
    assert chain.l1_linkage is not None
    assert chain.l1_linkage.resolution == L1Resolution.LINKED_TO_AWARD


def test_same_tender_number_links_even_when_reworded():
    bid = rec(1, D, OrderStage.L1_BIDDER, project="Roadbed and minor bridges, Khurda Road",
              refs=("TENDER:ECR/2026/0123",), value="404.88")
    award = rec(2, date(2026, 6, 20), project="Execution of roadbed works at Khurda Road",
                refs=("TENDER:ECR/2026/0123",), value="404.88")
    (chain,) = chains(bid, award)
    assert chain.link_rules == ("TENDER_REFERENCE",) and chain.status == ChainStatus.CONFIRMED


def test_same_normalized_project_text_links():
    a = rec(1, D, project="Doubling of line, A–B (Phase-II).", value="100")
    b = rec(2, date(2026, 7, 1), OrderStage.SIGNED_CONTRACT, project="doubling of line A B "
            "phase II", value="100")
    (chain,) = chains(a, b)
    assert chain.link_rules == ("PROJECT_TEXT",)
    assert chain.stages == (OrderStage.LOA, OrderStage.SIGNED_CONTRACT)


def test_same_project_text_with_different_tenders_stays_two_orders():
    a = rec(1, D, project="Supply of rails", refs=("TENDER:T-1001",), value="50")
    b = rec(2, date(2026, 7, 1), project="Supply of rails", refs=("TENDER:T-2002",), value="50")
    assert len(chains(a, b)) == 2
    # one tender, two packages: two orders
    c = rec(3, D, refs=("TENDER:T-3003", "PACKAGE:1"), value="10")
    d = rec(4, date(2026, 7, 1), refs=("TENDER:T-3003", "PACKAGE:2"), value="10")
    assert len(chains(c, d)) == 2


def test_two_legitimate_equal_value_orders_on_one_day():
    a = rec(1, D, refs=("LOA:L-0001",), value="75")
    b = rec(2, D, refs=("LOA:L-0002",), value="75")
    result = chains(a, b)
    assert len(result) == 2 and sum(c.confirmed_gross_value_crore or 0 for c in result) == 150


def test_value_revised_between_l1_and_loa_still_links_by_reference():
    bid = rec(1, D, OrderStage.L1_BIDDER, value="100", project="Tunnel T-5")
    award = rec(2, date(2026, 6, 20), value="110", letters=(D,), project="Tunnel T-5")
    (chain,) = chains(bid, award)
    assert chain.confirmed_gross_value_crore == Decimal(110)
    assert "L1_VALUE_DIFFERS_FROM_AWARD" in {i.code for i in chain.issues}


def test_duplicate_refiling_is_counted_once():
    a = rec(1, D, project="Tunnel T-5", value="100")
    again = rec(2, date(2026, 6, 2), project="Tunnel T-5", value="100")
    (chain,) = chains(a, again)
    assert chain.link_rules == ("DUPLICATE_STAGE_PROJECT_TEXT",)
    assert [t.kind for t in chain.transitions] == [TransitionKind.AWARD, TransitionKind.DUPLICATE]
    assert chain.confirmed_gross_value_crore == Decimal(100)


def test_letter_reference_to_a_different_project_is_blocked_and_flagged():
    bid = rec(1, D, OrderStage.L1_BIDDER, project="Bridge at Godavari")
    award = rec(2, date(2026, 6, 20), project="Tunnel at Banihal", letters=(D,))
    result = chains(bid, award)
    assert len(result) == 2
    loa = next(c for c in result if c.status == ChainStatus.CONFIRMED)
    assert "ORDER_LINK_BLOCKED" in {i.code for i in loa.issues}


def test_reference_numbers_keep_every_meaningful_character():
    assert reference_ids("Tender No. ECR/2026/0123, Package No. 3; LOA No: RVNL-LOA-77.") == (
        "LOA:RVNL-LOA-77", "PACKAGE:3", "TENDER:ECR/2026/0123")
    assert reference_ids("the contract no longer applies; tender number pending") == ()


def test_end_to_end_equal_value_projects_from_filings_stay_separate():
    u1 = "https://nsearchives.nseindia.com/corporate/P1.pdf"
    u2 = "https://nsearchives.nseindia.com/corporate/P2.pdf"
    texts = {u1: "Project Alpha at Kirandul", u2: "Project Beta at Bacheli"}
    data = build([*base_payloads(), nse_announcements(
        _loa("1", "01-Jun-2026 10:00:00", "Letter of Award from NMDC Limited", u1),
        _loa("2", "01-Jul-2026 10:00:00", "Letter of Award from NMDC Limited", u2))],
        document_texts=tuple(DocumentText(
            source_url=u, document_sha256=SHA,
            text=f"received Letter of Award from NMDC Limited for “{p}”. Broad consideration "
                 "or size of the order Rs. 100 crore") for u, p in texts.items()))
    assert len(data.order_chains) == 2
    assert all(c.confirmed_gross_value_crore == Decimal(100) for c in data.order_chains)


# --- M-2: chronological state machine ---------------------------------------------------------

P = "Doubling of line between A and B"
L1D, LOAD, XD, RD = D, date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)


def award(value: str = "100", **kw: Any) -> OrderRecord:
    return rec(1, LOAD, project=P, value=value, **kw)


def cancel(n: int = 2, day: date = XD, **kw: Any) -> OrderRecord:
    return rec(n, day, OrderStage.CANCELLATION, letters=(LOAD,), **kw)


def one(*records: OrderRecord) -> OrderChain:
    (chain,) = chains(*records)
    return chain


def test_full_cancellation_removes_the_whole_active_value():
    chain = one(award(), cancel(scope=CancellationScope.FULL))
    assert chain.status == ChainStatus.CANCELLED and not chain.counted_in_confirmed
    assert chain.cancelled_value_crore == Decimal(100)
    assert chain.confirmed_gross_value_crore is None
    assert [t.kind for t in chain.transitions] == [TransitionKind.AWARD,
                                                   TransitionKind.FULL_CANCELLATION]


def test_partial_cancellation_removes_only_the_stated_amount():
    chain = one(award(), cancel(scope=CancellationScope.PARTIAL, value="30"))
    assert chain.status == ChainStatus.PARTIALLY_CANCELLED and chain.counted_in_confirmed
    assert chain.confirmed_gross_value_crore == Decimal(70)  # 100 - 30
    assert chain.cancelled_value_crore == Decimal(30)
    step = chain.transitions[-1]
    assert (step.value_before_crore, step.amount_crore, step.value_after_crore) == (
        Decimal(100), Decimal(30), Decimal(70))
    analytics = order_book_analytics([chain], [], request(), date(2025, 1, 1))
    rate = {m.kind: m for m in analytics.metrics}[K.CANCELLATION_RATE]
    assert rate.value == Decimal("30.000000")  # 30 / 100, never 100 / 100


def test_partial_cancellation_by_stated_percentage():
    chain = one(award(), cancel(scope=CancellationScope.PARTIAL, pct="25"))
    assert chain.confirmed_gross_value_crore == Decimal("75.000000")  # 100 - 25 % of 100
    assert chain.cancelled_value_crore == Decimal("25.000000")


def test_partial_cancellation_without_an_amount_leaves_the_remainder_unknown():
    chain = one(award(), cancel(scope=CancellationScope.PARTIAL))
    assert chain.status == ChainStatus.PARTIALLY_CANCELLED
    assert chain.confirmed_gross_value_crore is None and chain.cancelled_value_crore is None
    assert "PARTIAL_CANCELLATION_VALUE_UNKNOWN" in {i.code for i in chain.issues}


def test_ambiguous_cancellation_never_assumes_full():
    chain = one(award(), cancel())  # UNSPECIFIED, no amount
    assert chain.status == ChainStatus.CANCELLATION_UNRESOLVED and not chain.counted_in_confirmed
    assert chain.cancelled_value_crore is None and chain.confirmed_gross_value_crore is None
    assert chain.transitions[-1].kind == TransitionKind.UNRESOLVED_CANCELLATION
    analytics = order_book_analytics([chain], [], request(), date(2025, 1, 1))
    rate = {m.kind: m for m in analytics.metrics}[K.CANCELLATION_RATE]
    assert rate.value is None and rate.reason == "CANCELLATION_UNRESOLVED"


def test_unspecified_cancellation_with_a_smaller_stated_amount_is_partial():
    chain = one(award(), cancel(value="40"))
    assert chain.status == ChainStatus.PARTIALLY_CANCELLED
    assert chain.confirmed_gross_value_crore == Decimal(60)  # 100 - 40


def test_full_wording_with_a_conflicting_amount_is_unresolved():
    chain = one(award(), cancel(scope=CancellationScope.FULL, value="30"))
    assert chain.status == ChainStatus.CANCELLATION_UNRESOLVED
    assert chain.cancelled_value_crore is None


def test_reaward_after_full_cancellation_reactivates_the_chain():
    chain = one(award(), cancel(scope=CancellationScope.FULL),
                rec(3, RD, project=P, value="120"))
    assert chain.status == ChainStatus.CONFIRMED and chain.counted_in_confirmed
    assert chain.reawarded
    assert chain.confirmed_gross_value_crore == Decimal(120)  # active
    assert chain.cancelled_value_crore == Decimal(100)  # historical
    assert [(t.kind, t.amount_crore) for t in chain.transitions] == [
        (TransitionKind.AWARD, Decimal(100)), (TransitionKind.FULL_CANCELLATION, Decimal(100)),
        (TransitionKind.REAWARD, Decimal(120))]
    analytics = order_book_analytics([chain], [], request(), date(2025, 1, 1))
    m = {x.kind: x for x in analytics.metrics}
    assert m[K.DISCLOSED_CONFIRMED_INFLOW].value == Decimal(220)  # two awards booked
    assert m[K.CANCELLATION_RATE].value == Decimal("45.454545")  # 100 / (100 + 120)


def test_revision_replaces_the_value_and_records_the_delta():
    chain = one(award(), rec(2, XD, OrderStage.REVISION, project=P, value="120"))
    assert chain.confirmed_gross_value_crore == Decimal(120)  # never 220
    assert chain.revision_delta_crore == Decimal(20)
    step = chain.transitions[-1]
    assert (step.value_before_crore, step.value_after_crore, step.amount_crore) == (
        Decimal(100), Decimal(120), Decimal(20))


def test_revision_after_partial_and_after_full_cancellation():
    partial = one(award(), cancel(scope=CancellationScope.PARTIAL, value="30"),
                  rec(3, RD, OrderStage.REVISION, project=P, value="80"))
    assert partial.confirmed_gross_value_crore == Decimal(80)
    assert partial.revision_delta_crore == Decimal(10)  # 80 - (100 - 30)
    full = one(award(), cancel(scope=CancellationScope.FULL),
               rec(3, RD, OrderStage.REVISION, project=P, value="80"))
    assert full.status == ChainStatus.CANCELLED and full.confirmed_gross_value_crore is None
    assert "REVISION_AFTER_FULL_CANCELLATION" in {i.code for i in full.issues}


def test_cancellation_after_revision_cancels_the_revised_value():
    chain = one(award(), rec(2, XD, OrderStage.REVISION, project=P, value="120"),
                cancel(3, RD, scope=CancellationScope.FULL))
    assert chain.cancelled_value_crore == Decimal(120)
    analytics = order_book_analytics([chain], [], request(), date(2025, 1, 1))
    rate = {m.kind: m for m in analytics.metrics}[K.CANCELLATION_RATE]
    assert rate.value == Decimal(100)  # 120 / (100 + 20)


@pytest.mark.parametrize(
    ("order", "cancellation", "reason"),
    [
        ({}, {"currency": "USD"}, "CURRENCY_MISMATCH"),
        ({"tax": TaxBasis.INCLUSIVE_OF_TAX}, {"tax": TaxBasis.EXCLUSIVE_OF_TAX},
         "TAX_BASIS_NOT_COMPARABLE"),
        ({"tax": TaxBasis.INCLUSIVE_OF_TAX}, {}, "TAX_BASIS_NOT_COMPARABLE"),  # UNKNOWN
        ({"jv": True, "share": "60"}, {"jv": True, "share": "40"}, "JV_ATTRIBUTION_MISMATCH"),
    ],
)
def test_incomparable_cancellation_is_never_subtracted(order, cancellation, reason):
    chain = one(award(**order), cancel(scope=CancellationScope.PARTIAL, value="30",
                                       **cancellation))
    assert chain.status == ChainStatus.PARTIALLY_CANCELLED
    assert chain.confirmed_gross_value_crore is None and chain.cancelled_value_crore is None
    issue = next(i for i in chain.issues if i.code == "CANCELLATION_NOT_COMPARABLE")
    assert reason in issue.message


def test_full_cancellation_does_not_need_the_amount_to_be_comparable():
    chain = one(award(tax=TaxBasis.INCLUSIVE_OF_TAX),
                cancel(scope=CancellationScope.FULL, value="100",
                       tax=TaxBasis.EXCLUSIVE_OF_TAX))
    assert chain.status == ChainStatus.CANCELLED
    assert chain.cancelled_value_crore == Decimal(100)  # the whole active value, not a sum
    assert "CANCELLATION_NOT_COMPARABLE" in {i.code for i in chain.issues}


def test_cancellation_scope_is_read_only_from_explicit_wording():
    u1 = "https://nsearchives.nseindia.com/corporate/A1.pdf"
    ux = "https://nsearchives.nseindia.com/corporate/X1.pdf"
    project = "for “Doubling of Line between A and B”"

    def run(text: str) -> OrderChain:
        data = build([*base_payloads(), nse_announcements(
            _loa("1", "01-Jun-2026 10:00:00", "Letter of Award from NMDC Limited", u1),
            _loa("2", "01-Jul-2026 10:00:00", "Cancellation of contract from NMDC Limited",
                 ux))],
            document_texts=(
                DocumentText(source_url=u1, document_sha256=SHA,
                             text=f"received Letter of Award from NMDC Limited {project}. "
                                  "Broad consideration or size of the order Rs. 100 crore"),
                DocumentText(source_url=ux, document_sha256=SHA,
                             text=f"in continuation to our letter dated 01.06.2026 {text}")))
        (chain,) = data.order_chains
        return chain

    partial = run("the contract has been partially cancelled. Broad consideration or size of "
                  "the order Rs. 30 crore")
    assert (partial.status, partial.confirmed_gross_value_crore) == (
        ChainStatus.PARTIALLY_CANCELLED, Decimal(70))
    share = run("25% of the contract is cancelled (partial short closure)")
    assert share.confirmed_gross_value_crore == Decimal("75.000000")
    whole = run("the LOA from NMDC Limited has been cancelled")
    assert whole.status == ChainStatus.CANCELLED and whole.cancelled_value_crore == Decimal(100)


# --- M-3: comparability contract --------------------------------------------------------------


def q(value: str = "100", currency: str = "INR", tax: TaxBasis = TaxBasis.EXCLUSIVE_OF_TAX,
      basis: FinancialBasis = FinancialBasis.CONSOLIDATED, unit: str = "CRORE") -> Quantity:
    return Quantity("x", Decimal(value), currency, tax, basis, unit)


def test_comparability_validator_dimensions():
    assert check_comparable([q(), q()]).ok
    assert check_comparable([q(), q(currency="USD")]).reasons == ("CURRENCY_MISMATCH",)
    assert check_comparable([q(), q(basis=FinancialBasis.STANDALONE)]).reasons == (
        "BASIS_MISMATCH",)
    assert check_comparable([q(), q(basis=FinancialBasis.UNKNOWN)]).reasons == ("BASIS_UNKNOWN",)
    assert check_comparable([q(), q(basis=FinancialBasis.BOTH)]).reasons == ("BASIS_UNKNOWN",)
    assert check_comparable([q(), q(basis=FinancialBasis.NOT_APPLICABLE)]).ok  # disclosed order
    assert check_comparable([q(), q(tax=TaxBasis.INCLUSIVE_OF_TAX)]).reasons == (
        "TAX_BASIS_NOT_COMPARABLE",)
    assert check_comparable([q(), q(tax=TaxBasis.UNKNOWN)]).reasons == (
        "TAX_BASIS_NOT_COMPARABLE",)
    unknown = [q(tax=TaxBasis.UNKNOWN), q(tax=TaxBasis.UNKNOWN)]
    assert not check_comparable(unknown).ok  # never against revenue / a reported book
    within = check_comparable(unknown, ORDER_VALUES, unknown_matches_unknown=True)
    assert within.ok and within.notes == (TAX_UNKNOWN_NOTE,)
    policy = check_comparable([q(), q(tax=TaxBasis.INCLUSIVE_OF_TAX)], allow_tax_policy=True)
    assert policy.ok and policy.notes == (TAX_POLICY_NOTE,)
    assert check_comparable([q(), q(unit="LAKH")]).reasons == ("UNIT_MISMATCH",)
    both = check_comparable([q(), q(currency="USD", tax=TaxBasis.INCLUSIVE_OF_TAX)])
    assert both.reasons == ("CURRENCY_MISMATCH", "TAX_BASIS_NOT_COMPARABLE")
    assert check_comparable([q(), q(currency="USD")], (Dimension.TAX,)).ok  # only named dims
    assert check_comparable([q()], ALL).ok
    assert same_period((date(2025, 4, 1), date(2026, 3, 31)),
                       (date(2025, 4, 1), date(2026, 3, 31))) is None
    assert same_period((date(2025, 4, 1), date(2026, 3, 31)),
                       (date(2025, 4, 1), date(2026, 2, 28))) == "PERIOD_MISMATCH"
    assert as_of_equals(date(2019, 3, 31), date(2026, 3, 31)) == "PERIOD_MISMATCH"


CITE = Citation(provider=Provider.COMPANY, authority=Authority.PRIMARY_COMPANY, reference="AR")
FY26 = (date(2025, 4, 1), date(2026, 3, 31))


def revenue(**kw: Any) -> PeriodRevenue:
    base: dict[str, Any] = {"period_start": FY26[0], "period_end": FY26[1],
                            "value_crore": Decimal(20000), "basis": FinancialBasis.CONSOLIDATED,
                            "citation": CITE}
    return PeriodRevenue.model_validate({**base, **kw})


def book(as_of: date, value: int = 90000, **kw: Any) -> ReportedOrderBook:
    base: dict[str, Any] = {"as_of": as_of, "value_crore": Decimal(value),
                            "tax_basis": TaxBasis.EXCLUSIVE_OF_TAX,
                            "basis": FinancialBasis.CONSOLIDATED, "citation": CITE}
    return ReportedOrderBook.model_validate({**base, **kw})


def metrics(data: Any) -> dict[OrderBookMetricKind, Any]:
    return {m.kind: m for m in data.order_book.metrics}


OPEN, CLOSE = date(2025, 3, 31), date(2026, 3, 31)


@pytest.mark.parametrize(
    ("closing", "reason"),
    [
        ({"currency": "USD"}, "CURRENCY_MISMATCH"),
        ({"basis": FinancialBasis.STANDALONE}, "BASIS_MISMATCH"),
        ({"basis": FinancialBasis.UNKNOWN}, "BASIS_UNKNOWN"),
        ({"tax_basis": TaxBasis.INCLUSIVE_OF_TAX}, "TAX_BASIS_NOT_COMPARABLE"),
        ({"tax_basis": TaxBasis.UNKNOWN}, "TAX_BASIS_NOT_COMPARABLE"),
    ],
)
def test_order_book_to_revenue_needs_comparable_book(closing, reason):
    data = build(base_payloads(), period_revenues=(revenue(),),
                 reported_order_books=(book(CLOSE, **closing),))
    metric = metrics(data)[K.ORDER_BOOK_TO_REVENUE]
    assert metric.value is None and metric.reason == reason


def test_order_book_must_be_at_the_revenue_period_end():
    stale = build(base_payloads(), period_revenues=(revenue(),),
                  reported_order_books=(book(date(2019, 3, 31), 50000),))
    metric = metrics(stale)[K.ORDER_BOOK_TO_REVENUE]
    assert metric.value is None and metric.reason == "PERIOD_MISMATCH"
    near = build(base_payloads(), period_revenues=(revenue(),),
                 reported_order_books=(book(date(2026, 3, 30)),))
    assert metrics(near)[K.ORDER_BOOK_TO_REVENUE].reason == "PERIOD_MISMATCH"  # never closest
    exact = build(base_payloads(), period_revenues=(revenue(),),
                  reported_order_books=(book(date(2019, 3, 31), 50000), book(CLOSE)))
    assert metrics(exact)[K.ORDER_BOOK_TO_REVENUE].value == Decimal("4.500000")  # 90000/20000


def order_feed(*extra: dict[str, Any]) -> list[RawPayload]:
    return [*base_payloads(), nse_announcements(
        announcement("01-Mar-2025 10:00:00", "Updates", "Newspaper", seq="0"),
        _loa("1", "01-Jun-2025 10:00:00", "Letter of Award from NMDC Limited worth Rs. 1000 "
             "crore (excluding GST)", "https://nsearchives.nseindia.com/corporate/O1.pdf"),
        *extra)]


def test_reported_inflow_must_be_comparable_and_same_period():
    usd = ReportedInflow(period_start=FY26[0], period_end=FY26[1], value_crore=Decimal(10),
                         currency="USD", tax_basis=TaxBasis.EXCLUSIVE_OF_TAX, citation=CITE)
    data = build(order_feed(), period_revenues=(revenue(),), reported_inflows=(usd,),
                 reported_order_books=(book(OPEN),))
    b2b = metrics(data)[K.BOOK_TO_BILL]
    # the USD figure is never divided by INR revenue: the ledger's 1000 is used, flagged
    assert b2b.value == Decimal("0.050000") and b2b.completeness == "DISCLOSED_ORDERS_ONLY"
    assert "REPORTED_INFLOW_NOT_COMPARABLE" in b2b.issues
    rec = data.order_book.reconciliation  # type: ignore[union-attr]
    assert rec is not None and rec.inflow_source == "LEDGER_DISCLOSED_ORDERS_ONLY"
    assert rec.inflow_crore == Decimal(1000)
    gst = usd.model_copy(update={"currency": "INR", "tax_basis": TaxBasis.INCLUSIVE_OF_TAX})
    data = build(order_feed(), period_revenues=(revenue(),), reported_inflows=(gst,))
    assert "REPORTED_INFLOW_TAX_BASIS_NOT_COMPARABLE" in metrics(data)[K.BOOK_TO_BILL].issues
    other_period = usd.model_copy(update={"currency": "INR", "value_crore": Decimal(5000),
                                          "period_end": date(2026, 2, 28)})
    data = build(order_feed(), period_revenues=(revenue(),), reported_inflows=(other_period,))
    b2b = metrics(data)[K.BOOK_TO_BILL]
    assert b2b.value == Decimal("0.050000")  # 1000 / 20000: the Feb-end figure is not used
    good = other_period.model_copy(update={"period_end": FY26[1]})
    data = build(order_feed(), period_revenues=(revenue(),), reported_inflows=(good,))
    assert metrics(data)[K.BOOK_TO_BILL].value == Decimal("0.250000")  # 5000 / 20000


def test_usd_revenue_is_never_divided_into_inr_inflow():
    data = build(order_feed(), period_revenues=(revenue(currency="USD"),))
    b2b = metrics(data)[K.BOOK_TO_BILL]
    assert b2b.value is None and b2b.reason == "CURRENCY_MISMATCH"


@pytest.mark.parametrize(
    ("closing", "reason"),
    [
        ({"currency": "USD"}, "CLOSING_CURRENCY_MISMATCH"),
        ({"basis": FinancialBasis.STANDALONE}, "CLOSING_BASIS_MISMATCH"),
        ({"tax_basis": TaxBasis.INCLUSIVE_OF_TAX}, "CLOSING_TAX_BASIS_NOT_COMPARABLE"),
    ],
)
def test_incomparable_closing_book_never_produces_a_difference(closing, reason):
    data = build(order_feed(), period_revenues=(revenue(),),
                 reported_order_books=(book(OPEN), book(CLOSE, 95000, **closing)))
    rec = data.order_book.reconciliation  # type: ignore[union-attr]
    assert rec is not None and rec.status == MetricStatus.AVAILABLE
    assert rec.implied_closing_crore == Decimal(71000)  # 90000 + 1000 - 20000
    assert rec.difference_crore is None and rec.reported_closing_crore is None
    assert {"CLOSING_BOOK_NOT_COMPARABLE", reason} <= set(rec.issues)


def test_consolidated_opening_against_standalone_revenue_is_unavailable():
    data = build(order_feed(), period_revenues=(revenue(basis=FinancialBasis.STANDALONE),),
                 reported_order_books=(book(OPEN),))
    rec = data.order_book.reconciliation  # type: ignore[union-attr]
    assert rec is not None and rec.reason == "BASIS_MISMATCH" and rec.implied_closing_crore is None
    assert metrics(data)[K.EXECUTION_RATE].reason == "BASIS_MISMATCH"


def test_reconciliation_subtracts_only_comparable_cancellations():
    exclusive = ReportedInflow(period_start=FY26[0], period_end=FY26[1],
                               value_crore=Decimal(3000), tax_basis=TaxBasis.EXCLUSIVE_OF_TAX,
                               citation=CITE)
    a = "https://nsearchives.nseindia.com/corporate/G1.pdf"
    x = "https://nsearchives.nseindia.com/corporate/GX.pdf"
    feed = [*base_payloads(), nse_announcements(
        announcement("01-Jan-2025 10:00:00", "Updates", "Newspaper", seq="0"),
        _loa("1", "01-Feb-2025 10:00:00", "Letter of Award from NMDC Limited worth Rs. 400 "
             "crore (Incl. GST)", a),
        _loa("2", "01-Aug-2025 10:00:00", "Cancellation of Letter of Award from NMDC Limited",
             x))]
    texts = (DocumentText(source_url=x, document_sha256=SHA, text="in continuation to our "
                          "letter dated 01.02.2025 the LOA from NMDC Limited has been cancelled"),)
    data = build(feed, period_revenues=(revenue(),), reported_inflows=(exclusive,),
                 reported_order_books=(book(OPEN),), document_texts=texts)
    rec = data.order_book.reconciliation  # type: ignore[union-attr]
    # the GST-inclusive 400 is never subtracted from an ex-GST opening book
    assert rec is not None and rec.status == MetricStatus.UNAVAILABLE
    assert rec.reason == "CANCELLATION_NOT_COMPARABLE"
    allowed = build(feed, period_revenues=(revenue(),), reported_inflows=(exclusive,),
                    reported_order_books=(book(OPEN),), document_texts=texts,
                    allow_tax_inclusive_order_values=True)
    rec = allowed.order_book.reconciliation  # type: ignore[union-attr]
    assert rec is not None and rec.cancellations_crore == Decimal(400)
    assert rec.implied_closing_crore == Decimal(72600)  # 90000 + 3000 - 20000 - 400
    assert TAX_POLICY_NOTE in rec.issues


# --- M-4: pending L1 linkage ------------------------------------------------------------------


def bid(n: int = 1, day: date = date(2026, 6, 1), value: str = "100", **kw: Any) -> OrderRecord:
    return rec(n, day, OrderStage.L1_BIDDER, value=value, **kw)


def pending(*records: OrderRecord, coverage: LedgerCoverage = FULL_COVERAGE) -> dict[str, Any]:
    result = chains(*records, coverage=coverage)
    analytics = order_book_analytics(result, records, request(), date(2025, 1, 1))
    return {"chains": result, "m": {x.kind: x for x in analytics.metrics}}


def test_complete_coverage_and_no_later_award_is_true_pending():
    out = pending(bid(project="Bridge at Godavari"))
    (chain,) = out["chains"]
    linkage = chain.l1_linkage
    assert linkage is not None and linkage.resolution == L1Resolution.TRUE_PENDING
    assert linkage.follow_up_search_performed and linkage.event_coverage_complete
    assert linkage.document_text_available and linkage.strong_link_key
    assert linkage.reasons == ("NO_LATER_AWARD_FOUND",)
    m = out["m"]
    assert m[K.DISCLOSED_PENDING_L1_VALUE].value == Decimal(100)
    assert m[K.PENDING_L1_TRUE_PENDING_VALUE].value == Decimal(100)
    assert m[K.PENDING_L1_UNRESOLVED_VALUE].value == 0


def test_missing_attachment_leaves_linkage_unresolved():
    # neither the bid nor a later same-client award has attachment text: either could be
    # the other, so the bid is not reported as pending and nothing is guessed either way
    out = pending(bid(doc=False), rec(2, date(2026, 7, 1), value="100", doc=False))
    bid_chain = next(c for c in out["chains"] if c.status == ChainStatus.PENDING_L1)
    linkage = bid_chain.l1_linkage
    assert linkage is not None and linkage.resolution == L1Resolution.UNRESOLVED_LINKAGE
    assert linkage.unresolved_award_candidates == 1
    assert {"L1_DOCUMENT_TEXT_ABSENT", "NO_STRONG_LINK_KEY",
            "LATER_AWARD_NEITHER_LINKED_NOR_RULED_OUT"} <= set(linkage.reasons)
    assert len(out["chains"]) == 2  # not linked by value or client
    m = out["m"]
    assert m[K.DISCLOSED_PENDING_L1_VALUE].value is None
    assert m[K.DISCLOSED_PENDING_L1_VALUE].reason == "PENDING_L1_LINKAGE_UNRESOLVED"
    assert m[K.PENDING_L1_TRUE_PENDING_VALUE].value == 0
    assert m[K.PENDING_L1_UNRESOLVED_VALUE].value == Decimal(100)


def test_truncated_source_is_source_incomplete():
    out = pending(bid(project="Bridge at Godavari"), coverage=LedgerCoverage(
        complete=False, ledger_start=date(2025, 1, 1),
        reasons=("ANNOUNCEMENT_FEED_TRUNCATED",)))
    linkage = out["chains"][0].l1_linkage
    assert linkage.resolution == L1Resolution.SOURCE_INCOMPLETE
    assert "ANNOUNCEMENT_FEED_TRUNCATED" in linkage.reasons and not linkage.event_coverage_complete
    assert out["m"][K.DISCLOSED_PENDING_L1_VALUE].value is None


def test_later_awards_ruled_out_by_explicit_evidence_keep_the_bid_pending():
    out = pending(bid(project="Bridge at Godavari"),
                  rec(2, date(2026, 7, 1), project="Tunnel at Banihal", value="100"),
                  rec(3, date(2026, 7, 2), value="100", letters=(date(2026, 3, 3),)),
                  # a different client alone proves nothing: its different project rules it out
                  rec(4, date(2026, 7, 3), client="Western Railway", project="Station "
                      "redevelopment at Surat", value="100"),
                  rec(5, date(2026, 5, 1), value="100", doc=False))  # before the bid
    linkage = next(c for c in out["chains"] if c.status == ChainStatus.PENDING_L1).l1_linkage
    assert linkage is not None and linkage.resolution == L1Resolution.TRUE_PENDING


def test_aggregate_never_overstates_the_pending_pipeline():
    # the later award names another project: it rules out bid 1 (which names its project)
    # but not bid 2 (whose attachment is missing)
    out = pending(bid(1, project="Bridge at Godavari", value="100"),
                  bid(2, date(2026, 6, 5), value="250", doc=False),
                  rec(3, date(2026, 8, 1), project="Tunnel at Banihal", value="250"))
    m = out["m"]
    assert m[K.DISCLOSED_PENDING_L1_VALUE].value is None  # 350 would overstate
    assert "UNRESOLVED_L1_CHAINS_1" in m[K.DISCLOSED_PENDING_L1_VALUE].issues
    assert m[K.PENDING_L1_TRUE_PENDING_VALUE].value == Decimal(100)
    assert m[K.PENDING_L1_UNRESOLVED_VALUE].value == Decimal(250)
    assert "EXCLUDES_UNRESOLVED_L1_CHAINS" in m[K.PENDING_L1_TRUE_PENDING_VALUE].issues


def test_end_to_end_bid_without_documents_is_unresolved_and_linked_bid_is_linked():
    data = build(order_payloads())  # no attachment text at all
    bid_chain = next(c for c in data.order_chains if c.status == ChainStatus.PENDING_L1)
    assert bid_chain.l1_linkage is not None
    assert bid_chain.l1_linkage.resolution == L1Resolution.UNRESOLVED_LINKAGE
    assert "L1_UNRESOLVED_LINKAGE" in {i.code for i in bid_chain.issues}
    assert metrics(data)[K.DISCLOSED_PENDING_L1_VALUE].reason in (
        "PENDING_L1_LINKAGE_UNRESOLVED", "VALUE_NOT_DISCLOSED_FOR_SOME_ORDERS")
    linked = build(order_payloads(), document_texts=docs())
    (chain,) = linked.order_chains
    assert chain.l1_linkage is not None
    assert chain.l1_linkage.resolution == L1Resolution.LINKED_TO_AWARD


def test_failed_or_unverified_announcement_feeds_are_source_incomplete():
    bid_row = announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL)
    old = announcement("01-Jan-2025 10:00:00", "General Updates", "Newspaper", seq="900")
    blocked_bse = RawPayload(kind=PayloadKind.BSE_ANNOUNCEMENTS,
                             url=f"{BSE_API}/AnnSubCategoryGetData/w?strScrip={BSE}",
                             status=FetchStatus.BLOCKED, retrieved_at=AS_OF,
                             error_code="HTTP_403")
    # NSE answered completely: one complete verified exchange feed is enough
    complete = build([*base_payloads(), nse_announcements(bid_row, old), blocked_bse],
                     document_texts=docs())
    (chain,) = complete.order_chains
    assert chain.l1_linkage.resolution == L1Resolution.TRUE_PENDING  # type: ignore[union-attr]
    # a second NSE page failed: the NSE feed is no longer complete
    blocked_nse = RawPayload(kind=PayloadKind.NSE_ANNOUNCEMENTS,
                             url="https://www.nseindia.com/api/corporate-announcements?page=2",
                             status=FetchStatus.BLOCKED, retrieved_at=AS_OF,
                             error_code="HTTP_403")
    partial = build([*base_payloads(), nse_announcements(bid_row, old), blocked_nse,
                     blocked_bse], document_texts=docs())
    (chain,) = partial.order_chains
    linkage = chain.l1_linkage
    assert linkage is not None and linkage.resolution == L1Resolution.SOURCE_INCOMPLETE
    assert {"NSE_ANNOUNCEMENTS_SOURCE_FAILED",
            "NO_COMPLETE_VERIFIED_ANNOUNCEMENT_FEED"} <= set(linkage.reasons)
    assert metrics(partial)[K.DISCLOSED_PENDING_L1_VALUE].value is None


def test_listings_verified_separately_are_not_tied_without_an_nse_isin():
    # NSE master down: Kite verifies the RVNL symbol (no ISIN); the BSE header for the
    # declared (IRCON) code states IRCON's ISIN. Nothing ties the two listings together.
    master_down = RawPayload(kind=PayloadKind.NSE_EQUITY_MASTER,
                             url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                             status=FetchStatus.BLOCKED, retrieved_at=AS_OF, error_code="HTTP_403")
    data = build([master_down, kite_master(), bse_header(code=IRCON_BSE, isin=IRCON_ISIN),
                  bse_feed(bse_ann("irc-1", IRCON_LOA)), bse_actions(IRCON_DIVIDEND),
                  nse_announcements(announcement("01-Oct-2026 10:00:00", "Dividend",
                                                 "Dividend Rs 1 per share", seq="1"))],
                 bse_code=IRCON_BSE)
    identity = data.identity
    assert identity.status == IdentityStatus.PARTIAL
    assert (identity.nse_symbol, identity.bse_code, identity.isin) == (SYMBOL, None, None)
    assert "LISTINGS_NOT_CROSS_VERIFIED" in {i.code for i in identity.issues}
    assert [e.exchanges for e in data.events] == [("NSE",)]  # RVNL's own NSE filing only
    assert data.orders == () and data.corporate_actions == ()


# --- residual A: a noisy client name never makes a bid falsely TRUE_PENDING -------------------

NOISY_BID_CLIENT = "M. P"
NOISY_AWARD_CLIENT = "Madhya Pradesh Paschim Kshetra Vidyut Vitran Co Ltd"


def linkage_of(result: list[OrderChain]) -> Any:
    bid_chain = next(c for c in result if c.status == ChainStatus.PENDING_L1)
    assert bid_chain.l1_linkage is not None
    return bid_chain.l1_linkage


def test_same_project_with_a_noisy_client_name_is_not_true_pending():
    bid_ = bid(client=NOISY_BID_CLIENT, project="Project Alpha")
    award_ = rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, project="Project Alpha",
                 value="100")
    result = chains(bid_, award_)
    assert len(result) == 2  # client strings differ: the linking rules do not merge them
    linkage = linkage_of(result)
    assert linkage.resolution == L1Resolution.UNRESOLVED_LINKAGE
    assert linkage.candidate_order_ids == (award_.order_id,)


def test_same_tender_with_a_noisy_client_name_is_not_true_pending():
    bid_ = bid(client=NOISY_BID_CLIENT, refs=("TENDER:MPPKVVCL-2024-77",))
    award_ = rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT,
                 refs=("TENDER:MPPKVVCL-2024-77",), value="100")
    assert linkage_of(chains(bid_, award_)).resolution == L1Resolution.UNRESOLVED_LINKAGE


def test_letter_reference_with_a_noisy_client_name_is_not_true_pending():
    bid_ = bid(client=NOISY_BID_CLIENT, project="Project Alpha")
    award_ = rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, letters=(bid_.announcement_date,),
                 value="100")
    assert linkage_of(chains(bid_, award_)).resolution == L1Resolution.UNRESOLVED_LINKAGE
    # with the same client the explicit reference links them (the normal strong rule)
    same = rec(3, date(2026, 7, 1), client="NMDC", letters=(bid_.announcement_date,),
               value="100")
    (chain,) = chains(bid(client="NMDC", project="Project Alpha"), same)
    assert chain.l1_linkage is not None
    assert chain.l1_linkage.resolution == L1Resolution.LINKED_TO_AWARD


def test_different_client_and_different_project_is_ruled_out():
    result = chains(bid(client=NOISY_BID_CLIENT, project="Project Alpha"),
                    rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, project="Project Beta",
                        value="100"))
    assert linkage_of(result).resolution == L1Resolution.TRUE_PENDING


def test_same_client_different_project_is_not_linked_and_ruled_out():
    result = chains(bid(project="Project Alpha"),
                    rec(2, date(2026, 7, 1), project="Project Beta", value="100"))
    assert len(result) == 2
    assert linkage_of(result).resolution == L1Resolution.TRUE_PENDING


def test_different_explicit_tenders_rule_out_even_the_same_project_text():
    result = chains(bid(client=NOISY_BID_CLIENT, project="Project Alpha",
                        refs=("TENDER:T-1001",)),
                    rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, project="Project Alpha",
                        refs=("TENDER:T-2002",), value="100"))
    assert linkage_of(result).resolution == L1Resolution.TRUE_PENDING


def test_two_possible_later_awards_stay_unresolved_never_guessed():
    a = rec(2, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, project="Project Alpha", value="100")
    b = rec(3, date(2026, 8, 1), client="MPPKVVCL", project="Project Alpha", value="100")
    result = chains(bid(client=NOISY_BID_CLIENT, project="Project Alpha"), a, b)
    linkage = linkage_of(result)
    assert linkage.resolution == L1Resolution.UNRESOLVED_LINKAGE
    assert set(linkage.candidate_order_ids) == {a.order_id, b.order_id}
    assert sum(c.status == ChainStatus.CONFIRMED for c in result) == 2  # nothing merged


def test_noisy_client_bid_is_excluded_from_the_known_pending_total():
    out = pending(bid(1, client=NOISY_BID_CLIENT, project="Project Alpha", value="100"),
                  bid(2, date(2026, 6, 2), project="Bridge at Godavari", value="40"),
                  rec(3, date(2026, 7, 1), client=NOISY_AWARD_CLIENT, project="Project Alpha",
                      value="100"))
    m = out["m"]
    assert m[K.DISCLOSED_PENDING_L1_VALUE].value is None
    assert m[K.PENDING_L1_TRUE_PENDING_VALUE].value == Decimal(40)
    assert m[K.PENDING_L1_UNRESOLVED_VALUE].value == Decimal(100)


# --- residual B: complete-token money parsing ---------------------------------------------------

RUPEES_PER_CRORE = Decimal(10_000_000)


def rupees(grouped: str) -> Decimal:
    """Crore from a rupee amount written with grouping commas: drop the commas, divide."""
    return Decimal(grouped.replace(",", "")) / RUPEES_PER_CRORE


@pytest.mark.parametrize(
    ("text", "crore"),
    [
        ("Rs. 1134,11,42,182", rupees("1134,11,42,182")),  # 11,341,142,182 rupees
        ("Rs. 1,134,11,42,182", rupees("1134,11,42,182")),
        ("Rs. 110,00,00,000", Decimal(110)),  # 1,100,000,000 / 10^7
        ("Rs. 7,20,000", Decimal("0.072")),  # 720,000 / 10^7
        ("Rs. 1,23,45,678", rupees("12345678")),
        ("Rs. 404,88,18,738/-", Decimal("404.8818738")),
        ("₹1,234", Decimal(1234) / RUPEES_PER_CRORE),
        ("₹1,234,567", Decimal(1234567) / RUPEES_PER_CRORE),
        ("₹123456789", Decimal("12.3456789")),
        ("Rs. 1134.50", Decimal("1134.50") / RUPEES_PER_CRORE),
        ("Rs. 148,26,89 ,066", rupees("148,26,89,066")),  # extraction blank before a comma
        ("Rs 148 ,26,89,066/-", rupees("148,26,89,066")),
        ("INR 164, 18,63,783.67/-", rupees("164,18,63,783.67")),  # blank after a comma
        ("Rs. 1,134 crore", Decimal(1134)),  # an explicit unit is never raw rupees
        ("Rs. 1,134.50 Cr", Decimal("1134.50")),
        ("Rs.100crore", Decimal(100)),
        ("Rs. 72 lakh", Decimal("0.72")),  # 72 x 10^5 / 10^7
        ("₹7.2 lakh", Decimal("0.072")),
        ("USD 7,152,236.59/-", Decimal("7152236.59") / RUPEES_PER_CRORE),  # crore of USD
    ],
)
def test_money_tokens_are_read_whole(text, crore):
    found, malformed = scan_amounts(text)
    assert malformed == 0 and [m.crore for m in found] == [crore]
    assert single_amount(text) == (found[0], False)


@pytest.mark.parametrize(
    "text",
    [
        "Rs. 1134,11,42,",  # a trailing group is missing
        "Rs. 148,26,89 ,",
        "Rs. ,",
        "Rs. 1,,23,000",
        "Rs. 1,lQ,86,49,140.99",  # cut short by an extraction letter: never Rs 1
        "Rs. 12O,00,000",
        "Rs. 1134 11 42 182",  # blanks without commas are never joined
        "Rs. 100, 200 and Rs. 300",  # a list or one number? unreadable, never 100
        "Rs. 1134,11,42",  # Indian groups but no final three-digit group
        "Rs. 1,2,345",
    ],
)
def test_malformed_money_never_yields_a_prefix(text):
    found, malformed = scan_amounts(text)
    assert malformed >= 1
    assert not any(m.crore in (Decimal(1134) / RUPEES_PER_CRORE, Decimal(148) / RUPEES_PER_CRORE,
                               Decimal(1) / RUPEES_PER_CRORE, Decimal(100) / RUPEES_PER_CRORE)
                   for m in found)
    assert single_amount(text)[0] is None


@pytest.mark.parametrize(
    "text",
    ["letter dated 12.03.2026", "Tender No. 1134,11,42,182", "a 25% share", "Tel +91 11 2345 6789",
     "Contract No. 110,00,00,000", "Hrs 10,000"],
)
def test_numbers_outside_monetary_context_are_not_money(text):
    assert scan_amounts(text) == ([], 0)


def test_large_amounts_propagate_through_order_economics():
    a_url = "https://nsearchives.nseindia.com/corporate/BIG1.pdf"
    x_url = "https://nsearchives.nseindia.com/corporate/BIGX.pdf"
    project = "Doubling of track between Navipet and Nizamabad"
    big = "Rs. 1134,11,42,182"
    value = rupees("1134,11,42,182")  # 1134.1142182 crore
    revenue = ReferenceRevenue(fiscal_year=2026, value_crore=Decimal(20000),
                               basis=FinancialBasis.CONSOLIDATED, citation=CITE)
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Jan-2025 10:00:00", "Updates", "Newspaper", seq="0"),
        _loa("1", "01-Jun-2026 10:00:00", f"Letter of Award from South Central Railway worth "
             f"{big}", a_url),
        _loa("2", "01-Jul-2026 10:00:00", "Partial cancellation of contract from South Central "
             "Railway", x_url))],
        reference_revenue=revenue,
        document_texts=(
            DocumentText(source_url=a_url, document_sha256=SHA,
                         text=f"received Letter of Award from South Central Railway for "
                              f"“{project}”. Broad consideration or size of the order {big}/-"),
            DocumentText(source_url=x_url, document_sha256=SHA,
                         text=f"in continuation to our letter dated 01.06.2026 the contract from "
                              f"South Central Railway for “{project}” is partially cancelled. "
                              "Broad consideration or size of the order Rs. 134,11,42,182/-")))
    award_event = next(e for e in data.events if e.event_type.value == "LOA")
    assert award_event.financial_value_crore == value
    assert award_event.materiality is not None
    assert award_event.materiality.ratio_pct == (value / 20000 * 100).quantize(Decimal("0.000001"))
    (chain,) = data.order_chains
    cancelled = rupees("134,11,42,182")
    assert chain.status == ChainStatus.PARTIALLY_CANCELLED
    assert chain.confirmed_gross_value_crore == value - cancelled == Decimal(1000)
    assert chain.confirmed_attributable_value_crore == Decimal(1000)
    m = metrics(data)
    assert m[K.DISCLOSED_CONFIRMED_INFLOW].value == value.quantize(Decimal("0.000001"))
    assert m[K.CANCELLATION_RATE].value == (cancelled / value * 100).quantize(Decimal("0.000001"))


def test_unreadable_annexure_amount_is_flagged_and_never_used():
    url = "https://nsearchives.nseindia.com/corporate/OCR1.pdf"
    data = build([*base_payloads(), nse_announcements(
        _loa("1", "01-Jun-2026 10:00:00", "Letter of Award from Eastern Railway", url))],
        document_texts=(DocumentText(
            source_url=url, document_sha256=SHA,
            text="received Letter of Award from Eastern Railway for “Bypass line”. Broad "
                 "consideration or size of the Rs. 1,lQ,86,49,140.99/ - (Rupees- One Hundred "
                 "Ten Crore ...)"),))
    (order,) = data.orders
    assert order.gross_value_crore is None
    assert "MALFORMED_AMOUNT" in {i.code for i in order.issues}
    (chain,) = data.order_chains
    assert chain.confirmed_gross_value_crore is None


def test_equal_large_values_still_never_link():
    a = rec(1, D, project="Project Alpha", value="1134.1142182")
    b = rec(2, date(2026, 7, 1), project="Project Beta", value="1134.1142182")
    assert len(chains(a, b)) == 2


@pytest.mark.parametrize(
    ("text", "crore"),
    [
        ("The project costs is Rs \n1544.60 Cr.", Decimal("1544.60")),  # marker, line break, value
        ("cost per set is Rs. \n120 Crores", Decimal(120)),
        # extraction split the paise off before the '/-' terminator: 72,73,45,018.74 rupees
        ("Rs. 72,73,45,018. 74/- (Rupees Seventy-Two Crore ...)", rupees("72,73,45,018.74")),
        ("Rs. 72, 73,45,018. 74 /-", rupees("72,73,45,018.74")),
    ],
)
def test_live_extraction_layouts_read_whole(text, crore):
    assert [m.crore for m in scan_amounts(text)[0]] == [crore]


def test_split_digits_not_before_the_terminator_are_unreadable():
    assert scan_amounts("Rs. 5,000. 25 orders") == ([], 1)


def test_a_component_amount_is_never_taken_for_the_unreadable_total():
    # live RVNL 2024-03-07 layout: the total has no readable marker, the supply part is
    # readable, the installation part is not: no value, never the supply component
    segment = ("the total cost of project is Rs order(s)/contract(s); 888,56,35, 186/ (The supply "
               "portion including tax is Rs. 782,69,76,778/ - and \ninstallation portion "
               "including tax is Rs. \n105,86 58,408.00/-)")
    found, malformed = scan_amounts(segment)
    assert [m.crore for m in found] == [rupees("782,69,76,778")] and malformed == 1
    assert single_amount(segment) == (None, True)


def test_explicit_letter_reference_with_conflicting_extracted_text_stays_unresolved():
    # live RVNL layout: same client, the LOA continues the bid's letter, but the extracted
    # tender numbers differ ('PUNE-TDC-2025' vs 'PUNE-TDC-2025-EPC-05'): not linked (blocked),
    # and never ruled out either
    bid_ = bid(client="Central Railway", project="Traction substation at Pune",
               refs=("TENDER:PUNE-TDC-2025",))
    award_ = rec(2, date(2026, 7, 1), client="Central Railway",
                 project="Traction substation at Pune", refs=("TENDER:PUNE-TDC-2025-EPC-05",),
                 letters=(bid_.announcement_date,), value="100")
    result = chains(bid_, award_)
    assert len(result) == 2
    loa = next(c for c in result if c.status == ChainStatus.CONFIRMED)
    assert "ORDER_LINK_BLOCKED" in {i.code for i in loa.issues}
    assert linkage_of(result).resolution == L1Resolution.UNRESOLVED_LINKAGE


def test_a_different_client_alone_never_rules_an_award_out():
    # the bid's 'client' is really project text; the later award names no project
    result = chains(bid(client="Varkala Sivagiri Railway Station", project="Varkala works"),
                    rec(2, date(2026, 7, 1), client="Southern Railway", value="100"))
    assert linkage_of(result).resolution == L1Resolution.UNRESOLVED_LINKAGE
