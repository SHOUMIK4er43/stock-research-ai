"""Box 13 due-diligence and risk intelligence: rules, evidence discipline, gating, integrity."""

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_financial_analytics import V, fy, make, pit
from test_forecasting import company

from stock_research_ai.application import due_diligence as engine
from stock_research_ai.application.credit_risk import parse_rating
from stock_research_ai.application.due_diligence import (
    build_due_diligence,
    build_due_diligence_async,
    canonical_due_diligence_json,
)
from stock_research_ai.application.financial_analytics import (
    build_financial_analytics,
)
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.red_flags import (
    RULES,
    DueDiligenceInputError,
    Ev,
    Trace,
    grade,
)
from stock_research_ai.domain import due_diligence as dd
from stock_research_ai.domain import management_intelligence as mi
from stock_research_ai.domain.due_diligence import (
    DiligenceSource,
    DueDiligenceDataset,
    DueDiligenceRequest,
    RiskSignal,
    Severity,
    SignalStatus,
    SourceAuthority,
)
from stock_research_ai.domain.evidence import EvidenceDocumentType
from stock_research_ai.domain.evidence import SourceAuthority as BoxAuthority
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
    Currency,
    Industry,
    PeriodType,
)
from stock_research_ai.domain.financial_canonical import ReportingBasis as B

M = CanonicalMetric
D = Decimal
SV = Severity
ST = SignalStatus
NOW = datetime(2026, 10, 10, tzinfo=UTC)
AS_OF = date(2026, 10, 10)
YEARS = list(range(2021, 2027))
SRC = DiligenceSource(authority=SourceAuthority.COMPANY_DOCUMENT,
                      reference="Annual report FY2026", url="https://example.com/ar.pdf")


def dec(x: object) -> Decimal:
    return Decimal(str(x))


def rows_for(
    *,
    revenue: list[object] | None = None,
    cfo: list[object] | None = None,
    margin: object = 20,
    other: object = 2,
    debt: object = 1000,
    rate: object = 8,
    cash: object = 200,
    short_term: object = 300,
    equity: object = 1500,
    assets: object = 3000,
    wc: tuple[object, object, object] = (15, 10, 5),
    receivables: list[object] | None = None,
    basis: B = B.CONSOLIDATED,
    drop: frozenset[tuple[CanonicalMetric, int]] = frozenset(),
    extra: list[V] | None = None,
) -> list[V]:
    revs = revenue or [1000, 1100, 1210, 1331, 1464, 1610]
    out = company(revs, start=YEARS[0], margin=margin, other=other, debt=debt, rate=rate, wc=wc,
                  basis=basis, drop=drop)
    if receivables is not None:
        out = [r for r in out if r.metric != M.TRADE_RECEIVABLES]
        out += [V(M.TRADE_RECEIVABLES, pit(y), dec(v), basis=basis)
                for y, v in zip(YEARS, receivables, strict=True)]
    flows = cfo if cfo is not None else [150] * len(YEARS)
    for y, c in zip(YEARS, flows, strict=True):
        if c is not None and (M.CFO, y) not in drop:
            out.append(V(M.CFO, fy(y), dec(c), basis=basis))
        out.append(V(M.TOTAL_ASSETS, pit(y), dec(assets), basis=basis))
        out.append(V(M.EQUITY_SHARE_CAPITAL, pit(y), D(100), basis=basis))
        out.append(V(M.RESERVES, pit(y), dec(equity) - 100, basis=basis))
        out.append(V(M.CASH_AND_CASH_EQUIVALENTS, pit(y), dec(cash), basis=basis))
        out.append(V(M.SHORT_TERM_BORROWINGS, pit(y), dec(short_term), basis=basis))
    return out + (extra or [])


def datasets(rows: list[V], industry: Industry = Industry.GENERAL) -> tuple[
        CanonicalFinancialDataset, FinancialAnalyticsDataset]:
    box4 = make(rows, industry)
    return box4, build_financial_analytics(box4, generated_at=NOW)


def run(rows: list[V] | None = None, *, industry: Industry = Industry.GENERAL,
        pair: tuple[CanonicalFinancialDataset, FinancialAnalyticsDataset] | None = None,
        **kw: Any) -> DueDiligenceDataset:
    box4, box5 = pair or datasets(rows if rows is not None else rows_for(), industry)
    upstream = {k: kw.pop(k) for k in ("box7", "box8", "box9", "box10", "box11", "box12",
                                       "bundle") if k in kw}
    request = DueDiligenceRequest(as_of=kw.pop("as_of", AS_OF), company_key=box4.company_key,
                                  **kw)
    return build_due_diligence(box4, box5, request=request, generated_at=NOW, **upstream)


def sig(data: DueDiligenceDataset, code: str) -> RiskSignal:
    (s,) = data.by_code(code)
    return s


def maybe(data: DueDiligenceDataset, code: str) -> RiskSignal | None:
    found = data.by_code(code)
    return found[0] if found else None


# --- 1. cash conversion -----------------------------------------------------------------------


def test_cash_conversion_rule_by_hand():
    good = run()
    s = sig(good, "WEAK_CASH_CONVERSION")
    assert s.status == ST.EVALUATED and s.severity == SV.GREEN
    weak = run(rows_for(cfo=[60] * 6))
    s = sig(weak, "WEAK_CASH_CONVERSION")
    assert s.severity == SV.RED and s.value is not None and s.value < D("0.5")
    assert s.risk_id in weak.red_flags and s.evidence_ids and s.source_ids and s.record_ids
    middle = run(rows_for(cfo=[100] * 6))
    assert sig(middle, "WEAK_CASH_CONVERSION").severity == SV.YELLOW


def test_cash_conversion_not_applicable_when_cumulative_pat_not_positive():
    data = run(rows_for(margin=1, other=0, cfo=[10] * 6))  # PBT negative every year
    s = sig(data, "WEAK_CASH_CONVERSION")
    assert s.status == ST.NOT_APPLICABLE and s.severity is None and s.risk_id not in data.red_flags


def test_profit_without_cash_counts_years():
    two = run(rows_for(cfo=[150, 150, 150, 150, -10, -20]))
    s = sig(two, "PROFIT_WITHOUT_CASH")
    assert (s.severity, s.value) == (SV.RED, D(2))
    one = run(rows_for(cfo=[150, 150, 150, 150, 150, -5]))
    assert sig(one, "PROFIT_WITHOUT_CASH").severity == SV.YELLOW


def test_profit_without_cash_missing_years_are_never_a_clean_reading():
    gaps = run(rows_for(cfo=[None, None, 150, 150, 150, 150]))
    s = sig(gaps, "PROFIT_WITHOUT_CASH")
    assert s.status == ST.UNAVAILABLE and s.reason == "INCOMPLETE_WINDOW"
    lower = run(rows_for(cfo=[None, None, 150, 150, -1, -1]))
    s = sig(lower, "PROFIT_WITHOUT_CASH")
    assert s.severity == SV.RED and "LOWER_BOUND_INCOMPLETE_WINDOW" in s.issues


def test_strong_cash_conversion_is_a_positive_control_listed_beside_flags():
    data = run(rows_for(cfo=[900] * 6, other=60))  # strong cash, but heavy other income
    strong = sig(data, "STRONG_CASH_CONVERSION")
    assert strong.signal_class == dd.SignalClass.POSITIVE_CONTROL
    assert strong.risk_id in data.positive_controls and strong.risk_id not in data.red_flags
    weak = run(rows_for(cfo=[60] * 6))
    assert maybe(weak, "STRONG_CASH_CONVERSION") is None  # absent controls are not listed


# --- 3-5. receivables, debtor and inventory days ----------------------------------------------


def test_receivables_outrunning_sales_by_hand():
    data = run(rows_for(receivables=[150, 165, 181.5, 199.65, 400, 600]))
    s = sig(data, "RECEIVABLES_OUTRUNNING_SALES")
    rec_cagr = ((D(600) / D("181.5")) ** (D(1) / 3) - 1) * 100
    rev_cagr = ((D(1610) / D(1210)) ** (D(1) / 3) - 1) * 100
    assert s.severity == SV.RED
    assert s.value is not None and abs(s.value - (rec_cagr - rev_cagr)) < D("0.000001")


