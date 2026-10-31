"""Box 13 over real Box 12 datasets: order book, L1, events, ratings, ownership, identity."""

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid5

import pytest
from pydantic import ValidationError
from test_due_diligence import (
    AS_OF,
    NOW,
    SRC,
    datasets,
    maybe,
    rows_for,
    run,
    sig,
)
from test_market_intelligence import (
    ISIN,
    L1_TEXT,
    L1_URL,
    SYMBOL,
    announcement,
    base_payloads,
    bse_announcements,
    build,
    docs,
    failed,
    nse_announcements,
    order_payloads,
    raw,
    screener_dataset,
    shareholding_payload,
)

from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.red_flags import DueDiligenceInputError
from stock_research_ai.application.source_coverage import screener_shareholding_facts
from stock_research_ai.domain import due_diligence as dd
from stock_research_ai.domain.due_diligence import DueDiligenceRequest, Severity, SignalStatus
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Outcome,
    SourceReference,
    SourceType,
)
from stock_research_ai.domain.market_intelligence import (
    FetchStatus,
    MarketIntelligenceDataset,
    OrderBookAnalytics,
    OrderBookMetric,
    OrderBookMetricKind,
    PayloadKind,
)

SV = Severity
ST = SignalStatus
K = OrderBookMetricKind
NSE_API = "https://www.nseindia.com/api"
EPC = {"business_model": "ORDER_BOOK_DRIVEN", "business_model_source": SRC}


def box13(box12: MarketIntelligenceDataset, **kw: Any) -> dd.DueDiligenceDataset:
    kw.setdefault("nse_symbol", SYMBOL)
    return run(box12=box12, **kw)


def with_metrics(box12: MarketIntelligenceDataset, **values: tuple[object, str | None]) \
        -> MarketIntelligenceDataset:
    assert box12.order_book is not None
    metrics = []
    for m in box12.order_book.metrics:
        if m.kind.value in values:
            value, reason = values[m.kind.value]
            m = OrderBookMetric.model_validate(m.model_dump() | {
                "value": None if value is None else Decimal(str(value)),
                "status": "AVAILABLE" if reason is None else "UNAVAILABLE", "reason": reason})
        metrics.append(m)
    book = OrderBookAnalytics.model_validate(box12.order_book.model_dump() | {
        "metrics": tuple(m.model_dump() for m in metrics)})
    return box12.model_copy(update={"order_book": book})


# --- order book --------------------------------------------------------------------------------


def test_order_book_applies_only_to_order_book_driven_companies():
    ledger = build(order_payloads(), document_texts=docs())
    plain = box13(ledger)
    assert not plain.order_book_quality.applicable and not plain.order_book_quality.signal_ids
    epc = box13(ledger, **EPC)
    q = epc.order_book_quality
    assert q.applicable and q.applicability_basis == ("DECLARED_ORDER_BOOK_DRIVEN",)
    assert q.confirmed_chains == 1 and q.pending_l1 is not None and q.pending_l1.chains == 0
    rate = sig(epc, "ORDER_CANCELLATION_RATE")
    assert rate.severity == SV.GREEN and rate.value == 0 and rate.evidence_ids


def test_pending_l1_is_never_confirmed_backlog():
    only_l1 = build([*base_payloads(), nse_announcements(
        announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL))],
        document_texts=docs())
    data = box13(only_l1, **EPC)
    pending = data.order_book_quality.pending_l1
    assert pending is not None and pending.chains == 1 and pending.true_pending == 1
    assert pending.counted_in_confirmed == 0 and data.order_book_quality.confirmed_chains == 0
    chain = only_l1.order_chains[0]
    forged = only_l1.model_copy(update={"order_chains": (chain.model_copy(update={
        "counted_in_confirmed": True}),)})
    with pytest.raises(ValidationError):
        box13(forged, **EPC)


