"""Box 13 input policy: conflicts, duplicates, promoter selling vs dilution, event direction."""

import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from test_due_diligence import AS_OF, NOW, SRC, datasets, holding, maybe, rows_for, run, sig
from test_due_diligence_market import (
    QUARTERS,
    box13,
    complete_announcement_sources,
    complete_corporate_action_sources,
)
from test_market_intelligence import announcement as filed
from test_market_intelligence import (
    base_payloads,
    build,
    nse_announcements,
    shareholding_payload,
)

from stock_research_ai.application.diligence_inputs import normalize, resolve
from stock_research_ai.application.due_diligence import (
    build_due_diligence,
    canonical_due_diligence_json,
)
from stock_research_ai.application.red_flags import DueDiligenceInputError
from stock_research_ai.domain import due_diligence as dd
from stock_research_ai.domain.due_diligence import (
    DiligenceSource,
    DueDiligenceDataset,
    DueDiligenceRequest,
    Severity,
    SignalStatus,
)
from stock_research_ai.domain.financial_canonical import Currency, ReportingBasis
from stock_research_ai.domain.market_intelligence import (
    Authority,
    Citation,
    FinancialBasis,
    MarketIntelligenceDataset,
    Provider,
    ReferenceRevenue,
)

D = Decimal
SV = Severity
ST = SignalStatus
FY_END = date(2026, 3, 31)
START, END = date(2025, 6, 30), date(2026, 6, 30)


def src(authority: str = "COMPANY_DOCUMENT", ref: str = "Annual report FY2026, p.1") -> \
        DiligenceSource:
    return DiligenceSource(authority=authority, reference=ref)  # type: ignore[arg-type]


def canon(data: DueDiligenceDataset) -> str:
    return canonical_due_diligence_json(data)


def entries(data: DueDiligenceDataset, subject: str) -> list[dd.DisagreementEntry]:
    return [e for e in data.source_disagreements.entries if e.subject == subject]


def variants(items: list[Any]) -> list[list[Any]]:
    """Original, reversed, every record twice, three times and ten times (interleaved)."""
    return [items, items[::-1], items * 2, items[::-1] * 3, items * 10]


def assert_order_and_duplicates_never_matter(field: str, items: list[Any],
                                             **kw: Any) -> DueDiligenceDataset:
    pair = datasets(rows_for())
    first = run(pair=pair, **{field: tuple(items)}, **kw)
    for variant in variants(items)[1:]:
        assert canon(run(pair=pair, **{field: tuple(variant)}, **kw)) == canon(first)
    return first


# --- H-1: one deterministic conflict policy --------------------------------------------------


def pledge_at(pct: object, authority: str = "COMPANY_DOCUMENT", ref: str = "SHP Q1") -> \
        dd.PledgePoint:
    return dd.PledgePoint(period_end=END, pledged_pct_of_promoter_holding=D(str(pct)),
                          source=src(authority, ref))


def test_same_key_same_authority_conflict_is_unresolved_and_order_independent():
    low, high = pledge_at(3, ref="SHP filing A"), pledge_at(25, ref="SHP filing B")
    data = assert_order_and_duplicates_never_matter("pledges", [low, high])
    s = sig(data, "PROMOTER_PLEDGE")
    assert s.status == ST.UNAVAILABLE and s.reason == "INPUT_CONFLICT" and s.severity is None
    assert s.risk_id not in data.red_flags and maybe(data, "NO_PLEDGE") is None
    assert {i.value for i in s.inputs} == {D(3), D(25)}  # both disagreeing values cited
    (entry,) = entries(data, "PROMOTER_PLEDGE_PCT")
    assert entry.status == "OPEN" and entry.canonical_source is None
    assert {v.value for v in entry.values} == {D(3), D(25)} and entry.origin == dd.Layer.REQUEST
    assert ("INPUT_CONFLICT", 1) in data.data_quality_summary.counts
    assert any("pledge" in i.question for i in data.open_diligence_items)
    # the disputed quarter is never plotted from one arbitrary value
    assert not [x for x in data.ownership_risks.series if x.category == "PLEDGED_OF_PROMOTER"]


def board(independent: int, ref: str) -> dd.BoardFact:
    return dd.BoardFact(as_of=FY_END, directors=9, independent_directors=independent,
                        executive_chair=False, source=src(ref=ref))


def contingent(amount: object, ref: str, *, desc: str | None = "income-tax demands",
               basis: str = "CONSOLIDATED", category: str = "TAX") -> dd.ContingentItem:
    return dd.ContingentItem(as_of=FY_END, basis=basis, category=category,  # type: ignore[arg-type]
                             description=desc, amount_crore=D(str(amount)), source=src(ref=ref))


def rpt(amount: object, ref: str = "AR note 38", counterparty: str = "X Ltd",
        relation: str = "PROMOTER_ENTITY", kind: str = "REVENUE") -> dd.RelatedPartyItem:
    return dd.RelatedPartyItem(fiscal_year=2026, basis="CONSOLIDATED", kind=kind,  # type: ignore[arg-type]
                               counterparty=counterparty, relation=relation,  # type: ignore[arg-type]
                               amount_crore=D(str(amount)), source=src(ref=ref))


def rating(grade_: str, ref: str) -> dd.RatingInput:
    return dd.RatingInput(action_date=date(2025, 6, 1), agency="CARE", instrument="NCD",
                          rating=f"CARE {grade_}", source=src("RATING_AGENCY", ref))


def report(opinion: str, ref: str) -> dd.AuditorReport:
    return dd.AuditorReport(fiscal_year=2026, basis="CONSOLIDATED", auditor="A & Co",
                            opinion=opinion, source=src(ref=ref))  # type: ignore[arg-type]


def cfo_change(planned: bool | None, ref: str, day: date = date(2025, 5, 1),
               role: str = "CFO", person: str | None = "R. Rao") -> dd.ManagementChange:
    return dd.ManagementChange(change_date=day, role=role, kind="RESIGNATION",  # type: ignore[arg-type]
                               person=person, planned_succession=planned, source=src(ref=ref))