def test_rising_debtor_days_and_improving_control():
    rising = run(rows_for(receivables=[150, 165, 181.5, 199.65, 219.6, 700]))
    s = sig(rising, "RISING_DEBTOR_DAYS")
    assert s.severity == SV.RED and s.value is not None and s.value > 45
    falling = run(rows_for(receivables=[300, 330, 363, 399, 150, 60]))
    assert sig(falling, "RISING_DEBTOR_DAYS").severity == SV.GREEN
    assert sig(falling, "IMPROVING_DEBTOR_DAYS").signal_class == dd.SignalClass.POSITIVE_CONTROL


def test_inventory_buildup_needs_cogs_and_is_unavailable_without_it():
    s = sig(run(), "INVENTORY_BUILDUP")
    assert s.status == ST.UNAVAILABLE and s.severity is None  # no COGS in the fixture


# --- 6-10. other income, accruals, leverage, cover, refinancing -------------------------------


def test_other_income_dependence_and_pbt_not_positive():
    heavy = run(rows_for(other=12))
    s = sig(heavy, "OTHER_INCOME_DEPENDENCE")
    assert s.severity == SV.RED and s.value is not None and s.value > 30
    loss = run(rows_for(margin=1, other=1))
    assert sig(loss, "OTHER_INCOME_DEPENDENCE").status == ST.NOT_APPLICABLE


def test_high_accruals():
    data = run(rows_for(cfo=[-200] * 6))
    s = sig(data, "HIGH_ACCRUALS")
    assert s.severity == SV.RED and s.value is not None and s.value > 10


def test_leverage_by_hand_net_cash_and_non_positive_ebitda():
    data = run(rows_for(debt=3000, cash=100))
    s = sig(data, "LEVERAGE")
    op = D(1610) * D("0.2")
    assert s.severity == SV.RED and s.value is not None
    assert abs(s.value - (D(2900) / op)) < D("0.000001")
    cash = run(rows_for(debt=100, cash=500))
    s = sig(cash, "LEVERAGE")
    assert s.severity == SV.GREEN and sig(cash, "NET_CASH").value == D(-400)
    losing = run(rows_for(margin=-5))
    assert sig(losing, "LEVERAGE").status == ST.NOT_APPLICABLE


def test_cited_sector_override_replaces_leverage_thresholds():
    override = dd.LeverageOverride(yellow_above=D(5), red_above=D(8), reason="cyclical",
                                   source=SRC)
    data = run(rows_for(debt=1500, cash=100), leverage_override=override)
    s = sig(data, "LEVERAGE")
    assert s.severity == SV.GREEN and {t.value for t in s.threshold} == {D(5), D(8)}
    assert "sector override" in (s.threshold[0].note or "")


def test_weak_interest_cover_and_zero_finance_cost():
    weak = run(rows_for(debt=3000, rate=10, cash=100))
    assert sig(weak, "WEAK_INTEREST_COVER").severity == SV.RED
    free = run(rows_for(rate=0))
    s = sig(free, "WEAK_INTEREST_COVER")
    assert s.status == ST.NOT_APPLICABLE and s.reason == "ZERO_FINANCE_COST"


def test_refinancing_risk_needs_leverage_and_cfo_deterioration_for_red():
    low = run(rows_for(debt=500))  # debt / equity 0.33
    assert sig(low, "REFINANCING_RISK").status == ST.NOT_APPLICABLE
    high = run(rows_for(debt=1000, short_term=900, equity=1000,
                        cfo=[150, 150, 150, 150, 200, 100]))
    s = sig(high, "REFINANCING_RISK")
    assert s.severity == SV.RED and s.value == D(90)
    improving = run(rows_for(debt=1000, short_term=900, equity=1000,
                             cfo=[150, 150, 150, 150, 100, 200]))
    s = sig(improving, "REFINANCING_RISK")
    assert s.severity == SV.YELLOW and "CFO_NOT_DETERIORATING" in s.issues


# --- 11-14. ownership ------------------------------------------------------------------------


def pledge(day: date, pct: object) -> dd.PledgePoint:
    return dd.PledgePoint(period_end=day, pledged_pct_of_promoter_holding=dec(pct), source=SRC)


Q = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]


def test_promoter_pledge_levels_trend_and_zero():
    assert sig(run(pledges=(pledge(Q[-1], 25),)), "PROMOTER_PLEDGE").severity == SV.RED
    assert sig(run(pledges=(pledge(Q[-1], 6),)), "PROMOTER_PLEDGE").severity == SV.YELLOW
    rising = run(pledges=(pledge(Q[1], 0), pledge(Q[2], 2), pledge(Q[3], 4.5)))
    s = sig(rising, "PROMOTER_PLEDGE")
    assert s.severity == SV.RED and "PLEDGE_RISING_TWO_CONSECUTIVE_QUARTERS" in s.issues
    zero = run(pledges=(pledge(Q[-1], 0),))
    assert sig(zero, "PROMOTER_PLEDGE").severity == SV.GREEN
    assert sig(zero, "NO_PLEDGE").risk_id in zero.positive_controls
    stale_zero = run(pledges=(pledge(date(2025, 1, 1), 0),))
    assert sig(stale_zero, "PROMOTER_PLEDGE").status == ST.UNAVAILABLE
    assert sig(stale_zero, "PROMOTER_PLEDGE").reason == "PLEDGE_DATA_STALE"
    assert maybe(stale_zero, "NO_PLEDGE") is None
    future_zero = run(pledges=(pledge(date(2027, 1, 1), 0),))
    assert sig(future_zero, "PROMOTER_PLEDGE").status == ST.UNAVAILABLE
    assert sig(future_zero, "PROMOTER_PLEDGE").reason == "PLEDGE_DATA_STALE"
    missing = sig(run(), "PROMOTER_PLEDGE")
    assert missing.status == ST.UNAVAILABLE and missing.severity is None


def holding(day: date, pct: object) -> dd.HoldingPoint:
    return dd.HoldingPoint(period_end=day, category=dd.HolderCategory.PROMOTER, pct=dec(pct),
                           source=SRC)


def shares(day: date, n: object, adjusted: bool = True) -> dd.ShareCount:
    return dd.ShareCount(as_of=day, shares=dec(n), bonus_split_adjusted=adjusted, source=SRC)


def test_promoter_selling_separates_dilution_with_share_counts():
    start, end = date(2025, 6, 30), date(2026, 6, 30)
    sold = run(holdings=(holding(start, 60), holding(end, 54)),
               share_counts=(shares(start, 1000), shares(end, 1000)))
    s = sig(sold, "PROMOTER_SELLING")
    assert s.severity == SV.RED and s.value == D(6)
    diluted = run(holdings=(holding(start, 60), holding(end, 54)),
                  share_counts=(shares(start, 1000), shares(end, D(1000) * 60 / 54)))
    s = sig(diluted, "PROMOTER_SELLING")
    assert s.severity == SV.GREEN and "DILUTION_SEPARATED_BY_SHARE_COUNTS" in s.issues
    unknown = run(holdings=(holding(start, 60), holding(end, 54)))
    s = sig(unknown, "PROMOTER_SELLING")
    assert s.status == ST.UNAVAILABLE and s.reason == "DILUTION_NOT_SEPARABLE"
    assert any("sale or dilution" in i.question for i in unknown.open_diligence_items)


def test_small_unadjusted_decline_grades_green_and_stable_holding_control():
    days = [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31),
            date(2026, 6, 30)]
    data = run(holdings=tuple(holding(d, 72.84) for d in days))
    s = sig(data, "PROMOTER_SELLING")
    assert s.severity == SV.GREEN and "UNADJUSTED_DECLINE_BELOW_THRESHOLD" in s.issues
    assert sig(data, "STABLE_PROMOTER_HOLDING").risk_id in data.positive_controls
    series = next(x for x in data.ownership_risks.series
                  if x.category == dd.HolderCategory.PROMOTER)
    assert series.change_4q_pp == 0 and series.change_qoq_pp == 0 and series.change_12q_pp is None


