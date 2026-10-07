"""DETAILED_RESEARCH_V1 Box 8: management & concall intelligence (offline).

Fixtures A-T are synthetic transcripts run through the real Box 1 catalogue, Box 2
retrieval and Box 3 extraction; actuals come from a real Box 4 dataset, Box 5 analytics
built from it, and (fixture J) a real Box 7 dataset with a segment restructure.

  A clean earnings call            K period mismatch (quarter vs FY)
  B numeric range guidance         L basis mismatch (standalone vs consolidated)
  C margin band guidance           M unknown speaker / unknown role
  D capex / capacity / order book  N ambiguous numbers -> statement only
  E lifecycle (all 7 states)       O language shift across calls
  F MET   G BEAT   H MISSED        P dividend / buyback / M&A statements
  I NOT_YET_DUE                    Q deferred answer   R no answer turn
  J Box 7 restructure blocks       S bank-sector mapping
                                   T deterministic JSON / IDs
"""

import ast
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_business_intelligence import COMPANY, T, ar, business, filings, prior_year
from test_financial_analytics import M, V, company, fy, make

from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.management_calls import normalize_name
from stock_research_ai.application.management_guidance import numeric_targets
from stock_research_ai.application.management_intelligence import (
    build_management_intelligence,
    canonical_management_json,
    verify_management_provenance,
)
from stock_research_ai.application.management_registry import (
    CERTAINTY,
    HEDGING,
    STATEMENT_RULES,
    SUBJECTS,
)
from stock_research_ai.application.management_statements import sentences
from stock_research_ai.domain import management_intelligence as domain
from stock_research_ai.domain.financial_canonical import (
    Currency,
    Industry,
    PeriodType,
    ReportingBasis,
)
from stock_research_ai.domain.management_intelligence import (
    AnswerStatus,
    Equivalence,
    EventType,
    GuidanceOutcome,
    GuidanceUnit,
    Lifecycle,
    ManagementIntelligenceDataset,
    ManagementLimits,
    OutcomeResult,
    Role,
    RoleBasis,
    Section,
    StatedChange,
    StatementType,
    SubjectLayer,
    TargetType,
)

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
BSE = "https://www.bseindia.com"
FY26, FY25, FY27 = date(2026, 3, 31), date(2025, 3, 31), date(2027, 3, 31)
R = OutcomeResult

# --- upstream: a real Box 4 dataset (FY2021-FY2026) and Box 5 analytics built from it ------
# FY26 consolidated: revenue growth 7.142857 %, EBIT margin 32.666667 %, PAT margin 23.5 %,
# tax rate 25 %; Box 4 CAPEX 60 crore a year.
BOX4 = make(company() + [V(M.CAPEX, fy(y), 60) for y in range(2021, 2027)]).model_copy(
    update={"company_key": COMPANY.company_key}
)
BOX5 = build_financial_analytics(BOX4, generated_at=NOW)

MANAGEMENT = [
    "MANAGEMENT: MR. ANIL SHARMA - MANAGING DIRECTOR AND CEO",
    "MS. PRIYA RAO - CHIEF FINANCIAL OFFICER",
]
ANALYSTS = [
    "ANALYSTS: MR. VIKRAM SETH - ALPHA SECURITIES",
    "MS. NEHA JAIN - BETA CAPITAL",
    "MR. ROHIT GUPTA - GAMMA FUND",
]
WELCOME = (
    "Moderator: Ladies and gentlemen, good day and welcome to the Widget Co earnings conference "
    "call. I now hand the conference over to Mr. Anil Sharma."
)


def call(
    title: str, when: str, speech: list[str], people: list[str] | None = None
) -> list[list[str]]:
    """One call: a title page (title, printed date, participant lists) and a speech page."""
    head = ["Widget Co Ltd", title, when, *(MANAGEMENT + ANALYSTS if people is None else people)]
    return [head, speech]


# Fixtures A, F, G, H, K, M, Q, R + currency isolation: the Q4 FY25 call guides FY26.
Q4FY25 = call(
    "Q4 FY25 Earnings Conference Call",
    "April 17, 2025",
    [
        WELCOME,
        "Anil Sharma: Thank you and good afternoon everyone. FY25 was a good year on a "
        "consolidated basis and demand remained healthy across our markets.",
        "We expect revenue growth of 5% to 6% in rupee terms for FY26.",
        "We expect EBIT margin for FY26 to be in the range of 32% to 34%.",
        "Priya Rao: Thank you Anil. We expect PAT margin for FY26 of 25% to 27%.",
        "We expect effective tax rate for FY26 of 26% to 27%.",
        "We expect EBIT margin for Q4 FY26 to be 30% to 31%.",
        "We expect capex of US$ 50 million for FY26.",
        "Moderator: We will now begin the question-and-answer session. The first question is "
        "from the line of Vikram Seth from Alpha Securities.",
        "Vikram Seth: Can you share the margin outlook for next year?",
        "Anil Sharma: We expect margins to remain in our band as pricing stays firm.",
        "Vikram Seth: Thank you.",
        "Moderator: The next question is from the line of Neha Jain from Beta Capital.",
        "Neha Jain: What is the order book for the new contract?",
        "Priya Rao: We are not in a position to disclose that, we will get back to you on that.",
        "Moderator: The next question is from the line of Rohit Gupta from Gamma Fund.",
        "Rohit Gupta: Can you comment on working capital?",
        "Moderator: The next question is from the line of Karan Mehta from Delta Advisors.",
        "Karan Mehta: What about pricing in the export market?",
        "Anil Sharma: Pricing has been stable through the year.",
        "Sunil Verma: I have a follow up on the pricing point please.",
        "Anil Sharma: Sure, prices are stable and we see no change.",
        "Moderator: That concludes the question-and-answer session. Thank you.",
    ],
)