def cwip(amount: object, ref: str) -> dd.CwipAgeing:
    return dd.CwipAgeing(as_of=FY_END, basis="CONSOLIDATED", bucket="GT_3Y",  # type: ignore[arg-type]
                         status="IN_PROGRESS", amount_crore=D(str(amount)), source=src(ref=ref))


CONFLICTS: list[tuple[str, Callable[[str], list[Any]], str, dict[str, Any]]] = [
    ("board", lambda r: [board(2, r + "A"), board(6, r + "B")], "BOARD_INDEPENDENCE", {}),
    ("contingent_liabilities", lambda r: [contingent(100, r + "A"), contingent(1200, r + "B")],
     "CONTINGENT_LIABILITY", {}),
    ("related_party", lambda r: [rpt(10, r + "A"), rpt(900, r + "B")],
     "RELATED_PARTY_DEPENDENCE", {}),
    ("ratings", lambda r: [rating("AA", r + "A"), rating("BB", r + "B")],
     "CREDIT_DETERIORATION", {}),
    ("auditor_reports", lambda r: [report("UNMODIFIED", r + "A"), report("QUALIFIED", r + "B")],
     "AUDITOR_SIGNAL", {}),
    ("management_changes", lambda r: [cfo_change(True, r + "A"), cfo_change(False, r + "B")],
     "MANAGEMENT_CHURN", {}),
    ("cwip_ageing", lambda r: [cwip(10, r + "A"), cwip(90, r + "B")], "STALLED_CWIP", {}),
    ("holdings", lambda r: [dd.HoldingPoint(period_end=END, category="PROMOTER",  # type: ignore[arg-type]
                                            pct=D(p), source=src(ref=r + p))
                            for p in ("70", "60")], "PROMOTER_SELLING",
     {"share_counts": ()}),
]


@pytest.mark.parametrize(("field", "make", "code", "extra"), CONFLICTS,
                         ids=[c[0] for c in CONFLICTS])
def test_every_family_withholds_a_same_authority_conflict(field, make, code, extra):
    items = make("source ")
    if field == "holdings":
        items = [holding(START, 70), *items]
    data = assert_order_and_duplicates_never_matter(field, items, **extra)
    s = sig(data, code)
    assert s.status == ST.UNAVAILABLE and s.reason == "INPUT_CONFLICT"
    assert s.risk_id not in data.red_flags and s.severity is None
    assert any(e.status == "OPEN" and e.origin == dd.Layer.REQUEST
               for e in data.source_disagreements.entries)


def test_higher_authority_selects_and_both_values_are_preserved():
    filing, screener = pledge_at(3, "EXCHANGE_FILING", "NSE SHP"), pledge_at(25, "SCREENER")
    data = assert_order_and_duplicates_never_matter("pledges", [screener, filing])
    s = sig(data, "PROMOTER_PLEDGE")
    assert s.status == ST.EVALUATED and s.value == 3 and s.severity == SV.GREEN
    (entry,) = entries(data, "PROMOTER_PLEDGE_PCT")
    assert entry.status == "RESOLVED" and entry.canonical_source is not None
    assert entry.canonical_source.startswith("EXCHANGE_FILING")
    assert {(v.authority, v.value) for v in entry.values} == {("EXCHANGE_FILING", D(3)),
                                                             ("SCREENER", D(25))}
    flipped = run(pledges=(pledge_at(25, "EXCHANGE_FILING", "NSE SHP"), pledge_at(3, "SCREENER")))
    assert sig(flipped, "PROMOTER_PLEDGE").severity == SV.RED  # the filing decides, not order
    analysts = run(pledges=(pledge_at(3, "ANALYST_INPUT", "a"), pledge_at(25, "ANALYST_INPUT")))
    assert sig(analysts, "PROMOTER_PLEDGE").reason == "INPUT_CONFLICT"  # never auto-selected
    company = run(pledges=(pledge_at(3, "ANALYST_INPUT"), pledge_at(25, "COMPANY_DOCUMENT")))
    assert sig(company, "PROMOTER_PLEDGE").value == 25


def test_same_value_from_two_sources_is_one_observation():
    data = run(pledges=(pledge_at(25, ref="SHP"), pledge_at("25.00", "EXCHANGE_FILING", "NSE")))
    s = sig(data, "PROMOTER_PLEDGE")
    assert s.severity == SV.RED and not entries(data, "PROMOTER_PLEDGE_PCT")
    assert len(s.inputs) == 1 and s.inputs[0].detail == "NSE"  # the best-authority citation


def test_different_period_basis_currency_or_share_basis_is_never_a_conflict():
    earlier = dd.PledgePoint(period_end=date(2026, 3, 31), pledged_pct_of_promoter_holding=D(3),
                             source=SRC)
    data = run(pledges=(earlier, pledge_at(25)))
    assert sig(data, "PROMOTER_PLEDGE").severity == SV.RED and not entries(
        data, "PROMOTER_PLEDGE_PCT")
    both = run(contingent_liabilities=(contingent(150, "s", basis="STANDALONE"),
                                       contingent(300, "c")))
    section = both.contingent_liabilities
    assert section.basis == dd.SignalBasis.CONSOLIDATED and section.total_crore == 300
    assert not entries(both, "CONTINGENT_LIABILITY")  # standalone never merged in
    inr = dd.RetainedEarnings(as_of=FY_END, basis="CONSOLIDATED", value_crore=D(10), source=SRC)
    usd = inr.model_copy(update={"currency": Currency.USD, "value_crore": D(20)})
    shares = [dd.ShareCount(as_of=END, shares=D(n), bonus_split_adjusted=a, source=SRC)
              for n, a in ((100, True), (200, False))]
    request = DueDiligenceRequest(as_of=AS_OF, company_key="k", retained_earnings=(inr, usd),
                                  share_counts=tuple(shares))
    inputs = normalize(request)
    assert not inputs.conflicts
    assert len(inputs.request.retained_earnings) == 2 and len(inputs.request.share_counts) == 2
    with pytest.raises(DueDiligenceInputError) as e:  # a foreign currency is not comparable
        run(retained_earnings=(inr, usd))
    assert e.value.code == "CURRENCY_MISMATCH"