def test_equity_dilution_with_counts_bonus_and_share_capital_proxy():
    data = run(share_counts=(shares(date(2023, 6, 30), 1000), shares(date(2026, 6, 30), 1300)))
    s = sig(data, "EQUITY_DILUTION")
    assert s.severity == SV.RED and s.value == D(30)
    bonus = run(share_counts=(shares(date(2023, 6, 30), 1000, False),
                              shares(date(2026, 6, 30), 2000, False)))
    assert sig(bonus, "EQUITY_DILUTION").reason == "BONUS_SPLIT_NOT_EXCLUDED"
    capital = rows_for()
    capital = [r for r in capital if not (r.metric == M.EQUITY_SHARE_CAPITAL
                                          and r.period.period_end == date(2026, 3, 31))]
    capital.append(V(M.EQUITY_SHARE_CAPITAL, pit(2026), D(200)))
    s = sig(run(capital), "EQUITY_DILUTION")
    assert s.status == ST.UNAVAILABLE and s.reason == "BONUS_NOT_EXCLUDED"  # never assumed


def issue(allottee: str, price: object, ref: object = 100) -> dd.PreferentialIssue:
    return dd.PreferentialIssue(issue_date=date(2025, 1, 10), kind="WARRANTS", allottee=allottee,
                                issue_price=dec(price), reference_price=dec(ref),
                                reference_basis="ICDR_FLOOR_PRICE", source=SRC)


def test_cheap_insider_allotment():
    assert sig(run(preferential_issues=(issue("PROMOTER_GROUP", 65),)),
               "CHEAP_INSIDER_ALLOTMENT").severity == SV.RED
    assert sig(run(preferential_issues=(issue("KMP", 80),)),
               "CHEAP_INSIDER_ALLOTMENT").severity == SV.YELLOW
    outsiders = sig(run(preferential_issues=(issue("NON_PROMOTER", 50),)),
                    "CHEAP_INSIDER_ALLOTMENT")
    assert outsiders.status == ST.NOT_APPLICABLE
    assert sig(run(), "CHEAP_INSIDER_ALLOTMENT").status == ST.UNAVAILABLE


# --- 15. related parties ---------------------------------------------------------------------


def rpt(kind: str, relation: str, amount: object, basis: str = "CONSOLIDATED",
        year: int = 2026) -> dd.RelatedPartyItem:
    return dd.RelatedPartyItem(fiscal_year=year, basis=basis, kind=kind, counterparty="X Ltd",
                               relation=relation, amount_crore=dec(amount), source=SRC)


def test_related_party_dependence_excludes_subsidiaries_on_consolidated_basis():
    data = run(related_party=(rpt("REVENUE", "PROMOTER_ENTITY", 300),
                              rpt("PURCHASES", "SUBSIDIARY", 900)))
    s = sig(data, "RELATED_PARTY_DEPENDENCE")
    assert s.severity == SV.YELLOW
    assert s.value is not None and abs(s.value - D(300) * 100 / D(1610)) < D("0.000001")
    assert "wrongdoing" in s.explanation  # a disclosure, not an accusation


def test_material_loan_to_promoter_entity_is_at_least_yellow():
    data = run(related_party=(rpt("LOAN_GIVEN", "PROMOTER_ENTITY", 30),))
    s = sig(data, "RELATED_PARTY_LOANS")
    assert s.severity == SV.YELLOW and s.value == D(2)
    big = run(related_party=(rpt("GUARANTEE_GIVEN", "KMP_ENTITY", 300),))
    assert sig(big, "RELATED_PARTY_LOANS").severity == SV.RED
    unclear = run(related_party=(rpt("REVENUE", "UNCLEAR", 10),))
    assert "COUNTERPARTY_RELATION_UNCLEAR" in sig(unclear, "RELATED_PARTY_DEPENDENCE").issues


# --- 16. auditor -----------------------------------------------------------------------------


def report(**kw: Any) -> dd.AuditorReport:
    base: dict[str, Any] = {"fiscal_year": 2026, "basis": "CONSOLIDATED", "auditor": "A & Co",
                            "opinion": "UNMODIFIED", "source": SRC}
    base.update(kw)
    return dd.AuditorReport.model_validate(base)


def test_auditor_signal_rules():
    assert sig(run(auditor_reports=(report(opinion="QUALIFIED"),)),
               "AUDITOR_SIGNAL").severity == SV.RED
    assert sig(run(auditor_reports=(report(emphasis_of_matter=True),)),
               "AUDITOR_SIGNAL").severity == SV.YELLOW
    kam = run(auditor_reports=(report(key_audit_matters=("Revenue recognition on contracts",)),))
    assert sig(kam, "AUDITOR_SIGNAL").severity == SV.GREEN  # a KAM is never a flag by itself
    assert any("Revenue recognition" in i.question for i in kam.open_diligence_items)
    resigned = dd.AuditorChange(change_date=date(2025, 8, 1), reason="RESIGNATION",
                                mid_term=True, concerns_unresolved=True, source=SRC)
    assert sig(run(auditor_reports=(report(),), auditor_changes=(resigned,)),
               "AUDITOR_SIGNAL").severity == SV.RED
    cag = dd.AuditorChange(change_date=date(2025, 8, 1), reason="REGULATOR_APPOINTMENT",
                           source=SRC)
    assert sig(run(auditor_reports=(report(),), auditor_changes=(cag,)),
               "AUDITOR_SIGNAL").severity == SV.GREEN
    missing = sig(run(), "AUDITOR_SIGNAL")
    assert missing.status == ST.UNAVAILABLE and missing.reason == "AUDITOR_REPORT_NOT_SUPPLIED"


def test_caro_keeps_its_standalone_basis():
    with pytest.raises(ValidationError):
        report(caro_adverse_remarks=2)
    data = run(auditor_reports=(report(basis="STANDALONE", caro_adverse_remarks=2),))
    s = sig(data, "AUDITOR_SIGNAL")
    assert s.severity == SV.YELLOW and "CARO" in s.explanation


# --- 17-21. balance sheet and accounting --------------------------------------------------------


def contingent(category: str, amount: object, basis: str = "CONSOLIDATED") -> dd.ContingentItem:
    return dd.ContingentItem(as_of=date(2026, 3, 31), basis=basis, category=category,
                             amount_crore=dec(amount), source=SRC)


def test_contingent_liabilities_total_categories_and_commitments():
    data = run(contingent_liabilities=(contingent("TAX", 500), contingent("LEGAL_CLAIM", 400),
                                       contingent("CAPITAL_COMMITMENT", 5000)))
    s = sig(data, "CONTINGENT_LIABILITY")
    assert s.severity == SV.RED and s.value == D(60)  # 900 / 1500; commitments excluded
    section = data.contingent_liabilities
    assert section.total_method == "SUM_OF_CATEGORIES" and section.capital_commitments_crore == 5000
    stated = run(contingent_liabilities=(contingent("TOTAL", 450), contingent("TAX", 300),
                                         contingent("BANK_GUARANTEE", 300)))
    s = sig(stated, "CONTINGENT_LIABILITY")
    assert s.value == D(30) and s.severity == SV.YELLOW  # the stated total, never total + parts
    assert "CATEGORIES_EXCEED_STATED_TOTAL_POSSIBLE_DOUBLE_COUNT" in s.issues
    assert sig(run(), "CONTINGENT_LIABILITY").status == ST.UNAVAILABLE


def test_stalled_cwip_from_ageing_only():
    def bucket(name: str, amount: object) -> dd.CwipAgeing:
        return dd.CwipAgeing(as_of=date(2026, 3, 31), basis="CONSOLIDATED", bucket=name,
                             status="IN_PROGRESS", amount_crore=dec(amount), source=SRC)
    data = run(cwip_ageing=(bucket("LT_1Y", 50), bucket("GT_3Y", 50)))
    assert sig(data, "STALLED_CWIP").severity == SV.RED
    assert sig(run(), "STALLED_CWIP").reason == "CWIP_AGEING_NOT_SUPPLIED"


