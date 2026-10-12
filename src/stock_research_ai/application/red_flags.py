"""DETAILED_RESEARCH_V1 Box 13: rule registry, shared context and the signal factory.

Every Box 13 rule is registered here with its inputs, formula text, applicability, thresholds,
unit, impact and (where a sample is involved) its documented minimum N. Rules evaluate to an
`Ev` (status, severity, value, trace of inputs); `Ctx.emit` turns it into a RiskSignal and
enforces the severity discipline: a YELLOW / RED reading without inputs, records and sources is
UNSUPPORTED, never a flag. Grading is `grade()` over the registered thresholds only.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from stock_research_ai.application.financial_metrics import (
    Fact,
    Frame,
    Got,
    Missing,
    Periods,
    quantize,
    weakest,
)
from stock_research_ai.domain.due_diligence import (
    DUE_DILIGENCE_ENGINE_VERSION,
    FORMULA_REGISTRY_VERSION,
    RISK_MAP_RULES_VERSION,
    RULE_REGISTRY_VERSION,
    DiligenceSource,
    DueDiligenceIssue,
    DueDiligenceManifest,
    DueDiligenceRequest,
    FormulaDefinition,
    Layer,
    Level,
    RiskCategory,
    RiskSignal,
    RuleDefinition,
    SectorGate,
    Severity,
    SignalBasis,
    SignalClass,
    SignalInput,
    SignalStatus,
    SourceAuthority,
    Threshold,
)
from stock_research_ai.domain.financial_analytics import (
    CalculatedMetric,
    CalculationStatus,
    FinancialAnalyticsDataset,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    Confidence,
    Currency,
    FinancialPeriod,
    ReportingBasis,
)

if TYPE_CHECKING:
    from stock_research_ai.application.diligence_inputs import NormalizedInputs

NS = UUID("6d3c1a9e-58b2-4f17-a0c4-9e2b7d5f13a8")
D = Decimal
S = SignalStatus
SV = Severity
C = RiskCategory
G = SectorGate
NONFIN = (G.NON_FINANCIAL,)
ALL = (G.NON_FINANCIAL, G.FINANCIAL)


class DueDiligenceInputError(ValueError):
    """The inputs cannot be combined (wrong company, forged lineage, broken reference)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# --- registry ---------------------------------------------------------------------------------


def T(severity: Severity, comparison: str, value: object, note: str | None = None) -> Threshold:
    return Threshold.model_validate(
        {"severity": severity, "comparison": comparison, "value": D(str(value)), "note": note}
    )


RULES: dict[str, RuleDefinition] = {}


def _rule(
    code: str,
    name: str,
    category: RiskCategory,
    inputs: tuple[str, ...],
    formula: str,
    applicability: str,
    sectors: tuple[SectorGate, ...],
    thresholds: tuple[Threshold, ...],
    unit: str,
    impact: Level,
    *,
    signal_class: SignalClass = SignalClass.COMPANY_RISK,
    minimum_sample: int | None = None,
    notes: str = "",
) -> None:
    assert code not in RULES, code
    RULES[code] = RuleDefinition(
        formula_id=f"{code}/1",
        code=code,
        version=1,
        name=name,
        signal_class=signal_class,
        category=category,
        inputs=inputs,
        formula_text=formula,
        applicability=applicability,
        sectors=sectors,
        thresholds=thresholds,
        unit=unit,
        impact=impact,
        minimum_sample=minimum_sample,
        notes=notes,
    )


Y, R = SV.YELLOW, SV.RED
L = Level

# 1-10: cash, working capital, earnings and leverage (non-financial companies)
_rule("WEAK_CASH_CONVERSION", "Weak cumulative cash conversion", C.CASH_FLOW,
      ("Box 5 CUMULATIVE_CFO_TO_PAT_5Y",), "sum(CFO, 5 FY) / sum(PAT, 5 FY)",
      "non-financial; only when cumulative PAT > 0", NONFIN,
      (T(R, "LT", "0.5"), T(Y, "LT", "0.8")), "TIMES", L.HIGH)
_rule("PROFIT_WITHOUT_CASH", "Profitable years with negative operating cash flow",
      C.CASH_FLOW, ("Box 4 PAT, CFO (each of the last 5 FY)",),
      "count(FY in last 5 where PAT > 0 and CFO < 0)",
      "non-financial; a count over available years is a lower bound: it can confirm a breach, "
      "never a clean (GREEN) reading, which needs all 5 years", NONFIN,
      (T(R, "GE", 2), T(Y, "GE", 1)), "YEARS", L.HIGH)
_rule("RECEIVABLES_OUTRUNNING_SALES", "Receivables growing faster than revenue",
      C.WORKING_CAPITAL, ("Box 4 TRADE_RECEIVABLES (latest, 3 years earlier)",
                          "Box 5 REVENUE_CAGR_3Y"),
      "CAGR(receivables, 3Y) - CAGR(revenue, 3Y), percentage points",
      "non-financial; both CAGRs need positive endpoints", NONFIN,
      (T(R, "GT", 20), T(Y, "GT", 10)), "PERCENTAGE_POINTS", L.MEDIUM)
_rule("RISING_DEBTOR_DAYS", "Debtor days above their 3-year average", C.WORKING_CAPITAL,
      ("Box 5 RECEIVABLE_DAYS (latest FY and the 3 FY before it)",),
      "debtor days(latest FY) - mean(debtor days of the 3 preceding FY)",
      "non-financial; all 4 years needed", NONFIN,
      (T(R, "GT", 45), T(Y, "GT", 20)), "DAYS", L.MEDIUM)
_rule("INVENTORY_BUILDUP", "Inventory days above their 3-year average", C.WORKING_CAPITAL,
      ("Box 5 INVENTORY_DAYS (latest FY and the 3 FY before it)",),
      "(inventory days(latest) / mean(inventory days, 3 preceding FY) - 1) x 100",
      "non-financial; the 3-year mean must be positive", NONFIN,
      (T(R, "GT", 50), T(Y, "GT", 25)), "PERCENT", L.MEDIUM)
_rule("OTHER_INCOME_DEPENDENCE", "Profit dependent on other income", C.FINANCIAL,
      ("Box 5 OTHER_INCOME_TO_PBT",), "other income / PBT x 100",
      "non-financial; not calculated when PBT <= 0", NONFIN,
      (T(R, "GT", 30), T(Y, "GT", 15)), "PERCENT", L.MEDIUM)