def test_resolve_is_symmetric():
    from stock_research_ai.application.diligence_inputs import Candidate
    a, b = Candidate(2, "10", "a", "x"), Candidate(2, "20", "b", "y")
    assert resolve([a, b]) == resolve([b, a]) and resolve([a, b]).status == "OPEN"
    c = Candidate(1, "20", "c", "z")
    assert resolve([a, c]).winner == c == resolve([c, a]).winner


# --- H-1 with Box 12: one quarter across filings and cited inputs ----------------------------


def shareholding_box12(*pcts: str) -> MarketIntelligenceDataset:
    penalty = filed("01-Mar-2026 10:00:00", "Action(s) taken or orders passed",
                    "Imposition of fine by NSE for the quarter", seq="5")
    recent = filed("01-Oct-2026 10:00:00", "Updates", "General filing", seq="6")
    return build([*complete_announcement_sources(penalty, recent),
                  *complete_corporate_action_sources(), shareholding_payload(
                      *[(q, p, str(100 - D(p)), "N") for q, p in
                        zip(QUARTERS, pcts, strict=True)])])


def test_filing_and_equal_authority_input_disagree_is_open_in_either_order():
    box12 = shareholding_box12("72", "72", "72", "72", "66")
    cited = dd.HoldingPoint(period_end=END, category="PROMOTER", pct=D(72),  # type: ignore[arg-type]
                            source=src("EXCHANGE_FILING", "BSE SHP"))
    data = box13(box12, holdings=(cited,))
    s = sig(data, "PROMOTER_SELLING")
    assert s.status == ST.UNAVAILABLE and s.reason == "INPUT_CONFLICT"
    assert {i.value for i in s.inputs} >= {D(66), D(72)}
    (entry,) = entries(data, "PROMOTER_HOLDING_PCT")
    assert entry.status == "OPEN" and {v.value for v in entry.values} == {D(66), D(72)}
    lower = box13(box12, holdings=(cited.model_copy(update={"source": SRC}),))
    assert sig(lower, "PROMOTER_SELLING").severity == SV.RED  # the filing outranks the AR
    (entry,) = entries(lower, "PROMOTER_HOLDING_PCT")
    assert entry.status == "RESOLVED" and entry.canonical_source is not None


def with_filings(box12: MarketIntelligenceDataset, *extra: Any) -> MarketIntelligenceDataset:
    return box12.model_copy(update={"shareholding": (*box12.shareholding, *extra)})


def test_same_quarter_revision_uses_the_latest_explicit_submission():
    box12 = shareholding_box12("72", "72", "72", "72", "72")
    latest = max(box12.shareholding, key=lambda f: f.period_end)
    later = latest.model_copy(update={
        "filing_id": UUID(int=7), "promoter_pct": D(66), "revised": True,
        "submitted_on": (latest.submitted_on or AS_OF) + timedelta(days=5)})
    s = sig(box13(with_filings(box12, later)), "PROMOTER_SELLING")
    assert s.severity == SV.RED and s.value == 6  # the revision, deterministically
    same_day = later.model_copy(update={"submitted_on": latest.submitted_on})
    data = box13(with_filings(box12, same_day))
    assert sig(data, "PROMOTER_SELLING").reason == "INPUT_CONFLICT"
    assert any(e.status == "OPEN" for e in entries(data, "PROMOTER_HOLDING_PCT"))
    swapped = box12.model_copy(update={"shareholding": (same_day, *box12.shareholding)})
    assert sig(box13(swapped), "PROMOTER_SELLING").reason == "INPUT_CONFLICT"


def test_box12_record_ids_identical_once_altered_fail_closed():
    box12 = shareholding_box12("72", "72", "72", "72", "66")
    plain = box13(box12)
    first = box12.shareholding[0]
    twice = box13(with_filings(box12, first))
    assert sig(twice, "PROMOTER_SELLING").value == sig(plain, "PROMOTER_SELLING").value
    assert twice.ownership_risks.series == plain.ownership_risks.series
    with pytest.raises(DueDiligenceInputError) as e:
        box13(with_filings(box12, first.model_copy(update={"promoter_pct": D(10)})))
    assert e.value.code == "RECORD_ID_CONFLICT"


# --- M-1: promoter selling vs dilution -------------------------------------------------------


def selling(p0: object, p1: object, s0: object | None = None, s1: object | None = None,
            adjusted: tuple[bool, bool] = (True, True)) -> dd.RiskSignal:
    counts = () if s0 is None else (
        dd.ShareCount(as_of=START, shares=D(str(s0)), bonus_split_adjusted=adjusted[0],
                      source=SRC),
        dd.ShareCount(as_of=END, shares=D(str(s1)), bonus_split_adjusted=adjusted[1],
                      source=SRC))
    return sig(run(holdings=(holding(START, p0), holding(END, p1)), share_counts=counts),
               "PROMOTER_SELLING")


def test_promoter_selling_matrix():
    pure = selling(70, 65, 100, 100)  # A: a 5 pp sale, no dilution
    assert pure.severity == SV.RED and pure.value == 5
    diluted = selling(70, 65, 100, 120)  # B: 70 -> 78 promoter shares
    assert diluted.severity == SV.GREEN and "DECLINE_EXPLAINED_BY_DILUTION" in diluted.issues
    assert "78" in diluted.calculation and diluted.value is not None and diluted.value < 0
    both = selling(70, 60, 100, 110)  # C: 70 -> 66 promoter shares through dilution
    assert both.severity == SV.YELLOW and both.value == D("3.636364")
    assert "PROMOTER_SHARES_FELL_DESPITE_DILUTION" in both.issues
    flat = selling(60, 50, 100, 120)  # percentage falls, absolute promoter shares unchanged
    assert flat.severity == SV.GREEN and flat.value == 0
    unknown = selling(70, 65)  # D: no counts, no ledger to rule an issue out
    assert unknown.status == ST.UNAVAILABLE and unknown.reason == "DILUTION_NOT_SEPARABLE"