# Fixtures B, C, D, I, N, P: the Q4 FY26 call guides FY27 (not yet due).
Q4FY26 = call(
    "Q4 FY26 Earnings Conference Call",
    "April 20, 2026",
    [
        WELCOME,
        "Anil Sharma: Thank you everyone. FY26 was a year of strong consolidated growth and "
        "healthy demand from our customers.",
        "For FY27 we expect revenue growth of 8% to 10% in rupee terms.",
        "Our EBIT margin band for FY27 is 20% to 22%.",
        "We plan capex of Rs. 500 crore in FY27 for the new plant.",
        "We will commission 200 MW of new capacity by March 2027.",
        "Our order book stands at Rs. 12,000 crore, executable over the next 30 months.",
        "We will maintain a dividend payout ratio of 40% of net profit for FY27.",
        "The board has approved a buyback of Rs. 1,000 crore at a price of Rs. 1,500 per share.",
        "We will continue to evaluate acquisitions in Europe to strengthen our portfolio.",
        "We expect revenue growth of 10% and EBIT margin of 20% next year.",
        "We expect PAT margin of 18% to 19% for FY27 including 1% from cost savings.",
        "Moderator: We will now begin the question-and-answer session. The first question is "
        "from the line of Vikram Seth from Alpha Securities.",
        "Vikram Seth: How should we think about the order book and margins next year?",
        "Priya Rao: We remain confident about margins and we will update the order book "
        "next quarter.",
    ],
)


async def run(
    root: Path,
    docs: dict[str, list[list[str]]],
    *,
    box4=BOX4,
    box5=BOX5,
    box7=None,
    limits: ManagementLimits | None = None,
):
    bundle, catalogue = await filings(
        root, {path: (T.CONCALL_TRANSCRIPT, pages, BSE) for path, pages in docs.items()}
    )
    data = build_management_intelligence(
        bundle=bundle,
        catalogue=catalogue,
        box4=box4,
        box5=box5,
        box7=box7,
        generated_at=NOW,
        limits=limits,
    )
    problems = verify_management_provenance(data, bundle, catalogue, box4, box5, box7)
    assert problems == [], problems
    return data, bundle, catalogue


@pytest.fixture
async def calls(tmp_path):
    return await run(tmp_path, {"/q4fy25.pdf": Q4FY25, "/q4fy26.pdf": Q4FY26})


def event(data: ManagementIntelligenceDataset, title: str):
    return next(e for e in data.events if e.title == title)


def person(data, title: str, key: str):
    e = event(data, title)
    return next(p for p in data.participants if p.event_id == e.event_id and p.person_key == key)


def guidance(data, quote_part: str):
    statements = {s.statement_id: s for s in data.statements}
    found = [g for g in data.guidance if quote_part in statements[g.statement_ids[0]].quote]
    assert len(found) == 1, (quote_part, found)
    return found[0]


def outcome(data, g) -> GuidanceOutcome:
    return next(o for o in data.outcomes if o.guidance_id == g.guidance_id)


def statement(data, quote_part: str):
    found = [s for s in data.statements if quote_part in s.quote]
    assert len(found) == 1, (quote_part, found)
    return found[0]


def unparsed(data, quote_part: str):
    found = [u for u in data.unparsed if quote_part in u.text]
    assert len(found) == 1, (quote_part, found)
    return found[0]


# --- A: events, participants, turns, sections ----------------------------------------------


async def test_a_clean_earnings_call_events_and_participants(calls):
    data, _bundle, _catalogue = calls
    q4 = event(data, "Q4 FY25 Earnings Conference Call")
    assert q4.event_type == EventType.EARNINGS_CALL
    assert q4.call_date == date(2025, 4, 17) and q4.call_date_text == "April 17, 2025"
    period = q4.reporting_period
    assert period is not None
    assert (period.period_type, period.fiscal_year, period.fiscal_quarter, period.period_end) == (
        PeriodType.QUARTER,
        2025,
        4,
        FY25,
    )
    assert q4.reporting_basis == ReportingBasis.CONSOLIDATED
    assert q4.basis_method == "EVENT_MANAGEMENT_TEXT"
    ceo = person(data, q4.title, "anil sharma")
    assert (ceo.raw_name, ceo.role, ceo.role_basis) == (
        "MR. ANIL SHARMA",
        Role.MANAGEMENT,
        RoleBasis.PARTICIPANT_LIST,
    )
    assert ceo.designation == "MANAGING DIRECTOR AND CEO" and ceo.organisation is None
    analyst = person(data, q4.title, "vikram seth")
    assert (analyst.role, analyst.organisation) == (Role.ANALYST, "ALPHA SECURITIES")
    moderator = person(data, q4.title, "moderator")
    assert (moderator.role, moderator.role_basis) == (Role.MODERATOR, RoleBasis.SPEAKER_LABEL)
    introduced = person(data, q4.title, "karan mehta")
    assert (introduced.role, introduced.role_basis) == (
        Role.ANALYST,
        RoleBasis.MODERATOR_INTRODUCTION,
    )
    assert introduced.designation is None  # never inferred


async def test_a_prepared_remarks_and_qa_turns(calls):
    data, _b, _c = calls
    q4 = event(data, "Q4 FY25 Earnings Conference Call")
    turns = [t for t in data.turns if t.event_id == q4.event_id]
    assert [t.sequence for t in turns] == list(range(len(turns)))
    sections = [t.section for t in turns]
    first_qa = sections.index(Section.QA)
    assert set(sections[:first_qa]) == {Section.PREPARED} and set(sections[first_qa:]) == {
        Section.QA
    }
    opener = turns[first_qa]
    assert "question-and-answer" in opener.text
    assert q4.prepared_block_ids and q4.qa_block_ids
    for t in turns:
        assert t.provenance.page_start >= 1 and t.provenance.block_ids
        assert not t.truncated and t.word_count == len(t.text.split())