_rule("HIGH_ACCRUALS", "High accruals", C.ACCOUNTING, ("Box 5 CASH_FLOW_ACCRUAL_RATIO",),
      "(PAT - CFO) / average total assets x 100", "non-financial", NONFIN,
      (T(R, "GT", 10), T(Y, "GT", 5)), "PERCENT", L.MEDIUM)
_rule("LEVERAGE", "Net debt to operating EBITDA", C.LEVERAGE,
      ("Box 5 NET_DEBT", "Box 4 OPERATING_PROFIT, else Box 5 EBITDA - Box 4 OTHER_INCOME"),
      "net debt / operating EBITDA (net debt = gross debt - cash and cash equivalents, Box 5)",
      "non-financial; not calculated when operating EBITDA <= 0; net cash grades GREEN; a "
      "cited sector override replaces the thresholds", NONFIN,
      (T(R, "GT", 4), T(Y, "GT", "2.5")), "TIMES", L.HIGH)
_rule("WEAK_INTEREST_COVER", "Weak interest cover", C.LEVERAGE, ("Box 5 INTEREST_COVERAGE",),
      "EBIT / finance cost", "non-financial; not calculated when finance cost is 0", NONFIN,
      (T(R, "LT", "1.5"), T(Y, "LT", 3)), "TIMES", L.HIGH)
_rule("REFINANCING_RISK", "Short-term share of borrowings", C.LIQUIDITY,
      ("Box 4 SHORT_TERM_BORROWINGS", "Box 5 GROSS_DEBT", "Box 5 DEBT_TO_EQUITY",
       "Box 4 CFO (latest, prior FY)"),
      "short-term borrowings / gross debt x 100",
      "non-financial; only meaningful when debt / equity > 0.5; RED needs > 85 % AND CFO lower "
      "than the prior year (otherwise YELLOW)", NONFIN,
      (T(R, "GT", 85, "and CFO deteriorating"), T(Y, "GT", 70)), "PERCENT", L.HIGH)
# 11-16: ownership, governance, auditor
_rule("PROMOTER_PLEDGE", "Promoter pledge", C.OWNERSHIP, ("PledgePoint (cited)",),
      "pledged promoter shares / promoter shares x 100 (latest quarter)",
      "all sectors; RED also when the pledge rose by >= 2 pp in each of 2 consecutive quarters",
      ALL, (T(R, "GT", 20), T(Y, "GT", 5)), "PERCENT", L.HIGH)
_rule("PROMOTER_SELLING", "Promoter holding decline", C.OWNERSHIP,
      ("Box 12 shareholding filings", "HoldingPoint PROMOTER (cited)", "ShareCount (cited)"),
      "promoter % (4 quarters earlier) - promoter % (latest), percentage points; with "
      "comparable share counts: (promoter shares earlier - promoter shares latest) / shares "
      "latest x 100, promoter shares = total shares x promoter % (a fall in promoter shares "
      "stays visible through dilution); unadjusted only when dilution is ruled out",
      "all sectors; share counts compare only when both are bonus / split adjusted, or both "
      "unadjusted with no bonus / split in the window; otherwise DILUTION_NOT_SEPARABLE", ALL,
      (T(R, "GE", 5), T(Y, "GE", 2)), "PERCENTAGE_POINTS", L.MEDIUM)
_rule("EQUITY_DILUTION", "Equity dilution over 3 years", C.OWNERSHIP,
      ("ShareCount (cited, bonus/split adjusted)", "Box 4 EQUITY_SHARE_CAPITAL",
       "Box 12 corporate actions"),
      "(shares(latest) / shares(3 years earlier) - 1) x 100, excluding bonus / split",
      "all sectors; share capital is used only when no bonus / split / merger lies in the "
      "window", ALL, (T(R, "GT", 25), T(Y, "GT", 10)), "PERCENT", L.MEDIUM)
_rule("CHEAP_INSIDER_ALLOTMENT", "Discounted allotment to insiders", C.GOVERNANCE,
      ("PreferentialIssue (cited)",),
      "(reference price - issue price) / reference price x 100 (largest, last 3 years)",
      "all sectors; promoter-group / KMP allottees only", ALL,
      (T(R, "GT", 30), T(Y, "GT", 15)), "PERCENT", L.MEDIUM)
_rule("RELATED_PARTY_DEPENDENCE", "Related-party revenue / purchases", C.GOVERNANCE,
      ("RelatedPartyItem REVENUE + PURCHASES (cited)", "Box 4 revenue (same FY, same basis)"),
      "(related-party revenue + purchases) / revenue x 100; consolidated basis excludes "
      "subsidiaries (eliminated on consolidation)",
      "all sectors; numerator and revenue on the same basis and fiscal year", ALL,
      (T(R, "GT", 25), T(Y, "GT", 10)), "PERCENT", L.MEDIUM)
_rule("RELATED_PARTY_LOANS", "Loans / advances / guarantees to promoter or KMP entities",
      C.GOVERNANCE, ("RelatedPartyItem LOAN / ADVANCE / GUARANTEE (cited)",
                     "Box 5 SHAREHOLDERS_EQUITY (same date, same basis)"),
      "(loans + advances + guarantees to promoter / KMP entities) / net worth x 100",
      "all sectors; material at >= 1 % of net worth (YELLOW); RED above 10 %", ALL,
      (T(R, "GT", 10), T(Y, "GE", 1)), "PERCENT", L.MEDIUM)
_rule("AUDITOR_SIGNAL", "Auditor report and auditor change", C.AUDITOR,
      ("AuditorReport (cited)", "AuditorChange (cited)"),
      "RED: qualified / adverse / disclaimer opinion, or a mid-term resignation with concerns "
      "stated as unresolved; YELLOW: emphasis of matter, going-concern uncertainty, modified "
      "IFC opinion, CARO adverse remarks, restatement, policy change, any auditor change in "
      "3 years", "all sectors; key audit matters alone are diligence items, never a flag; no "
      "motive is inferred", ALL,
      (T(R, "EQ", 2, "opinion modified / resignation with concerns"),
       T(Y, "EQ", 1, "one or more yellow conditions")), "CODE", L.HIGH)
# 17-21: balance sheet and accounting
_rule("CONTINGENT_LIABILITY", "Contingent liabilities to net worth", C.FINANCIAL,
      ("ContingentItem (cited)", "Box 5 SHAREHOLDERS_EQUITY (same date, same basis)"),
      "contingent liabilities (stated total, else sum of categories; commitments excluded) / "
      "net worth x 100", "non-financial; net worth must be positive", NONFIN,
      (T(R, "GT", 50), T(Y, "GT", 25)), "PERCENT", L.MEDIUM)
