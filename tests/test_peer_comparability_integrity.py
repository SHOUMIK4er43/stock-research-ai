"""Box 9 peer comparability integrity: a peer metric enters a benchmark only when the business
model, the exact metric definition, the reporting basis, the period, and (for money) the
currency all match; anything else fails closed and never alters a statistic.

  A GENERAL + GENERAL                    -> eligible        M flow vs point-in-time   -> no
  B BANK + BANK                          -> eligible        N CONSOLIDATED vs STANDALONE -> no
  C NBFC + NBFC (lender metrics)         -> eligible        O money across currencies -> no
  D NON_BANK_FINANCIAL + BANK            -> excluded        P ratio across currencies -> yes
  E UNKNOWN target    F UNKNOWN peer     -> never eligible  Q excluded peer never in a median
  G different sector family              -> excluded        R ... nor in a rank
  H same family, other formula variant   -> DEFINITION_MISMATCH  S zero eligible -> UNAVAILABLE
  I REVENUE vs REVENUE_FROM_OPERATIONS   -> not identical   T HUDCO exact regression
  J COMMON / accounting / total equity   -> not identical   U INFY   V YESBANK
  K same ratio, definition, aligned FY   -> comparable      W Box 11 never reads Box 9
  L FY vs TTM                            -> not comparable  X Box 14 shows counts honestly
"""

import ast
import asyncio
import inspect
import tempfile
from dataclasses import replace as dc_replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_business_classification_integrity import HUDCO, HUDCO_NAMES
from test_financial_analytics import B, M, V, company, fy, make, pit
from test_financial_analytics import replace as rows_replace
from test_industry_intelligence import BANK, FMCG, IT, NBFC, NOW, PRODUCTS, screener
from test_screener_dataset import page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import fundamental_valuation as box11
from stock_research_ai.application import peer_financials as pf
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
from stock_research_ai.application.industry_intelligence import (
    build_industry_intelligence,
    verify_industry_provenance,
)
from stock_research_ai.application.peer_registry import BENCHMARK_METRICS, METRIC_INDEX
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.application.report_sections import peer_eligibility_notes
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    Currency,
    Industry,
    PeriodType,
)
from stock_research_ai.domain.fundamental_valuation import (
    MultipleInput,
    MultipleSource,
    ValuationRequest,
)
from stock_research_ai.domain.industry_intelligence import (
    Eligibility,
    IndustryIntelligenceDataset,
    ObservationStatus,
)

OS = ObservationStatus
E = Eligibility
FY26 = date(2026, 3, 31)
FIXTURES = Path(__file__).parent / "fixtures" / "screener"
FINANCE = (*BANK[:2], ("Broad Industry", "Finance"), ("Industry", "Financial Institution"))
LENDER_METRICS = ("BORROWINGS_TO_EQUITY", "COST_OF_BORROWINGS", "PAT_TO_TOTAL_INCOME",
                  "BORROWINGS_GROWTH_YOY")
NEUTRAL = tuple(m.code for m in BENCHMARK_METRICS if m.neutral)


def lender_rows(**kw) -> list[V]:
    """A lender: the industrial fixture plus total income and borrowings every year."""
    rows = company(**kw)
    basis = kw.get("basis", B.CONSOLIDATED)
    return rows + [V(M.TOTAL_INCOME, fy(y), 1100 + 100 * (y - 2021), basis=basis)
                   for y in range(2021, 2027)] + [
        V(M.TOTAL_BORROWINGS, pit(y), 4000 + 400 * (y - 2021), basis=basis)
        for y in range(2020, 2027)]


