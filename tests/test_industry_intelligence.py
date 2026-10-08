"""DETAILED_RESEARCH_V1 Box 9: industry & competitive intelligence (offline).

Inputs are built with the real upstream builders: Box 4 / Box 5 from the Box 5 test factory,
Box 7 from synthetic filings through Box 1-3 + Box 7, Box 8 from synthetic transcripts through
Box 1-3 + Box 8. Only the Box 1B peer table / classification is synthetic.

  A target + three clean same-industry peers   N incompatible KPI definition
  B unrelated peer -> EXCLUDED                  O bank target + bank peers
  C same sector, different business -> LIMITED  P industrial target + peers
  D aligned FY financial benchmark              Q segment structure mismatch
  E period mismatch                             R exact company alias
  F basis mismatch                              S ambiguous company alias
  G absolute money across currencies            T management market-share claim
  H percentage across currencies                U corroborated claim
  I higher-is-better rank                       V conflicting evidence
  J lower-is-better rank                        W demand evidence
  K no directionality -> no rank                X pricing evidence
  L exact KPI   M proxy KPI                     Y resource limit   Z determinism
"""

import ast
import dataclasses
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError
from test_business_intelligence import BANK_KPI, ar, business, prior_year
from test_financial_analytics import B, M, V, company, fy, make, replace
from test_management_intelligence import BOX4, MANAGEMENT, WELCOME, call, run

from stock_research_ai.application.competitive_evidence import TextUnit, identity, market_claims
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.industry_intelligence import (
    build_industry_intelligence,
    canonical_industry_json,
    verify_industry_provenance,
)
from stock_research_ai.application.industry_structure import market_signals
from stock_research_ai.application.peer_financials import rank, statistics
from stock_research_ai.application.peer_registry import BENCHMARK_METRICS, METRIC_INDEX
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain import industry_intelligence as domain
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.financial_canonical import (
    Currency,
    FinancialPeriod,
    Industry,
    PeriodType,
)
from stock_research_ai.domain.industry_intelligence import (
    ClaimStatus,
    Comparability,
    ContextType,
    Directionality,
    Eligibility,
    IndustryIntelligenceDataset,
    IndustryLimits,
    KPIEquivalence,
    Nature,
    ObservationStatus,
    Resolution,
    SegmentMatch,
    SignalDirection,
    SourceLayer,
)
from stock_research_ai.domain.screener_dataset import (
    ClassificationLevel,
    DetailedScreenerDataset,
    PeerComparison,
    PeerRow,
    PeerStatus,
    ScreenerCompanyProfile,
    ScreenerSourceRef,
)

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
NS = UUID("5a1d0c3e-2f4b-5a6c-8d7e-9f0a1b2c3d4e")
FY26 = date(2026, 3, 31)
OS = ObservationStatus
TARGET = "screener:1489"  # the Box 8 test company key (Widget Co)

IT = (
    ("Broad Sector", "Information Technology"),
    ("Sector", "Information Technology"),
    ("Broad Industry", "IT - Software"),
    ("Industry", "Computers - Software & Consulting"),
)
PRODUCTS = (*IT[:3], ("Industry", "Software Products"))
FMCG = (
    ("Broad Sector", "Fast Moving Consumer Goods"),
    ("Sector", "Fast Moving Consumer Goods"),
    ("Broad Industry", "Food Products"),
    ("Industry", "Packaged Foods"),
)
BANK = (
    ("Broad Sector", "Financial Services"),
    ("Sector", "Financial Services"),
    ("Broad Industry", "Banks"),
    ("Industry", "Private Sector Bank"),
)
NBFC = (*BANK[:2], ("Broad Industry", "Finance"), ("Industry", "Non Banking Financial Company"))