_rule("STALLED_CWIP", "CWIP older than 3 years", C.ACCOUNTING, ("CwipAgeing (cited)",),
      "CWIP aged > 3 years / total CWIP x 100", "non-financial; only with an ageing schedule",
      NONFIN, (T(R, "GT", 40), T(Y, "GT", 20)), "PERCENT", L.MEDIUM)
_rule("UNUSUAL_TAX_RATE", "Unusual effective tax rate", C.ACCOUNTING,
      ("Box 5 TAX_RATE (last 5 FY)", "Box 4 PBT", "StatutoryTaxRate (cited, optional)"),
      "negative tax while PBT > 0 (years in last 5); |ETR(latest) - median ETR (prior 4 FY)|; "
      "|ETR(latest) - cited statutory rate|", "all sectors; never a hard-coded statutory rate",
      ALL, (T(R, "GE", 2, "years of negative tax while profitable"),
            T(Y, "GE", 1, "year of negative tax while profitable"),
            T(Y, "GT", 10, "pp from the historical median or the cited statutory rate")),
      "MIXED", L.MEDIUM)
_rule("RECURRING_EXCEPTIONALS", "Recurring exceptional items", C.ACCOUNTING,
      ("Box 4 EXCEPTIONAL_ITEMS (last 5 FY)",),
      "count(FY in last 5 with non-zero reported exceptional items)",
      "all sectors; a count over available years is a lower bound", ALL,
      (T(R, "EQ", 5), T(Y, "GE", 3)), "YEARS", L.MEDIUM)
_rule("GOODWILL_HEAVY", "Goodwill and intangibles to net worth", C.ACCOUNTING,
      ("Box 4 GOODWILL", "Box 4 INTANGIBLE_ASSETS", "Box 5 SHAREHOLDERS_EQUITY"),
      "(goodwill + intangible assets) / net worth x 100",
      "all sectors; net worth must be positive; with one component unreported the sum is a "
      "lower bound (it can confirm a breach, never a clean reading)", ALL,
      (T(R, "GT", 100), T(Y, "GT", 50)), "PERCENT", L.MEDIUM)
# 22-25: management
_rule("EXECUTIVE_PAY_VS_PROFIT", "Executive pay against profit", C.GOVERNANCE,
      ("ExecutivePay (cited, same scope)", "Box 4 PAT"),
      "pay growth / PAT growth over the same years; pay / PAT x 100",
      "all sectors; needs >= 2 years of same-scope pay; YELLOW when pay grew > 2x PAT growth, "
      "when pay grew while PAT fell, or when pay > 5 % of PAT", ALL,
      (T(Y, "GT", 2, "times PAT growth"), T(Y, "GT", 5, "% of PAT")), "TIMES", L.LOW)
_rule("UNRELIABLE_GUIDANCE", "Guidance delivery", C.MANAGEMENT,
      ("Box 8 guidance outcomes (completed quantitative targets, target period in the last 3 "
       "years)",),
      "(MET + BEAT) / completed targets x 100; PARTIAL counts as not met",
      "all sectors; open (not yet due) guidance never counts", ALL,
      (T(R, "LT", 30), T(Y, "LT", 50)), "PERCENT", L.MEDIUM, minimum_sample=5)
_rule("CREDIT_DETERIORATION", "Credit-rating deterioration", C.LIQUIDITY,
      ("RatingInput (cited, by instrument)", "Box 12 credit-rating filings"),
      "per agency and instrument, notches between consecutive long-term ratings (Indian scale "
      "AAA=1 ... D=20); outlook / watch; the agency's own action word",
      "all sectors; only the Indian long-term scale is mapped; a short-term or structured "
      "rating is never mapped; withdrawn ratings are not deterioration", ALL,
      (T(R, "GE", 2, "notches, or a rating below BBB-"),
       T(Y, "GE", 1, "notch, negative outlook / watch, or a stated downgrade")),
      "NOTCHES", L.HIGH)
_rule("MANAGEMENT_CHURN", "CEO / CFO / auditor turnover", C.MANAGEMENT,
      ("ManagementChange (cited)", "AuditorChange (cited)"),
      "count(departures of CEO/MD or CFO + auditor changes in the last 3 years), excluding "
      "disclosed planned succession and regulator appointments",
      "all sectors", ALL, (T(R, "GE", 3), T(Y, "GE", 2)), "COUNT", L.MEDIUM)
# further company-risk rules
_rule("NEGATIVE_EQUITY", "Negative net worth", C.LEVERAGE, ("Box 5 SHAREHOLDERS_EQUITY",),
      "shareholders' equity < 0", "all sectors", ALL, (T(R, "LT", 0),), "MONEY", L.HIGH)
_rule("EXCHANGE_PENALTIES", "Exchange / regulator penalties", C.REGULATORY,
      ("Box 12 EXCHANGE_PENALTY events", "PenaltyInput (cited)", "Box 4 PAT"),
      "count(penalty events in the last 3 years); RED when a stated penalty >= 1 % of PAT",
      "all sectors; severity never read from wording", ALL,
      (T(R, "GE", 1, "% of PAT for one stated penalty"), T(Y, "GE", 1, "penalty event")),
      "COUNT", L.MEDIUM)
_rule("BOARD_INDEPENDENCE", "Independent directors below the listing minimum", C.GOVERNANCE,
      ("BoardFact (cited)",),
      "independent directors / directors x 100 against 50 % (executive chair) or 33.33 %",
      "all sectors; with the chair's status unknown only a breach of 33.33 % is graded", ALL,
      (T(Y, "LT", "33.333333", "or 50 with an executive chair"),), "PERCENT", L.MEDIUM)
_rule("MATERIAL_ORDER_CANCELLATION", "Material order cancellation filed", C.ORDER_BOOK,
      ("Box 12 ORDER_CANCELLATION event materiality",),
      "stated value / reference annual revenue x 100 (Box 12)",
      "all sectors; only with a stated value and a cited revenue", ALL,
      (T(R, "GE", 5), T(Y, "GE", 1)), "PERCENT", L.MEDIUM)
_rule("MATERIAL_LITIGATION", "Material adverse litigation filed", C.LEGAL,
      ("Box 12 LITIGATION event materiality", "ExchangeEventAssessment ADVERSE (cited)"),
      "stated value / reference annual revenue x 100 (Box 12)",
      "all sectors; only with a stated value, a cited revenue and a cited ADVERSE direction "
      "(an award in the company's favour or an unknown direction is information only)", ALL,
      (T(R, "GE", 5), T(Y, "GE", 1)), "PERCENT", L.MEDIUM)