def test_order_metric_thresholds_and_government_neutrality():
    ledger = build(order_payloads(), document_texts=docs())
    high = with_metrics(ledger, CANCELLATION_RATE=(30, None), EXECUTION_RATE=(8, None),
                        GOVERNMENT_SHARE=(20, None), TOP_CLIENT_SHARE=(80, None))
    data = box13(high, **EPC)
    assert sig(data, "ORDER_CANCELLATION_RATE").severity == SV.RED
    assert sig(data, "LOW_EXECUTION_RATE").severity == SV.RED
    assert sig(data, "CLIENT_CONCENTRATION").severity == SV.RED  # private client base
    gov = with_metrics(ledger, GOVERNMENT_SHARE=(95, None), TOP_CLIENT_SHARE=(90, None))
    data = box13(gov, **EPC)
    client = sig(data, "CLIENT_CONCENTRATION")
    assert client.status == ST.INFORMATIONAL and client.risk_id not in data.red_flags
    government = sig(data, "GOVERNMENT_CONCENTRATION")
    assert government.status == ST.INFORMATIONAL and government.value == 95
    unknown = box13(ledger, **EPC)  # government share not disclosed for every order
    assert sig(unknown, "CLIENT_CONCENTRATION").status == ST.INFORMATIONAL


def test_order_book_data_limitations_stay_data_limitations():
    ledger = build(order_payloads(), document_texts=docs())
    mixed = with_metrics(ledger, JV_ATTRIBUTABLE_CONFIRMED_VALUE=(None, "MIXED_TAX_BASIS"))
    data = box13(mixed, **EPC)
    kinds = {k for k, _ in data.data_quality_summary.counts}
    assert "VALUE_BASIS_DISAGREEMENT" in kinds
    unresolved = build([*base_payloads(), nse_announcements(
        announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL))])
    data = box13(unresolved, **EPC)  # no attachment text: linkage cannot be resolved
    pending = data.order_book_quality.pending_l1
    assert pending is not None and pending.counted_in_confirmed == 0
    assert all(data.signal(i).category != dd.RiskCategory.DATA_QUALITY for i in data.red_flags)


# --- exchange events, penalties, ratings ---------------------------------------------------------


def penalty_payload() -> Any:
    return nse_announcements(announcement(
        "01-Mar-2026 10:00:00", "Action(s) taken or orders passed",
        "Imposition of fine by NSE for the quarter", seq="5"))


WINDOW_START = date(2023, 6, 30)
WINDOW_END = date(2026, 10, 10)


def attested_window(payload: Any, *, start: date = WINDOW_START, end: date = WINDOW_END,
                    gaps: tuple[tuple[date, date], ...] = ()) -> Any:
    return payload.model_copy(update={
        "coverage_window_start": start,
        "coverage_window_end": end,
        "coverage_window_complete": True,
        "coverage_window_gaps": gaps,
    })


def complete_announcement_sources(*rows: dict[str, Any], start: date = WINDOW_START,
                                   end: date = WINDOW_END,
                                   gaps: tuple[tuple[date, date], ...] = ()) -> list[Any]:
    return [*base_payloads(),
            attested_window(nse_announcements(*rows), start=start, end=end, gaps=gaps),
            attested_window(bse_announcements(), start=start, end=end, gaps=gaps)]


def complete_corporate_action_sources() -> list[Any]:
    return [
        attested_window(raw(PayloadKind.NSE_CORPORATE_ACTIONS, {"data": []},
                            f"{NSE_API}/corporate-actions")),
        attested_window(raw(PayloadKind.BSE_CORPORATE_ACTIONS, {"Table": []},
                            "https://api.bseindia.com/corporate-actions")),
    ]


def test_exchange_penalty_event_feeds_a_signal_and_the_event_register():
    box12 = build([*base_payloads(), penalty_payload()])
    data = box13(box12)
    s = sig(data, "EXCHANGE_PENALTIES")
    assert s.severity == SV.YELLOW and s.value == 1 and s.evidence_ids
    entry = next(e for e in data.exchange_event_risks.entries
                 if e.event_type == "EXCHANGE_PENALTY")
    assert entry.status == "INFORMATIONAL" and entry.severity is None  # no value: no grade
    assert any(g.kind == "EXCHANGE_PENALTY" for g in data.governance_risks.catalogue)
    big = dd.PenaltyInput(penalty_date=date(2026, 3, 1), authority="NSE",
                          amount_inr=Decimal(100_000_000), reason="fine", source=SRC)
    data = box13(box12, penalties=(big,))
    assert sig(data, "EXCHANGE_PENALTIES").severity == SV.RED  # 10 crore >= 1 % of PAT


def test_unattested_announcement_ledger_does_not_give_clean_penalty_reading():
    box12 = build(order_payloads(), document_texts=docs())
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert s.status == ST.UNAVAILABLE and s.reason == "ANNOUNCEMENT_COVERAGE_INCOMPLETE"