async def test_m_unknown_speaker_keeps_unknown_role(calls):
    data, _b, _c = calls
    title = "Q4 FY25 Earnings Conference Call"
    sunil = person(data, title, "sunil verma")
    assert (sunil.role, sunil.role_basis) == (Role.UNKNOWN, RoleBasis.NONE)
    assert sunil.warnings == ("ROLE_UNKNOWN",)
    assert any(
        w.code == "ROLE_UNKNOWN" and "sunil verma" in (w.detail or "") for w in data.warnings
    )
    # an unknown speaker never produces management statements
    assert not [s for s in data.statements if s.speaker_id == sunil.participant_id]


# --- Q&A: pairing, Q (deferred), R (no answer) ---------------------------------------------


async def test_qa_pairing_and_answer_statuses(calls):
    data, _b, _c = calls
    q4 = event(data, "Q4 FY25 Earnings Conference Call")
    people = {p.participant_id: p.person_key for p in data.participants}
    pairs = [q for q in data.qa_pairs if q.event_id == q4.event_id]
    summary = [
        (people[q.questioner_id], q.answer_status, [people[r] for r in q.responder_ids])
        for q in pairs
    ]
    assert summary == [
        ("vikram seth", AnswerStatus.ANSWERED, ["anil sharma"]),
        ("neha jain", AnswerStatus.DEFERRED, ["priya rao"]),
        ("rohit gupta", AnswerStatus.NO_ANSWER_TURN, []),
        ("karan mehta", AnswerStatus.ANSWERED, ["anil sharma"]),
        ("sunil verma", AnswerStatus.ANSWERED, ["anil sharma"]),
    ]
    deferred = pairs[1]
    assert deferred.deferral_phrases == ("not in a position to disclose", "we will get back")
    assert "DEALS" in deferred.topics  # 'order book' question
    assert pairs[4].questioner_role == Role.UNKNOWN
    # the 'Thank you.' courtesy turn opens no new pair
    assert len(pairs[0].question_turn_ids) == 1


# --- statements ------------------------------------------------------------------------------


async def test_statement_classification_and_provenance(calls):
    data, bundle, _c = calls
    blocks = {b.block_id: b for e in bundle.extractions for b in e.blocks}
    s = statement(data, "We expect EBIT margin for FY26")
    assert s.statement_types[0] == StatementType.EXPECTATION
    assert StatementType.MARGIN in s.statement_types and s.forward_looking
    assert s.speaker_role == Role.MANAGEMENT and s.confidence.value == "MEDIUM"
    assert "MARGINS" in s.topics
    assert s.provenance.sentence_index is not None
    assert s.quote in " ".join(blocks[b].normalized_text for b in s.provenance.block_ids)
    # analyst / moderator text is never a management statement
    assert not [x for x in data.statements if "What about pricing" in x.quote]
    # pleasantries are not statements
    assert not [x for x in data.statements if x.quote == "Thank you Anil."]


async def test_d_operational_statements(calls):
    data, _b, _c = calls
    capex = guidance(data, "We plan capex of Rs. 500 crore")
    assert (capex.subject.layer, capex.subject.target_code) == (SubjectLayer.BOX4_METRIC, "CAPEX")
    assert (capex.unit, capex.currency, capex.value, capex.scale_to_crore) == (
        GuidanceUnit.MONEY,
        Currency.INR,
        Decimal(500),
        Decimal(1),
    )
    assert outcome(data, capex).result == R.NOT_YET_DUE
    capacity = statement(data, "200 MW of new capacity by March 2027")
    assert StatementType.CAPACITY in capacity.statement_types and capacity.forward_looking
    order_book = statement(data, "order book stands at Rs. 12,000 crore")
    assert StatementType.ORDER_BOOK in order_book.statement_types
    assert "executable over the next 30 months" in order_book.quote
    assert not [g for g in data.guidance if g.statement_ids == (order_book.statement_id,)]


async def test_p_capital_allocation_statements(calls):
    data, _b, _c = calls
    payout = statement(data, "dividend payout ratio of 40%")
    buyback = statement(data, "buyback of Rs. 1,000 crore")
    deal = statement(data, "evaluate acquisitions in Europe")
    assert StatementType.DIVIDEND_BUYBACK in payout.statement_types
    assert StatementType.DIVIDEND_BUYBACK in buyback.statement_types
    assert StatementType.M_AND_A in deal.statement_types
    item = guidance(data, "dividend payout ratio of 40%")
    assert (item.subject.subject_code, item.subject.equivalence) == (
        "DIVIDEND_PAYOUT",
        Equivalence.PROXY,
    )
    assert outcome(data, item).result == R.NOT_COMPARABLE
    # the buyback sentence carries two amounts and no registered numeric subject: no guidance
    assert not [g for g in data.guidance if g.statement_ids == (buyback.statement_id,)]


# --- B, C, I: guidance parsing ----------------------------------------------------------------


async def test_b_numeric_range_guidance(calls):
    data, _b, _c = calls
    g = guidance(data, "For FY27 we expect revenue growth of 8% to 10%")
    assert (g.subject.subject_code, g.subject.layer, g.subject.target_code) == (
        "REVENUE_GROWTH",
        SubjectLayer.BOX5_METRIC,
        "REVENUE_GROWTH_YOY",
    )
    assert g.subject.equivalence == Equivalence.EXACT and g.qualifier == "REPORTED_CURRENCY"
    assert (g.target_type, g.low, g.high, g.unit, g.decimals) == (
        TargetType.RANGE,
        Decimal(8),
        Decimal(10),
        GuidanceUnit.PERCENT,
        0,
    )
    assert g.target_period is not None
    assert (g.target_period.period_type, g.target_period.period_end, g.target_period.method) == (
        PeriodType.FY,
        FY27,
        "EXPLICIT_LABEL",
    )
    assert g.issued_date == date(2026, 4, 20) and g.reporting_basis == ReportingBasis.CONSOLIDATED
    assert g.confidence.value == "HIGH" and g.lifecycle == Lifecycle.ISSUED