_rule("MATERIAL_REGULATORY_ACTION", "Material regulatory / exchange action filed",
      C.REGULATORY, ("Box 12 REGULATORY / EXCHANGE_PENALTY event materiality",
                     "ExchangeEventAssessment ADVERSE (cited, REGULATORY only)"),
      "stated value / reference annual revenue x 100 (Box 12)",
      "all sectors; only with a stated value and a cited revenue; an exchange penalty is "
      "adverse by type, a regulatory event only with a cited ADVERSE direction", ALL,
      (T(R, "GE", 5), T(Y, "GE", 1)), "PERCENT", L.MEDIUM)
_rule("LARGE_PROMOTER_SALE", "Large promoter-category sale", C.OWNERSHIP,
      ("Box 12 insider-trading disclosures", "market capitalisation"),
      "sale value / market capitalisation x 100", "all sectors; needs a market capitalisation",
      ALL, (T(R, "GE", 2), T(Y, "GE", "0.5")), "PERCENT", L.MEDIUM)
_rule("UNEXPLAINED_BASIS_DIFFERENCE", "Unexplained standalone / consolidated difference",
      C.ACCOUNTING, ("Box 4 standalone and consolidated values (same FY)",
                     "ReconciliationReview (cited)"),
      "count of material (> 10 % of consolidated) differences in the latest fiscal year that "
      "stay unexplained after a recorded review of the explaining sources",
      "all sectors; never without a recorded review", ALL, (T(Y, "GE", 1),), "COUNT", L.MEDIUM)
# order book (order-book-driven companies)
_rule("ORDER_CANCELLATION_RATE", "Order cancellation rate", C.ORDER_BOOK,
      ("Box 12 CANCELLATION_RATE",), "cancelled value / (awarded value + revision deltas) x 100",
      "order-book-driven companies; disclosed orders only", ALL,
      (T(R, "GT", 25), T(Y, "GT", 10)), "PERCENT", L.MEDIUM)
_rule("LOW_EXECUTION_RATE", "Order-book execution rate", C.ORDER_BOOK,
      ("Box 12 EXECUTION_RATE",), "period revenue / opening order book x 100",
      "order-book-driven companies", ALL, (T(R, "LT", 10), T(Y, "LT", 20)), "PERCENT",
      L.MEDIUM)
_rule("CLIENT_CONCENTRATION", "Top-client concentration of awarded orders",
      C.CUSTOMER_CONCENTRATION, ("Box 12 TOP_CLIENT_SHARE", "Box 12 GOVERNMENT_SHARE"),
      "top client's awarded value / total awarded value x 100 (last 365 days)",
      "order-book-driven companies; graded only when government / PSU clients are < 50 % of "
      "awards - otherwise reported as information (government exposure is not a risk by "
      "itself)", ALL, (T(R, "GT", 75), T(Y, "GT", 50)), "PERCENT", L.MEDIUM)
_rule("GOVERNMENT_CONCENTRATION", "Government / PSU share of awarded orders",
      C.CUSTOMER_CONCENTRATION, ("Box 12 GOVERNMENT_SHARE",),
      "government + PSU awarded value / total awarded value x 100",
      "order-book-driven companies; information only, never a severity", ALL, (), "PERCENT",
      L.UNKNOWN)
_rule("FREQUENT_ORDER_REVISIONS", "Frequent order revisions", C.ORDER_BOOK,
      ("Box 12 order chains",), "revised confirmed chains / confirmed chains x 100",
      "order-book-driven companies; >= 5 confirmed chains", ALL, (T(Y, "GT", 25),), "PERCENT",
      L.LOW, minimum_sample=5)
_rule("STALE_ORDERS", "Old orders in the confirmed ledger", C.ORDER_BOOK,
      ("Box 12 order chains",),
      "confirmed chains first confirmed > 5 years before as_of / confirmed chains x 100",
      "order-book-driven companies; >= 5 confirmed chains with a confirmation date", ALL,
      (T(Y, "GT", 25),), "PERCENT", L.LOW, minimum_sample=5)
# positive controls (never erase a flag)
P = SignalClass.POSITIVE_CONTROL
_rule("STRONG_CASH_CONVERSION", "Strong cumulative cash conversion", C.CASH_FLOW,
      ("Box 5 CUMULATIVE_CFO_TO_PAT_5Y",), "sum(CFO, 5 FY) / sum(PAT, 5 FY) >= 1",
      "non-financial", NONFIN, (T(SV.GREEN, "GE", 1),), "TIMES", L.UNKNOWN, signal_class=P)
_rule("POSITIVE_FCF_HISTORY", "Positive free cash flow history", C.CASH_FLOW,
      ("Box 5 FCF (last 5 FY)",), "FCF > 0 in >= 4 of the last 5 FY (all 5 available)",
      "non-financial", NONFIN, (T(SV.GREEN, "GE", 4),), "YEARS", L.UNKNOWN, signal_class=P)
_rule("NET_CASH", "Net cash balance sheet", C.LEVERAGE, ("Box 5 NET_DEBT",),
      "gross debt - cash and cash equivalents < 0", "non-financial", NONFIN,
      (T(SV.GREEN, "LT", 0),), "MONEY", L.UNKNOWN, signal_class=P)
_rule("STRONG_INTEREST_COVER", "Strong interest cover", C.LEVERAGE,
      ("Box 5 INTEREST_COVERAGE",), "EBIT / finance cost >= 8", "non-financial", NONFIN,
      (T(SV.GREEN, "GE", 8),), "TIMES", L.UNKNOWN, signal_class=P)
_rule("DECLINING_LEVERAGE", "Declining leverage", C.LEVERAGE,
      ("Box 5 DEBT_TO_EQUITY (latest, 2 FY earlier)",),
      "debt / equity fell by >= 0.1x over 2 fiscal years", "non-financial", NONFIN,
      (T(SV.GREEN, "LE", "-0.1"),), "TIMES", L.UNKNOWN, signal_class=P)
_rule("IMPROVING_DEBTOR_DAYS", "Improving debtor days", C.WORKING_CAPITAL,
      ("Box 5 RECEIVABLE_DAYS",), "debtor days(latest) - mean(3 preceding FY) <= -10",
      "non-financial", NONFIN, (T(SV.GREEN, "LE", -10),), "DAYS", L.UNKNOWN, signal_class=P)