def test_tax_rule_negative_tax_and_cited_statutory_rate_only():
    rows = rows_for()
    rows = [r for r in rows if not (r.metric == M.TOTAL_TAX and r.period.fiscal_year in
                                    (2025, 2026))]
    rows += [V(M.TOTAL_TAX, fy(2025), D(-5)), V(M.TOTAL_TAX, fy(2026), D(-5))]
    s = sig(run(rows), "UNUSUAL_TAX_RATE")
    assert s.severity == SV.RED
    plain = sig(run(), "UNUSUAL_TAX_RATE")
    assert plain.severity == SV.GREEN and "NO_CITED_STATUTORY_RATE" in plain.issues
    cited = dd.StatutoryTaxRate(rate_pct=D("25.168"), applies_from_fiscal_year=2020, source=SRC)
    low = run(rows_for(), statutory_tax_rate=cited.model_copy(update={"rate_pct": D(40)}))
    assert sig(low, "UNUSUAL_TAX_RATE").severity == SV.YELLOW


def exceptional_rows(values: list[object | None]) -> list[V]:
    return rows_for(extra=[V(M.EXCEPTIONAL_ITEMS, fy(y), dec(v))
                           for y, v in zip(YEARS[1:], values, strict=True) if v is not None])


def test_recurring_exceptionals_and_a_one_time_gain():
    assert sig(run(exceptional_rows([5, 5, 5, 5, 5])), "RECURRING_EXCEPTIONALS").severity == SV.RED
    assert sig(run(exceptional_rows([5, 0, 5, 0, 5])),
               "RECURRING_EXCEPTIONALS").severity == SV.YELLOW
    once = run(exceptional_rows([0, 0, 0, 0, 80]))
    assert sig(once, "RECURRING_EXCEPTIONALS").severity == SV.GREEN
    adjusted = next(m for m in once.earnings_quality.adjusted
                    if m.code == "ADJUSTED_PAT_EX_EXCEPTIONALS")
    assert adjusted.status == "AVAILABLE" and adjusted.kind == dd.MetricKind.ADJUSTED
    unknown = sig(run(), "RECURRING_EXCEPTIONALS")
    assert unknown.status == ST.UNAVAILABLE  # not reported is never zero


def test_goodwill_heavy_lower_bound_and_negative_equity():
    heavy = run(rows_for(extra=[V(M.GOODWILL, pit(2026), D(1700)),
                                V(M.INTANGIBLE_ASSETS, pit(2026), D(10))]))
    assert sig(heavy, "GOODWILL_HEAVY").severity == SV.RED
    partial = run(rows_for(extra=[V(M.GOODWILL, pit(2026), D(10))]))
    s = sig(partial, "GOODWILL_HEAVY")
    assert s.status == ST.UNAVAILABLE and s.reason == "COMPONENT_NOT_REPORTED"
    bound = run(rows_for(extra=[V(M.GOODWILL, pit(2026), D(1000))]))
    s = sig(bound, "GOODWILL_HEAVY")
    assert s.severity == SV.YELLOW and "LOWER_BOUND_COMPONENT_NOT_REPORTED" in s.issues
    negative = run(rows_for(equity=-200))
    assert sig(negative, "NEGATIVE_EQUITY").severity == SV.RED
    assert sig(negative, "GOODWILL_HEAVY").status != ST.EVALUATED


# --- 22-25. management ---------------------------------------------------------------------------


def test_executive_pay_vs_profit():
    pay = (dd.ExecutivePay(fiscal_year=2024, scope="MD+CFO", amount_crore=D(2), source=SRC),
           dd.ExecutivePay(fiscal_year=2026, scope="MD+CFO", amount_crore=D(4), source=SRC))
    s = sig(run(executive_pay=pay), "EXECUTIVE_PAY_VS_PROFIT")
    assert s.severity == SV.YELLOW  # pay +100 % against PAT ~+21 %
    one = (pay[0],)
    assert sig(run(executive_pay=one), "EXECUTIVE_PAY_VS_PROFIT").status == \
        ST.INSUFFICIENT_SAMPLE
    # no pay data is missing evidence for an applicable rule, never NOT_APPLICABLE
    missing = sig(run(), "EXECUTIVE_PAY_VS_PROFIT")
    assert (missing.status, missing.reason) == (ST.UNAVAILABLE, "PAY_HISTORY_NOT_SUPPLIED")


def change(role: str, kind: str, day: date, planned: bool | None = None) -> dd.ManagementChange:
    return dd.ManagementChange(change_date=day, role=role, kind=kind, planned_succession=planned,
                               source=SRC)


def test_management_churn_counts_unplanned_departures():
    three = run(management_changes=(change("CEO_MD", "RESIGNATION", date(2024, 5, 1)),
                                    change("CFO", "RESIGNATION", date(2025, 5, 1))),
                auditor_changes=(dd.AuditorChange(change_date=date(2025, 9, 1),
                                                  reason="RESIGNATION", source=SRC),))
    assert sig(three, "MANAGEMENT_CHURN").severity == SV.RED
    planned = run(management_changes=(change("CEO_MD", "RETIREMENT", date(2024, 5, 1), True),
                                      change("CFO", "RESIGNATION", date(2025, 5, 1))))
    assert sig(planned, "MANAGEMENT_CHURN").severity == SV.GREEN
    assert sig(run(), "MANAGEMENT_CHURN").status == ST.UNAVAILABLE


def test_board_independence_depends_on_the_chair():
    def board(ind: int, chair: bool | None) -> dd.BoardFact:
        return dd.BoardFact(as_of=date(2026, 3, 31), directors=10, independent_directors=ind,
                            executive_chair=chair, source=SRC)
    assert sig(run(board=(board(4, True),)), "BOARD_INDEPENDENCE").severity == SV.YELLOW
    assert sig(run(board=(board(4, False),)), "BOARD_INDEPENDENCE").severity == SV.GREEN
    assert sig(run(board=(board(4, None),)), "BOARD_INDEPENDENCE").status == ST.INFORMATIONAL
    assert sig(run(board=(board(2, None),)), "BOARD_INDEPENDENCE").severity == SV.YELLOW


# --- 23. guidance reliability (Box 8) -----------------------------------------------------------