async def test_c_margin_band_and_i_not_yet_due(calls):
    data, _b, _c = calls
    g = guidance(data, "Our EBIT margin band for FY27 is 20% to 22%")
    assert (g.subject.subject_code, g.subject.equivalence, g.low, g.high) == (
        "EBIT_MARGIN",
        Equivalence.EXACT,
        Decimal(20),
        Decimal(22),
    )
    o = outcome(data, g)
    assert (o.result, o.rule, o.actual) == (R.NOT_YET_DUE, "NOT_YET_DUE", None)
    assert "2027-03-31" in o.reason and "2026-03-31" in o.reason


async def test_n_ambiguous_numbers_stay_statements(calls):
    data, _b, _c = calls
    two_subjects = statement(data, "revenue growth of 10% and EBIT margin of 20%")
    two_numbers = statement(data, "PAT margin of 18% to 19% for FY27 including 1%")
    for s in (two_subjects, two_numbers):
        assert not [g for g in data.guidance if s.statement_id in g.statement_ids]
    assert unparsed(data, "revenue growth of 10% and EBIT").kind == "GUIDANCE_AMBIGUOUS"
    assert unparsed(data, "including 1% from cost").kind == "GUIDANCE_VALUE_AMBIGUOUS"


# --- F, G, H, K, currency: outcomes vs Box 5 / Box 4 actuals ------------------------------------


async def test_f_g_h_outcomes_against_box5(calls):
    data, _b, _c = calls
    metrics = {m.metric_id: m for m in BOX5.all_metrics()}
    met = outcome(data, guidance(data, "EBIT margin for FY26 to be in the range of 32% to 34%"))
    beat = outcome(data, guidance(data, "revenue growth of 5% to 6% in rupee terms for FY26"))
    missed = outcome(data, guidance(data, "PAT margin for FY26 of 25% to 27%"))
    assert (met.result, beat.result, missed.result) == (R.MET, R.BEAT, R.MISSED)
    for o, code in ((met, "EBIT_MARGIN"), (beat, "REVENUE_GROWTH_YOY"), (missed, "PAT_MARGIN")):
        assert o.actual is not None and o.rule_version == "guidance-outcome-rules/1"
        actual = metrics[o.actual.record_id]
        assert (actual.code, actual.value, actual.period.period_end) == (
            code,
            o.actual.value,
            FY26,
        )
        assert o.actual.layer == SubjectLayer.BOX5_METRIC
        assert set(o.actual.upstream_ids) == set(actual.input_value_ids)
    assert beat.actual is not None and beat.actual.value == Decimal("7.142857")


async def test_directionality_not_registered_gives_ambiguous(calls):
    data, _b, _c = calls
    o = outcome(data, guidance(data, "effective tax rate for FY26 of 26% to 27%"))
    assert (o.result, o.rule) == (R.AMBIGUOUS, "RANGE/DIRECTIONALITY_UNREGISTERED")
    assert o.actual is not None and o.actual.value == Decimal(25)


async def test_k_period_mismatch_quarter_vs_fy(calls):
    data, _b, _c = calls
    g = guidance(data, "EBIT margin for Q4 FY26 to be 30% to 31%")
    assert g.target_period is not None and g.target_period.period_type == PeriodType.QUARTER
    o = outcome(data, g)
    assert (o.result, o.rule) == (R.NOT_COMPARABLE, "PERIOD_MISMATCH")


async def test_currency_isolation(calls):
    data, _b, _c = calls
    g = guidance(data, "capex of US$ 50 million for FY26")
    assert (g.currency, g.scale_to_crore) == (Currency.USD, Decimal("0.1"))
    o = outcome(data, g)
    assert (o.result, o.rule) == (R.NOT_COMPARABLE, "CURRENCY_MISMATCH")


async def test_partial_and_basis_mismatch(tmp_path):
    """Growth below the range but positive -> PARTIAL; L standalone guidance vs only
    consolidated actuals -> NOT_COMPARABLE."""
    doc = call(
        "Q4 FY25 Earnings Conference Call",
        "April 17, 2025",
        [
            WELCOME,
            "Anil Sharma: Good afternoon everyone, thank you for joining us today on this call "
            "to discuss the consolidated results of the year.",
            "We expect revenue growth of 8% to 10% in reported terms for FY26.",
            "On a standalone basis we expect EBIT margin for FY26 of 32% to 34%.",
        ],
    )
    data, _b, _c = await run(tmp_path, {"/call.pdf": doc})
    assert data.events[0].basis_method == "BOTH_BASES_MENTIONED"
    partial = outcome(data, guidance(data, "revenue growth of 8% to 10% in reported terms"))
    assert (partial.result, partial.rule) == (R.PARTIAL, "RANGE/POSITIVE_GROWTH_BELOW_RANGE")
    assert partial.warnings == ("BASIS_FROM_ONLY_AVAILABLE_ACTUAL",)
    standalone = guidance(data, "On a standalone basis")
    assert (standalone.reporting_basis, standalone.basis_method) == (
        ReportingBasis.STANDALONE,
        "STATEMENT_TEXT",
    )
    o = outcome(data, standalone)
    assert (o.result, o.rule) == (R.NOT_COMPARABLE, "BASIS_MISMATCH")