_rule("NO_PLEDGE", "No promoter pledge", C.OWNERSHIP, ("PledgePoint (cited)",),
      "pledged share of promoter holding = 0 (latest quarter)", "all sectors", ALL,
      (T(SV.GREEN, "EQ", 0),), "PERCENT", L.UNKNOWN, signal_class=P)
_rule("STABLE_PROMOTER_HOLDING", "Stable promoter holding", C.OWNERSHIP,
      ("Box 12 shareholding filings",), "|promoter % change over 4 quarters| < 0.5 pp",
      "all sectors", ALL, (T(SV.GREEN, "LT", "0.5"),), "PERCENTAGE_POINTS", L.UNKNOWN,
      signal_class=P)
_rule("CONSISTENT_GUIDANCE_DELIVERY", "Consistent guidance delivery", C.MANAGEMENT,
      ("Box 8 guidance outcomes",), "(MET + BEAT) / completed x 100 >= 80",
      "all sectors", ALL, (T(SV.GREEN, "GE", 80),), "PERCENT", L.UNKNOWN, signal_class=P,
      minimum_sample=5)
_rule("RATING_UPGRADE", "Credit-rating upgrade", C.LIQUIDITY,
      ("RatingInput (cited)", "Box 12 credit-rating filings"),
      "an upgrade (by notches or the agency's stated action) in the last 3 years",
      "all sectors", ALL, (T(SV.GREEN, "GE", 1),), "NOTCHES", L.UNKNOWN, signal_class=P)

#: Formulas of Box 13 metrics that are not rules (forensic table, adjusted values, models).
FORMULAS: dict[str, FormulaDefinition] = {}


def _formula(code: str, name: str, inputs: tuple[str, ...], text: str, unit: str,
             applicability: str = "non-financial") -> None:
    assert code not in FORMULAS, code
    FORMULAS[code] = FormulaDefinition(
        formula_id=f"DD_{code}/1", code=code, name=name, inputs=inputs, formula_text=text,
        applicability=applicability, unit=unit, version=1,
    )


_formula("RECEIVABLES_CAGR_3Y", "Receivables CAGR (3Y)", ("Box 4 TRADE_RECEIVABLES",),
         "((receivables(t) / receivables(t-3)) ^ (1/3) - 1) x 100 (Box 5 cagr)", "PERCENT")
_formula("RECEIVABLES_CAGR_GAP_3Y", "Receivables CAGR - revenue CAGR (3Y)",
         ("RECEIVABLES_CAGR_3Y", "Box 5 REVENUE_CAGR_3Y"), "receivables CAGR - revenue CAGR",
         "PERCENTAGE_POINTS")
_formula("DEBTOR_DAYS_VS_3Y_AVG", "Debtor days - 3Y average",
         ("Box 5 RECEIVABLE_DAYS",), "days(t) - mean(days(t-1), days(t-2), days(t-3))", "DAYS")
_formula("INVENTORY_DAYS_VS_3Y_AVG", "Inventory days vs 3Y average",
         ("Box 5 INVENTORY_DAYS",), "(days(t) / mean(days(t-1..t-3)) - 1) x 100", "PERCENT")
_formula("CUMULATIVE_CFO_TO_EBITDA_5Y", "Cumulative CFO / EBITDA (5Y)",
         ("Box 4 CFO", "Box 5 EBITDA"), "sum(CFO, 5 FY) / sum(EBITDA, 5 FY); sum EBITDA > 0",
         "TIMES")
_formula("EXCEPTIONAL_TO_PAT", "Exceptional items / PAT", ("Box 4 EXCEPTIONAL_ITEMS",
                                                          "Box 4 PAT"),
         "|exceptional items| / PAT x 100; PAT > 0", "PERCENT")
_formula("EXCEPTIONAL_YEARS_5Y", "Years with exceptional items (5Y)",
         ("Box 4 EXCEPTIONAL_ITEMS",), "count(FY with non-zero exceptional items)", "YEARS",
         "all sectors")
_formula("PROFIT_WITHOUT_CASH_YEARS_5Y", "Years with PAT > 0 and CFO < 0 (5Y)",
         ("Box 4 PAT", "Box 4 CFO"), "count(FY with PAT > 0 and CFO < 0)", "YEARS")
_formula("FINANCE_COST_TO_AVERAGE_DEBT", "Cost of debt", ("Box 4 FINANCE_COST",
                                                          "Box 5 GROSS_DEBT"),
         "finance cost / mean(gross debt opening, closing) x 100; mean debt > 0", "PERCENT")
_formula("CWIP_GROWTH_YOY", "CWIP growth", ("Box 4 CWIP",), "(CWIP(t) / CWIP(t-1) - 1) x 100",
         "PERCENT")
_formula("GOODWILL_INTANGIBLES_TO_NET_WORTH", "Goodwill + intangibles / net worth",
         ("Box 4 GOODWILL", "Box 4 INTANGIBLE_ASSETS", "Box 5 SHAREHOLDERS_EQUITY"),
         "(goodwill + intangibles) / net worth x 100", "PERCENT", "all sectors")
_formula("POSITIVE_FCF_YEARS_5Y", "Years with positive FCF (5Y)", ("Box 5 FCF",),
         "count(FY with FCF > 0)", "YEARS")
_formula("SHARE_CAPITAL_GROWTH_3Y", "Equity share capital growth (3Y)",
         ("Box 4 EQUITY_SHARE_CAPITAL",), "(capital(t) / capital(t-3) - 1) x 100", "PERCENT",
         "all sectors")
_formula("DEBT_FUNDED_GROWTH_3Y", "Debt-funded asset growth (3Y)",
         ("Box 5 GROSS_DEBT", "Box 4 TOTAL_ASSETS"),
         "(gross debt(t) - gross debt(t-3)) / (total assets(t) - total assets(t-3)) x 100; "
         "asset growth > 0", "PERCENT")
_formula("SHORT_TERM_DEBT_SHARE", "Short-term debt / gross debt",
         ("Box 4 SHORT_TERM_BORROWINGS", "Box 5 GROSS_DEBT"), "short-term / gross x 100",
         "PERCENT")
_formula("NET_DEBT_TO_OPERATING_EBITDA", "Net debt / operating EBITDA",
         ("Box 5 NET_DEBT", "Box 4 OPERATING_PROFIT"), "net debt / operating EBITDA", "TIMES")
_formula("WORKING_CAPITAL_ABSORPTION", "Working-capital absorption",
         ("Box 4 WORKING_CAPITAL_CHANGES", "Box 4 CFO_BEFORE_WORKING_CAPITAL"),
         "-(working-capital changes) / CFO before working capital x 100; base > 0", "PERCENT")