def screener(
    cid: str, name: str, symbol: str, levels, peers: list[tuple[str, str, str]] = ()
) -> DetailedScreenerDataset:
    """A Box 1B dataset: classification and (optionally) the peer table, as printed."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    table = None
    if peers:
        rows = [(cid, name, symbol), *peers]
        table = PeerComparison(
            source_url="https://www.screener.in/api/company/1/peers/",
            retrieved_at=NOW,
            content_sha256=hashlib.sha256(f"peers|{cid}".encode()).hexdigest(),
            size_bytes=1,
            columns=("S.No.", "Name", "ROCE %"),
            peers=tuple(
                PeerRow(
                    rank=f"{i}.",
                    name=n,
                    company_url=f"https://www.screener.in/company/{s}/consolidated/",
                    screener_company_id=c,
                    is_subject_company=c == cid,
                    cells=(),
                )
                for i, (c, n, s) in enumerate(rows, start=1)
            ),
        )
    return DetailedScreenerDataset(
        company_key=f"screener:{cid}",
        source=ScreenerSourceRef(
            page_url=url,
            retrieved_at=NOW,
            retrieval_id=uuid5(NS, f"r|{cid}"),
            raw_artifact_id=uuid5(NS, f"a|{cid}"),
            content_sha256=hashlib.sha256(cid.encode()).hexdigest(),
            size_bytes=1,
        ),
        profile=ScreenerCompanyProfile(
            name=name,
            page_url=url,
            source_company_key=cid,
            warehouse_id=None,
            reporting_basis=ReportingScope.CONSOLIDATED,
            bse_code=None,
            nse_symbol=symbol,
            website_url=None,
            about=None,
            key_points=None,
            price=None,
            classification=tuple(
                ClassificationLevel(level=lv, name=n, url=f"https://www.screener.in/market/{i}/")
                for i, (lv, n) in enumerate(levels)
            ),
        ),
        peer_status=PeerStatus(status="RETRIEVED" if peers else "NOT_REQUESTED"),
        peers=table,
    )


def dec(year: int) -> FinancialPeriod:
    """A December fiscal year (a different fiscal calendar)."""
    return FinancialPeriod(
        period_type=PeriodType.FY,
        label=f"Dec {year - 1}",
        period_start=date(year - 2, 12, 31),
        period_end=date(year - 1, 12, 31),
        duration_months=12,
        fiscal_year=year - 1,
    )


_FINANCE: dict[str, tuple[object, object]] = {}


def finance(key: str, rows, industry: Industry = Industry.GENERAL, fy_end: int = 3):
    """Box 4 + Box 5 for one company (cached; Box 5 is built by the real engine)."""
    if key not in _FINANCE:
        marker = [V(M.CAPEX, fy(2021), 1 + len(key) + sum(map(ord, key)) % 50, rows[0].basis)]
        box4 = make(rows + marker, industry, fy_end).model_copy(update={"company_key": key})
        _FINANCE[key] = (box4, build_financial_analytics(box4, generated_at=NOW))
    return _FINANCE[key]


def layers(key: str, name: str, symbol: str, levels, rows=None, **kw) -> CompanyLayers:
    box4, box5 = finance(key, rows if rows is not None else company(), **kw)
    cid = key.split(":")[1]
    return CompanyLayers(key, screener(cid, name, symbol, levels), box4, box5)  # type: ignore[arg-type]


HIGH = replace(
    company(),
    M.REVENUE_FROM_OPERATIONS,
    {y: 1000 + 100 * (y - 2021) for y in range(2021, 2026)} | {2026: 1600},
)
LOW = replace(
    company(),
    M.REVENUE_FROM_OPERATIONS,
    {y: 1000 + 100 * (y - 2021) for y in range(2021, 2026)} | {2026: 1450},
)
DECEMBER = [
    dataclasses.replace(r, period=dec(r.period.fiscal_year))
    if r.period.period_type == PeriodType.FY
    else r
    for r in company()
]
USD = [dataclasses.replace(r, currency=Currency.USD) for r in company()]

PEER_ROWS = [
    ("2001", "Rival Systems", "RIVAL"),
    ("2002", "Gamma Tech", "GAMMA"),
    ("2003", "Delta Infotech", "DELTA"),
    ("2004", "Omega Foods", "OMEGA"),
    ("2005", "ProductSoft", "PRODSOFT"),
    ("2006", "Mystery Co", "MYSTERY"),
    ("2007", "Calendar Tech", "CALTECH"),
    ("2008", "Solo Systems", "SOLO"),
    ("2009", "Dollar Tech", "DOLLAR"),
]


def it_peers() -> list[CompanyLayers]:
    return [
        layers("screener:2001", "Rival Systems", "RIVAL", IT, HIGH),
        layers("screener:2002", "Gamma Tech", "GAMMA", IT, LOW),
        layers(
            "screener:2003", "Delta Infotech", "DELTA", IT, company() + [V(M.CAPEX, fy(2022), 7)]
        ),
        layers("screener:2004", "Omega Foods", "OMEGA", FMCG),
        layers("screener:2005", "ProductSoft", "PRODSOFT", PRODUCTS),
        # 2006: no Box 1B profile at all -> UNKNOWN
        layers("screener:2007", "Calendar Tech", "CALTECH", IT, DECEMBER, fy_end=12),
        layers("screener:2008", "Solo Systems", "SOLO", IT, company(basis=B.STANDALONE)),
        layers("screener:2009", "Dollar Tech", "DOLLAR", IT, USD),
    ]


def target(box8=None, box7=None) -> CompanyLayers:
    box5 = build_financial_analytics(BOX4, generated_at=NOW)
    return CompanyLayers(
        TARGET, screener("1489", "Widget Co", "WIDGET", IT, PEER_ROWS), BOX4, box5, box7, box8
    )


def build(t: CompanyLayers, peers: list[CompanyLayers], **kw) -> IndustryIntelligenceDataset:
    return build_industry_intelligence(target=t, peers=peers, generated_at=NOW, **kw)


def candidate(data, name: str):
    return next(c for c in data.candidates if c.peer_company_name == name)


def bench(data, code: str):
    return next(b for b in data.financial_benchmarks if b.metric_code == code)


def obs(b, key: str):
    return next(o for o in b.peers if o.company_key == key)


@pytest.fixture(scope="module")
def industrial() -> tuple[IndustryIntelligenceDataset, CompanyLayers, list[CompanyLayers]]:
    t, peers = target(), it_peers()
    return build(t, peers), t, peers


# --- A, B, C: candidates, eligibility, peer set -----------------------------------------------


def test_a_b_c_peer_eligibility(industrial):
    data, _t, _p = industrial
    assert len(data.candidates) == 9  # every peer-table row except the target itself
    for name in ("Rival Systems", "Gamma Tech", "Delta Infotech"):
        c = candidate(data, name)
        assert c.eligibility == Eligibility.INCLUDED
        assert {"SCREENER_PEER_TABLE", "SAME_SECTOR_FAMILY", "SAME_INDUSTRY"} <= set(
            c.selection_reasons
        )
        assert c.table_ref is not None and c.table_ref.displayed_name == name
        assert c.source_classification[-1].name == "Computers - Software & Consulting"
    omega = candidate(data, "Omega Foods")
    assert (omega.eligibility, omega.exclusion_reasons) == (
        Eligibility.EXCLUDED,
        ("DIFFERENT_BROAD_SECTOR",),
    )
    product = candidate(data, "ProductSoft")
    assert product.eligibility == Eligibility.INCLUDED_LIMITED
    assert "SAME_BROAD_INDUSTRY" in product.selection_reasons
    assert product.exclusion_reasons == ("DIFFERENT_INDUSTRY",)
    mystery = candidate(data, "Mystery Co")
    assert (mystery.eligibility, mystery.exclusion_reasons) == (
        Eligibility.UNKNOWN,
        ("PEER_PROFILE_UNAVAILABLE",),
    )
    s = data.peer_set
    assert set(s.excluded_ids) == {omega.peer_candidate_id}
    assert set(s.unknown_ids) == {mystery.peer_candidate_id}
    assert set(s.limited_ids) == {product.peer_candidate_id}
    assert len(s.included_ids) == 6 and not s.beyond_limit_ids  # + ProductSoft (limited)
    assert data.status == "COMPLETE"
    assert any(u.kind == "PEER_UNRESOLVED" and u.text == "Mystery Co" for u in data.unresolved)
    limited = next(a for a in data.comparability if a.peer_company_key == "screener:2005")
    business_model = next(d for d in limited.dimensions if d.dimension.value == "BUSINESS_MODEL")
    assert business_model.status == Comparability.PARTIALLY_COMPARABLE


def test_regulated_model_and_sector_family_exclusions():
    bank_target = CompanyLayers(
        "screener:3783",
        screener(
            "3783",
            "Yes Bank",
            "YESBANK",
            BANK,
            [("4001", "Alpha Bank", "ALPHA"), ("4002", "Beta Finance", "BETA")],
        ),
        BOX4.model_copy(update={"industry": Industry.BANK, "company_key": "screener:3783"}),
    )
    peers = [
        CompanyLayers(
            "screener:4001",
            screener("4001", "Alpha Bank", "ALPHA", BANK),
            BOX4.model_copy(update={"industry": Industry.BANK}),
        ),
        CompanyLayers(
            "screener:4002",
            screener("4002", "Beta Finance", "BETA", NBFC),
            BOX4.model_copy(update={"industry": Industry.NBFC}),
        ),
    ]
    data = build(bank_target, peers)
    assert candidate(data, "Alpha Bank").eligibility == Eligibility.INCLUDED
    beta = candidate(data, "Beta Finance")
    assert (beta.eligibility, beta.exclusion_reasons) == (
        Eligibility.EXCLUDED,
        ("REGULATED_MODEL_DIFFERS",),
    )


def test_y_peer_limit_is_explicit_and_partial():
    t, peers = target(), it_peers()
    data = build(t, peers, limits=IndustryLimits(max_peers=2))
    assert data.status == "PARTIAL"
    assert len(data.peer_set.included_ids) == 2 and len(data.peer_set.beyond_limit_ids) == 5
    assert any(
        w.code == "RESOURCE_LIMIT_REACHED" and "canonical peers" in (w.detail or "")
        for w in data.warnings
    )
    ranks = sorted(
        (c.selection_rank, c.peer_company_name) for c in data.candidates if c.selection_rank
    )
    # registered criteria: INCLUDED first, then the displayed table order
    assert [n for _, n in ranks][:3] == ["Rival Systems", "Gamma Tech", "Delta Infotech"]
    assert ranks[-1][1] == "ProductSoft"  # INCLUDED_LIMITED after every INCLUDED peer
    assert data.statistics.canonical_peers == 2


# --- D-K: financial benchmarks ------------------------------------------------------------------


def test_d_e_f_g_h_benchmark_alignment(industrial):
    data, _t, _p = industrial
    growth = bench(data, "REVENUE_GROWTH_YOY")
    assert (growth.period_type, growth.period_end, growth.reporting_basis.value) == (
        PeriodType.FY,
        FY26,
        "CONSOLIDATED",
    )
    assert growth.target.value == Decimal("7.142857")
    rival = obs(growth, "screener:2001")
    assert (rival.status, rival.value, rival.alignment.value) == (
        OS.COMPARABLE,
        Decimal("14.285714"),
        "ALIGNED",
    )
    december = obs(growth, "screener:2007")  # E: Dec fiscal year
    assert (december.status, december.alignment.value) == (OS.PERIOD_MISMATCH, "NEAR_ALIGNED")
    solo = obs(growth, "screener:2008")  # F: standalone only
    assert solo.status == OS.BASIS_MISMATCH and "STANDALONE" in (solo.detail or "")
    dollar_growth = obs(growth, "screener:2009")  # H: a percentage crosses currencies
    assert dollar_growth.status == OS.COMPARABLE  # Box 5 percentages carry no currency
    revenue = bench(data, "REVENUE_FROM_OPERATIONS")  # G: absolute money does not
    dollar_revenue = obs(revenue, "screener:2009")
    assert dollar_revenue.status == OS.CURRENCY_MISMATCH
    assert all(
        o.value is None or o.status != OS.COMPARABLE or o.currency == Currency.INR
        for o in revenue.peers
    )
    assert revenue.statistics.mean is None  # no mean of absolute sizes


def test_i_higher_is_better_ranking_and_statistics(industrial):
    data, _t, _p = industrial
    growth = bench(data, "REVENUE_GROWTH_YOY")
    comparable = sorted(o.value for o in growth.peers if o.status == OS.COMPARABLE)
    # Rival 14.29, Gamma 3.57, Delta 7.14 (tie), ProductSoft 7.14 (tie), Dollar 7.14 (tie)
    assert growth.statistics.peer_count == len(comparable) == 5
    assert growth.statistics.median == Decimal("7.142857")
    assert (growth.statistics.minimum, growth.statistics.maximum) == (comparable[0], comparable[-1])
    r = growth.ranking
    assert r is not None and r.directionality == Directionality.HIGHER_IS_BETTER
    assert (r.rank, r.of, r.ties, r.percentile) == (2, 6, 3, Decimal("20.00"))
    position = next(p for p in data.positions if p.metric_code == "REVENUE_GROWTH_YOY")
    assert "rank = 2/6" in position.text and "peer median = 7.142857" in position.text


def test_j_lower_is_better_rank_is_inverse():
    r, _ = rank(
        Decimal("1.5"), [Decimal(2), Decimal(3), Decimal("1.2")], Directionality.LOWER_IS_BETTER, 3
    )
    assert r is not None and (r.rank, r.of, r.percentile) == (2, 4, Decimal("66.67"))
    up, _ = rank(
        Decimal("1.5"), [Decimal(2), Decimal(3), Decimal("1.2")], Directionality.HIGHER_IS_BETTER, 3
    )
    assert up is not None and up.rank == 3


def test_k_no_directionality_no_rank_and_minimum_peers(industrial):
    data, _t, _p = industrial
    for code in ("TAX_RATE", "DEBT_TO_EQUITY", "REVENUE_FROM_OPERATIONS"):
        b = bench(data, code)
        assert b.directionality == Directionality.NOT_REGISTERED and b.ranking is None
        assert "not registered" in (b.rank_withheld_reason or "")
    few, reason = rank(Decimal(1), [Decimal(2), Decimal(3)], Directionality.HIGHER_IS_BETTER, 3)
    assert few is None and "2 comparable peers < 3" in (reason or "")
    assert statistics([], mean=True).median is None


def test_p_industrial_target_metric_families(industrial):
    data, _t, _p = industrial
    codes = {b.metric_code for b in data.financial_benchmarks}
    assert {"EBITDA_MARGIN", "ROCE", "OPERATING_MARGIN", "REVENUE_GROWTH_YOY"} <= codes
    blocked = {m.metric_code for m in data.not_applicable_metrics}
    assert {"ROA", "GROSS_NPA_RATIO", "DEPOSITS", "ADVANCES", "COST_TO_INCOME"} <= blocked
    assert not codes & blocked


def test_metric_registry_is_explicit():
    assert len({m.code for m in BENCHMARK_METRICS}) == len(BENCHMARK_METRICS)
    assert METRIC_INDEX["TAX_RATE"].directionality == Directionality.NOT_REGISTERED
    assert METRIC_INDEX["GROSS_NPA_RATIO"].directionality == Directionality.LOWER_IS_BETTER
    assert METRIC_INDEX["EBITDA_MARGIN"].families == "NON_FINANCIAL"
    assert all(
        m.directionality == Directionality.NOT_REGISTERED for m in BENCHMARK_METRICS if m.money
    )  # absolute size is never ranked


# --- O, J, L, M, N: banks and KPI equivalence ---------------------------------------------------


async def test_o_l_m_n_bank_kpis(tmp_path):
    box7, box4_7, _bundle = await business(
        tmp_path, {"/ar.pdf": ar(BANK_KPI)}, industry=Industry.BANK
    )
    t_box4 = box4_7.model_copy(update={"company_key": "screener:3783"})
    t = CompanyLayers(
        "screener:3783",
        screener(
            "3783",
            "Yes Bank",
            "YESBANK",
            BANK,
            [
                ("4001", "Alpha Bank", "ALPHA"),
                ("4002", "Beta Bank", "BETA"),
                ("4003", "Gamma Bank", "GAMMAB"),
                ("4004", "Delta Bank", "DELTAB"),
            ],
        ),
        t_box4,
        None,
        box7.model_copy(update={"company_key": "screener:3783"}),
    )

    def peer(cid: str, name: str, npa: str, *, unit=None, kind=None) -> CompanyLayers:
        values = []
        for k in box7.canonical_kpi_values:
            if k.canonical_kpi == "GROSS_NPA":
                k = k.model_copy(update={"value": Decimal(npa)})
                if unit is not None:
                    k = k.model_copy(update={"unit": unit})
            if k.canonical_kpi == "DEPOSITS" and kind is not None:
                k = k.model_copy(update={"period_kind": kind})
            values.append(k)
        b7 = box7.model_copy(
            update={
                "company_key": f"screener:{cid}",
                "dataset_id": uuid5(NS, cid),
                "canonical_kpi_values": tuple(values),
            }
        )
        return CompanyLayers(
            f"screener:{cid}",
            screener(cid, name, "X" + cid, BANK),
            box4_7.model_copy(update={"company_key": f"screener:{cid}"}),
            None,
            b7,
        )

    from stock_research_ai.domain.business_intelligence import KPIPeriodKind, KPIUnit

    peers = [
        peer("4001", "Alpha Bank", "2.0"),
        peer("4002", "Beta Bank", "1.2"),
        peer("4003", "Gamma Bank", "3.0", kind=KPIPeriodKind.FY),  # M: deposits as a flow
        peer("4004", "Delta Bank", "9", unit=KPIUnit.MONEY),  # N: NPA in money, not %
    ]
    data = build(t, peers)
    assert data.sector_family == "FINANCIAL"
    codes = {b.metric_code for b in data.financial_benchmarks}
    assert not codes & {"EBITDA_MARGIN", "ROCE", "OPERATING_MARGIN", "RECEIVABLE_DAYS"}
    assert {"EBITDA_MARGIN", "ROCE"} <= {m.metric_code for m in data.not_applicable_metrics}
    npa = next(k for k in data.kpi_benchmarks if k.kpi_code == "GROSS_NPA")
    assert npa.directionality == Directionality.LOWER_IS_BETTER
    by_key = {o.company_key: o for o in npa.peers}
    assert by_key["screener:4001"].equivalence == KPIEquivalence.EXACT  # L
    delta = by_key["screener:4004"]
    assert (delta.equivalence, delta.status) == (
        KPIEquivalence.NOT_COMPARABLE,
        OS.DEFINITION_MISMATCH,
    )  # N
    assert npa.statistics.peer_count == 3  # Alpha, Beta, Gamma: exact and comparable
    assert npa.ranking is not None and (npa.ranking.rank, npa.ranking.of) == (2, 4)  # J: 1.6
    deposits = next(k for k in data.kpi_benchmarks if k.kpi_code == "DEPOSITS")
    gamma = next(o for o in deposits.peers if o.company_key == "screener:4003")
    assert gamma.equivalence == KPIEquivalence.PROXY  # M: displayed, not ranked
    assert "PROXY_KPI_DISPLAYED_NOT_RANKED" in deposits.warnings
    assert deposits.statistics.peer_count == 3 and deposits.ranking is None  # no direction
    assert any(u.kind == "KPI_DEFINITION_MISMATCH" for u in data.unresolved)
    problems = verify_industry_provenance(data, t, peers)
    assert problems == [], problems


# --- Q: segments -------------------------------------------------------------------------------


async def test_q_segment_structure_mismatch(tmp_path):
    from test_business_intelligence import segment_table

    old = ["Segment revenue", "Chemicals 1,000 900", "Agri 300 250", "Total 1,300 1,150"]
    head = [
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
    changed, _b4, _b = await business(
        tmp_path / "a", {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(head + new, note)}
    )
    stable, _b4s, _bs = await business(tmp_path / "b", {"/ar26.pdf": ar(segment_table())})
    t = dataclasses.replace(target(), box7=stable)
    peer = dataclasses.replace(
        it_peers()[0], box7=changed.model_copy(update={"company_key": "screener:2001"})
    )
    data = build(t, [peer])
    seg = next(s for s in data.segment_comparisons if s.domain == "SEGMENT")
    assert seg.status == SegmentMatch.STRUCTURE_CHANGED
    assert "agri (REPORTABLE)" in seg.identical_names  # a shared name never overrides it
    clean = build(
        t,
        [
            dataclasses.replace(
                peer, box7=stable.model_copy(update={"company_key": "screener:2001"})
            )
        ],
    )
    same = next(s for s in clean.segment_comparisons if s.domain == "SEGMENT")
    assert same.status == SegmentMatch.IDENTICAL_NAME and "unverified" in same.note


# --- R-X: competitive evidence ------------------------------------------------------------------

TARGET_CALL = call(
    "Q4 FY26 Earnings Conference Call",
    "April 20, 2026",
    [
        WELCOME,
        "Anil Sharma: Thank you everyone, and welcome to the results call for the year.",
        "We continue to win large deals from Rival Systems in Europe.",
        "We expect competition from Tata to remain intense in the mid-market segment.",
        "We will remain the market leader in engineering services.",
        "We expect our market share of 20% in the Indian widget market to hold.",
        "Demand improved sequentially across our markets.",
        "Pricing remains stable for renewals.",
        "Discretionary spending declined in Europe this quarter.",
        "We expect the industry to remain fragmented with many small players.",
        "Moderator: We will now begin the question-and-answer session. The first question is "
        "from the line of Vikram Seth from Alpha Securities.",
        "Vikram Seth: How do you compare with Gamma Tech on pricing in the market?",
        "Anil Sharma: We do not comment on other companies.",
    ],
)
RIVAL_CALL = call(
    "Q4 FY26 Earnings Conference Call",
    "April 22, 2026",
    [
        WELCOME,
        "Anil Sharma: Thank you everyone, and welcome to the results call for the year.",
        "Widget Co will hold a market share of 20% in the Indian widget market.",
        "Demand declined sharply across our markets this quarter.",
    ],
    MANAGEMENT,
)


@pytest.fixture
async def evidence(tmp_path):
    t_box8, t_bundle, _c = await run(tmp_path / "t", {"/t.pdf": TARGET_CALL})
    r_box8, r_bundle, _c2 = await run(tmp_path / "r", {"/r.pdf": RIVAL_CALL})
    peers = it_peers()
    peers[0] = dataclasses.replace(
        peers[0], box8=r_box8.model_copy(update={"company_key": "screener:2001"})
    )
    t = target(box8=t_box8)
    data = build(t, peers)
    return data, t, peers, [t_bundle, r_bundle]


def mention(data, part: str, raw: str | None = None):
    return next(
        m
        for m in data.competitive_mentions
        if part in m.quote and (raw is None or m.raw_mention == raw)
    )


async def test_r_s_competitive_mentions(evidence):
    data, _t, _p, _b = evidence
    win = mention(data, "Rival Systems")
    assert (win.resolution, win.mentioned_company_key, win.context_type) == (
        Resolution.RESOLVED,
        "screener:2001",
        ContextType.CUSTOMER_WIN,
    )
    assert win.source.layer == SourceLayer.BOX8_STATEMENT and win.nature == Nature.COMPANY_ASSERTION
    tata = mention(data, "competition from Tata")
    assert (tata.resolution, tata.mentioned_company_key) == (Resolution.AMBIGUOUS, None)
    assert "TCS" in tata.candidates
    assert any(u.kind == "AMBIGUOUS_COMPANY_ALIAS" for u in data.unresolved)
    asked = mention(data, "compare with Gamma Tech")
    assert (asked.nature, asked.mentioned_company_key, asked.source.layer) == (
        Nature.ANALYST_QUESTION,
        "screener:2002",
        SourceLayer.BOX8_TURN,
    )
    # a resolved competitor mention enriches the peer candidate
    rival = candidate(data, "Rival Systems")
    assert "NAMED_COMPETITOR" in rival.selection_reasons and win.mention_id in rival.mention_ids


async def test_t_u_market_claims(evidence):
    data, _t, _p, _b = evidence
    leader = next(c for c in data.market_claims if "market leader" in c.quote)
    assert (leader.qualitative, leader.stated_share, leader.status) == (
        True,
        None,
        ClaimStatus.UNVERIFIED,
    )  # T: a management claim stays a claim
    assert leader.category == "engineering services"
    ours = next(
        c
        for c in data.market_claims
        if c.source_company_key == TARGET and c.stated_share is not None
    )
    theirs = next(c for c in data.market_claims if c.source_company_key == "screener:2001")
    assert theirs.company_key == TARGET  # Rival's statement is ABOUT Widget Co
    assert (ours.stated_share, ours.category) == (Decimal(20), "indian widget")
    assert ours.status == theirs.status == ClaimStatus.CORROBORATED  # U
    assert theirs.claim_id in ours.corroborating_ids


def unit(company: str, text: str, nature=Nature.COMPANY_ASSERTION, period="Q4 FY26") -> TextUnit:
    return TextUnit(
        company,
        text,
        SourceLayer.BOX8_STATEMENT,
        uuid5(NS, company + text),
        UUID(int=1),
        nature,
        None,
        None,
        None,
        None,
        None,
        date(2026, 4, 20),
        period,
        None,
    )


def test_v_conflicting_share_claims_are_preserved():
    ident = identity({TARGET: ("Widget Co", "WIDGET"), "screener:2001": ("Rival Systems", "RIVAL")})
    claims, conflicts = market_claims(
        [
            unit(TARGET, "We have a market share of 20% in the Indian widget market."),
            unit(
                "screener:2001", "Widget Co has a market share of 30% in the Indian widget market."
            ),
        ],
        ident,
        {TARGET: {TARGET}, "screener:2001": {"screener:2001"}},
    )
    assert {c.status for c in claims} == {ClaimStatus.CONFLICTED}
    assert len(conflicts) == 1 and conflicts[0].resolution == "UNRESOLVED"
    assert {conflicts[0].company_a, conflicts[0].company_b} == {TARGET, "screener:2001"}
    single, _ = market_claims(
        [
            unit(
                TARGET,
                "Widget Co holds a 12% market share in widgets.",
                nature=Nature.COMPANY_DISCLOSURE,
            )
        ],
        ident,
        {TARGET: {TARGET}},
    )
    assert single[0].status == ClaimStatus.SINGLE_SOURCE
    none, _ = market_claims(
        [unit(TARGET, "Our client runs the world's largest supply chain.")],
        ident,
        {TARGET: {TARGET}},
    )
    assert none == []  # 'largest' alone is not a market-position claim


async def test_v_w_x_signals_and_signal_conflicts(evidence):
    data, _t, _p, _b = evidence
    improved = next(s for s in data.demand_signals if "improved sequentially" in s.quote)
    assert (improved.direction, improved.direction_words) == (
        SignalDirection.INCREASING,
        ("improved",),
    )
    declined = next(s for s in data.demand_signals if "spending declined" in s.quote)
    assert declined.direction == SignalDirection.DECREASING
    stable = next(s for s in data.pricing_signals if "remains stable" in s.quote)
    assert stable.direction == SignalDirection.STABLE  # X
    assert not [s for s in data.demand_signals if s.nature == Nature.ANALYST_QUESTION]
    conflict = next(c for c in data.conflicts if c.topic == "DEMAND")  # V
    assert {conflict.company_a, conflict.company_b} == {TARGET, "screener:2001"}
    assert "Q4 FY26" in conflict.claim_a and conflict.resolution == "UNRESOLVED"


def test_w_signal_direction_needs_explicit_words():
    demand, pricing = market_signals(
        [
            unit(TARGET, "Demand remains strong in North America."),
            unit(TARGET, "Demand was a topic in many client conversations."),
            unit(TARGET, "It is not about asking a higher price."),
            unit(TARGET, "Budgets are flat to slightly higher this year."),
            unit(TARGET, "What is the demand outlook?", nature=Nature.ANALYST_QUESTION),
        ]
    )
    assert [s.direction for s in demand] == [
        SignalDirection.INCREASING,
        SignalDirection.UNKNOWN,
        SignalDirection.MIXED,
    ]
    assert [s.direction for s in pricing] == [SignalDirection.UNKNOWN]  # negated


async def test_structure_and_themes(evidence):
    data, _t, _p, _b = evidence
    fragmented = next(o for o in data.structure_observations if o.topic == "FRAGMENTATION")
    assert fragmented.nature == Nature.COMPANY_ASSERTION and fragmented.company_key == TARGET
    assert fragmented.event_date == date(2026, 4, 20)
    demand = next(t for t in data.themes if t.topic == "DEMAND")
    assert set(demand.companies) == {TARGET, "screener:2001"}
    assert demand.first_date == date(2026, 4, 20) and demand.last_date == date(2026, 4, 22)


async def test_provenance_verifier_resolves_and_detects_tampering(evidence):
    data, t, peers, bundles = evidence
    assert verify_industry_provenance(data, t, peers, bundles) == []
    win = mention(data, "Rival Systems")
    forged = data.model_copy(
        update={
            "competitive_mentions": tuple(
                m.model_copy(update={"quote": "Rival Systems is exiting the market."})
                if m is win
                else m
                for m in data.competitive_mentions
            )
        }
    )
    assert any("text differs" in p for p in verify_industry_provenance(forged, t, peers, bundles))
    growth = bench(data, "REVENUE_GROWTH_YOY")
    rival = obs(growth, "screener:2001")
    changed = growth.model_copy(
        update={
            "peers": tuple(
                o.model_copy(update={"value": Decimal(99)}) if o is rival else o
                for o in growth.peers
            )
        }
    )
    tampered = data.model_copy(
        update={
            "financial_benchmarks": tuple(
                changed if b is growth else b for b in data.financial_benchmarks
            )
        }
    )
    assert any("Box 5 metric" in p for p in verify_industry_provenance(tampered, t, peers, bundles))
    c = candidate(data, "Gamma Tech")
    moved = data.model_copy(
        update={
            "candidates": tuple(
                x.model_copy(
                    update={"table_ref": x.table_ref.model_copy(update={"displayed_name": "X"})}
                )
                if x is c
                else x
                for x in data.candidates
            )
        }
    )
    assert any("peer-table row differs" in p for p in verify_industry_provenance(moved, t, peers))


# --- Z: determinism, contracts, guards ----------------------------------------------------------


async def test_z_identical_inputs_give_identical_json(evidence):
    data, t, peers, _b = evidence
    again = build(t, list(reversed(peers)))  # input order does not matter
    assert canonical_industry_json(data) == canonical_industry_json(again)
    later = build_industry_intelligence(
        target=t, peers=peers, generated_at=datetime(2027, 1, 1, tzinfo=UTC)
    )
    assert later.dataset_id == data.dataset_id
    payload = json.loads(canonical_industry_json(data))
    assert payload["schema_version"] == "INDUSTRY_INTELLIGENCE_DATASET_V1"
    assert payload["registry_version"] == "peer-registry/1"
    assert {s["layer"] for s in payload["sources"]} >= {"BOX1B", "BOX4", "BOX5", "BOX8"}
    assert all(UUID(c["peer_candidate_id"]).version == 5 for c in payload["candidates"])


def test_contracts_are_strict_and_immutable(industrial):
    data, _t, _p = industrial
    c = data.candidates[0]
    with pytest.raises(ValidationError):
        c.eligibility = Eligibility.EXCLUDED  # type: ignore[misc]
    with pytest.raises(ValidationError):
        domain.PeerCandidate.model_validate({**c.model_dump(), "score": 1})
    with pytest.raises(ValidationError, match="exclusion reasons"):
        domain.PeerCandidate.model_validate(
            {**c.model_dump(), "eligibility": "EXCLUDED", "exclusion_reasons": ()}
        )
    raw = data.model_dump()
    raw["peer_set"]["included_ids"] = raw["peer_set"]["included_ids"][1:]
    with pytest.raises(ValidationError, match="partition"):
        IndustryIntelligenceDataset.model_validate(raw)
    raw = data.model_dump()
    raw["positions"][0]["benchmark_id"] = UUID(int=5)
    with pytest.raises(ValidationError, match="unknown benchmark"):
        IndustryIntelligenceDataset.model_validate(raw)


def test_no_score_or_subjective_label_fields(industrial):
    data, _t, _p = industrial
    tokens = {
        t
        for m in vars(domain).values()
        if isinstance(m, type) and hasattr(m, "model_fields")
        for f in m.model_fields
        for t in f.split("_")
    }  # type: ignore[attr-defined]
    assert not tokens & {"score", "moat", "grade", "rating", "recommendation", "price"}
    text = canonical_industry_json(data).lower()
    for word in ("moat", "buy", "sell", "strong company", "weak company", "best company"):
        assert f'"{word}' not in text


def _box9_modules() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    app = root / "application"
    return [
        root / "domain" / "industry_intelligence.py",
        *(
            app / f"{n}.py"
            for n in (
                "peer_registry",
                "peer_selection",
                "peer_comparability",
                "peer_financials",
                "peer_kpis",
                "competitive_evidence",
                "industry_structure",
                "industry_intelligence",
            )
        ),
    ]


def test_box9_modules_stay_deterministic_and_offline():
    banned = {"httpx", "requests", "anthropic", "openai", "random", "socket", "urllib", "aiohttp"}
    banned_parts = (
        "adapters",
        "llm",
        "aicredits",
        "ai_credits",
        "browser",
        "market_",
        "valuation",
        "forecast",
        "report",
        "summary",
        "analyst_model",
        "challenge",
    )
    modules = _box9_modules()
    assert all(p.exists() for p in modules) and len(modules) == 9
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