def layers(key: str, levels, rows: list[V] | None = None, industry=Industry.GENERAL,
           cls: BusinessClass | None = None, peers: list[tuple[str, str, str]] = (),
           box4=None) -> CompanyLayers:
    """One company: Box 1B profile (+ peer table), Box 4 (rows or an existing dataset, rekeyed
    and reclassified) and Box 5 built by the real engine."""
    cid = key.split(":")[1]
    data = box4 if box4 is not None else make(rows if rows is not None else company(), industry)
    data = data.model_copy(update={"company_key": key, "industry": industry,
                                   "business_class": cls or (
                                       BusinessClass.FINANCIAL if industry in (
                                           Industry.BANK, Industry.NBFC, Industry.INSURANCE)
                                       else BusinessClass.NON_FINANCIAL
                                       if industry == Industry.GENERAL
                                       else BusinessClass.UNKNOWN)})
    return CompanyLayers(key, screener(cid, f"Co {cid}", f"S{cid}", levels, list(peers)), data,
                         build_financial_analytics(data, generated_at=NOW))


def table(*cids: str) -> list[tuple[str, str, str]]:
    return [(c, f"Co {c}", f"S{c}") for c in cids]


def build(target: CompanyLayers, peers: list[CompanyLayers]) -> IndustryIntelligenceDataset:
    data = build_industry_intelligence(target=target, peers=peers, generated_at=NOW)
    assert verify_industry_provenance(data, target, peers) == []
    return data


def cand(data, cid: str):
    return next(c for c in data.candidates if c.peer_company_key == f"screener:{cid}")


def bench(data, code: str):
    return next(b for b in data.financial_benchmarks if b.metric_code == code)


def obs(b, cid: str):
    return next(o for o in b.peers if o.company_key == f"screener:{cid}")


# --- A, K: same model, same definition, aligned -------------------------------------------------


def test_a_k_general_peers_with_identical_definitions_are_comparable():
    t = layers("screener:100", IT, peers=table("101"))
    data = build(t, [layers("screener:101", IT)])
    assert cand(data, "101").eligibility == E.INCLUDED
    assert cand(data, "101").business_model == "NON_FINANCIAL"
    for code in ("EBITDA_MARGIN", "ROE", "REVENUE_GROWTH_YOY", "PAT"):
        b = bench(data, code)
        o = obs(b, "101")
        assert (o.status, o.alignment.value, o.alignment_days) == (OS.COMPARABLE, "ALIGNED", 0)
        assert o.definition == b.definition and b.definition
        assert (b.availability, b.statistics.peer_count, b.excluded_peers) == ("AVAILABLE", 1, 0)


# --- B, C, D: regulated business models -----------------------------------------------------------


def test_b_bank_with_bank_is_eligible():
    t = layers("screener:200", BANK, industry=Industry.BANK, peers=table("201"))
    data = build(t, [layers("screener:201", BANK, industry=Industry.BANK)])
    assert cand(data, "201").eligibility == E.INCLUDED
    assert obs(bench(data, "ROE"), "201").status == OS.COMPARABLE
    # a bank gets no lender benchmark: registered for NON_BANK_FINANCIAL only
    blocked = {m.metric_code for m in data.not_applicable_metrics}
    assert set(LENDER_METRICS) <= blocked
    assert not {b.metric_code for b in data.financial_benchmarks} & set(LENDER_METRICS)


def test_c_nbfc_with_nbfc_compares_lender_metrics():
    t = layers("screener:300", NBFC, lender_rows(), Industry.NBFC, peers=table("301"))
    data = build(t, [layers("screener:301", NBFC, lender_rows(), Industry.NBFC)])
    assert cand(data, "301").eligibility == E.INCLUDED
    for code in ("BORROWINGS_TO_EQUITY", "PAT_TO_TOTAL_INCOME", "ROA", "ROE"):
        assert obs(bench(data, code), "301").status == OS.COMPARABLE, code