async def test_constant_currency_and_unstated_currency_are_never_scored(tmp_path):
    doc = call(
        "Q4 FY25 Earnings Conference Call",
        "April 17, 2025",
        [
            WELCOME,
            "Anil Sharma: Good afternoon everyone, thank you for joining us today on this call "
            "to discuss the results of the year.",
            "We expect revenue growth of 5% to 6% in constant currency terms for FY26.",
            "Priya Rao: Our revenue guidance for FY26 is 5% to 6%.",
        ],
    )
    data, _b, _c = await run(tmp_path, {"/call.pdf": doc})
    cc = guidance(data, "constant currency terms")
    plain = guidance(data, "Our revenue guidance for FY26")
    assert (cc.qualifier, cc.subject.equivalence) == ("CONSTANT_CURRENCY", Equivalence.PROXY)
    assert (plain.qualifier, plain.subject.equivalence) == (None, Equivalence.PROXY)
    for g in (cc, plain):
        assert (outcome(data, g).result, outcome(data, g).rule) == (
            R.NOT_COMPARABLE,
            "PROXY_DEFINITION",
        )
    # an unstated qualifier is compatible with constant currency for the lifecycle
    assert plain.lifecycle == Lifecycle.REITERATED and plain.previous_guidance_id == cc.guidance_id


# --- E: lifecycle; O: language ------------------------------------------------------------------


LIFECYCLE_STEPS = [
    ("Q1 FY26", "July 20, 2025", "We expect EBIT margin for FY27 to be 20% to 22%."),
    ("Q2 FY26", "October 18, 2025", "We maintain our EBIT margin guidance for FY27 at 20% to 22%."),
    (
        "Q3 FY26",
        "January 16, 2026",
        "We are raising our EBIT margin guidance for FY27 to 21% to 23%.",
    ),
    (
        "Q4 FY26",
        "April 20, 2026",
        "We are lowering our EBIT margin guidance for FY27 to 20% to 22%.",
    ),
    ("Q1 FY27", "July 19, 2026", "We now expect EBIT margin for FY27 of 20.5% to 21.5%."),
    ("Q2 FY27", "October 17, 2026", "We now expect EBIT margin for FY27 of 19% to 23%."),
    ("Q3 FY27", "January 15, 2027", "We are withdrawing our EBIT margin guidance for FY27."),
]
HEDGED = (
    "Demand may soften and margins could be under pressure as the environment is uncertain and "
    "challenging, depending on client decisions."
)
CERTAIN = (
    "We are confident about demand and we will deliver on our plans as we remain committed to our "
    "targets, clearly."
)


def lifecycle_documents() -> dict[str, list[list[str]]]:
    """One transcript per call (Box 2 retrieves each call as its own document)."""
    docs: dict[str, list[list[str]]] = {}
    for index, (quarter, when, sentence) in enumerate(LIFECYCLE_STEPS):
        tone = HEDGED if index == 0 else CERTAIN
        speech = [
            WELCOME,
            f"Anil Sharma: Good afternoon everyone and thank you for joining this {quarter} call. "
            f"{sentence} {tone}",
        ]
        if quarter == "Q4 FY26":
            speech.append("For FY28 we expect EBIT margin of 22% to 24%.")
        path = f"/{quarter.replace(' ', '').lower()}.pdf"
        docs[path] = call(f"{quarter} Earnings Conference Call", when, speech, MANAGEMENT)
    return docs


async def test_e_lifecycle_sequence(tmp_path):
    data, _b, _c = await run(tmp_path, lifecycle_documents())
    assert len(data.events) == 7
    assert [e.call_date for e in data.events] == sorted(e.call_date for e in data.events)
    fy27 = [g for g in data.guidance if g.target_period and g.target_period.period_end == FY27]
    assert [g.lifecycle for g in fy27] == [
        Lifecycle.ISSUED,
        Lifecycle.REITERATED,
        Lifecycle.RAISED,
        Lifecycle.LOWERED,
        Lifecycle.NARROWED,
        Lifecycle.WIDENED,
        Lifecycle.WITHDRAWN,
    ]
    assert [g.stated_change for g in fy27] == [
        StatedChange.NONE,
        StatedChange.MAINTAINED,
        StatedChange.RAISED,
        StatedChange.LOWERED,
        StatedChange.NONE,
        StatedChange.NONE,
        StatedChange.WITHDRAWN,
    ]
    for previous, current in zip(fy27, fy27[1:], strict=False):
        assert current.previous_guidance_id == previous.guidance_id
        assert "STATED_CHANGE_MISMATCH" not in current.warnings
    withdrawn = fy27[-1]
    assert withdrawn.target_type == TargetType.QUALITATIVE and withdrawn.low is None
    assert outcome(data, withdrawn).result == R.NOT_COMPARABLE
    # a new target period is a new guidance item, never 'raised'
    fy28 = [g for g in data.guidance if g.target_period and g.target_period.fiscal_year == 2028]
    assert [(g.lifecycle, g.previous_guidance_id) for g in fy28] == [(Lifecycle.ISSUED, None)]
    line = next(t for t in data.timelines if FY27.isoformat() in t.target_period_key)
    assert line.guidance_ids == tuple(g.guidance_id for g in fy27)
    assert line.lifecycles == tuple(g.lifecycle for g in fy27)


async def test_o_language_shift_is_descriptive(tmp_path):
    data, _b, _c = await run(tmp_path, lifecycle_documents())
    first, second = data.events[0], data.events[1]
    rows = {(m.event_id, m.section): m for m in data.language}
    a = rows[(first.event_id, Section.UNKNOWN)]
    b = rows[(second.event_id, Section.UNKNOWN)]
    assert a.lexicon_version == "management-language/1"
    assert {
        "hedging:may",
        "hedging:could",
        "hedging:uncertain",
        "hedging:challenging",
        "hedging:depending on",
    } <= set(a.phrase_counts)
    assert a.hedging_count == 5 and a.certainty_count == 0
    assert b.certainty_count >= 4 and b.hedging_count == 0
    assert a.hedging_per_1000_words == (Decimal(5000) / a.management_words).quantize(
        Decimal("0.0001")
    )
    assert b.previous_event_id == first.event_id
    assert b.hedging_rate_change is not None and b.hedging_rate_change < 0
    assert b.certainty_rate_change is not None and b.certainty_rate_change > 0
    assert a.previous_event_id is None and a.hedging_rate_change is None


