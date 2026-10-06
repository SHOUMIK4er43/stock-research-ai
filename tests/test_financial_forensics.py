"""DETAILED_RESEARCH_V1 Box 5: earnings-quality checks, anomalies and forensic models (offline).

Fixtures Q (incomplete Beneish), R (incomplete Piotroski) and S (Altman not applicable),
plus every Piotroski / Beneish component, the earnings-quality rules and the anomaly rules.
"""

import json
from decimal import Decimal

from test_financial_analytics import (
    FY26,
    A,
    M,
    analyse,
    company,
    metric,
    q,
    replace,
)

from stock_research_ai.application.financial_forensics import BENEISH_WEIGHTS
from stock_research_ai.application.financial_rules import (
    ANOMALY_RULES,
    EARNINGS_QUALITY_RULES,
    GradedRule,
    rule_definitions,
)
from stock_research_ai.domain.financial_analytics import (
    AnalyticsUnit,
    CalculationStatus,
    ComponentStatus,
    ForensicModel,
    Severity,
)
from stock_research_ai.domain.financial_canonical import Industry

S = CalculationStatus
YEARS = range(2021, 2027)


def model(a, kind, variant=None):
    return next(
        r
        for r in a.forensic_scores
        if r.model == kind and (variant is None or r.variant == variant)
    )


def check(a, code):
    return next(c for c in a.earnings_quality if c.code == code)


def anomalies(a, code):
    return [x for x in a.anomalies if x.code == code]


# --- Piotroski ------------------------------------------------------------------------------------


def test_piotroski_all_components():
    f = model(A, ForensicModel.PIOTROSKI_F_SCORE)
    assert (f.status, f.components_available, f.score_denominator) == (S.CALCULATED, 9, 9)
    status = {c.name: c.status for c in f.components}
    assert status == {
        "F1_ROA_POSITIVE": ComponentStatus.PASS,
        "F2_CFO_POSITIVE": ComponentStatus.PASS,
        "F3_ROA_IMPROVED": status["F3_ROA_IMPROVED"],
        "F4_CFO_EXCEEDS_PAT": ComponentStatus.PASS,
        "F5_LEVERAGE_DECREASED": ComponentStatus.PASS,  # debt flat, assets up
        "F6_CURRENT_RATIO_IMPROVED": status["F6_CURRENT_RATIO_IMPROVED"],
        "F7_NO_EQUITY_ISSUANCE": ComponentStatus.PASS,
        "F8_GROSS_MARGIN_IMPROVED": status["F8_GROSS_MARGIN_IMPROVED"],
        "F9_ASSET_TURNOVER_IMPROVED": status["F9_ASSET_TURNOVER_IMPROVED"],
    }
    assert f.score == Decimal(f.components_passed) and f.availability_label == (
        f"{f.components_passed} / 9"
    )
    roa = metric(A, "ROA").value
    roa_prior = metric(A, "ROA", FY26.replace(year=2025)).value
    assert status["F3_ROA_IMPROVED"] == (
        ComponentStatus.PASS if roa > roa_prior else ComponentStatus.FAIL
    )
    for component in f.components:
        assert component.canonical_value_ids and component.input_values


def test_r_partial_piotroski_never_scores_out_of_nine():
    a = analyse(company(**{"RAW_MATERIAL_COST": None}))
    f = model(a, ForensicModel.PIOTROSKI_F_SCORE)
    assert (f.status, f.score, f.zone) == (S.INPUT_MISSING, None, None)
    assert (f.components_available, f.score_denominator) == (8, 8)
    assert f.availability_label == f"{f.components_passed} / 8 available"
    gross = next(c for c in f.components if c.name == "F8_GROSS_MARGIN_IMPROVED")
    assert gross.status == ComponentStatus.UNAVAILABLE and gross.missing_inputs
    assert f.coverage == q(Decimal(8) / 9)


# --- Altman ---------------------------------------------------------------------------------------