def test_audit_stale_announcement_case_is_unavailable_not_green_zero():
    old = nse_announcements(announcement(
        "01-Jan-2024 10:00:00", "Updates", "General filing", seq="1"))
    box12 = build([*base_payloads(), old])
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    signal = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert item.status.value == "STALE" and item.age_days == 1012
    assert signal.status == ST.UNAVAILABLE and signal.severity is None
    assert signal.reason == "ANNOUNCEMENT_COVERAGE_INCOMPLETE"
    assert any(i.kind == "INCOMPLETE_COVERAGE_WINDOW"
               for i in box13(box12).data_quality_summary.items)


def test_complete_fresh_announcement_window_allows_cited_green_penalty_reading():
    box12 = build(complete_announcement_sources(), document_texts=docs())
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert item.coverage_window_complete and item.required_sources_complete
    assert s.severity == SV.GREEN and s.record_ids


def test_partial_window_with_no_penalty_never_becomes_green():
    partial = date(2026, 4, 10)
    box12 = build(complete_announcement_sources(start=partial))
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert s.status == ST.UNAVAILABLE and s.severity is None


def test_each_required_exchange_source_must_cover_the_full_window():
    payloads = [*base_payloads(),
                attested_window(nse_announcements()),
                attested_window(bse_announcements(), start=date(2026, 4, 10))]
    box12 = build(payloads)
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert item.required_sources_complete and item.coverage_window_start == date(2026, 4, 10)
    assert s.status == ST.UNAVAILABLE and s.severity is None


def test_complete_window_preserves_observed_penalty_as_adverse():
    payloads = complete_announcement_sources(announcement(
        "01-Mar-2026 10:00:00", "Action(s) taken or orders passed",
        "Imposition of fine by NSE for the quarter", seq="5"))
    box12 = build(payloads)
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert s.status == ST.EVALUATED and s.severity == SV.YELLOW and s.value == 1


def test_incomplete_window_keeps_found_penalty_but_does_not_claim_complete_coverage():
    payloads = complete_announcement_sources(
        announcement("01-Mar-2026 10:00:00", "Action(s) taken or orders passed",
                     "Imposition of fine by NSE for the quarter", seq="5"),
        start=date(2026, 4, 10))
    box12 = build(payloads)
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    data = box13(box12)
    s = sig(data, "EXCHANGE_PENALTIES")
    assert item.coverage_window_complete and item.coverage_window_start == date(2026, 4, 10)
    assert s.status == ST.EVALUATED and s.severity == SV.YELLOW and s.value == 1
    assert "PENALTY_COVERAGE_INCOMPLETE" in s.issues
    assert any(i.kind == "INCOMPLETE_COVERAGE_WINDOW"
               for i in data.data_quality_summary.items)


def test_one_failed_required_exchange_source_blocks_absence_conclusion():
    payloads = [attested_window(nse_announcements()),
                failed(PayloadKind.BSE_ANNOUNCEMENTS, FetchStatus.NETWORK_ERROR, "TEST_NETWORK")]
    box12 = build([*base_payloads(), *payloads])
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert not item.required_sources_complete
    assert s.status == ST.UNAVAILABLE


def test_source_extraction_failure_blocks_absence_conclusion():
    broken = raw(PayloadKind.NSE_ANNOUNCEMENTS, "{malformed", f"{NSE_API}/corporate-announcements")
    payloads = [attested_window(broken), attested_window(bse_announcements())]
    box12 = build([*base_payloads(), *payloads])
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert not item.processing_succeeded and s.status == ST.UNAVAILABLE


def test_wrong_issuer_rows_do_not_count_toward_complete_coverage():
    foreign = announcement("01-Oct-2026 10:00:00", "Updates", "General filing", seq="9",
                           symbol="TCS", isin="INE467B01029")
    box12 = build(complete_announcement_sources(foreign))
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert not item.processing_succeeded and not item.coverage_window_complete
    assert s.status == ST.UNAVAILABLE and not box12.events


def test_fresh_feed_with_too_short_window_is_unavailable():
    box12 = build(complete_announcement_sources(start=date(2026, 9, 1)))
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert s.status == ST.UNAVAILABLE


def test_correct_start_with_stale_coverage_end_is_unavailable():
    box12 = build(complete_announcement_sources(end=date(2025, 1, 1)))
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert s.status == ST.UNAVAILABLE and s.severity is None


def test_known_internal_coverage_gap_blocks_absence_conclusion():
    gap = ((date(2024, 1, 1), date(2025, 12, 31)),)
    box12 = build(complete_announcement_sources(gaps=gap))
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert item.coverage_window_gaps == gap and item.coverage_window_complete
    assert s.status == ST.UNAVAILABLE