def test_d_non_bank_financial_and_bank_are_not_comparable():
    t = layers("screener:400", NBFC, lender_rows(), Industry.NBFC, peers=table("401", "402"))
    bank = layers("screener:401", BANK, lender_rows(), Industry.BANK)
    unresolved_bank_target = layers("screener:410", BANK, industry=Industry.BANK,
                                    peers=table("402"))
    unresolved = layers("screener:402", FINANCE, lender_rows(), Industry.UNKNOWN,
                        BusinessClass.FINANCIAL)
    data = build(t, [bank, unresolved])
    assert (cand(data, "401").eligibility, cand(data, "401").exclusion_reasons) == (
        E.EXCLUDED, ("REGULATED_MODEL_DIFFERS",))
    for b in data.financial_benchmarks:  # an excluded bank is never even observed
        assert "screener:401" not in {o.company_key for o in b.peers}
    # a bank compares only with a proven bank: an unresolved financial subtype is UNKNOWN
    other = build(unresolved_bank_target, [unresolved])
    assert (cand(other, "402").eligibility, cand(other, "402").exclusion_reasons) == (
        E.UNKNOWN, ("FINANCIAL_SUBTYPE_UNRESOLVED",))
    assert all(b.statistics.peer_count == 0 for b in other.financial_benchmarks)


# --- E, F, G: unresolved and incompatible classifications ---------------------------------------


def test_e_unknown_target_is_never_silently_comparable():
    t = layers("screener:500", IT, industry=Industry.UNKNOWN, cls=BusinessClass.UNKNOWN,
               peers=table("501"))
    data = build(t, [layers("screener:501", IT)])
    assert data.sector_family == "UNKNOWN" and data.business_model == "UNKNOWN"
    assert (cand(data, "501").eligibility, cand(data, "501").exclusion_reasons) == (
        E.UNKNOWN, ("SECTOR_FAMILY_UNRESOLVED",))
    assert all(b.statistics.peer_count == 0 for b in data.financial_benchmarks)


def test_f_unknown_peer_is_excluded_as_unknown():
    t = layers("screener:600", IT, peers=table("601", "602"))
    unknown = layers("screener:601", IT, industry=Industry.UNKNOWN, cls=BusinessClass.UNKNOWN)
    data = build(t, [unknown])  # 602: no Box 1B profile at all
    assert cand(data, "601").eligibility == E.UNKNOWN
    assert cand(data, "602").exclusion_reasons == ("PEER_PROFILE_UNAVAILABLE",)
    assert all(b.statistics.peer_count == 0 for b in data.financial_benchmarks)


def test_g_incompatible_sector_family_is_excluded():
    t = layers("screener:700", IT, peers=table("701", "702"))
    lender_labelled_it = layers("screener:701", IT, lender_rows(), Industry.NBFC)
    foods = layers("screener:702", FMCG)
    data = build(t, [lender_labelled_it, foods])
    assert cand(data, "701").exclusion_reasons == ("SECTOR_FAMILY_DIFFERS",)
    assert cand(data, "702").exclusion_reasons == ("DIFFERENT_BROAD_SECTOR",)
    assert {c.eligibility for c in data.candidates} == {E.EXCLUDED}


# --- H, I, J: exact definitions ------------------------------------------------------------------


def _without(rows: list[V], *metrics) -> list[V]:
    return [r for r in rows if r.metric not in metrics]


def test_h_same_family_other_formula_variant_is_a_definition_mismatch():
    implied = _without(company(), M.TOTAL_TAX, M.CURRENT_TAX, M.DEFERRED_TAX)
    t = layers("screener:800", IT, peers=table("801", "802"))
    data = build(t, [layers("screener:801", IT, implied), layers("screener:802", IT)])
    tax = bench(data, "TAX_RATE")
    assert "TAX_RATE/2/TOTAL_TAX" in (tax.definition or "")
    mismatch = obs(tax, "801")
    assert mismatch.status == OS.DEFINITION_MISMATCH and "IMPLIED" in (mismatch.detail or "")
    assert obs(tax, "802").status == OS.COMPARABLE
    assert (tax.statistics.peer_count, tax.excluded_peers, tax.exclusion_counts) == (
        1, 1, {"DEFINITION_MISMATCH": 1})