async def test_themes_and_chronology(calls):
    data, _b, _c = calls
    demand = next(t for t in data.themes if t.topic == "DEMAND" and t.source == "MANAGEMENT")
    first, second = data.events[0], data.events[1]
    assert demand.event_ids == (first.event_id, second.event_id)
    assert (demand.first_date, demand.last_date) == (date(2025, 4, 17), date(2026, 4, 20))
    asked = {t.topic for t in data.themes if t.source == "QUESTIONS"}
    assert {"MARGINS", "DEALS", "PRICING"} <= asked


# --- J: Box 7 segment restructure ------------------------------------------------------------


async def test_j_box7_restructure_blocks_segment_outcome(tmp_path):
    old = ["Segment revenue", "Chemicals 1,000 900", "Agri 300 250", "Total 1,300 1,150"]
    new_head = [
        "Notes to the Consolidated Financial Statements",
        "Segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026",
    ]
    new = [
        "Segment revenue",
        "Specialty Chemicals 700",
        "Bulk Chemicals 500",
        "Agri 400",
        "Total 1,600",
    ]
    note = [
        "During the year the Company reorganised its reportable segments. Comparative "
        "segment figures have not been restated as it is impracticable to do so."
    ]
    box7, _box4_7, _bundle7 = await business(
        tmp_path / "box7",
        {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(new_head + new, note)},
    )
    doc = call(
        "Q4 FY25 Earnings Conference Call",
        "April 17, 2025",
        [
            WELCOME,
            "Anil Sharma: Good afternoon everyone, thank you for joining us today on this call "
            "to discuss the results of the year.",
            "We expect Agri revenue growth of 25% to 30% in rupee terms for FY26.",
        ],
    )
    data, _b, _c = await run(tmp_path / "box8", {"/call.pdf": doc}, box7=box7)
    g = guidance(data, "Agri revenue growth")
    assert (g.subject.layer, g.subject.target_code, g.subject.equivalence) == (
        SubjectLayer.BOX7_SEGMENT,
        "SEGMENT_REVENUE_GROWTH:agri",
        Equivalence.EXACT,
    )
    o = outcome(data, g)
    assert (o.result, o.rule) == (R.NOT_COMPARABLE, "STRUCTURE_CHANGED")
    assert "NOT_COMPARABLE_STRUCTURE" in o.warnings
    assert data.source_box7 is not None and data.sector == box7.sector.value


# --- S: bank sector safety -------------------------------------------------------------------


async def test_s_bank_guidance_mapping_is_sector_safe(tmp_path):
    bank = BOX4.model_copy(update={"industry": Industry.BANK})
    doc = call(
        "Q4 FY26 Earnings Conference Call",
        "April 20, 2026",
        [
            WELCOME,
            "Anil Sharma: Good afternoon everyone, thank you for joining us today on this call "
            "to discuss the results of the bank.",
            "We expect EBITDA margin of 30% to 32% for FY27.",
            "We expect capacity utilisation to improve sharply next year.",
            "We expect credit cost of 1.2% to 1.4% for FY27.",
            "We expect NIM of 3.5% to 3.7% for FY27.",
            "We expect advances growth of 15% to 17% for FY27.",
        ],
    )
    data, _b, _c = await run(tmp_path, {"/call.pdf": doc}, box4=bank, box5=None)
    assert data.sector == "FINANCIAL" and data.source_box5 is None
    assert any(w.code == "SOURCE_MISSING" for w in data.warnings)
    codes = {g.subject.subject_code for g in data.guidance}
    assert codes == {"CREDIT_COST", "NIM", "ADVANCES_GROWTH"}
    assert unparsed(data, "EBITDA margin").kind == "SUBJECT_SECTOR_BLOCKED"
    assert unparsed(data, "capacity utilisation").kind == "SUBJECT_SECTOR_BLOCKED"
    credit = guidance(data, "credit cost")
    assert (credit.subject.layer, credit.subject.target_code) == (
        SubjectLayer.BOX7_KPI,
        "CREDIT_COST",
    )
    assert (outcome(data, credit).result, outcome(data, credit).rule) == (
        R.INSUFFICIENT_DATA,
        "ACTUAL_NOT_AVAILABLE",
    )
    nim = guidance(data, "NIM of 3.5%")
    assert (nim.subject.layer, nim.subject.equivalence) == (SubjectLayer.UNLINKED, Equivalence.NONE)
    assert "SUBJECT_UNLINKED" in nim.warnings
    assert outcome(data, nim).rule == "SUBJECT_UNLINKED"


async def test_bank_subjects_are_blocked_for_industrial_companies(tmp_path):
    doc = call(
        "Q4 FY26 Earnings Conference Call",
        "April 20, 2026",
        [
            WELCOME,
            "Anil Sharma: Good afternoon everyone, thank you for joining us today on this call "
            "to discuss the results of the year.",
            "We expect NIM of 3.5% to 3.7% for FY27.",
        ],
    )
    data, _b, _c = await run(tmp_path, {"/call.pdf": doc})
    assert not data.guidance
    assert unparsed(data, "NIM of 3.5%").kind == "SUBJECT_SECTOR_BLOCKED"


# --- T: determinism, contracts, provenance, bounds -------------------------------------------