def test_bonus_and_split_never_read_as_selling_or_buying():
    bonus = selling(70, 70, 100, 200, adjusted=(False, False))  # E: 1:1 bonus, no sale
    assert bonus.severity == SV.GREEN and bonus.value == 0
    split = selling(70, 70, 100, 1000, adjusted=(False, False))
    assert split.severity == SV.GREEN and split.value == 0
    restated = selling(70, 70, 200, 200)
    assert restated.severity == SV.GREEN and restated.value == 0
    # the audit case: a real 60 -> 40 % sale across a 1:1 bonus with counts as filed
    masked = selling(60, 40, 100, 200, adjusted=(False, False))
    assert masked.status == ST.UNAVAILABLE and masked.reason == "DILUTION_NOT_SEPARABLE"
    assert "UNADJUSTED_COUNTS_ACROSS_POSSIBLE_BONUS_SPLIT" in masked.explanation
    mixed = selling(60, 40, 100, 200, adjusted=(True, False))
    assert mixed.reason == "DILUTION_NOT_SEPARABLE" and "SHARE_COUNT_BASES_DIFFER" in \
        mixed.explanation


def test_duplicate_and_reordered_holdings_and_counts_change_nothing():
    counts = [dd.ShareCount(as_of=d, shares=D(n), bonus_split_adjusted=True, source=SRC)
              for d, n in ((START, 100), (END, 110))]
    pair = datasets(rows_for())
    base = run(pair=pair, holdings=(holding(START, 70), holding(END, 60)),
               share_counts=tuple(counts))
    for hs in variants([holding(START, 70), holding(END, 60)]):
        for cs in (counts, counts[::-1] * 3):
            again = run(pair=pair, holdings=tuple(hs), share_counts=tuple(cs))
            assert canon(again) == canon(base)
    assert sig(base, "PROMOTER_SELLING").severity == SV.YELLOW


def test_conflicting_share_counts_withhold_the_separation():
    counts = (dd.ShareCount(as_of=START, shares=D(100), bonus_split_adjusted=True, source=SRC),
              dd.ShareCount(as_of=END, shares=D(110), bonus_split_adjusted=True, source=SRC),
              dd.ShareCount(as_of=END, shares=D(140), bonus_split_adjusted=True,
                            source=src(ref="other filing")))
    s = sig(run(holdings=(holding(START, 70), holding(END, 60)), share_counts=counts),
            "PROMOTER_SELLING")
    assert s.status == ST.UNAVAILABLE and s.reason == "INPUT_CONFLICT"


# --- M-2: an event category and its size are not an adverse direction -----------------------

REFERENCE = ReferenceRevenue(fiscal_year=2026, value_crore=D(5000),
                             basis=FinancialBasis.CONSOLIDATED,
                             citation=Citation(provider=Provider.CALLER,
                                               authority=Authority.EXPLICIT_INPUT,
                                               reference="Box 4 FY26 consolidated revenue"))
TEXTS = {
    "favourable_award": ("Outcome of arbitration", "Arbitral award in favour of the Company for "
                         "Rs. 500 crore"),
    "claim_against": ("Litigation", "Claim of Rs. 500 crore filed against the Company before "
                      "the arbitral tribunal"),
    "liability": ("Litigation", "Liability of Rs. 500 crore imposed on the Company by the "
                  "tribunal order"),
    "regulatory": ("Action(s) taken or orders passed", "Income tax demand order of Rs. 500 "
                   "crore"),
    "cancellation": ("Updates", "Cancellation of the letter of award worth Rs. 500 crore"),
    "order_award": ("Bagging/Receiving of orders/contracts", "Letter of Award from NMDC "
                    "Limited worth Rs. 500 crore"),
    "dividend": ("Dividend", "Interim dividend of Rs. 500 crore declared"),
    "fund_raise": ("Updates", "Fund raising by issue of debentures of Rs. 500 crore"),
    "acquisition": ("Updates", "Acquisition of a subsidiary for Rs. 500 crore"),
    "penalty": ("Updates", "Imposition of fine by NSE of Rs. 500 crore"),
}


def event_box12(*names: str) -> MarketIntelligenceDataset:
    rows = [filed(f"0{i + 1}-Sep-2026 10:00:00", TEXTS[n][0], TEXTS[n][1], seq=str(100 + i))
            for i, n in enumerate(names)]
    return build([*base_payloads(), nse_announcements(*rows)], reference_revenue=REFERENCE)


def entry_of(data: DueDiligenceDataset, box12: MarketIntelligenceDataset,
             index: int = 0) -> dd.ExchangeEventRisk:
    event_id = box12.events[index].event_id
    return next(e for e in data.exchange_event_risks.entries if e.event_id == event_id)


def assess(box12: MarketIntelligenceDataset, direction: str, index: int = 0,
           ref: str = "Award text, p.2") -> dd.ExchangeEventAssessment:
    return dd.ExchangeEventAssessment(event_id=box12.events[index].event_id,
                                      direction=direction,  # type: ignore[arg-type]
                                      source=src("EXCHANGE_FILING", ref))


@pytest.mark.parametrize("name", ["favourable_award", "claim_against", "liability",
                                  "regulatory"])
def test_litigation_and_regulatory_size_alone_is_never_a_flag(name):
    box12 = event_box12(name)
    assert box12.events[0].event_type.value in ("LITIGATION", "REGULATORY")
    assert box12.events[0].materiality is not None
    assert box12.events[0].materiality.ratio_pct == 10
    data = box13(box12)
    entry = entry_of(data, box12)
    assert entry.status == "INFORMATIONAL" and entry.severity is None
    assert entry.direction == "NOT_ESTABLISHED"
    assert maybe(data, "MATERIAL_LITIGATION") is None
    assert maybe(data, "MATERIAL_REGULATORY_ACTION") is None and data.red_flags == ()
    assert any("adverse to the company" in i.question for i in data.open_diligence_items)