def test_i_revenue_and_revenue_from_operations_never_substitute():
    provider = [dc_replace(r, metric=M.REVENUE) if r.metric == M.REVENUE_FROM_OPERATIONS
                else r for r in company()]
    t = layers("screener:900", IT, peers=table("901"))
    data = build(t, [layers("screener:901", IT, provider)])
    growth = obs(bench(data, "REVENUE_GROWTH_YOY"), "901")
    assert growth.status == OS.DEFINITION_MISMATCH and "top line" in (growth.detail or "")
    absolute = obs(bench(data, "REVENUE_FROM_OPERATIONS"), "901")
    assert absolute.status == OS.MISSING  # a provider 'Revenue' is never filled in
    # TOTAL_INCOME is its own canonical input: a definition naming it is never identical
    assert pf.definition_of("REVENUE_GROWTH_YOY/2", (), ("REVENUE",)) != pf.definition_of(
        "REVENUE_GROWTH_YOY/2", (), ("TOTAL_INCOME",))


def test_j_common_accounting_and_total_equity_never_substitute():
    assert not {"COMMON_EQUITY", "SHAREHOLDERS_EQUITY", "TOTAL_EQUITY"} & set(METRIC_INDEX)
    total_only = _without(company(), M.RESERVES, M.EQUITY_SHARE_CAPITAL) + [
        V(M.TOTAL_EQUITY, pit(y), v.value) for v in company()
        if v.metric == M.RESERVES for y in [v.period.fiscal_year]]
    owners = company() + [V(M.MINORITY_INTEREST, pit(y), 5) for y in range(2020, 2027)] + [
        V(M.PAT_ATTRIBUTABLE_TO_OWNERS, fy(y), v.value) for v in company()
        if v.metric == M.PAT for y in [v.period.fiscal_year]]
    t = layers("screener:1000", IT, peers=table("1001", "1002", "1003"))
    data = build(t, [layers("screener:1001", IT, total_only),
                     layers("screener:1002", IT, owners), layers("screener:1003", IT)])
    roe = bench(data, "ROE")
    assert "SHAREHOLDERS_EQUITY/2/CAPITAL_PLUS_RESERVES" in (roe.definition or "")
    assert "COMMON_EQUITY" not in (roe.definition or "")
    assert obs(roe, "1001").status == OS.DEFINITION_MISMATCH  # total equity (may hold NCI)
    assert "TOTAL_EQUITY" in (obs(roe, "1001").definition or "")
    assert obs(roe, "1002").status == OS.DEFINITION_MISMATCH  # owners' PAT numerator
    assert "PAT_ATTRIBUTABLE_TO_OWNERS" in (obs(roe, "1002").definition or "")
    assert obs(roe, "1003").status == OS.COMPARABLE and roe.statistics.peer_count == 1


# --- L, M: period type gates (unit level, the same function Box 9 uses) ---------------------------


def _value(period_type: PeriodType, end: date, definition: str = "D") -> pf.Value:
    from stock_research_ai.domain.financial_canonical import ReportingBasis
    from stock_research_ai.domain.industry_intelligence import SourceLayer
    return pf.Value(SourceLayer.BOX5_METRIC, uuid4(), uuid4(), "screener:2", "ROE",
                    Decimal(10), True, "CALCULATED", "PERCENT", period_type, end,
                    ReportingBasis.CONSOLIDATED, None, (), None, None, definition)


@pytest.mark.parametrize("anchor_type,peer_type", [
    (PeriodType.FY, PeriodType.TTM),  # L
    (PeriodType.FY, PeriodType.QUARTER),
    (PeriodType.POINT_IN_TIME, PeriodType.FY),  # M: balance vs flow
    (PeriodType.FY, PeriodType.POINT_IN_TIME),
])
def test_l_m_period_type_mismatch_is_never_comparable(monkeypatch, anchor_type, peer_type):
    anchor = _value(anchor_type, FY26)
    monkeypatch.setattr(pf, "values_of", lambda company, metric: [_value(peer_type, FY26)])
    o = pf.peer_observation(anchor, METRIC_INDEX["ROE"], CompanyLayers("screener:2"))
    assert o.status == OS.PERIOD_MISMATCH and o.value is None