def test_not_found_optional_coverage_never_turns_into_a_clean_penalty_signal():
    box12 = build([*base_payloads()])
    item = next(i for i in box12.coverage.items if i.category.value == "MATERIAL_ANNOUNCEMENTS")
    s = sig(box13(box12), "EXCHANGE_PENALTIES")
    assert item.status.value == "SOURCE_UNAVAILABLE"
    assert s.status == ST.UNAVAILABLE


def ratings_payload(action: str, grade: str = "AA") -> Any:
    return raw(PayloadKind.NSE_CREDIT_RATINGS, [
        {"AppID": "2", "Symbol": SYMBOL, "ISIN": "INE415G08011", "CompanyName": "RVNL",
         "NameOfCRAgency": "CARE", "CreditRating": grade, "RatingAction": action,
         "DateofCR": "01-10-2026", "BroadcastDateTime": "02-OCT-2026 10:00:00"}],
        f"{NSE_API}/cr")


def test_filed_rating_actions_without_instrument():
    down = box13(build([*base_payloads(), ratings_payload("Downgrade")]))
    s = sig(down, "CREDIT_DETERIORATION")
    assert s.severity == SV.YELLOW and "states a downgrade" in s.calculation
    assert down.liquidity_credit_risks.ratings[0].instrument is None
    assert any("instrument" in i.question for i in down.open_diligence_items)
    up = box13(build([*base_payloads(), ratings_payload("Upgrade", "AAA")]))
    assert sig(up, "RATING_UPGRADE").risk_id in up.positive_controls
    junk = box13(build([*base_payloads(), ratings_payload("Reaffirm", "BB")]))
    assert sig(junk, "CREDIT_DETERIORATION").severity == SV.RED


def test_no_rating_deterioration_requires_complete_fresh_window():
    rating = ratings_payload("Reaffirm")
    box12 = build([*base_payloads(), rating])
    signal = sig(box13(box12), "CREDIT_DETERIORATION")
    assert signal.status == ST.UNAVAILABLE and signal.reason == "RATING_COVERAGE_INCOMPLETE"


def test_complete_rating_window_allows_evaluated_no_deterioration():
    payloads = [*base_payloads(),
                attested_window(ratings_payload("Reaffirm")),
                attested_window(nse_announcements())]
    box12 = build(payloads)
    signal = sig(box13(box12), "CREDIT_DETERIORATION")
    assert signal.status == ST.EVALUATED and signal.severity == SV.GREEN


def test_complete_rating_source_with_no_rated_instrument_is_not_applicable():
    rating_feed = raw(PayloadKind.NSE_CREDIT_RATINGS, {"data": []}, f"{NSE_API}/cr")
    box12 = build([*base_payloads(), attested_window(rating_feed),
                   attested_window(nse_announcements())])
    signal = sig(box13(box12), "CREDIT_DETERIORATION")
    assert signal.status == ST.NOT_APPLICABLE and signal.reason == "NO_RATING_INSTRUMENT"


def insider_payload(category: str, kind: str, value: str) -> Any:
    return raw(PayloadKind.NSE_INSIDER_TRADING, {"data": [
        {"symbol": SYMBOL, "acqName": "Promoter Co", "personCategory": category,
         "tdpTransactionType": kind, "acqMode": "Market Sale", "secType": "Equity Shares",
         "secAcq": "1000", "secVal": value, "befAcqSharesNo": "5000", "afterAcqSharesNo": "4000",
         "acqfromDt": "02-Sep-2025", "acqtoDt": "02-Sep-2025", "intimDt": "02-Sep-2025",
         "date": "03-Sep-2025 17:27", "pid": "1"}]}, f"{NSE_API}/pit")


def test_large_promoter_sale_is_sized_against_market_cap_only():
    box12 = build([*base_payloads(), insider_payload("Promoters", "Disposal", "300000000")])
    info = box13(box12)
    entry = next(e for e in info.exchange_event_risks.entries if e.event_type == "PROMOTER_SALE")
    assert entry.status == "INFORMATIONAL"  # no market cap: never graded from wording
    cap = dd.MarketCapInput(as_of=date(2026, 3, 31), value_crore=Decimal(1000), source=SRC)
    sized = box13(box12, market_cap=cap)
    s = sig(sized, "LARGE_PROMOTER_SALE")
    assert s.severity == SV.RED and s.value == 3  # 30 crore of 1000
    employee = box13(build([*base_payloads(), insider_payload("Employees", "Disposal", "1")]))
    assert not [e for e in employee.exchange_event_risks.entries
                if e.event_type == "PROMOTER_SALE"]