def test_cited_direction_decides_and_conflicting_directions_stay_open():
    box12 = event_box12("favourable_award")
    favourable = box13(box12, event_assessments=(assess(box12, "FAVOURABLE"),))
    entry = entry_of(favourable, box12)
    assert entry.direction == "FAVOURABLE" and entry.status == "INFORMATIONAL"
    assert favourable.red_flags == ()
    assert not any("adverse to the company" in i.question
                   for i in favourable.open_diligence_items)
    claim = event_box12("claim_against")
    adverse = box13(claim, event_assessments=(assess(claim, "ADVERSE"),))
    s = sig(adverse, "MATERIAL_LITIGATION")
    assert s.severity == SV.RED and s.value == 10 and s.risk_id in adverse.red_flags
    assert "Award text, p.2" in {i.detail for i in s.inputs}  # the cited direction is evidence
    both = [assess(claim, "ADVERSE", ref="order p.1"), assess(claim, "FAVOURABLE", ref="p.9")]
    one = box13(claim, event_assessments=tuple(both))
    other = box13(claim, event_assessments=tuple(both[::-1] * 2))
    assert canon(one) == canon(other)
    assert entry_of(one, claim).direction == "CONFLICTING" and one.red_flags == ()
    assert any(e.subject == "EVENT_DIRECTION" and e.status == "OPEN"
               for e in one.source_disagreements.entries)
    regulatory = event_box12("regulatory")
    graded = box13(regulatory, event_assessments=(assess(regulatory, "ADVERSE"),))
    assert sig(graded, "MATERIAL_REGULATORY_ACTION").severity == SV.RED


def test_adverse_by_type_events_keep_their_grading():
    box12 = event_box12("cancellation", "penalty")
    data = box13(box12)
    assert sig(data, "MATERIAL_ORDER_CANCELLATION").severity == SV.RED
    assert entry_of(data, box12).direction == "ADVERSE_BY_EVENT_TYPE"
    penalty = sig(data, "MATERIAL_REGULATORY_ACTION")
    assert penalty.severity == SV.RED and penalty.evidence_ids


@pytest.mark.parametrize("name", ["order_award", "dividend", "fund_raise", "acquisition"])
def test_positive_or_neutral_events_are_never_risk_flags(name):
    box12 = event_box12(name)
    assert box12.events[0].financial_value_crore == 500
    data = box13(box12)
    assert data.red_flags == ()
    assert all(e.severity is None for e in data.exchange_event_risks.entries)


def test_an_assessment_must_cite_a_supplied_event():
    box12 = event_box12("claim_against")
    stray = dd.ExchangeEventAssessment(event_id=UUID(int=1), direction="ADVERSE",  # type: ignore[arg-type]
                                       source=SRC)
    with pytest.raises(DueDiligenceInputError) as e:
        box13(box12, event_assessments=(stray,))
    assert e.value.code == "BROKEN_REFERENCE"
    with pytest.raises(DueDiligenceInputError):
        run(event_assessments=(stray,))


def test_event_entry_cannot_carry_a_flag_without_an_adverse_direction():
    with pytest.raises(ValueError, match="adverse"):
        dd.ExchangeEventRisk(event_id=UUID(int=2), event_date=AS_OF, event_type="LITIGATION",
                             category=dd.RiskCategory.LEGAL, title="t", value_crore=D(1),
                             materiality_pct=D(10), status="EVALUATED", severity=SV.RED,
                             rule="r", source_ids=(), direction="NOT_ESTABLISHED")


# --- M-3: exact duplicates count once; distinct same-value records stay distinct ------------


def test_related_party_duplicates_count_once_distinct_records_add_up():
    one = assert_order_and_duplicates_never_matter("related_party", [rpt(100)])
    s = sig(one, "RELATED_PARTY_DEPENDENCE")
    assert one.related_party_risks.totals == ((dd.RelatedPartyKind.REVENUE, D(100)),)
    assert s.value == D("6.211180") and s.severity == SV.GREEN  # 100 / 1610, never 200
    assert len(one.related_party_risks.record_ids) == 1
    assert set(one.related_party_risks.record_ids) <= set(s.record_ids)
    cited_twice = run(related_party=(rpt(100, "AR note 38"), rpt(100, "Screener RPT tab")))
    assert sig(cited_twice, "RELATED_PARTY_DEPENDENCE").value == D("6.211180")
    distinct = run(related_party=(rpt(100), rpt(100, counterparty="Y Ltd")))
    assert distinct.related_party_risks.totals == ((dd.RelatedPartyKind.REVENUE, D(200)),)
    assert sig(distinct, "RELATED_PARTY_DEPENDENCE").severity == SV.YELLOW
    assert len(distinct.related_party_risks.record_ids) == 2


def test_contingent_duplicates_count_once_distinct_items_add_up():
    one = assert_order_and_duplicates_never_matter("contingent_liabilities",
                                                   [contingent(300, "Note 40")])
    s = sig(one, "CONTINGENT_LIABILITY")
    assert s.value == 20 and s.severity == SV.GREEN  # 300 / 1500; a duplicate never makes 40
    assert len(one.contingent_liabilities.record_ids) == 1
    distinct = run(contingent_liabilities=(contingent(300, "Note 40", desc="income tax"),
                                           contingent(300, "Note 40", desc="GST demand")))
    assert sig(distinct, "CONTINGENT_LIABILITY").value == 40
    unnamed = run(contingent_liabilities=(contingent(300, "Note 40 p.146", desc=None),
                                          contingent(300, "Note 40 p.147", desc=None)))
    assert sig(unnamed, "CONTINGENT_LIABILITY").value == 40  # no identity: never merged
    stated = run(contingent_liabilities=(contingent(450, "t", category="TOTAL", desc="Total"),
                                         contingent(300, "a"), contingent(300, "a"),
                                         contingent(300, "b", category="BANK_GUARANTEE")))
    assert sig(stated, "CONTINGENT_LIABILITY").value == 30  # the stated total, still once


