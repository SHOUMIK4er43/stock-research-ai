"""V1 RC1 (Tech Mahindra): financial concept integrity.

Screener's 'EPS in Rs' (49.10) is not Tech Mahindra's basic EPS (54.28): the aggregator
divides by shares in issue, the company by the weighted average net of treasury shares.
Box 5 preferred the source's unspecified EPS over the company's basic EPS, and the report
printed both as 'EPS reported'. The company's own basic EPS now decides, every growth pair
compares one concept, and a claim names the exact concept.
"""

from datetime import UTC, datetime
from decimal import Decimal

from test_financial_reconciliation import (
    FY25,
    FY26,
    B,
    M,
    T,
    build,
    evidence_company_for,
    filings,
    one,
    profit_and_loss,
)
from test_screener_dataset import dataset
from test_summary_input import INFY

from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.domain.financial_canonical import PeriodType

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


async def _infy_with_filing(tmp_path, fixture_dir):
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    bundle, catalogue = await filings(
        tmp_path / "f", {"/ar.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Standalone")])},
        company=evidence_company_for(screener))
    return build([screener], bundle, catalogue)


async def test_company_basic_eps_decides_over_a_sources_unspecified_eps(tmp_path, fixture_dir):
    data = await _infy_with_filing(tmp_path, fixture_dir)
    basic = one(data, M.BASIC_EPS, B.STANDALONE, FY26, PeriodType.FY)
    aggregator = one(data, M.EPS, B.STANDALONE, FY26, PeriodType.FY)
    assert basic.value == Decimal("71.58") and aggregator.value != basic.value
    box5 = build_financial_analytics(data, generated_at=NOW)
    growth = [m for m in box5.all_metrics() if m.code == "EPS_GROWTH_YOY"
              and m.reporting_basis.value == "STANDALONE" and m.period.period_end == FY26
              and m.period.period_type == PeriodType.FY]
    assert len(growth) == 1 and growth[0].status.value == "CALCULATED"
    inputs = {i.metric for i in growth[0].provenance}
    assert inputs == {M.BASIC_EPS}  # both endpoints the company's basic EPS (71.58 / 64.50)
    assert abs(growth[0].value - (Decimal("71.58") / Decimal("64.50") - 1) * 100) < Decimal(
        "0.0001")
    assert one(data, M.BASIC_EPS, B.STANDALONE, FY25, PeriodType.FY).value == Decimal("64.50")


def test_report_claims_name_the_exact_profit_and_eps_concepts():
    from test_business_classification_integrity import HUDCO, run_layers

    report = run_layers(HUDCO)["report"]
    facts = [c for c in report.claims if c.claim_type.value == "FACT"]
    pat = [c for c in facts if c.metric == "PAT"]
    eps = [c for c in facts if c.metric == "EPS"]
    assert pat and all("including non-controlling interests" in c.statement for c in pat)
    assert eps and all("basic or diluted not stated" in c.statement for c in eps)


async def test_provider_borrowings_are_never_labelled_debt_excluding_leases(tmp_path, fixture_dir):
    """Screener's 'Borrowings' include lease liabilities (Tech Mahindra FY26: borrowings 69.6
    + leases 2,116.6 = Screener 2,186 crore). Gross debt from that line is labelled the
    provider's borrowings, and leases are never added to it a second time."""
    screener = await dataset(tmp_path / "s", fixture_dir, INFY)
    data = build([screener])
    box5 = build_financial_analytics(data, generated_at=NOW)
    latest = max(m.period.period_end for m in box5.all_metrics()
                 if m.code == "GROSS_DEBT" and m.status.value == "CALCULATED")
    gross = next(m for m in box5.all_metrics() if m.code == "GROSS_DEBT"
                 and m.period.period_end == latest and m.status.value == "CALCULATED")
    assert gross.formula_id.endswith("/PROVIDER_BORROWINGS")
    assert "excluding leases" not in gross.label
    adjusted = next(m for m in box5.all_metrics() if m.code == "LEASE_ADJUSTED_DEBT"
                    and m.period.period_end == latest)
    assert adjusted.status.value != "CALCULATED"
    assert any("already include lease liabilities" in x for x in adjusted.missing_inputs)


def test_report_names_provider_borrowings_as_including_leases():
    from test_business_classification_integrity import SCREENER, run_layers

    report = run_layers([(SCREENER, INFY)])["report"]  # a non-financial company
    debt = [c for c in report.claims if c.metric == "GROSS_DEBT"]
    assert debt and not [c for c in debt if "excluding leases" in c.statement]
    provider = [c for c in debt if any(f.endswith("/PROVIDER_BORROWINGS") for f in c.formula_ids)]
    assert provider and all("include lease liabilities" in c.statement for c in provider)


def test_a_singular_non_controlling_interest_balance_maps_under_equity():
    """TECHM FY26 prints 'Non controlling Interest' (singular) under Equity: 461.6 crore,
    total equity 300,770 less owners' 296,154 INR mn. Unmapped, it blocked every DCF."""
    from stock_research_ai.application.financial_parsing import map_filing_label
    from stock_research_ai.domain.financial_canonical import CanonicalMetric, StatementType

    bs = StatementType.BALANCE_SHEET
    for label in ("Non controlling Interest", "Non-controlling interests"):
        assert map_filing_label(bs, label, ["equity"], False) == (
            CanonicalMetric.MINORITY_INTEREST)
    # outside the equity caption it is never guessed into the balance
    assert map_filing_label(bs, "Non controlling Interest", ["current liabilities"],
                            False) is None