def test_l_fiscal_calendar_gap_is_exposed_and_never_pooled(monkeypatch):
    anchor = _value(PeriodType.FY, FY26)
    monkeypatch.setattr(pf, "values_of",
                        lambda company, metric: [_value(PeriodType.FY, date(2025, 12, 31))])
    o = pf.peer_observation(anchor, METRIC_INDEX["ROE"], CompanyLayers("screener:2"))
    assert (o.status, o.alignment.value, o.alignment_days) == (OS.PERIOD_MISMATCH,
                                                               "NEAR_ALIGNED", 90)


# --- N, O, P: basis and currency ------------------------------------------------------------------


def test_n_o_p_basis_and_currency():
    usd = [dc_replace(r, currency=Currency.USD) for r in company()]
    t = layers("screener:1100", IT, peers=table("1101", "1102"))
    data = build(t, [layers("screener:1101", IT, company(basis=B.STANDALONE)),
                     layers("screener:1102", IT, usd)])
    for code in ("ROE", "PAT"):
        assert obs(bench(data, code), "1101").status == OS.BASIS_MISMATCH  # N
    assert obs(bench(data, "PAT"), "1102").status == OS.CURRENCY_MISMATCH  # O
    assert obs(bench(data, "ROE"), "1102").status == OS.COMPARABLE  # P: a ratio crosses


def test_a_box4_value_failing_extraction_integrity_never_enters_a_benchmark(monkeypatch):
    t = layers("screener:1150", IT, peers=table("1151"))
    peer = layers("screener:1151", IT)
    assert peer.box4 is not None
    rejected = {id(o) for o in peer.box4.observations if o.metric == M.PAT}
    monkeypatch.setattr(pf, "extraction_integrity_issue",
                        lambda o: "NOTE_REFERENCE" if id(o) in rejected else None)
    pat = bench(build(t, [peer]), "PAT")
    o = obs(pat, "1151")
    assert o.status == OS.NOT_CALCULATED and "EXTRACTION_INTEGRITY_REJECTED" in (o.detail or "")
    assert (pat.statistics.peer_count, pat.availability) == (0, "UNAVAILABLE")


# --- Q, R, S: aggregates ------------------------------------------------------------------------


def _stats(data, code):
    b = bench(data, code)
    r = b.ranking
    return (b.statistics, None if r is None else (r.rank, r.of, r.percentile, r.ties))


def test_q_r_excluded_and_ineligible_peers_never_alter_median_or_rank():
    high = rows_replace(lender_rows(), M.PAT, {y: 900 for y in range(2021, 2027)})
    peers = [layers(f"screener:{c}", NBFC, lender_rows(), Industry.NBFC) for c in
             ("1201", "1202", "1203")]
    clean_t = layers("screener:1200", NBFC, lender_rows(), Industry.NBFC,
                     peers=table("1201", "1202", "1203"))
    noisy_t = layers("screener:1200", NBFC, lender_rows(), Industry.NBFC,
                     peers=table("1201", "1202", "1203", "1204", "1205", "1206"))
    noise = [
        layers("screener:1204", BANK, high, Industry.BANK),  # EXCLUDED
        layers("screener:1205", FINANCE, high, Industry.UNKNOWN, BusinessClass.FINANCIAL),
        layers("screener:1206", FMCG, high),  # different broad sector
    ]
    clean, noisy = build(clean_t, peers), build(noisy_t, peers + noise)
    assert cand(noisy, "1205").eligibility == E.INCLUDED_LIMITED  # neutral metrics only
    for code in ("ROA", "BORROWINGS_TO_EQUITY", "PAT_TO_TOTAL_INCOME"):
        assert _stats(clean, code) == _stats(noisy, code), code  # Q + R
        assert obs(bench(noisy, code), "1205").status == OS.CLASSIFICATION_MISMATCH
        assert bench(noisy, code).statistics.peer_count == 3
    # a neutral metric admits the limited peer (and says so); the excluded ones never
    roe = bench(noisy, "ROE")
    assert obs(roe, "1205").status == OS.COMPARABLE and roe.statistics.peer_count == 4
    assert not {"screener:1204", "screener:1206"} & {o.company_key for o in roe.peers}