def box8_for(box4: CanonicalFinancialDataset,
             outcomes: list[tuple[str, int, mi.Lifecycle]]) -> mi.ManagementIntelligenceDataset:
    """A minimal but valid Box 8 dataset: one call, one statement per guidance item."""
    sha = engine._sha(canonical_dataset_json(box4))
    prov = mi.Provenance(document_id=UUID(int=1), extraction_id=UUID(int=2),
                         document_type=EvidenceDocumentType.CONCALL_TRANSCRIPT,
                         artifact_sha256="a" * 64, source_url="https://example.com/call.pdf",
                         authority=BoxAuthority.PRIMARY_REGULATORY, page_start=1, page_end=1,
                         block_ids=(UUID(int=3),), locator="p1")
    event = mi.ManagementEvent(
        event_id=UUID(int=10), company_key=box4.company_key, document_id=UUID(int=1),
        document_type=EvidenceDocumentType.CONCALL_TRANSCRIPT, artifact_sha256="a" * 64,
        event_type=mi.EventType.EARNINGS_CALL, title="Q4 call", call_date=date(2023, 5, 1),
        call_date_text=None, reporting_period=None, reporting_basis=B.CONSOLIDATED,
        basis_method="TEST", authority=BoxAuthority.PRIMARY_REGULATORY,
        source_url="https://example.com/call.pdf", page_start=1, page_end=1)
    person = mi.Participant(participant_id=UUID(int=11), event_id=event.event_id, raw_name="CFO",
                            normalized_name="cfo", person_key="cfo", role=mi.Role.MANAGEMENT,
                            role_basis=mi.RoleBasis.PARTICIPANT_LIST, designation=None,
                            organisation=None, provenance=prov)
    turn = mi.Turn(turn_id=UUID(int=12), event_id=event.event_id,
                   participant_id=person.participant_id, sequence=0, section=mi.Section.PREPARED,
                   text="guidance", truncated=False, word_count=1, provenance=prov)
    statements, guidance, results, timelines = [], [], [], []
    for n, (result, year, lifecycle) in enumerate(outcomes):
        sid, gid = UUID(int=1000 + n), UUID(int=2000 + n)
        statements.append(mi.ManagementStatement(
            statement_id=sid, event_id=event.event_id, turn_id=turn.turn_id,
            speaker_id=person.participant_id, speaker_role=mi.Role.MANAGEMENT, quote="g",
            normalized_text="g", statement_types=(mi.StatementType.GUIDANCE,), topics=(),
            forward_looking=True, historical_reference=False, confidence=mi.Confidence.HIGH,
            provenance=prov))
        guidance.append(mi.GuidanceItem(
            guidance_id=gid, statement_ids=(sid,), event_id=event.event_id,
            issued_date=date(2023, 5, 1), issued_period=None,
            subject=mi.SubjectLink(subject_code=f"S{n}", layer=mi.SubjectLayer.BOX4_METRIC,
                                   target_code="REVENUE", equivalence=mi.Equivalence.EXACT,
                                   reason="t"),
            measure=mi.Measure.GROWTH, qualifier=None, target_type=mi.TargetType.POINT,
            value=D(10), direction=mi.Direction.INCREASE, unit=mi.GuidanceUnit.PERCENT,
            target_period=mi.ReportingPeriod(period_type=PeriodType.FY, fiscal_year=year,
                                             period_end=date(year, 3, 31), raw_text=f"FY{year}",
                                             method="EXPLICIT_LABEL"),
            reporting_basis=B.CONSOLIDATED, basis_method="T", raw_target_text="10%",
            stated_change=mi.StatedChange.NONE, lifecycle=lifecycle,
            confidence=mi.Confidence.HIGH))
        actual = mi.ActualReference(layer=mi.SubjectLayer.BOX4_METRIC,
                                    record_id=box4.canonical_values[0].canonical_value_id,
                                    code="REVENUE", value=D(1), unit="PERCENT",
                                    period_type=PeriodType.FY, period_end=date(year, 3, 31),
                                    reporting_basis=B.CONSOLIDATED)
        scored = result in ("MET", "BEAT", "MISSED", "PARTIAL")
        results.append(mi.GuidanceOutcome(outcome_id=UUID(int=3000 + n), guidance_id=gid,
                                          result=mi.OutcomeResult(result), rule_version="t",
                                          rule="t", actual=actual if scored else None,
                                          reason="t"))
        timelines.append(mi.GuidanceTimeline(timeline_id=UUID(int=4000 + n), subject_code=f"S{n}",
                                             qualifier=None, target_period_key=str(year),
                                             guidance_ids=(gid,), lifecycles=(lifecycle,)))
    ref = mi.UpstreamReference(layer="BOX4", dataset_id=box4.dataset_id, sha256=sha,
                               schema_version=box4.schema_version)
    return mi.ManagementIntelligenceDataset(
        dataset_id=UUID(int=99), company_key=box4.company_key, company_name="Example Ltd",
        sector="GENERAL", generated_at=NOW,
        source_box3=mi.UpstreamReference(layer="BOX3", dataset_id=UUID(int=98), sha256="b" * 64,
                                         schema_version="EVIDENCE_BUNDLE_V1"),
        source_box4=ref, source_box5=None, source_box7=None, limits=mi.ManagementLimits(),
        status="COMPLETE", events=(event,), participants=(person,), turns=(turn,), qa_pairs=(),
        statements=tuple(statements), guidance=tuple(guidance), timelines=tuple(timelines),
        outcomes=tuple(results), accuracy=(), language=(), themes=(), unparsed=(), warnings=(),
        statistics=mi.ManagementStatistics(
            documents_consumed=1, events=1, participants=1, participants_by_role={}, turns=1,
            qa_pairs=0, answer_status_counts={}, statements=len(statements),
            statements_by_type={}, guidance=len(guidance), guidance_linked_exact=0,
            guidance_linked_proxy=0, guidance_unlinked=0, lifecycle_counts={},
            outcome_counts={}, unparsed=0, warnings_by_code={}))


def with_box8(outcomes: list[tuple[str, int, mi.Lifecycle]]) -> DueDiligenceDataset:
    pair = datasets(rows_for())
    return run(pair=pair, box8=box8_for(pair[0], outcomes))


ISSUED = mi.Lifecycle.ISSUED


def test_guidance_reliability_uses_completed_targets_only():
    data = with_box8([("MET", 2024, ISSUED), ("MISSED", 2024, ISSUED), ("MISSED", 2025, ISSUED),
                      ("PARTIAL", 2025, ISSUED), ("MET", 2026, ISSUED), ("MISSED", 2026, ISSUED),
                      ("NOT_YET_DUE", 2027, ISSUED), ("NOT_YET_DUE", 2028, ISSUED)])
    r = data.management_reliability
    assert (r.completed, r.met, r.missed, r.partial, r.open) == (6, 2, 3, 1, 2)
    s = sig(data, "UNRELIABLE_GUIDANCE")
    assert s.severity == SV.YELLOW and s.value is not None and abs(s.value - D(200) / 6) < 1
    assert set(s.record_ids) >= {UUID(int=3000)} and s.evidence_ids


def test_guidance_below_minimum_sample_and_open_guidance_never_punished():
    one = with_box8([("MISSED", 2025, ISSUED), ("NOT_YET_DUE", 2027, ISSUED),
                     ("NOT_YET_DUE", 2027, ISSUED)])
    s = sig(one, "UNRELIABLE_GUIDANCE")
    assert s.status == ST.INSUFFICIENT_SAMPLE and s.risk_id not in one.red_flags
    assert one.management_reliability.open == 2
    good = with_box8([("MET", 2025, ISSUED)] * 5 + [("BEAT", 2026, ISSUED)])
    assert sig(good, "UNRELIABLE_GUIDANCE").severity == SV.GREEN
    assert sig(good, "CONSISTENT_GUIDANCE_DELIVERY").risk_id in good.positive_controls
    withdrawn = with_box8([("MET", 2025, mi.Lifecycle.WITHDRAWN)] + [("MET", 2025, ISSUED)] * 5)
    assert withdrawn.management_reliability.withdrawn == 1
    assert sig(run(), "UNRELIABLE_GUIDANCE").reason == "BOX8_NOT_SUPPLIED"


# --- 24. credit ratings -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "scale", "notch", "outlook"),
    [
        ("CRISIL AA+/Stable", "INDIA_LONG_TERM", 2, "STABLE"),
        ("[ICRA]AA (Negative)", "UNSUPPORTED", None, "NOT_STATED"),
        ("[ICRA]AA; Negative", "INDIA_LONG_TERM", 3, "NEGATIVE"),
        ("CARE A1+", "INDIA_SHORT_TERM", None, "NOT_STATED"),
        ("IND BBB-", "INDIA_LONG_TERM", 10, "NOT_STATED"),
        ("CARE AA (CE)", "UNSUPPORTED", None, "NOT_STATED"),
        ("ACUITE BB+", "INDIA_LONG_TERM", 11, "NOT_STATED"),
        ("AAA", "INDIA_LONG_TERM", 1, "NOT_STATED"),
        ("CRISIL AA Rating Watch with Negative Implications", "INDIA_LONG_TERM", 3,
         "WATCH_NEGATIVE"),
        ("Provisional CARE AA", "INDIA_LONG_TERM", 3, "NOT_STATED"),
        ("Aa2", "UNSUPPORTED", None, "NOT_STATED"),  # another scale: never mapped
    ],
)
def test_rating_parser(text, scale, notch, outlook):
    parsed = parse_rating(text)
    assert (parsed.scale, parsed.notch, parsed.outlook.value) == (scale, notch, outlook)


def rating(day: date, grade_: str, **kw: Any) -> dd.RatingInput:
    return dd.RatingInput.model_validate({"action_date": day, "agency": "CRISIL",
                                          "instrument": "NCD", "rating": grade_,
                                          "source": SRC.model_copy(update={
                                              "authority": SourceAuthority.RATING_AGENCY}),
                                          **kw})