def test_altman_variants():
    original = model(A, ForensicModel.ALTMAN_Z, "ORIGINAL_1968")
    assert (original.status, original.score) == (S.INPUT_MISSING, None)
    market = next(c for c in original.components if c.name == "X4_MARKET")
    assert market.status == ComponentStatus.UNAVAILABLE and "market" in market.missing_inputs[0]
    double = model(A, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995")
    x = {c.name: c.value for c in double.components}
    assert x["X1"] == q(Decimal(600) / 2750)
    assert x["X4_BOOK"] == q(Decimal(1500) / 1250)
    expected = (
        Decimal("6.56") * x["X1"]
        + Decimal("3.26") * x["X2"]
        + Decimal("6.72") * x["X3"]
        + Decimal("1.05") * x["X4_BOOK"]
    )
    assert double.score == q(expected) and double.zone == "SAFE_ZONE"


def test_s_altman_not_applicable_to_banks():
    a = analyse(company(), industry=Industry.BANK)
    for variant in ("ORIGINAL_1968", "Z_DOUBLE_PRIME_1995"):
        result = model(a, ForensicModel.ALTMAN_Z, variant)
        assert (result.status, result.score, result.components_available) == (
            S.UNSUPPORTED_SECTOR,
            None,
            0,
        )


# --- Beneish ---------------------------------------------------------------------------


def test_beneish_components():
    m = model(A, ForensicModel.BENEISH_M)
    assert m.status == S.CALCULATED and m.components_available == 8
    c = {x.name: x.value for x in m.components}
    assert c["SGI"] == q(Decimal(1500) / 1400)
    assert c["DSRI"] == q((Decimal(325) / 1500) / (Decimal(300) / 1400))
    assert c["GMI"] == q(((Decimal(1400) - 560) / 1400) / ((Decimal(1500) - 590) / 1500))
    assert c["DEPI"] == q((Decimal(40) / (40 + 720)) / (Decimal(40) / (40 + 750)))
    assert c["SGAI"] == q((Decimal(70) / 1500) / (Decimal(70) / 1400))
    assert c["TATA"] == q(Decimal(-30) / 2750)
    assert c["LVGI"] == q(((Decimal(500) + 200) / 2750) / ((Decimal(480) + 200) / 2600))
    score = Decimal("-4.84") + sum(BENEISH_WEIGHTS[n] * v for n, v in c.items())
    assert m.score == q(score)
    assert m.zone in ("ABOVE_SCREENING_THRESHOLD", "BELOW_SCREENING_THRESHOLD")
    assert "manipulation" in m.applicability and "never" in m.applicability


def test_q_incomplete_beneish():
    a = analyse(company(**{"SELLING_EXPENSE": None, "LONG_TERM_BORROWINGS": None}))
    m = model(a, ForensicModel.BENEISH_M)
    assert (m.status, m.score, m.zone, m.components_available) == (S.INPUT_MISSING, None, None, 6)
    missing = {x.name for x in m.components if x.status == ComponentStatus.UNAVAILABLE}
    assert missing == {"SGAI", "LVGI"}
    assert m.availability_label == "6 / 8 components available"


# --- earnings quality ---------------------------------------------------------------------------


def test_checks_have_rules_inputs_and_neutral_wording():
    checks = {c.code: c for c in A.earnings_quality}
    assert set(checks) == {r.code for r in EARNINGS_QUALITY_RULES} | {"REPEATED_NEGATIVE_FCF"}
    for c in A.earnings_quality:
        assert c.rule_id.startswith("EARNINGS_QUALITY/") and c.rule_id.endswith("/2")
        if c.status == S.CALCULATED:
            assert c.severity is not None and c.observed is not None
    assert checks["CFO_VS_PAT"].severity == Severity.INFO
    assert checks["OTHER_INCOME_DEPENDENCE"].severity == Severity.INFO
    assert checks["CFO_TO_EBITDA"].severity == Severity.INFO
    text = json.dumps([c.observation for c in A.earnings_quality]).lower()
    for word in ("fraud", "manipulation", "buy", "sell"):
        assert word not in text


def test_accrual_check():
    rows = replace(
        company(), M.CFO, {y: Decimal(270 + 40 * (y - 2021)) * Decimal("0.75") - 200 for y in YEARS}
    )
    c = check(analyse(rows), "ACCRUAL_RATIO")
    assert c.severity == Severity.MEDIUM  # 200 / 2675 = 7.5 % of average assets
    assert c.observed == q(Decimal(200) * 100 / 2675)


def test_receivables_vs_revenue_divergence():
    rows = replace(
        company(),
        M.TRADE_RECEIVABLES,
        {2021: 200, 2022: 225, 2023: 250, 2024: 275, 2025: 300, 2026: 400},
    )
    a = analyse(rows)
    c = check(a, "RECEIVABLES_VS_REVENUE")
    assert c.severity == Severity.HIGH and c.canonical_value_ids
    assert c.observed == q(q(Decimal(100) / 3) - q((Decimal(1500) / 1400 - 1) * 100))
    assert anomalies(a, "RECEIVABLE_DAYS_SPIKE")


def test_cfo_vs_pat_divergence():
    rows = replace(company(), M.CFO, {y: 300 for y in YEARS})
    rows = replace(rows, M.CFO, {**{y: 300 for y in YEARS}, 2026: 150})
    a = analyse(rows)
    assert check(a, "CFO_VS_PAT").severity == Severity.HIGH  # 150 / 352.5 < 0.5
    assert check(a, "PROFIT_GROWTH_WITHOUT_CASH").severity == Severity.HIGH
    assert check(a, "PAT_VS_CFO_GROWTH").severity == Severity.HIGH
    revenue_up_cfo_down = anomalies(a, "REVENUE_UP_CFO_DOWN")
    assert revenue_up_cfo_down and revenue_up_cfo_down[-1].severity == Severity.HIGH


def test_other_income_and_exceptional_item_dependence():
    rows = replace(company(), M.OTHER_INCOME, {y: 30 for y in YEARS} | {2026: 200})
    a = analyse(rows + replace([], M.EXCEPTIONAL_ITEMS, {2026: -80}))
    other = check(a, "OTHER_INCOME_DEPENDENCE")
    assert other.severity == Severity.HIGH and other.observed == q(Decimal(200) * 100 / 470)
    exceptional = check(a, "EXCEPTIONAL_ITEM_DEPENDENCE")
    assert exceptional.severity == Severity.MEDIUM  # |-80| / 470 = 17 %
    assert anomalies(a, "OTHER_INCOME_MATERIAL") and anomalies(a, "EXCEPTIONAL_ITEMS_MATERIAL")


def test_tax_anomalies():
    taxes = {y: Decimal(270 + 40 * (y - 2021)) / 4 for y in YEARS} | {2026: Decimal(47)}
    a = analyse(replace(company(), M.TOTAL_TAX, taxes))
    change = anomalies(a, "TAX_RATE_CHANGE")
    assert change and change[-1].severity == Severity.MEDIUM  # 25 % -> 10 %: |-15| pp
    assert check(a, "TAX_RATE_INSTABILITY").severity == Severity.MEDIUM
    assert check(a, "CASH_TAX_VS_BOOK_TAX").severity == Severity.HIGH


def test_debt_rising_with_negative_fcf():
    rows = replace(company(), M.PURCHASE_OF_PPE, {y: -600 for y in YEARS})
    rows = replace(rows, M.TOTAL_BORROWINGS, {y: 300 for y in YEARS} | {2026: 600})
    a = analyse(rows)
    (signal,) = [x for x in anomalies(a, "DEBT_UP_NEGATIVE_FCF") if x.periods[0].period_end == FY26]
    assert signal.severity == Severity.HIGH and signal.threshold == Decimal(50)
    assert "FCF was negative" in signal.explanation
    assert check(a, "DEBT_GROWTH_DESPITE_PROFITS").severity == Severity.HIGH


def test_repeated_negative_fcf():
    rows = replace(company(), M.PURCHASE_OF_PPE, {y: -60 for y in YEARS} | {2025: -600, 2026: -600})
    c = check(analyse(rows), "REPEATED_NEGATIVE_FCF")
    assert (c.severity, c.observed, c.unit) == (Severity.MEDIUM, Decimal(2), AnalyticsUnit.COUNT)
    missing = check(analyse(company(**{"PURCHASE_OF_PPE": None})), "REPEATED_NEGATIVE_FCF")
    assert (missing.status, missing.severity) == (S.INPUT_MISSING, None)


def test_more_anomaly_rules():
    rows = replace(company(), M.FINANCE_COST, {y: 20 for y in YEARS} | {2026: 60})
    rows = replace(rows, M.PURCHASE_OF_PPE, {y: -60 for y in YEARS} | {2026: -200})
    a = analyse(
        replace(
            rows,
            M.REVENUE_FROM_OPERATIONS,
            {y: 1000 + 100 * (y - 2021) for y in YEARS} | {2026: 1420},
        )
    )
    coverage = anomalies(a, "INTEREST_COVERAGE_DETERIORATION")
    assert coverage and coverage[-1].severity == Severity.HIGH
    capex = anomalies(a, "CAPEX_UP_WITHOUT_GROWTH")
    assert capex and capex[-1].severity == Severity.HIGH and len(capex[-1].metric_ids) == 2
    assert not anomalies(a, "ROCE_DOWN_DESPITE_GROWTH")  # ROCE rose here: no signal
    pbt = {y: 270 + 40 * (y - 2021) for y in YEARS} | {2026: 300}
    falling = analyse(replace(company(), M.PBT, pbt))
    (roce,) = [
        x for x in anomalies(falling, "ROCE_DOWN_DESPITE_GROWTH") if x.periods[0].period_end == FY26
    ]
    assert roce.severity == Severity.MEDIUM  # 27.27 % -> 18.29 %: -8.98 pp
    assert roce.rule_id == "ANOMALY/ROCE_DOWN_DESPITE_GROWTH/2"
    assert "revenue grew" in roce.explanation


def test_dilution_divergence():
    rows = replace(company(), M.EPS, {})
    eps = {
        y: (Decimal(270 + 40 * (y - 2021)) * Decimal("0.75") / 10).quantize(Decimal("0.01"))
        for y in YEARS
    } | {2026: Decimal("30.00")}
    a = analyse(rows + replace([], M.EPS, eps))
    assert check(a, "PAT_VS_EPS_DIVERGENCE").severity in (Severity.MEDIUM, Severity.HIGH)
    assert anomalies(a, "EPS_DILUTION_DIVERGENCE")


# --- severity model ----------------------------------------------------------------------------


def test_severity_is_deterministic():
    rule = GradedRule(
        "ANOMALY",
        "X",
        "M",
        "AT_LEAST",
        Decimal(1),
        Decimal(2),
        Decimal(3),
        AnalyticsUnit.PERCENT,
        "x",
    )
    assert [rule.grade(Decimal(v)) for v in ("0.5", "1", "2", "3", "9")] == [
        Severity.INFO,
        Severity.LOW,
        Severity.MEDIUM,
        Severity.HIGH,
        Severity.HIGH,
    ]
    at_most = GradedRule(
        "ANOMALY",
        "Y",
        "M",
        "AT_MOST",
        Decimal(-1),
        Decimal(-2),
        Decimal(-3),
        AnalyticsUnit.PERCENT,
        "y",
    )
    assert at_most.grade(Decimal(-2.5)) == Severity.MEDIUM and at_most.grade(Decimal(0)) == (
        Severity.INFO
    )
    absolute = GradedRule(
        "ANOMALY",
        "Z",
        "M",
        "ABSOLUTE_AT_LEAST",
        Decimal(5),
        Decimal(10),
        Decimal(20),
        AnalyticsUnit.PERCENTAGE_POINTS,
        "z",
    )
    assert absolute.grade(Decimal(-12)) == Severity.MEDIUM
    definitions = rule_definitions()
    assert len({d.rule_id for d in definitions}) == len(definitions)
    for r in (*EARNINGS_QUALITY_RULES, *ANOMALY_RULES):
        assert r.low != r.medium != r.high


def test_anomalies_carry_rules_periods_and_inputs():
    rows = replace(
        company(),
        M.TRADE_RECEIVABLES,
        {2021: 200, 2022: 225, 2023: 250, 2024: 275, 2025: 300, 2026: 450},
    )
    a = analyse(rows)
    (x,) = [y for y in anomalies(a, "RECEIVABLE_DAYS_SPIKE") if y.periods[0].period_end == FY26]
    assert x.rule_id == "ANOMALY/RECEIVABLE_DAYS_SPIKE/2" and x.status == "TRIGGERED"
    assert x.metric_ids and x.canonical_value_ids
    assert "may require review" in x.explanation
    assert not anomalies(A, "RECEIVABLE_DAYS_SPIKE")