def test_management_churn_duplicates_count_once_distinct_changes_count():
    one = assert_order_and_duplicates_never_matter("management_changes",
                                                   [cfo_change(None, "Reg 30 filing")])
    s = sig(one, "MANAGEMENT_CHURN")
    assert s.value == 1 and s.severity == SV.GREEN
    refiled = run(management_changes=(cfo_change(None, "NSE filing"),
                                      cfo_change(None, "BSE filing")))
    assert sig(refiled, "MANAGEMENT_CHURN").value == 1  # one change, two filings
    three = run(management_changes=(
        cfo_change(None, "a"), cfo_change(None, "b", day=date(2024, 2, 1), person="S. Iyer"),
        cfo_change(None, "c", day=date(2024, 9, 1), role="CEO_MD", person="T. Das")))
    s = sig(three, "MANAGEMENT_CHURN")
    assert s.value == 3 and s.severity == SV.RED and len(s.record_ids) == 3


def test_duplicate_records_never_inflate_other_counts():
    fine = dd.PenaltyInput(penalty_date=date(2026, 3, 1), authority="NSE",
                           amount_inr=D(100_000_000), reason="fine", source=SRC)
    data = assert_order_and_duplicates_never_matter("penalties", [fine])
    assert sig(data, "EXCHANGE_PENALTIES").value == 1
    issue = dd.PreferentialIssue(issue_date=date(2025, 1, 10), kind="WARRANTS",
                                 allottee="PROMOTER_GROUP", issue_price=D(70),
                                 reference_price=D(100), reference_basis="ICDR_FLOOR_PRICE",
                                 source=SRC)
    data = assert_order_and_duplicates_never_matter("preferential_issues", [issue])
    assert sig(data, "CHEAP_INSIDER_ALLOTMENT").value == 30
    assert sum(1 for g in data.governance_risks.catalogue if g.kind == "WARRANTS") == 1
    rated = assert_order_and_duplicates_never_matter("ratings", [rating("AA", "r1")])
    assert len(rated.liquidity_credit_risks.ratings) == 1


# --- determinism, immutability, bounds ------------------------------------------------------


def _hash(*models: Any) -> str:
    return hashlib.sha256("".join(m.model_dump_json() for m in models).encode()).hexdigest()


def test_inputs_are_never_mutated_by_the_policy():
    box4, box5 = datasets(rows_for())
    box12 = event_box12("claim_against")
    request = DueDiligenceRequest(
        as_of=AS_OF, company_key=box4.company_key, nse_symbol="RVNL",
        pledges=(pledge_at(3, ref="a"), pledge_at(25, ref="b"), pledge_at(3, ref="a")),
        related_party=(rpt(100), rpt(100)), event_assessments=(assess(box12, "ADVERSE"),))
    before = _hash(box4, box5, box12, request)
    build_due_diligence(box4, box5, request=request, generated_at=NOW, box12=box12)
    assert _hash(box4, box5, box12, request) == before
    assert len(request.pledges) == 3  # the caller's tuple is untouched


@pytest.mark.parametrize("n", [1_000, 5_000])
def test_input_policy_is_bounded(n):
    """n explicit records across families (each family is capped at MAX_EXPLICIT_INPUTS)."""
    per = min(n, dd.MAX_EXPLICIT_INPUTS)
    rest = n - per
    families = {
        "contingent_liabilities": [contingent(1, f"r{i}", desc=f"case {i}") for i in range(per)],
        "related_party": [rpt(1, counterparty=f"P{i}") for i in range(min(rest, per))],
        "pledges": [dd.PledgePoint(period_end=date(1990, 1, 1) + timedelta(days=i),
                                   pledged_pct_of_promoter_holding=D(1), source=SRC)
                    for i in range(max(0, rest - per))],
    }
    assert sum(len(v) for v in families.values()) == n
    pair = datasets(rows_for())
    started = time.perf_counter()
    data = run(pair=pair, **{k: tuple(v) for k, v in families.items()})
    assert time.perf_counter() - started < 60  # generous: an indexed policy, not pairwise
    assert len(data.contingent_liabilities.record_ids) == per
    expected = (D(per) * 100 / D(1500)).quantize(D("0.000001"))
    assert sig(data, "CONTINGENT_LIABILITY").value == expected
    reordered = run(pair=pair, **{k: tuple(v[::-1]) for k, v in families.items()})
    assert canon(reordered) == canon(data)


# --- standalone / consolidated: an explanation conflict is a data limitation, not a risk ------

RECON_FY = 2026


def why(kind: str, text: str, *, metric: str = "REVENUE", year: int = RECON_FY,
        authority: str = "COMPANY_DOCUMENT", ref: str = "AR FY2026 note 45") -> \
        dd.ReconciliationExplanation:
    return dd.ReconciliationExplanation(metric=metric, fiscal_year=year, kind=kind,  # type: ignore[arg-type]
                                        explanation=text, source=src(authority, ref))


REVIEW = dd.ReconciliationReview(fiscal_year=RECON_FY, sources_checked=("note 45",),
                                 source=src(ref="AR FY2026 consolidation schedule"))
SUBSIDIARY = why("GROUP_STRUCTURE", "subsidiary revenue explains the difference", ref="AR note 45")
CLASSIFICATION = why("OTHER", "accounting classification explains the difference",
                     ref="Investor presentation Q4 p.9")
PAT_WHY = why("OTHER", "minority share of profit", metric="PAT")  # leaves REVENUE the only gap


def recon_pair(*, debt: object = 1000) -> Any:
    """Consolidated vs a standalone ledger 40 % smaller on revenue (and so PAT)."""
    return datasets(rows_for() + rows_for(revenue=[600, 660, 726, 798, 878, 966],
                                          debt=debt, basis=ReportingBasis.STANDALONE))


def latest(data: DueDiligenceDataset, metric: str = "REVENUE") -> dd.BasisComparison:
    return next(c for c in data.standalone_consolidated_reconciliation
                if c.metric == metric and c.period_end == FY_END)


def company_risk(data: DueDiligenceDataset) -> list[tuple[str, str]]:
    return sorted((data.signal(i).code, data.signal(i).severity.value)  # type: ignore[union-attr]
                  for i in data.red_flags)