def test_credit_deterioration_by_instrument():
    multi = run(ratings=(rating(date(2024, 1, 1), "CRISIL AA"),
                         rating(date(2025, 1, 1), "CRISIL A+")))
    s = sig(multi, "CREDIT_DETERIORATION")
    assert s.severity == SV.RED and s.value == 2
    one = run(ratings=(rating(date(2024, 1, 1), "CRISIL AA"), rating(date(2025, 1, 1), "AA-")))
    assert sig(one, "CREDIT_DETERIORATION").severity == SV.YELLOW
    junk = run(ratings=(rating(date(2025, 1, 1), "CRISIL BB+"),))
    assert sig(junk, "CREDIT_DETERIORATION").severity == SV.RED
    outlook = run(ratings=(rating(date(2025, 1, 1), "CRISIL AA", outlook="NEGATIVE"),))
    assert sig(outlook, "CREDIT_DETERIORATION").severity == SV.YELLOW
    up = run(ratings=(rating(date(2024, 1, 1), "AA-"), rating(date(2025, 1, 1), "AA")))
    assert sig(up, "CREDIT_DETERIORATION").status == ST.UNAVAILABLE
    assert sig(up, "CREDIT_DETERIORATION").reason == "RATING_COVERAGE_INCOMPLETE"
    assert maybe(up, "RATING_UPGRADE").risk_id in up.positive_controls
    other = run(ratings=(rating(date(2024, 1, 1), "CRISIL AA"),
                         rating(date(2025, 1, 1), "CRISIL A+", instrument="Bank loans")))
    assert sig(other, "CREDIT_DETERIORATION").status == ST.UNAVAILABLE  # no comparable movement


def test_missing_withdrawn_and_unsupported_ratings():
    assert sig(run(), "CREDIT_DETERIORATION").reason == "NO_RATING_DATA"
    withdrawn = run(ratings=(rating(date(2024, 1, 1), "CRISIL AA"),
                             rating(date(2025, 1, 1), "CRISIL AA", action="WITHDRAWN")))
    s = sig(withdrawn, "CREDIT_DETERIORATION")
    assert s.status == ST.UNAVAILABLE and s.reason == "RATING_COVERAGE_INCOMPLETE"
    assert "RATING_WITHDRAWN" in s.issues
    assert any("withdrawn" in i.question for i in withdrawn.open_diligence_items)
    short = run(ratings=(rating(date(2025, 1, 1), "CRISIL A1+"),))
    assert sig(short, "CREDIT_DETERIORATION").reason == "UNSUPPORTED_RATING_SCALE"


# --- forensic models ------------------------------------------------------------------------


def model(data: DueDiligenceDataset, name: str, variant: str | None = None) -> dd.ForensicModelView:
    return next(m for m in data.financial_forensics.models
                if m.model == name and (variant is None or m.variant == variant))


def test_partial_models_are_never_scores():
    data = run()
    for name in ("PIOTROSKI_F_SCORE", "BENEISH_M"):
        view = model(data, name)
        assert view.status == "UNAVAILABLE" and view.score is None and view.missing_components
    altman = model(data, "ALTMAN_Z", "ORIGINAL_1968_MARKET_VALUE")
    assert altman.status == "UNAVAILABLE" and "X2" in altman.missing_components
    assert "X4" in altman.missing_components


def test_altman_original_with_cited_retained_earnings_and_market_value_by_hand():
    rows = rows_for(extra=[V(M.TOTAL_CURRENT_ASSETS, pit(2026), D(1200)),
                           V(M.TOTAL_CURRENT_LIABILITIES, pit(2026), D(700)),
                           V(M.TOTAL_LIABILITIES, pit(2026), D(1500))])
    re_ = dd.RetainedEarnings(as_of=date(2026, 3, 31), basis="CONSOLIDATED",
                              value_crore=D(900), source=SRC)
    cap = dd.MarketCapInput(as_of=date(2026, 3, 31), value_crore=D(4500), source=SRC)
    data = run(rows, retained_earnings=(re_,), market_cap=cap)
    view = model(data, "ALTMAN_Z", "ORIGINAL_1968_MARKET_VALUE")
    parts = {c.name: c.value for c in view.components}
    assert parts["X2"] == D("0.3") and parts["X4"] == D(3)
    expected = sum((D(w) * (parts[n] or 0) for n, w in
                    (("X1", "1.2"), ("X2", "1.4"), ("X3", "3.3"), ("X4", "0.6"), ("X5", "1.0"))),
                   D(0))
    assert view.status == "CALCULATED" and abs((view.score or 0) - expected) < D("0.00001")
    assert view.zone == ("SAFE" if expected > D("2.99") else "GREY" if expected >= D("1.81")
                         else "DISTRESS")


def test_reserves_proxy_is_opt_in_and_flagged():
    rows = rows_for(extra=[V(M.TOTAL_CURRENT_ASSETS, pit(2026), D(1200)),
                           V(M.TOTAL_CURRENT_LIABILITIES, pit(2026), D(700)),
                           V(M.TOTAL_LIABILITIES, pit(2026), D(1500))])
    cap = dd.MarketCapInput(as_of=date(2026, 3, 31), value_crore=D(4500), source=SRC)
    data = run(rows, market_cap=cap, altman_allow_reserves_proxy=True)
    view = model(data, "ALTMAN_Z", "ORIGINAL_1968_MARKET_VALUE")
    assert view.status == "CALCULATED" and "X2_RESERVES_PROXY_NOT_RETAINED_EARNINGS" in \
        view.warnings


# --- sector gating ----------------------------------------------------------------------------

INDUSTRIAL = ("WEAK_CASH_CONVERSION", "PROFIT_WITHOUT_CASH", "RECEIVABLES_OUTRUNNING_SALES",
              "RISING_DEBTOR_DAYS", "INVENTORY_BUILDUP", "OTHER_INCOME_DEPENDENCE",
              "HIGH_ACCRUALS", "LEVERAGE", "WEAK_INTEREST_COVER", "REFINANCING_RISK")


@pytest.mark.parametrize("industry", [Industry.BANK, Industry.NBFC, Industry.INSURANCE])
def test_financial_companies_never_get_industrial_rules(industry):
    data = run(rows_for(cfo=[-500] * 6, debt=9000), industry=industry,
               pledges=(pledge(Q[-1], 25),))
    assert data.sector_gate == dd.SectorGate.FINANCIAL
    for code in INDUSTRIAL:
        s = sig(data, code)
        assert s.status == ST.UNSUPPORTED_SECTOR_RULE and s.severity is None
    assert set(INDUSTRIAL) <= set(data.sector_specific_risks.gated_rules)
    assert all(m.status == "NOT_APPLICABLE" for m in data.financial_forensics.models)
    assert sig(data, "PROMOTER_PLEDGE").severity == SV.RED  # governance still applies
    assert not data.positive_controls or all(
        data.signal(i).code not in ("NET_CASH", "STRONG_CASH_CONVERSION")
        for i in data.positive_controls)
    assert any(i.code == "FINANCIAL_SECTOR_RULES_UNSUPPORTED" for i in data.issues)


# --- standalone / consolidated ------------------------------------------------------------------


def two_bases(stand_revenue: list[object]) -> list[V]:
    return rows_for() + rows_for(revenue=stand_revenue, basis=B.STANDALONE)


def test_consolidated_is_primary_and_standalone_is_the_control_layer():
    data = run(two_bases([1000, 1100, 1210, 1331, 1464, 1610]))
    assert data.analytical_basis == dd.SignalBasis.CONSOLIDATED
    assert data.control_basis == dd.SignalBasis.STANDALONE
    for s in data.signals:
        assert {i.basis for i in s.inputs if i.layer in (dd.Layer.BOX4, dd.Layer.BOX5)} <= \
            {dd.SignalBasis.CONSOLIDATED}  # never mixed into one calculation
    revenue = [c for c in data.standalone_consolidated_reconciliation if c.metric == "REVENUE"]
    assert revenue and all(c.status == dd.ReconciliationStatus.IMMATERIAL for c in revenue)