async def test_t_identical_inputs_give_identical_json(tmp_path):
    a, bundle, catalogue = await run(tmp_path, {"/q4fy25.pdf": Q4FY25, "/q4fy26.pdf": Q4FY26})
    b = build_management_intelligence(
        bundle=bundle, catalogue=catalogue, box4=BOX4, box5=BOX5, box7=None, generated_at=NOW
    )
    assert canonical_management_json(a) == canonical_management_json(b)
    assert a.dataset_id == b.dataset_id
    later = build_management_intelligence(
        bundle=bundle,
        catalogue=catalogue,
        box4=BOX4,
        box5=BOX5,
        box7=None,
        generated_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert later.dataset_id == a.dataset_id
    assert [g.guidance_id for g in later.guidance] == [g.guidance_id for g in a.guidance]
    payload = json.loads(canonical_management_json(a))
    assert payload["schema_version"] == "MANAGEMENT_INTELLIGENCE_DATASET_V1"
    assert payload["engine_version"] == "management-intelligence/1"
    assert payload["registry_version"] == "management-registry/1"
    assert {
        u["layer"] for u in (payload["source_box3"], payload["source_box4"], payload["source_box5"])
    } == {"BOX3", "BOX4", "BOX5"}
    assert all(UUID(e["event_id"]).version == 5 for e in payload["events"])
    assert all(UUID(s["statement_id"]).version == 5 for s in payload["statements"])


async def test_contracts_are_strict_and_immutable(calls):
    data, _b, _c = calls
    g = data.guidance[0]
    with pytest.raises(ValidationError):
        g.lifecycle = Lifecycle.RAISED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        domain.GuidanceItem.model_validate({**g.model_dump(), "score": 5})
    with pytest.raises(ValidationError):
        domain.GuidanceItem.model_validate({**g.model_dump(), "target_type": "RANGE", "low": None})
    o = data.outcomes[0]
    with pytest.raises(ValidationError):
        domain.GuidanceOutcome.model_validate({**o.model_dump(), "result": "MET", "actual": None})
    s = data.statements[0]
    with pytest.raises(ValidationError):
        domain.Provenance.model_validate({**s.provenance.model_dump(), "block_ids": ()})


async def test_dataset_rejects_orphans(calls):
    data, _b, _c = calls
    raw = data.model_dump()
    raw["turns"][0]["participant_id"] = UUID(int=1)
    with pytest.raises(ValidationError, match="orphaned"):
        ManagementIntelligenceDataset.model_validate(raw)
    raw = data.model_dump()
    raw["outcomes"][0]["guidance_id"] = UUID(int=2)
    with pytest.raises(ValidationError, match="has no guidance"):
        ManagementIntelligenceDataset.model_validate(raw)
    raw = data.model_dump()
    raw["guidance"][0]["statement_ids"] = (UUID(int=3),)
    with pytest.raises(ValidationError, match="orphaned"):
        ManagementIntelligenceDataset.model_validate(raw)


async def test_provenance_verifier_resolves_and_detects_tampering(calls):
    data, bundle, catalogue = calls
    assert verify_management_provenance(data, bundle, catalogue, BOX4, BOX5, None) == []
    s = data.statements[0]
    forged = data.model_copy(
        update={
            "statements": (
                s.model_copy(update={"quote": "We promise 99% growth."}),
                *data.statements[1:],
            )
        }
    )
    problems = verify_management_provenance(forged, bundle, catalogue, BOX4, BOX5, None)
    assert any("text not found" in p for p in problems)
    scored = next(o for o in data.outcomes if o.result == R.MET)
    assert scored.actual is not None
    changed = scored.model_copy(
        update={"actual": scored.actual.model_copy(update={"value": Decimal("99.9")})}
    )
    tampered = data.model_copy(
        update={"outcomes": tuple(changed if o is scored else o for o in data.outcomes)}
    )
    problems = verify_management_provenance(tampered, bundle, catalogue, BOX4, BOX5, None)
    assert any("Box 5 metric missing / changed" in p for p in problems)
    # every outcome walks back to its statement, turn, event and Box 3 blocks
    statements = {x.statement_id: x for x in data.statements}
    turns = {t.turn_id: t for t in data.turns}
    blocks = {b.block_id for e in bundle.extractions for b in e.blocks}
    for o in data.outcomes:
        g = next(x for x in data.guidance if x.guidance_id == o.guidance_id)
        st = statements[g.statement_ids[0]]
        assert turns[st.turn_id].event_id == g.event_id
        assert set(st.provenance.block_ids) <= blocks


async def test_resource_limits_mark_partial(tmp_path):
    data, _b, _c = await run(
        tmp_path,
        {"/q4fy25.pdf": Q4FY25},
        limits=ManagementLimits(max_statements=3, max_turns=8),
    )
    assert data.status == "PARTIAL"
    limited = [w for w in data.warnings if w.code == "RESOURCE_LIMIT_REACHED"]
    assert {w.detail.split(":")[0] for w in limited if w.detail} == {"turns", "statements"}
    assert len(data.statements) <= 3 and len(data.turns) == 8
    assert all(w.severity.value == "HIGH" for w in limited)


async def test_accuracy_summaries_are_counts_only(calls):
    data, _b, _c = calls
    overall = next(a for a in data.accuracy if a.scope == "OVERALL")
    counts = {r: sum(1 for o in data.outcomes if o.result == r) for r in R}
    assert overall.issued == len(data.guidance)
    assert (overall.met, overall.beat, overall.missed, overall.partial) == (
        counts[R.MET],
        counts[R.BEAT],
        counts[R.MISSED],
        counts[R.PARTIAL],
    )
    assert overall.due == overall.issued - overall.not_yet_due
    fields = set(domain.AccuracySummary.model_fields)
    assert not {f for f in fields if any(w in f for w in ("score", "grade", "rank", "rating"))}


async def test_statistics(calls):
    data, _b, _c = calls
    stats = data.statistics
    assert stats.events == 2 and stats.documents_consumed == 2
    assert stats.guidance == len(data.guidance) and stats.statements == len(data.statements)
    assert stats.participants_by_role["ANALYST"] >= 4
    assert stats.answer_status_counts == {"ANSWERED": 4, "DEFERRED": 1, "NO_ANSWER_TURN": 1}
    assert sum(stats.outcome_counts.values()) == len(data.outcomes)


async def test_no_transcript_is_reported(tmp_path):
    bundle, catalogue = await filings(tmp_path, {"/ar.pdf": ar(["Annual report", "Revenue"])})
    data = build_management_intelligence(
        bundle=bundle, catalogue=catalogue, box4=BOX4, box5=BOX5, box7=None, generated_at=NOW
    )
    assert not data.events and data.status == "COMPLETE"
    assert any(w.code == "TRANSCRIPT_MISSING" for w in data.warnings)


# --- pure parsing rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("growth of 1.5% to 3% in FY27", [("RANGE", "1.5", "3", None)]),
        ("margin between 20 and 22% this year", [("RANGE", "20", "22", None)]),
        ("growth of 8-10%", [("RANGE", "8", "10", None)]),
        ("at least 25% of revenue", [("MINIMUM", None, None, "25")]),
        ("up to 12% of sales", [("MAXIMUM", None, None, "12")]),
        ("15 to 20 basis points expansion", [("RANGE", "15", "20", None)]),
        ("INR 800 crores to INR 1,000 crores of gains", [("RANGE", "800", "1000", None)]),
        ("2.4% and 1%", [("POINT", None, None, "2.4"), ("POINT", None, None, "1")]),
    ],
)
def test_numeric_target_parsing(text, expected):
    found = [
        (
            t.target_type.value,
            None if t.low is None else str(t.low),
            None if t.high is None else str(t.high),
            None if t.value is None else str(t.value),
        )
        for t in numeric_targets(text)
    ]
    assert found == expected