def assert_conflict_is_a_limitation(data: DueDiligenceDataset, metric: str = "REVENUE") -> None:
    c = latest(data, metric)
    assert c.status == dd.ReconciliationStatus.UNAVAILABLE and c.reason == "INPUT_CONFLICT"
    assert c.explanation is None and c.explanation_record_id is None
    assert c.percentage_difference is not None  # the difference itself is still shown
    s = sig(data, "UNEXPLAINED_BASIS_DIFFERENCE")
    assert s.risk_id not in data.red_flags
    assert s.risk_id not in {e.risk_id for e in data.risk_map}
    (entry,) = entries(data, "RECONCILIATION_EXPLANATION")
    assert entry.status == "OPEN" and entry.canonical_source is None and len(entry.values) == 2
    assert ("INPUT_CONFLICT", 1) in data.data_quality_summary.counts
    asks = [i.question for i in data.open_diligence_items
            if "conflicting explanations" in i.question]
    assert asks == [f"Resolve the conflicting explanations for the standalone / consolidated "
                    f"difference in {metric} FY{RECON_FY}"]


def test_explanation_conflict_with_review_is_never_material_unexplained():
    """The Medium: a review plus two equal-authority explanations used to read as YELLOW."""
    pair = recon_pair()
    revenue_only = run(pair=pair, reconciliation_reviews=(REVIEW,),
                       reconciliation_explanations=(SUBSIDIARY, CLASSIFICATION, PAT_WHY))
    assert_conflict_is_a_limitation(revenue_only)
    s = sig(revenue_only, "UNEXPLAINED_BASIS_DIFFERENCE")
    assert s.status == ST.UNAVAILABLE and s.reason == "INPUT_CONFLICT" and s.severity is None
    assert s.risk_id not in revenue_only.red_flags
    assert {i.value for i in s.inputs if i.role.startswith("conflicting")} == {None}
    assert len([i for i in s.inputs if i.role.startswith("conflicting")]) == 2
    # a separately unexplained PAT is still counted; the disputed REVENUE never is
    both = run(pair=pair, reconciliation_reviews=(REVIEW,),
               reconciliation_explanations=(SUBSIDIARY, CLASSIFICATION))
    assert latest(both).reason == "INPUT_CONFLICT"
    assert latest(both, "PAT").status == dd.ReconciliationStatus.MATERIAL_UNEXPLAINED
    s = sig(both, "UNEXPLAINED_BASIS_DIFFERENCE")
    assert s.severity == SV.YELLOW and s.value == 1


def test_explanation_conflict_without_review_is_still_a_conflict():
    data = run(pair=recon_pair(),
               reconciliation_explanations=(SUBSIDIARY, CLASSIFICATION, PAT_WHY))
    assert_conflict_is_a_limitation(data)
    assert sig(data, "UNEXPLAINED_BASIS_DIFFERENCE").reason == "SOURCES_NOT_REVIEWED"


def test_same_kind_different_text_is_a_conflict():
    x = why("GROUP_STRUCTURE", "subsidiary X consolidation explains difference", ref="AR p.1")
    y = why("GROUP_STRUCTURE", "JV Y consolidation explains difference", ref="AR p.2")
    data = run(pair=recon_pair(), reconciliation_reviews=(REVIEW,),
               reconciliation_explanations=(x, y, PAT_WHY))
    assert_conflict_is_a_limitation(data)


def test_single_and_identical_explanations_explain_the_difference():
    pair = recon_pair()
    other = why("OTHER", "inter-segment eliminations")
    for explanations in ((other,), (other, why("OTHER", "inter-segment eliminations",
                                               ref="Investor presentation Q4 p.9"))):
        data = run(pair=pair, reconciliation_reviews=(REVIEW,),
                   reconciliation_explanations=explanations)
        assert latest(data).status == dd.ReconciliationStatus.MATERIAL_EXPLAINED
        assert latest(data).explanation == "inter-segment eliminations"
        assert not entries(data, "RECONCILIATION_EXPLANATION")  # agreement, not a conflict
    grouped = run(pair=pair, reconciliation_reviews=(REVIEW,),
                  reconciliation_explanations=(SUBSIDIARY,))
    assert latest(grouped).status == dd.ReconciliationStatus.EXPECTED_GROUP_DIFFERENCE


def test_higher_authority_explanation_is_selected_in_either_order():
    filed = why("GROUP_STRUCTURE", "subsidiary revenue", authority="EXCHANGE_FILING",
                ref="Reg 33 results")
    screener = why("OTHER", "classification", authority="SCREENER", ref="screener.in")
    runs = [run(pair=recon_pair(), reconciliation_reviews=(REVIEW,),
                reconciliation_explanations=order) for order in ((filed, screener),
                                                                 (screener, filed))]
    assert canon(runs[0]) == canon(runs[1])
    data = runs[0]
    assert latest(data).status == dd.ReconciliationStatus.EXPECTED_GROUP_DIFFERENCE
    assert latest(data).explanation == "subsidiary revenue"
    (entry,) = entries(data, "RECONCILIATION_EXPLANATION")
    assert entry.status == "RESOLVED" and "EXCHANGE_FILING" in (entry.canonical_source or "")
    assert {v.authority for v in entry.values} == {"EXCHANGE_FILING", "SCREENER"}
    assert not any("conflicting explanations" in i.question for i in data.open_diligence_items)


def test_no_review_and_reviewed_unexplained_keep_their_states():
    pair = recon_pair()
    unreviewed = run(pair=pair)
    # RC1: a stable group-footprint gap is STRUCTURAL_PATTERN until reviewed (Phase 22)
    assert latest(unreviewed).status in (dd.ReconciliationStatus.MATERIAL_NOT_REVIEWED,
                                         dd.ReconciliationStatus.STRUCTURAL_PATTERN)
    assert sig(unreviewed, "UNEXPLAINED_BASIS_DIFFERENCE").risk_id not in unreviewed.red_flags
    reviewed = run(pair=pair, reconciliation_reviews=(REVIEW,))
    assert latest(reviewed).status == dd.ReconciliationStatus.MATERIAL_UNEXPLAINED
    s = sig(reviewed, "UNEXPLAINED_BASIS_DIFFERENCE")
    assert s.severity == SV.YELLOW and s.risk_id in reviewed.red_flags
    assert s.risk_id in {e.risk_id for e in reviewed.risk_map}