# --- ownership --------------------------------------------------------------------------------


QUARTERS = ("30-JUN-2025", "30-SEP-2025", "31-DEC-2025", "31-MAR-2026", "30-JUN-2026")


def test_promoter_selling_from_exchange_filings_with_dilution_ruled_out():
    sh = shareholding_payload(*[(q, p, str(100 - Decimal(p)), "N") for q, p in
                                zip(QUARTERS, ("72.84", "72", "70", "68", "66"), strict=True)])
    penalty = announcement("01-Mar-2026 10:00:00", "Action(s) taken or orders passed",
                           "Imposition of fine by NSE for the quarter", seq="5")
    recent = announcement("01-Oct-2026 10:00:00", "Updates", "General filing", seq="6")
    box12 = build([sh, *complete_announcement_sources(penalty, recent),
                   *complete_corporate_action_sources()])
    s = sig(box13(box12), "PROMOTER_SELLING")
    assert s.severity == SV.RED and s.value == Decimal("6.84")
    assert "DILUTION_RULED_OUT_BY_EVENT_LEDGER" in s.issues
    raise_ = nse_announcements(announcement("01-Jan-2026 10:00:00", "Updates",
                                            "Issue of non-convertible debentures", seq="7"))
    blocked = build([*base_payloads(), sh, raise_])
    s = sig(box13(blocked), "PROMOTER_SELLING")
    assert s.status == ST.UNAVAILABLE and s.reason == "DILUTION_NOT_SEPARABLE"


def test_stable_exchange_holding_and_disagreements_are_explicit():
    sh = shareholding_payload(*[(q, "72.84", "27.16", "N") for q in QUARTERS])
    facts = screener_shareholding_facts(screener_dataset("72.90"))
    box12 = build([*base_payloads(), sh], comparable_facts=tuple(facts))
    cited = dd.HoldingPoint(period_end=date(2026, 6, 30), category="PROMOTER", pct=Decimal(70),
                            source=SRC)
    data = box13(box12, holdings=(cited,))
    assert sig(data, "STABLE_PROMOTER_HOLDING").risk_id in data.positive_controls
    entries = [e for e in data.source_disagreements.entries if e.group.value == "SHAREHOLDING"]
    origins = {e.origin for e in entries}
    assert origins == {dd.Layer.BOX12, dd.Layer.BOX13}
    assert all(e.status == "RESOLVED" and e.canonical_source for e in entries)
    assert all(len(e.values) == 2 for e in entries)
    keys = [(e.group, e.subject, e.period, tuple(sorted(str(v.value) for v in e.values)))
            for e in data.source_disagreements.entries]
    assert len(keys) == len(set(keys))  # no duplicate alerts
    series = next(s for s in data.ownership_risks.series if s.category == "PROMOTER")
    assert {p.layer for p in series.points} == {dd.Layer.BOX12}  # the filing outranks the input
    assert "PROMOTER_PLEDGE" in data.ownership_risks.unparsed_categories


# --- identity, blocked sources, Box 4 disagreements ----------------------------------------------


def test_box12_identity_must_bind_to_the_request():
    box12 = build(base_payloads())
    with pytest.raises(DueDiligenceInputError) as e:
        box13(box12, nse_symbol="IRCON")
    assert e.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(DueDiligenceInputError) as e:
        run(box12=box12)
    assert e.value.code == "IDENTITY_UNBOUND"
    assert box13(box12, nse_symbol=None, isin=ISIN).sources[-1].layer == dd.Layer.BOX12


def test_blocked_sources_are_data_limitations():
    box12 = build([*base_payloads(), failed(PayloadKind.BSE_ANNOUNCEMENTS, FetchStatus.BLOCKED,
                                            "HTTP_403")])
    data = box13(box12)
    items = [i for i in data.data_quality_summary.items if i.kind == "BLOCKED_SOURCE"]
    assert items and "never bypassed" in items[0].detail
    assert data.red_flags == ()