def test_money_units_and_scale():
    (t,) = numeric_targets("capex of Rs. 1,200 crore")
    assert (t.unit, t.currency, t.value, t.scale_to_crore) == (
        GuidanceUnit.MONEY,
        Currency.INR,
        Decimal(1200),
        Decimal(1),
    )
    (usd,) = numeric_targets("revenue of $2 bn")
    assert (usd.currency, usd.scale_to_crore) == (Currency.USD, Decimal(100))


def test_sentence_splitting_keeps_abbreviations_and_initials():
    text = "Mr. Sharma said growth was 5%. M. B. Mahesh asked about Rs. 500 crore. Done now."
    parts = [text[a:b] for a, b in sentences(text)]
    assert parts == [
        "Mr. Sharma said growth was 5%.",
        "M. B. Mahesh asked about Rs. 500 crore.",
        "Done now.",
    ]


def test_name_normalisation_is_exact_not_fuzzy():
    assert (
        normalize_name("MR. VINAY M. TONSE") == normalize_name("Vinay M. Tonse") == "vinay m tonse"
    )
    assert normalize_name("Sanjana B.") == "sanjana b"
    assert normalize_name("Chandra R. Srikanth") != normalize_name("Chandra Srikanth")


def test_registries_are_versioned_and_explicit():
    assert domain.REGISTRY_VERSION == "management-registry/1"
    assert domain.OUTCOME_RULES_VERSION == "guidance-outcome-rules/1"
    assert domain.LANGUAGE_LEXICON_VERSION == "management-language/1"
    assert len({r.code for r in SUBJECTS}) == len(SUBJECTS)
    by_code = {r.code: r for r in SUBJECTS}
    assert by_code["OPERATING_MARGIN"].equivalence == Equivalence.PROXY
    assert by_code["NIM"].layer == SubjectLayer.UNLINKED
    assert by_code["EBITDA_MARGIN"].sectors == "NON_FINANCIAL"
    assert by_code["TAX_RATE"].higher_is_better is None  # no assumed directionality
    assert {t for t, _ in STATEMENT_RULES} <= set(StatementType)
    assert "may" in HEDGING and "confident" in CERTAINTY


# --- guards -----------------------------------------------------------------------------------


def _box8_modules() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    return [
        root / "domain" / "management_intelligence.py",
        *sorted((root / "application").glob("management_*.py")),
    ]


def test_box8_modules_stay_deterministic_and_offline():
    banned = {"httpx", "requests", "anthropic", "openai", "random", "socket", "urllib", "aiohttp"}
    banned_parts = (
        "adapters",
        "llm",
        "aicredits",
        "ai_credits",
        "browser",
        "market",
        "valuation",
        "forecast",
        "report",
        "summary",
        "analyst_model",
        "challenge",
    )
    modules = _box8_modules()
    assert len(modules) == 8
    for path in modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                for name in names:
                    assert name.split(".")[0] not in banned, (path.name, name)
                    assert not any(part in name for part in banned_parts), (path.name, name)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("uuid4", "now"):
                pytest.fail(f"{path.name} uses non-deterministic {node.func.attr}")  # type: ignore[union-attr]


async def test_no_subjective_management_labels(calls):
    data, _b, _c = calls
    text = canonical_management_json(data).lower()
    for word in (
        "evasive",
        "dishonest",
        "honest",
        "credible",
        "trustworth",
        "good management",
        "poor management",
        "management score",
        "credibility",
    ):
        assert word not in text, word
    vocabulary = {
        member.value
        for enum in (
            domain.OutcomeResult,
            domain.AnswerStatus,
            domain.Lifecycle,
            domain.Confidence,
            domain.StatementType,
        )
        for member in enum
    }
    for word in ("EVASIVE", "CREDIBLE", "HONEST", "SCORE", "GRADE", "RANK"):
        assert not any(word in v for v in vocabulary), word
    model_fields = {
        f
        for m in vars(domain).values()
        if isinstance(m, type) and hasattr(m, "model_fields")
        for f in m.model_fields
    }  # type: ignore[attr-defined]
    assert not {f for f in model_fields if "score" in f or "credib" in f or "rating" in f}


def test_period_labels_are_never_read_as_values():
    (t,) = numeric_targets("our EBIT margin guidance for FY27 to 21% to 23%")
    assert (t.target_type, t.low, t.high) == (TargetType.RANGE, Decimal(21), Decimal(23))
    (q,) = numeric_targets("margin in Q1 at 20.5%")
    assert q.value == Decimal("20.5")