def test_s_zero_eligible_peers_make_the_benchmark_unavailable():
    t = layers("screener:1300", FINANCE, lender_rows(), Industry.UNKNOWN,
               BusinessClass.FINANCIAL, peers=table("1301"))
    data = build(t, [layers("screener:1301", FINANCE, lender_rows(), Industry.NBFC)])
    b = bench(data, "BORROWINGS_TO_EQUITY")
    assert (b.availability, b.statistics.peer_count, b.statistics.median, b.ranking) == (
        "UNAVAILABLE", 0, None, None)
    assert b.exclusion_counts == {"CLASSIFICATION_MISMATCH": 1}
    assert "no eligible peer" in (b.unavailable_reason or "")
    assert not [p for p in data.positions if p.metric_code == "BORROWINGS_TO_EQUITY"]
    # the contract refuses a benchmark whose counts / availability disagree with its peers
    with pytest.raises(ValidationError):
        b.model_validate({**b.model_dump(), "availability": "AVAILABLE"})
    with pytest.raises(ValidationError):
        b.model_validate({**b.model_dump(), "excluded_peers": 0})


# --- T: HUDCO -------------------------------------------------------------------------------------


def _pages(cases) -> list:
    async def acquire():
        out = []
        for fixtures, case in cases:
            with tempfile.TemporaryDirectory() as t:
                out.append(await page(Path(t), fixtures, case))
        return out
    return asyncio.run(acquire())


def _box4(cases):
    screeners = tuple(build_screener_dataset(e, b, None) for e, b in _pages(cases))
    return build_canonical_dataset(company_key=screeners[0].company_key,
                                   company_name=screeners[0].profile.name, screeners=screeners,
                                   bundle=None, catalogue=None, generated_at=NOW)


@pytest.fixture(scope="module")
def hudco():
    real = _box4(HUDCO)
    assert (real.industry, real.classification) == (Industry.UNKNOWN, BusinessClass.FINANCIAL)
    levels = tuple(zip(("Broad Sector", "Sector", "Broad Industry", "Industry"), HUDCO_NAMES,
                        strict=True))
    t = layers("screener:1274074", levels, industry=Industry.UNKNOWN,
               cls=BusinessClass.FINANCIAL, box4=real,
               peers=table("2522", "2809", "8001", "8002", "8003"))
    peers = [
        layers("screener:2522", levels, industry=Industry.NBFC, box4=real),  # a resolved NBFC
        layers("screener:2809", levels, industry=Industry.UNKNOWN,  # subtype unresolved
               cls=BusinessClass.FINANCIAL, box4=real),
        layers("screener:8001", BANK, industry=Industry.BANK, box4=real),  # a bank
        layers("screener:8002", levels, company()),  # an industrial company (GENERAL)
        layers("screener:8003", levels, industry=Industry.UNKNOWN, cls=BusinessClass.UNKNOWN,
               box4=real),  # unresolved business class
    ]
    return build(t, peers), t, peers