_formula("ADJUSTED_PAT_EX_EXCEPTIONALS", "PAT excluding exceptional items (tax-effected)",
         ("Box 4 PAT", "Box 4 EXCEPTIONAL_ITEMS", "Box 5 TAX_RATE"),
         "PAT - exceptional items x (1 - effective tax rate / 100)", "MONEY", "all sectors")
_formula("ADJUSTED_PAT_EX_OTHER_INCOME", "PAT excluding other income (tax-effected)",
         ("Box 4 PAT", "Box 4 OTHER_INCOME", "Box 5 TAX_RATE"),
         "PAT - other income x (1 - effective tax rate / 100)", "MONEY")
_formula("TAX_NORMALIZED_PAT", "PAT at the median historical tax rate",
         ("Box 4 PBT", "Box 5 TAX_RATE (last 5 FY)"),
         "PBT x (1 - median(ETR of FY with 0 <= ETR <= 60, >= 3 years) / 100)", "MONEY",
         "all sectors")
_formula("ALTMAN_Z_ORIGINAL", "Altman Z (1968, public manufacturers)",
         ("Box 5 Altman X1, X3, X5", "retained earnings (cited)", "market cap (cited or "
          "Box 12 price x cited shares)", "Box 5 TOTAL_LIABILITIES"),
         "1.2 X1 + 1.4 X2 + 3.3 X3 + 0.6 X4 + 1.0 X5; > 2.99 SAFE, 1.81-2.99 GREY, < 1.81 "
         "DISTRESS", "SCORE")
_formula("CONTRACT_ITEM_TO_REVENUE", "Contract item / revenue",
         ("WorkingCapitalDisclosure (cited)", "Box 4 revenue"), "item / revenue x 100",
         "PERCENT")

POLICIES: tuple[tuple[str, str], ...] = (
    ("ANALYTICAL_BASIS", "CONSOLIDATED when Box 4 holds consolidated values, else STANDALONE "
     "(recorded); standalone is kept as the forensic / control layer; no calculation mixes "
     "the two"),
    ("MISSING_VALUES", "a missing input is UNAVAILABLE, never zero; a count over available "
     "years is a lower bound that can confirm a breach but never a clean reading"),
    ("SEVERITY", "YELLOW / RED only with valid inputs, an applicable formula, a crossed "
     "threshold and evidence; otherwise UNSUPPORTED / UNAVAILABLE"),
    ("POSITIVE_CONTROLS", "listed beside flags; never change a flag's severity"),
    ("DATA_LIMITATION", "data gaps are listed in the data-quality summary, never as company "
     "risk"),
    ("SECTOR_GATING", "industrial rules (cash conversion, working capital, EBITDA leverage, "
     "Altman, Beneish, Piotroski) are UNSUPPORTED_SECTOR_RULE for banks, NBFCs and insurers"),
    ("RISK_MAP_LIKELIHOOD", "PERSISTENCE: breaches in the latest and 2 prior fiscal years - 3 "
     "HIGH, 2 MEDIUM, 1 LOW; UNKNOWN when a prior year cannot be evaluated or the rule is "
     "not periodic"),
    ("RISK_MAP_IMPACT", "the impact registered with the rule (static, documented)"),
    ("RECONCILIATION_MATERIALITY", "a standalone / consolidated difference is material above "
     "10 % of the consolidated value"),
    ("EVENT_MATERIALITY", "an exchange event is graded only when it is adverse to the company "
     "AND numerically material (Box 12 stated value / reference revenue: >= 1 % YELLOW, >= 5 % "
     "RED). Adverse by type: order cancellations and exchange penalties. Litigation and "
     "regulatory events need a cited ADVERSE direction (an event category and a large value "
     "never establish one); awards, orders, dividends, fund raises, buybacks, M&A / JV are "
     "never graded. Otherwise INFORMATIONAL, with a diligence item when material"),
    ("INPUT_CONFLICTS", "explicit inputs: exact duplicates count once; records are keyed by "
     "the fact they state (period, basis, category, instrument ..., never the value); equal "
     "content from several sources is one observation; different content is resolved by "
     "AUTHORITY_RANK only when one content holds the best rank (not an analyst input), with "
     "every value preserved; otherwise the disagreement stays OPEN and the rules that need "
     "it are UNAVAILABLE (INPUT_CONFLICT). Input order never matters"),
    ("RECORD_IDENTITY", "an upstream record ID seen twice with identical content is one "
     "record; with different content the build is refused (RECORD_ID_CONFLICT)"),
    ("GUIDANCE_SAMPLE", "management reliability needs >= 5 completed quantitative targets"),
    ("RATINGS", "Indian long-term scale only (AAA..D, +/-), agency prefixes stripped; "
     "structured / credit-enhanced (SO / CE) and short-term grades are not mapped"),
    ("NO_ADVICE", "Box 13 never rates the security, recommends, sizes or sets a price"),
)


def manifest() -> DueDiligenceManifest:
    return DueDiligenceManifest(
        engine_version=DUE_DILIGENCE_ENGINE_VERSION,
        rule_registry_version=RULE_REGISTRY_VERSION,
        formula_registry_version=FORMULA_REGISTRY_VERSION,
        risk_map_rules_version=RISK_MAP_RULES_VERSION,
        rules=tuple(RULES.values()),
        formulas=tuple(FORMULAS.values()),
        policies=POLICIES,
    )


def holds(value: Decimal, t: Threshold) -> bool:
    return {
        "GT": value > t.value,
        "GE": value >= t.value,
        "LT": value < t.value,
        "LE": value <= t.value,
        "EQ": value == t.value,
    }[t.comparison]


def grade(value: Decimal, thresholds: Sequence[Threshold]) -> Severity:
    """The worst severity whose threshold holds; GREEN when none does."""
    for severity in (SV.RED, SV.YELLOW):
        if any(t.severity == severity and holds(value, t) for t in thresholds):
            return severity
    return SV.GREEN


def worse(a: Severity, b: Severity) -> Severity:
    order = [SV.GREEN, SV.YELLOW, SV.RED]
    return a if order.index(a) >= order.index(b) else b


def q(value: Decimal) -> Decimal:
    return quantize(value)


# --- Box 5 metric view ----------------------------------------------------------------------