def test_each_metric_is_reconciled_independently():
    data = run(pair=recon_pair(debt=500), reconciliation_reviews=(REVIEW,),
               reconciliation_explanations=(SUBSIDIARY, CLASSIFICATION, PAT_WHY))
    assert latest(data, "REVENUE").reason == "INPUT_CONFLICT"
    assert latest(data, "PAT").status == dd.ReconciliationStatus.MATERIAL_EXPLAINED
    assert latest(data, "GROSS_DEBT").status == dd.ReconciliationStatus.MATERIAL_UNEXPLAINED
    s = sig(data, "UNEXPLAINED_BASIS_DIFFERENCE")
    assert s.severity == SV.YELLOW and s.value == 1 and s.risk_id in data.red_flags


def test_explanation_key_is_metric_and_year():
    """A REVENUE FY2026 conflict never touches PAT FY2026 or REVENUE FY2025."""
    pat = PAT_WHY
    prior = why("OTHER", "FY2025 eliminations", year=RECON_FY - 1)
    data = run(pair=recon_pair(), reconciliation_reviews=(REVIEW,),
               reconciliation_explanations=(SUBSIDIARY, CLASSIFICATION, pat, prior))
    assert latest(data).reason == "INPUT_CONFLICT"
    assert latest(data, "PAT").status == dd.ReconciliationStatus.MATERIAL_EXPLAINED
    earlier = next(c for c in data.standalone_consolidated_reconciliation
                   if c.metric == "REVENUE" and c.period_end == date(2025, 3, 31))
    assert earlier.status == dd.ReconciliationStatus.MATERIAL_EXPLAINED
    assert earlier.explanation == "FY2025 eliminations"
    pat_2025 = next(c for c in data.standalone_consolidated_reconciliation
                    if c.metric == "PAT" and c.period_end == date(2025, 3, 31))
    # not explained by the FY2025 REVENUE explanation (unreviewed: flagged or structural)
    assert pat_2025.status in (dd.ReconciliationStatus.MATERIAL_NOT_REVIEWED,
                               dd.ReconciliationStatus.STRUCTURAL_PATTERN)
    assert pat_2025.explanation != "FY2025 eliminations"


def test_conflicting_data_never_adds_company_risk():
    """Core invariant: B = A + a conflicting equal-authority explanation."""
    pair = recon_pair()
    clean = (SUBSIDIARY, PAT_WHY)
    a = run(pair=pair, reconciliation_reviews=(REVIEW,), reconciliation_explanations=clean)
    b = run(pair=pair, reconciliation_reviews=(REVIEW,),
            reconciliation_explanations=(*clean, CLASSIFICATION))
    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    assert company_risk(b) == company_risk(a)
    assert sum(rank[s] for _, s in company_risk(b)) <= sum(rank[s] for _, s in company_risk(a))
    assert [e.risk_id for e in b.risk_map] == list(b.red_flags)  # risk map mirrors red flags
    assert {e.code for e in b.risk_map} == {e.code for e in a.risk_map}
    assert sig(a, "UNEXPLAINED_BASIS_DIFFERENCE").severity == SV.GREEN
    assert sig(b, "UNEXPLAINED_BASIS_DIFFERENCE").risk_id not in b.red_flags
    assert b.source_disagreements.open_count > a.source_disagreements.open_count
    assert len(b.open_diligence_items) > len(a.open_diligence_items)


def test_explanation_conflict_is_deterministic_and_inputs_are_untouched():
    pair = recon_pair()
    items = [SUBSIDIARY, CLASSIFICATION]
    first = run(pair=pair, reconciliation_reviews=(REVIEW,),
                reconciliation_explanations=tuple(items))
    for variant in [items] * 10 + variants(items)[1:] + [[SUBSIDIARY, CLASSIFICATION, SUBSIDIARY]]:
        assert canon(run(pair=pair, reconciliation_reviews=(REVIEW,),
                         reconciliation_explanations=tuple(variant))) == canon(first)
    box4, box5 = pair
    request = DueDiligenceRequest(as_of=AS_OF, company_key=box4.company_key,
                                  reconciliation_reviews=(REVIEW,),
                                  reconciliation_explanations=tuple(items))
    before = _hash(box4, box5, request)
    build_due_diligence(box4, box5, request=request, generated_at=NOW)
    assert _hash(box4, box5, request) == before


def test_explanation_conflict_is_independent_of_hash_seed():
    here = Path(__file__).parent
    script = (
        f"import sys, hashlib; sys.path.insert(0, {str(here)!r})\n"
        "import test_due_diligence_inputs as t\n"
        "d = t.run(pair=t.recon_pair(), reconciliation_reviews=(t.REVIEW,), "
        "reconciliation_explanations=(t.CLASSIFICATION, t.SUBSIDIARY))\n"
        "print(hashlib.sha256(t.canon(d).encode()).hexdigest())\n")
    out = {subprocess.run([sys.executable, "-c", script], check=True, capture_output=True,
                          text=True, env={**os.environ, "PYTHONHASHSEED": seed},
                          cwd=here.parent).stdout.strip() for seed in ("0", "1", "4242")}
    assert len(out) == 1


def test_many_explanations_stay_bounded():
    pair = recon_pair()
    metrics = [f"METRIC_{i}" for i in range(dd.MAX_EXPLICIT_INPUTS // 2)]
    items = [why("OTHER", f"text {i % 2}", metric=m, ref=f"r{i % 2}")
             for m in metrics for i in range(2)][:dd.MAX_EXPLICIT_INPUTS]
    items[:2] = [SUBSIDIARY, CLASSIFICATION]
    started = time.perf_counter()
    data = run(pair=pair, reconciliation_reviews=(REVIEW,),
               reconciliation_explanations=tuple(items))
    assert time.perf_counter() - started < 60
    assert latest(data).reason == "INPUT_CONFLICT"
    assert sig(data, "UNEXPLAINED_BASIS_DIFFERENCE").risk_id in data.red_flags  # PAT, reviewed