def test_t_hudco_exact_regression(hudco):
    data, _t, _peers = hudco
    assert (data.business_model, data.sector_family) == (
        "NON_BANK_FINANCIAL/SUBTYPE_UNRESOLVED", "FINANCIAL")
    ledger = {c.peer_company_key.split(":")[1]: (c.eligibility, c.exclusion_reasons)
              for c in data.candidates}
    assert ledger == {
        "2522": (E.INCLUDED_LIMITED, ("FINANCIAL_SUBTYPE_UNRESOLVED",)),
        "2809": (E.INCLUDED_LIMITED, ("FINANCIAL_SUBTYPE_UNRESOLVED",)),
        "8001": (E.UNKNOWN, ("FINANCIAL_SUBTYPE_UNRESOLVED",)),  # bank: never assumed
        "8002": (E.EXCLUDED, ("SECTOR_FAMILY_DIFFERS",)),  # no industrial peer
        "8003": (E.UNKNOWN, ("SECTOR_FAMILY_UNRESOLVED",)),  # UNKNOWN never qualifies
    }
    by = {b.metric_code: b for b in data.financial_benchmarks}
    # lender metrics: an unresolved subtype proves no lender definition -> unavailable
    for code in ("ROA", "BORROWINGS_TO_EQUITY", "BORROWINGS_GROWTH_YOY"):
        assert by[code].availability == "UNAVAILABLE", code
        assert by[code].exclusion_counts == {"CLASSIFICATION_MISMATCH": 2}, code
    # business-model-neutral metrics: both financial non-bank peers, identical definitions
    roe = by["ROE"]
    assert roe.target.value == Decimal("20.198278")
    assert roe.definition == ("ROE/2 <- SHAREHOLDERS_EQUITY/2/CAPITAL_PLUS_RESERVES | "
                              "EQUITY_SHARE_CAPITAL,PAT,RESERVES")
    assert (roe.statistics.peer_count, roe.statistics.median) == (2, Decimal("20.198278"))
    growth = by["REVENUE_GROWTH_YOY"]
    assert growth.definition == "REVENUE_GROWTH_YOY/2 <- - | REVENUE"  # the provider top line
    assert "REVENUE_FROM_OPERATIONS" not in by and "TOTAL_INCOME" not in (growth.definition or "")
    assert {m.metric_code for m in data.not_applicable_metrics} >= {
        "EBITDA_MARGIN", "ROCE", "REVENUE_FROM_OPERATIONS", "DEBT_TO_EQUITY"}
    assert not data.kpi_benchmarks  # no bank-only KPI (CASA, ...) is benchmarked
    for b in data.financial_benchmarks:  # bank / industrial / UNKNOWN peers never observed
        assert {o.company_key for o in b.peers} <= {"screener:2522", "screener:2809"}


# --- U, V: INFY and YESBANK -----------------------------------------------------------------------


def test_u_infy_it_peer_comparisons_stay_intact():
    real = _box4([(FIXTURES, INFY)])
    t = layers("screener:1500", IT, industry=real.industry, box4=real, peers=table("1501", "1502"))
    data = build(t, [layers("screener:1501", IT, industry=real.industry, box4=real),
                     layers("screener:1502", PRODUCTS, industry=real.industry, box4=real)])
    assert cand(data, "1501").eligibility == E.INCLUDED
    assert cand(data, "1502").exclusion_reasons == ("DIFFERENT_INDUSTRY",)
    for code in ("EBITDA_MARGIN", "ROCE", "OPERATING_MARGIN"):  # industrial metrics: IT peer
        assert obs(bench(data, code), "1501").status == OS.COMPARABLE
        # a different Screener Industry enters business-model-neutral metrics only
        assert obs(bench(data, code), "1502").status == OS.CLASSIFICATION_MISMATCH
    assert obs(bench(data, "ROE"), "1502").status == OS.COMPARABLE
    assert bench(data, "ROE").target.value == Decimal("34.732411")
    blocked = {m.metric_code for m in data.not_applicable_metrics}
    assert {"ROA", "DEPOSITS", "GROSS_NPA_RATIO", *LENDER_METRICS} <= blocked


def test_v_yesbank_bank_peers_stay_valid_and_lender_metrics_never_leak():
    real = _box4([(FIXTURES, YESBANK)])
    assert real.industry == Industry.BANK
    t = layers("screener:1600", BANK, industry=Industry.BANK, box4=real,
               peers=table("1601", "1602"))
    data = build(t, [layers("screener:1601", BANK, industry=Industry.BANK, box4=real),
                     layers("screener:1602", NBFC, industry=Industry.NBFC, box4=real)])
    assert cand(data, "1601").eligibility == E.INCLUDED
    assert cand(data, "1602").exclusion_reasons == ("REGULATED_MODEL_DIFFERS",)
    assert bench(data, "ROE").target.value == Decimal("7.095595")
    assert all(o.status == OS.COMPARABLE for b in data.financial_benchmarks for o in b.peers
               if b.availability == "AVAILABLE")
    codes = {b.metric_code for b in data.financial_benchmarks}
    assert not codes & {*LENDER_METRICS, "EBITDA_MARGIN", "ROCE"}


# --- W: Box 11 ------------------------------------------------------------------------------------