def test_material_difference_is_unexplained_only_after_a_recorded_review():
    rows = two_bases([600, 660, 726, 798, 878, 966])
    first = run(rows)
    latest = next(c for c in first.standalone_consolidated_reconciliation
                  if c.metric == "REVENUE" and c.period_end == date(2026, 3, 31))
    assert latest.status == dd.ReconciliationStatus.MATERIAL_NOT_REVIEWED
    assert sig(first, "UNEXPLAINED_BASIS_DIFFERENCE").status == ST.UNAVAILABLE
    review = dd.ReconciliationReview(fiscal_year=2026, sources_checked=("note 45",), source=SRC)
    reviewed = run(rows, reconciliation_reviews=(review,))
    latest = next(c for c in reviewed.standalone_consolidated_reconciliation
                  if c.metric == "REVENUE" and c.period_end == date(2026, 3, 31))
    assert latest.status == dd.ReconciliationStatus.MATERIAL_UNEXPLAINED
    assert sig(reviewed, "UNEXPLAINED_BASIS_DIFFERENCE").severity == SV.YELLOW
    why = dd.ReconciliationExplanation(metric="REVENUE", fiscal_year=2026, kind="GROUP_STRUCTURE",
                                       explanation="subsidiary revenue", source=SRC)
    explained = run(rows, reconciliation_reviews=(review,), reconciliation_explanations=(why,))
    latest = next(c for c in explained.standalone_consolidated_reconciliation
                  if c.metric == "REVENUE" and c.period_end == date(2026, 3, 31))
    assert latest.status == dd.ReconciliationStatus.EXPECTED_GROUP_DIFFERENCE


def test_standalone_only_company_uses_standalone_and_records_the_limitation():
    data = run(rows_for(basis=B.STANDALONE))
    assert data.analytical_basis == dd.SignalBasis.STANDALONE and data.control_basis is None
    assert ("MISSING_CONSOLIDATED_BASIS", 1) in data.data_quality_summary.counts


# --- evidence discipline, determinism, immutability, integrity ------------------------------------


def test_every_flag_and_control_carries_evidence():
    data = run(rows_for(cfo=[60] * 6, debt=3000, other=12), pledges=(pledge(Q[-1], 25),))
    assert data.red_flags
    for s in data.signals:
        if s.severity in (SV.YELLOW, SV.RED) or s.signal_class == dd.SignalClass.POSITIVE_CONTROL:
            assert s.inputs and (s.evidence_ids or s.record_ids) and s.source_ids
            assert s.confidence is not None and s.period_end is not None or s.period


def test_a_crossed_threshold_without_evidence_is_unsupported_not_red():
    data = run()
    ctx_ev = Ev(ST.EVALUATED, dd.SignalBasis.NOT_APPLICABLE, SV.RED, D(99), trace=Trace())
    from stock_research_ai.application import red_flags

    ctx = red_flags.Ctx.__new__(red_flags.Ctx)
    ctx.signals, ctx.persistence, ctx.gated, ctx.dd_key = [], {}, set(), "k"
    ctx.financial = False
    ctx.box4 = make([])  # a GENERAL Box 4: the gate reads Box 4's classification
    rid = ctx.emit("PROMOTER_PLEDGE", ctx_ev)
    (s,) = ctx.signals
    assert s.risk_id == rid and s.status == ST.UNSUPPORTED and s.severity is None
    assert "THRESHOLD_CROSSED_WITHOUT_EVIDENCE" in s.issues
    with pytest.raises(ValidationError):
        RiskSignal.model_validate(s.model_dump() | {"status": "EVALUATED", "severity": "RED",
                                                    "reason": None})
    assert data.red_flags == ()


def test_missing_values_are_never_zero():
    rows = rows_for(drop=frozenset({(M.FINANCE_COST, 2026)}))
    data = run(rows)
    s = sig(data, "WEAK_INTEREST_COVER")
    assert s.status == ST.UNAVAILABLE and s.value is None
    for m in data.financial_forensics.metrics:
        assert (m.status == "AVAILABLE") == (m.value is not None)


def test_determinism_and_generated_at_outside_ids():
    rows = rows_for(cfo=[60] * 6)
    pair = datasets(rows)
    a = run(pair=pair, pledges=(pledge(Q[-1], 6),))
    b = run(pair=pair, pledges=(pledge(Q[-1], 6),))
    assert canonical_due_diligence_json(a) == canonical_due_diligence_json(b)
    later = build_due_diligence(pair[0], pair[1], request=a.request,
                                generated_at=datetime(2030, 1, 1, tzinfo=UTC))
    assert later.dataset_id == a.dataset_id and later.signals == a.signals
    assert canonical_due_diligence_json(later) == canonical_due_diligence_json(a)


def test_async_wrapper_matches():
    import asyncio

    pair = datasets(rows_for())
    req = DueDiligenceRequest(as_of=AS_OF, company_key=pair[0].company_key)
    sync = build_due_diligence(pair[0], pair[1], request=req, generated_at=NOW)
    asyncd = asyncio.run(build_due_diligence_async(pair[0], pair[1], request=req,
                                                   generated_at=NOW))
    assert canonical_due_diligence_json(sync) == canonical_due_diligence_json(asyncd)


def test_inputs_are_never_mutated():
    pair = datasets(rows_for())
    box8 = box8_for(pair[0], [("MET", 2025, ISSUED)])
    before = (pair[0].model_dump_json(), pair[1].model_dump_json(), box8.model_dump_json())
    req = DueDiligenceRequest(as_of=AS_OF, company_key=pair[0].company_key,
                              pledges=(pledge(Q[-1], 6),))
    req_before = req.model_dump_json()
    build_due_diligence(pair[0], pair[1], request=req, generated_at=NOW, box8=box8)
    assert (pair[0].model_dump_json(), pair[1].model_dump_json(),
            box8.model_dump_json()) == before
    assert req.model_dump_json() == req_before


def test_integrity_refusals():
    box4, box5 = datasets(rows_for())
    other4, other5 = datasets(rows_for(cfo=[1] * 6))
    req = DueDiligenceRequest(as_of=AS_OF, company_key=box4.company_key)
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, other5, request=req, generated_at=NOW)
    assert e.value.code == "BOX4_MISMATCH"
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, box5, generated_at=NOW,
                            request=req.model_copy(update={"company_key": "screener:Y"}))
    assert e.value.code == "COMPANY_MISMATCH"
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, box5, request=req.model_copy(update={"as_of": date(2025, 1, 1)}),
                            generated_at=NOW)
    assert e.value.code == "INCOMPATIBLE_PERIOD"
    usd = dd.MarketCapInput(as_of=date(2026, 3, 31), value_crore=D(1), currency=Currency.USD,
                            source=SRC)
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, box5, request=req.model_copy(update={"market_cap": usd}),
                            generated_at=NOW)
    assert e.value.code == "CURRENCY_MISMATCH"
    forged = FinancialAnalyticsDataset.model_construct(**dict(box5))
    forged = forged.model_copy(update={"status": "FORGED"})
    with pytest.raises(ValidationError):
        build_due_diligence(box4, forged, request=req, generated_at=NOW)
    box8 = box8_for(other4, [])
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, box5, request=req, generated_at=NOW, box8=box8)
    assert e.value.code == "BOX4_MISMATCH"


def test_broken_evidence_reference_is_refused_when_box3_is_supplied():
    from stock_research_ai.domain.document_intelligence import EvidenceBundle, ExtractionLimits

    box4, box5 = datasets(rows_for())
    bundle = EvidenceBundle(
        bundle_id=UUID(int=5), company_key=box4.company_key, retrieval_run_id=UUID(int=6),
        extracted_at=NOW, parser_version="t", limits=ExtractionLimits(), documents_attempted=0,
        documents_extracted=0, documents_partial=0, documents_failed=0, total_pages=0,
        total_characters=0, total_tables=0, total_evidence_items=0, by_document_type=(),
        by_evidence_kind=(), warnings=(), extractions=())
    cited = SRC.model_copy(update={"evidence_ids": (UUID(int=77),)})
    req = DueDiligenceRequest(as_of=AS_OF, company_key=box4.company_key,
                              pledges=(dd.PledgePoint(period_end=Q[-1],
                                                      pledged_pct_of_promoter_holding=D(1),
                                                      source=cited),))
    with pytest.raises(DueDiligenceInputError) as e:
        build_due_diligence(box4, box5, request=req, generated_at=NOW, bundle=bundle)
    assert e.value.code == "BROKEN_EVIDENCE_REFERENCE"
    unverified = build_due_diligence(box4, box5, request=req, generated_at=NOW)
    assert ("EVIDENCE_NOT_VERIFIED", 1) in unverified.data_quality_summary.counts