class Box5View:
    """Box 5 metrics by (basis, code, Box 4 period key), read like the Box 5 engine reads them."""

    def __init__(self, box5: FinancialAnalyticsDataset) -> None:
        self.index: dict[tuple[ReportingBasis, str, str], CalculatedMetric] = {}
        for m in box5.all_metrics():
            self.index[(m.reporting_basis, m.code, m.period.key())] = m

    def got(self, code: str, basis: ReportingBasis, period: FinancialPeriod) -> Got:
        m = self.index.get((basis, code, period.key()))
        if m is None:
            return Missing(CalculationStatus.INSUFFICIENT_HISTORY, f"{code} @ {period.label}")
        if m.value is None or m.confidence is None:
            # the metric's OWN status (Box 5 relabels it INPUT_MISSING only for dependants)
            return Missing(m.status, f"{code} @ {period.label} ({m.status.value})",
                           m.provenance, (m.metric_id,))
        return Fact(m.value, m.provenance, m.confidence, m.warnings, (m.metric_id,), m.formula_id)

    def metric(
        self, code: str, basis: ReportingBasis, period: FinancialPeriod
    ) -> CalculatedMetric | None:
        return self.index.get((basis, code, period.key()))


# --- evaluation trace -------------------------------------------------------------------------


AUTHORITY_CONFIDENCE = {
    SourceAuthority.EXCHANGE_FILING: Confidence.HIGH,
    SourceAuthority.COMPANY_DOCUMENT: Confidence.HIGH,
    SourceAuthority.RATING_AGENCY: Confidence.HIGH,
    SourceAuthority.MARKET_PROVIDER: Confidence.HIGH,
    SourceAuthority.SCREENER: Confidence.MEDIUM,
    SourceAuthority.ANALYST_INPUT: Confidence.LOW,
}


def source_label(source: DiligenceSource) -> str:
    return (source.url or source.reference)[:1000]


@dataclass
class Trace:
    inputs: list[SignalInput] = field(default_factory=list)
    record_ids: set[UUID] = field(default_factory=set)
    evidence_ids: set[UUID] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    confidences: list[Confidence] = field(default_factory=list)

    def merge(self, other: "Trace") -> None:
        self.inputs.extend(other.inputs)
        self.record_ids |= other.record_ids
        self.evidence_ids |= other.evidence_ids
        self.source_ids |= other.source_ids
        self.confidences.extend(other.confidences)


def basis_of(basis: ReportingBasis | str | None) -> SignalBasis:
    value = basis.value if isinstance(basis, ReportingBasis) else basis
    if value in ("CONSOLIDATED", "STANDALONE"):
        return SignalBasis(value)
    return SignalBasis.NOT_APPLICABLE


@dataclass
class Ev:
    """One rule evaluation, before it becomes a RiskSignal."""

    status: SignalStatus
    basis: SignalBasis
    severity: Severity | None = None
    value: Decimal | None = None
    reason: str | None = None
    period_label: str | None = None
    period_end: date | None = None
    trace: Trace = field(default_factory=Trace)
    calculation: str = ""
    explanation: str = ""
    issues: list[str] = field(default_factory=list)
    discriminator: str = ""
    thresholds: tuple[Threshold, ...] | None = None  # a cited override of the registry
    unit: str | None = None


def unavailable(basis: SignalBasis, reason: str, *, status: SignalStatus = S.UNAVAILABLE,
                period: FinancialPeriod | None = None, trace: Trace | None = None,
                explanation: str = "", value: Decimal | None = None) -> Ev:
    return Ev(
        status=status,
        basis=basis,
        reason=_code(reason),
        period_label=period.label if period else None,
        period_end=period.period_end if period else None,
        trace=trace or Trace(),
        explanation=explanation or reason,
        value=value,
    )


def _code(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text.upper())
    parts = [p for p in cleaned.split("_") if p]
    code = "_".join(parts)[:80] or "UNSPECIFIED"
    return code if code[0].isalpha() else f"X_{code}"[:80]


def missing_reason(got: Missing) -> tuple[SignalStatus, str]:
    if got.status == CalculationStatus.NOT_APPLICABLE:
        return S.NOT_APPLICABLE, "NOT_APPLICABLE"
    if got.status == CalculationStatus.DIVISION_BY_ZERO:
        return S.NOT_APPLICABLE, "ZERO_DENOMINATOR"
    if got.status == CalculationStatus.UNSUPPORTED_SECTOR:
        return S.UNSUPPORTED_SECTOR_RULE, "UNSUPPORTED_SECTOR"
    return S.UNAVAILABLE, got.status.value


# --- context ----------------------------------------------------------------------------------