def test_w_box11_cannot_consume_a_box9_benchmark_as_a_valuation_multiple(hudco):
    tree = ast.parse(Path(box11.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in imported if "industry" in m or "peer" in m}
    assert "box9" not in inspect.signature(box11.build_valuation).parameters
    assert not {"PE", "PB", "EV_EBITDA", "EV_EBIT", "P_E", "P_B"} & set(METRIC_INDEX)
    benchmark = hudco[0].financial_benchmarks[0]
    with pytest.raises(ValidationError):  # a benchmark is not a structured multiple
        MultipleInput.model_validate({**benchmark.model_dump(), "method": "PB"})
    with pytest.raises(ValidationError):  # nor is a whole Box 9 dataset a request field
        ValuationRequest.model_validate({"industry": hudco[0].model_dump()})
    with pytest.raises(ValidationError):  # a peer-reference multiple must cite its data
        MultipleInput.model_validate({"method": "PB", "scenario": "BASE", "fiscal_year": 2027,
                                      "multiple": "1.0",
                                      "source": MultipleSource.PEER_REFERENCE.value})


# --- X: Box 13 / Box 14 ---------------------------------------------------------------------------


def test_x_detailed_report_states_eligibility_and_counts_honestly(hudco):
    data, t, _peers = hudco
    notes = peer_eligibility_notes(data, list(data.financial_benchmarks))
    assert notes[0].startswith("Peer eligibility (target business model "
                               "NON_BANK_FINANCIAL/SUBTYPE_UNRESOLVED): 5 candidates")
    assert "INCLUDED_LIMITED 2" in notes[0] and "FINANCIAL_SUBTYPE_UNRESOLVED 3" in notes[0]
    assert "Roe: 2 eligible, 0 excluded." in notes
    assert ("Borrowings to equity: benchmark unavailable - no eligible peer "
            "(CLASSIFICATION_MISMATCH 2).") in notes
    box4, box5 = t.box4, t.box5
    assert box4 is not None and box5 is not None
    d13 = build_due_diligence(box4, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=box4.company_key), generated_at=NOW, box9=data)
    # Box 13: missing peers are a comparability limitation, never a company risk
    without = build_due_diligence(box4, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=box4.company_key), generated_at=NOW)
    assert [(s.code, s.status, s.severity) for s in d13.signals] == [
        (s.code, s.status, s.severity) for s in without.signals]
    assert len(d13.red_flags) == len(without.red_flags)
    peer_context = {p.metric_code: p for p in d13.sector_specific_risks.peer_context}
    assert all(p.peer_count == 0 or p.peer_median is not None for p in peer_context.values())
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=box4.company_key), generated_at=NOW, box4=box4, box5=box5,
        box13=d13, box9=data)
    peers = report.table("PEER_BENCHMARKS")
    assert peers.footnotes[0] == notes[0]
    assert any("benchmark unavailable" in f for f in peers.footnotes)
    limitations = [c for c in report.claims if c.claim_type.value == "DATA_LIMITATION"
                   and "no eligible comparable peer" in c.statement]
    assert limitations and "borrowings to equity" in limitations[0].statement
    rows = {r.key.split("|")[0]: r for r in peers.rows}
    assert all(c.status.value != "AVAILABLE" for c in rows["BORROWINGS_TO_EQUITY"].cells
               if c.column == "PEER_MEDIAN")


def test_verifier_refuses_a_forged_eligibility(hudco):
    data, t, peers = hudco
    b = next(x for x in data.financial_benchmarks if x.metric_code == "BORROWINGS_TO_EQUITY")
    forged_peer = b.peers[0].model_copy(update={"status": OS.COMPARABLE})
    forged = b.model_copy(update={"peers": (forged_peer, *b.peers[1:])})
    other = data.model_copy(update={"financial_benchmarks": tuple(
        forged if x.benchmark_id == b.benchmark_id else x for x in data.financial_benchmarks)})
    problems = verify_industry_provenance(other, t, peers)
    assert any("neutral-only peer" in p for p in problems)
    assert any("definition differs" in p for p in problems)  # the forged row has none