def test_box8_outcomes_must_cite_records_of_the_supplied_box4():
    pair = datasets(rows_for())
    box8 = box8_for(pair[0], [("MET", 2025, ISSUED)])
    outcome = box8.outcomes[0]
    assert outcome.actual is not None
    forged = box8.model_copy(update={"outcomes": (outcome.model_copy(update={
        "actual": outcome.actual.model_copy(update={"record_id": UUID(int=12345)})}),)})
    with pytest.raises(DueDiligenceInputError) as e:
        run(pair=pair, box8=forged)
    assert e.value.code == "BROKEN_REFERENCE"


def test_order_books_stated_on_both_bases_are_reconciled():
    def book(basis: str, value: object) -> dd.OrderBookReport:
        return dd.OrderBookReport(as_of=date(2026, 3, 31), value_crore=dec(value), basis=basis,
                                  definition="remaining performance obligations", source=SRC)
    rows = rows_for() + rows_for(basis=B.STANDALONE)
    data = run(rows, order_book_reports=(book("CONSOLIDATED", 1000), book("STANDALONE", 700)))
    (row,) = [c for c in data.standalone_consolidated_reconciliation if c.metric == "ORDER_BOOK"]
    assert row.status == dd.ReconciliationStatus.MATERIAL_NOT_REVIEWED
    assert row.percentage_difference == 30


def test_order_book_driven_declaration_needs_a_source():
    with pytest.raises(ValidationError):
        DueDiligenceRequest(as_of=AS_OF, company_key="x", business_model="ORDER_BOOK_DRIVEN")


# --- data quality, risk map, registry, boundaries ----------------------------------------------


def test_data_limitations_are_never_company_risk():
    data = run()
    assert all(s.category != dd.RiskCategory.DATA_QUALITY for s in data.signals)
    kinds = {k for k, _ in data.data_quality_summary.counts}
    assert {"MISSING_UPSTREAM", "FORECAST_INPUTS_UNAVAILABLE",
            "VALUATION_INPUTS_UNAVAILABLE"} <= kinds
    assert data.data_quality_summary.risk_kind == "DATA_LIMITATION"
    assert data.red_flags == ()


def test_risk_map_uses_persistence_and_registered_impact():
    data = run(rows_for(cfo=[60] * 6))
    entry = next(e for e in data.risk_map if e.code == "WEAK_CASH_CONVERSION")
    assert entry.impact == dd.Level.HIGH and "IMPACT/1" in entry.impact_rule
    assert entry.likelihood in (dd.Level.HIGH, dd.Level.MEDIUM, dd.Level.LOW, dd.Level.UNKNOWN)
    pledge_entry = next(e for e in run(pledges=(pledge(Q[-1], 25),)).risk_map
                        if e.code == "PROMOTER_PLEDGE")
    assert pledge_entry.likelihood == dd.Level.UNKNOWN  # not a periodic rule


def test_mitigants_never_change_severity():
    data = run(rows_for(cfo=[400] * 6, other=60))
    flag = sig(data, "OTHER_INCOME_DEPENDENCE")
    assert flag.severity == SV.RED and flag.category == dd.RiskCategory.FINANCIAL
    lev = run(rows_for(debt=100, cash=500, rate=250))  # net cash but weak cover
    cover = sig(lev, "WEAK_INTEREST_COVER")
    assert cover.severity in (SV.YELLOW, SV.RED)
    assert sig(lev, "NET_CASH").risk_id in cover.mitigating_signal_ids
    assert cover.mitigant and "does not change the severity" in cover.mitigant


def test_registry_documents_every_rule():
    data = run()
    manifest_codes = {r.code for r in data.manifest.rules}
    assert {s.code for s in data.signals} <= manifest_codes
    for r in data.manifest.rules:
        assert r.formula_id == f"{r.code}/1" and r.formula_text and r.applicability
        assert r.sectors and r.unit
        if r.signal_class == dd.SignalClass.COMPANY_RISK and r.code != "GOVERNMENT_CONCENTRATION":
            assert any(t.severity in (SV.YELLOW, SV.RED) for t in r.thresholds)
    assert RULES["UNRELIABLE_GUIDANCE"].minimum_sample == 5
    assert all(f.formula_text and f.version == 1 for f in data.manifest.formulas)
    assert grade(D(3), RULES["LEVERAGE"].thresholds) == SV.YELLOW


def test_no_advice_language_anywhere():
    data = run(rows_for(cfo=[60] * 6, debt=3000), pledges=(pledge(Q[-1], 25),))
    text = canonical_due_diligence_json(data).upper()
    for term in ("BUY ", " SELL ", " HOLD ", "OVERWEIGHT", "UNDERWEIGHT", "TARGET PRICE",
                 "POSITION SIZE", "BUY ZONE", "SELL TRIGGER"):
        assert term not in text.replace("BUYBACK", ""), term
    assert data.recommendation == "NONE"
    dump = data.model_dump()
    dump["signals"][0]["explanation"] = "we would buy this"
    with pytest.raises(ValidationError, match="advice"):
        DueDiligenceDataset.model_validate(dump)


def _module_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    return [root / "domain" / "due_diligence.py"] + [
        root / "application" / f"{m}.py" for m in (
            "due_diligence", "red_flags", "earnings_quality", "governance_risk", "credit_risk",
            "order_book_risk", "data_quality", "diligence_inputs")]


def test_box13_modules_are_offline_and_clock_free():
    banned_calls = {"now", "today", "utcnow", "uuid4", "urlopen", "getrandbits"}
    banned_imports = {"httpx", "requests", "socket", "random", "urllib", "time", "secrets"}
    for path in _module_paths():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned_calls, (path.name, node.func.attr)
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.split(".")[0] in banned_imports for n in names), (path.name, names)


def test_negative_working_capital_is_not_a_flag_by_itself():
    rows = rows_for(wc=(5, 2, 60))  # payables far above receivables + inventory
    data = run(rows)
    assert not any(data.signal(i).category == dd.RiskCategory.WORKING_CAPITAL
                   for i in data.red_flags)
    series = next(x for x in data.working_capital_risks.series
                  if x.code == "PAYABLE_DAYS" and x.frequency == "ANNUAL")
    assert len(series.points) == 5
    quarterly = next(x for x in data.working_capital_risks.series
                     if x.code == "PAYABLE_DAYS" and x.frequency == "QUARTERLY")
    assert quarterly.points == () and quarterly.reason


def test_missing_comparative_period_makes_averages_unavailable():
    rows = rows_for(drop=frozenset({(M.TRADE_RECEIVABLES, 2023)}))
    data = run(rows)
    for code in ("RISING_DEBTOR_DAYS", "RECEIVABLES_OUTRUNNING_SALES"):
        s = sig(data, code)
        assert s.status in (ST.UNAVAILABLE, ST.NOT_APPLICABLE) and s.value is None, code


def test_working_capital_disclosures_are_ratios_on_the_same_basis():
    item = dd.WorkingCapitalDisclosure(as_of=date(2026, 3, 31), basis="CONSOLIDATED",
                                       item="CONTRACT_ASSETS", amount_crore=D(322), source=SRC)
    data = run(working_capital=(item,))
    (metric,) = data.working_capital_risks.disclosures
    assert metric.code == "CONTRACT_ASSETS_TO_REVENUE" and metric.value == 20
    other = item.model_copy(update={"basis": "STANDALONE"})
    (metric,) = run(working_capital=(other,)).working_capital_risks.disclosures
    assert metric.status == "UNAVAILABLE"  # no standalone revenue: never the consolidated one