@dataclass
class Ctx:
    box4: CanonicalFinancialDataset
    box5: FinancialAnalyticsDataset
    request: DueDiligenceRequest
    frames: dict[ReportingBasis, Frame]
    currency: Currency
    analytical: ReportingBasis
    control: ReportingBasis | None
    periods: Periods
    latest: dict[ReportingBasis, date | None]
    financial: bool
    b5: Box5View
    obs_source: dict[UUID, str]
    dd_key: str
    evidence_index: dict[UUID, tuple[UUID, str, int]] = field(default_factory=dict)
    signals: list[RiskSignal] = field(default_factory=list)
    persistence: dict[UUID, list[Severity | None]] = field(default_factory=dict)
    issues: list[DueDiligenceIssue] = field(default_factory=list)
    gated: set[str] = field(default_factory=set)
    inputs: "NormalizedInputs | None" = None  # the input policy's conflicts (diligence_inputs)

    # facts --------------------------------------------------------------------------------

    @property
    def gate(self) -> SectorGate:
        return G.FINANCIAL if self.financial else G.NON_FINANCIAL

    def canonical(self, metrics: Sequence[object], period: FinancialPeriod,
                  basis: ReportingBasis | None = None) -> Got:
        frame = self.frames.get(basis or self.analytical)
        if frame is None:
            return Missing(CalculationStatus.INSUFFICIENT_HISTORY, "basis not in Box 4")
        return frame.get(metrics, period)  # type: ignore[arg-type]

    def m5(self, code: str, period: FinancialPeriod, basis: ReportingBasis | None = None) -> Got:
        return self.b5.got(code, basis or self.analytical, period)

    def add(self, trace: Trace, role: str, got: Got, unit: str, layer: Layer,
            basis: ReportingBasis | None = None) -> None:
        """Record a Box 4 / Box 5 input (a Fact, or a Missing that still names its records)."""
        b = basis_of(basis or self.analytical)
        value = got.value if isinstance(got, Fact) else None
        period = next((i.period for i in got.inputs), None)
        trace.inputs.append(SignalInput(
            role=role, value=q(value) if value is not None else None, unit=unit,
            period=period.label if period else None,
            period_end=period.period_end if period else None, basis=b, layer=layer,
            record_id=got.derived_from[0] if got.derived_from else (
                got.inputs[0].canonical_value_id if got.inputs else None),
            detail=None if isinstance(got, Fact) else got.what[:1000],
        ))
        trace.record_ids |= set(got.derived_from)
        for i in got.inputs:
            trace.record_ids.add(i.canonical_value_id)
            trace.evidence_ids.add(i.selected_observation_id)
            source = self.obs_source.get(i.selected_observation_id)
            if source:
                trace.source_ids.add(source)
        if isinstance(got, Fact):
            trace.confidences.append(got.confidence)

    def explicit(self, trace: Trace, role: str, value: Decimal | None, unit: str,
                 record_id: UUID, source: DiligenceSource, *, basis: SignalBasis | None = None,
                 period: str | None = None, period_end: date | None = None) -> None:
        trace.inputs.append(SignalInput(
            role=role, value=q(value) if value is not None else None, unit=unit, period=period,
            period_end=period_end, basis=basis or SignalBasis.NOT_APPLICABLE,
            layer=Layer.REQUEST, record_id=record_id, detail=source.reference[:1000],
        ))
        trace.record_ids.add(record_id)
        trace.evidence_ids |= set(source.evidence_ids)
        trace.source_ids.add(source_label(source))
        trace.confidences.append(AUTHORITY_CONFIDENCE[source.authority])

    def record(self, trace: Trace, role: str, value: Decimal | None, unit: str, layer: Layer,
               record_id: UUID, source_id: str | None, *, period: str | None = None,
               period_end: date | None = None, evidence: Iterable[UUID] = (),
               confidence: Confidence = Confidence.HIGH, detail: str | None = None) -> None:
        trace.inputs.append(SignalInput(
            role=role, value=q(value) if value is not None else None, unit=unit, period=period,
            period_end=period_end, basis=SignalBasis.NOT_APPLICABLE, layer=layer,
            record_id=record_id, detail=detail[:1000] if detail else None,
        ))
        trace.record_ids.add(record_id)
        trace.evidence_ids |= set(evidence)
        if source_id:
            trace.source_ids.add(source_id[:1000])
        trace.confidences.append(confidence)

    # signals ------------------------------------------------------------------------------

    def applies(self, code: str) -> bool:
        return self.gate in RULES[code].sectors

    def emit(self, code: str, ev: Ev, history: list[Severity | None] | None = None) -> UUID:
        rule = RULES[code]
        thresholds = ev.thresholds if ev.thresholds is not None else rule.thresholds
        status, severity, reason = ev.status, ev.severity, ev.reason
        issues = list(ev.issues)
        if self.gate not in rule.sectors:
            status, severity, reason = S.UNSUPPORTED_SECTOR_RULE, None, "UNSUPPORTED_SECTOR_RULE"
            self.gated.add(code)
        trace = ev.trace
        if severity in (SV.YELLOW, SV.RED) and not (
            trace.inputs and (trace.record_ids or trace.evidence_ids) and trace.source_ids
        ):
            status, severity, reason = S.UNSUPPORTED, None, "NO_EVIDENCE"
            issues.append("THRESHOLD_CROSSED_WITHOUT_EVIDENCE")
        if status != S.EVALUATED:
            severity = None
        elif reason is not None:
            reason = None
        confidence = weakest(trace.confidences) if trace.confidences else None
        risk_id = uuid5(
            NS, f"{self.dd_key}|{rule.formula_id}|{ev.basis.value}|{ev.period_end}|"
            f"{ev.discriminator}",
        )
        early = None
        if (
            rule.signal_class == SignalClass.COMPANY_RISK
            and status == S.EVALUATED
            and ev.value is not None
            and thresholds
        ):
            bands = "; ".join(
                f"{t.severity.value} when {t.comparison} {t.value}"
                + (f" ({t.note})" if t.note else "") for t in thresholds
            )
            early = f"{rule.code} is {q(ev.value)} {ev.unit or rule.unit}: {bands}"
        if rule.signal_class == SignalClass.POSITIVE_CONTROL and (
            status != S.EVALUATED
            or severity != SV.GREEN
            or ev.value is None
            or not all(holds(ev.value, t) for t in thresholds)
        ):
            return risk_id  # an absent or unsupported positive control is not listed
        explanation = ev.explanation or rule.name
        signal = RiskSignal(
            risk_id=risk_id,
            rule_id=rule.formula_id,
            code=code,
            signal_class=rule.signal_class,
            category=rule.category,
            title=rule.name,
            status=status,
            severity=severity,
            metric=code,
            value=q(ev.value) if ev.value is not None else None,
            unit=ev.unit or rule.unit,
            threshold=thresholds,
            period=ev.period_label,
            period_end=ev.period_end,
            basis=ev.basis,
            formula_id=rule.formula_id,
            calculation=(ev.calculation or rule.formula_text)[:1000],
            inputs=tuple(trace.inputs),
            evidence_ids=tuple(sorted(trace.evidence_ids, key=str)),
            record_ids=tuple(sorted(trace.record_ids, key=str)),
            source_ids=tuple(sorted(trace.source_ids)),
            explanation=explanation[:1000],
            early_warning=early[:1000] if early else None,
            confidence=confidence,
            issues=tuple(sorted(set(issues))),
            reason=reason,
        )
        if any(s.risk_id == risk_id for s in self.signals):
            raise ValueError(f"duplicate signal {code} {ev.discriminator}")
        self.signals.append(signal)
        if history is not None:
            self.persistence[risk_id] = history
        return risk_id

    def present(self, ids: Iterable[UUID]) -> tuple[UUID, ...]:
        known = {s.risk_id for s in self.signals}
        return tuple(i for i in ids if i in known)


def periodic(
    ctx: Ctx,
    code: str,
    fn: Callable[[Ctx, date], Ev],
    latest: date | None,
    shift_months: Callable[[date, int], date],
) -> UUID:
    """Evaluate a period rule at the latest FY, and at the two before it for the risk map."""
    if latest is None:
        return ctx.emit(code, unavailable(basis_of(ctx.analytical), "NO_FISCAL_YEAR"))
    ev = fn(ctx, latest)
    history = [ev.severity if ev.status == S.EVALUATED else None]
    for k in (1, 2):
        prior = fn(ctx, shift_months(latest, -12 * k))
        history.append(prior.severity if prior.status == S.EVALUATED else None)
    return ctx.emit(code, ev, history)