def test_box4_source_precedence_is_a_resolved_disagreement():
    box4, _ = datasets(rows_for())
    pat = next(v for v in box4.canonical_values if v.metric == CanonicalMetric.PAT
               and v.period.fiscal_year == 2026)
    selected = next(o for o in box4.observations if o.observation_id == pat.selected_observation_id)
    loser = selected.model_copy(update={
        "observation_id": uuid5(pat.canonical_value_id, "screener"),
        "normalized_value": pat.value + 10,
        "source": SourceReference(source_type=SourceType.SCREENER,
                                  source_url="https://www.screener.in/company/X/",
                                  retrieved_or_extracted_at=NOW, locator="#pl r9",
                                  raw_artifact_id=uuid5(pat.canonical_value_id, "page"))})
    records = tuple(r.model_copy(update={"outcome": Outcome.SOURCE_PRECEDENCE,
                                         "conflicting_observation_ids": (loser.observation_id,),
                                         "max_difference": Decimal(10)})
                    if r.record_id == pat.reconciliation_record_id else r
                    for r in box4.reconciliation)
    values = tuple(v.model_copy(update={"outcome": Outcome.SOURCE_PRECEDENCE,
                                        "conflicting_observation_ids": (loser.observation_id,)})
                   if v.canonical_value_id == pat.canonical_value_id else v
                   for v in box4.canonical_values)
    changed = CanonicalFinancialDataset.model_validate(box4.model_copy(update={
        "observations": (*box4.observations, loser), "reconciliation": records,
        "canonical_values": values}).model_dump())
    box5 = build_financial_analytics(changed, generated_at=NOW)
    data = build_due_diligence(changed, box5, generated_at=NOW, request=DueDiligenceRequest(
        as_of=AS_OF, company_key=changed.company_key))
    (entry,) = [e for e in data.source_disagreements.entries if e.subject == "PAT"]
    assert entry.group.value == "FINANCIAL" and entry.status == "RESOLVED"
    assert entry.canonical_source and entry.canonical_source.startswith("FILING")
    assert {v.value for v in entry.values} == {pat.value, pat.value + 10}


def test_box12_after_as_of_is_refused():
    box12 = build(base_payloads())
    with pytest.raises(DueDiligenceInputError) as e:
        box13(box12, as_of=date(2026, 10, 1))
    assert e.value.code == "INCOMPATIBLE_PERIOD"


def test_market_cap_from_box12_price_needs_a_current_quote_and_cited_shares():
    box12 = build(base_payloads())
    assert box12.snapshot is None
    data = box13(box12)
    view = next(m for m in data.financial_forensics.models
                if m.variant == "ORIGINAL_1968_MARKET_VALUE")
    assert "X4" in view.missing_components
    assert maybe(data, "LARGE_PROMOTER_SALE") is None


def test_reported_order_book_is_cited_and_its_absence_is_a_data_limitation():
    ledger = build(order_payloads(), document_texts=docs())
    missing = box13(ledger, **EPC)
    assert ("MISSING_REPORTED_ORDER_BOOK", 1) in missing.data_quality_summary.counts
    report = dd.OrderBookReport(as_of=date(2026, 3, 31), value_crore=Decimal(93100),
                                basis="STANDALONE",
                                definition="remaining performance obligations (Ind AS 115)",
                                source=SRC)
    data = box13(ledger, order_book_reports=(report,), **EPC)
    q = data.order_book_quality
    assert q.reported_order_book_crore == 93100 and q.reported_order_book_layer == dd.Layer.REQUEST
    assert q.reported_order_book_tax_basis == "UNKNOWN" and q.reported_order_book_basis == \
        "STANDALONE"
    assert "MISSING_REPORTED_ORDER_BOOK" not in {k for k, _ in data.data_quality_summary.counts}


def test_advice_like_words_in_upstream_text_never_break_the_build():
    box12 = build([*base_payloads(),
                   insider_payload("Promoters", "Disposal", "300000000")])
    cap = dd.MarketCapInput(as_of=date(2026, 3, 31), value_crore=Decimal(1000), source=SRC)
    odd = dd.RatingInput(action_date=date(2025, 1, 1), agency="CARE",
                         instrument="Buy / Sell / Hold facility (on hold)", rating="CARE AA-",
                         action="DOWNGRADED", source=SRC)
    data = box13(box12, market_cap=cap, ratings=(odd,))
    assert sig(data, "CREDIT_DETERIORATION").severity == SV.YELLOW
    inputs = sig(data, "CREDIT_DETERIORATION").inputs
    assert any("Hold facility" in (i.detail or "") for i in inputs)
    text = " ".join(data.signal(i).explanation.upper() for i in data.red_flags)
    assert " SELL " not in f" {text} " and " HOLD " not in f" {text} "
