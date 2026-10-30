"""V1 RC1 (Tech Mahindra): the Summary is an analysis that agrees with Detailed Research.

The TECHM Summary printed 'Mar 2026' for a fiscal year and a balance-sheet date alike, called
FY2026 -> TTM an increase (overlapping periods), named Screener's net profit 'PAT' and its EPS
'EPS', showed an undated snapshot as current, and closed its risk section with a placeholder.
"""

import asyncio
import re
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_generation import FakeModel, generate, summary_input
from test_summary_input import INFY

from stock_research_ai.application.summary_generation import build_summary_claims

RAW_PERIOD = re.compile(r"\b(?:in|—) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}\b")


@pytest.fixture
async def infy(tmp_path, fixture_dir):
    value = await summary_input(tmp_path, fixture_dir)
    return value, build_summary_claims(value), await generate(value, FakeModel())


async def test_comparisons_are_like_for_like_only(infy):
    _value, claims, _result = infy
    trends = [c for c in claims if c.kind == "trend"]
    assert trends
    for c in trends:
        assert "TTM" not in " ".join(c.periods) and "TTM" not in c.text
        if c.block in ("annual", "quarterly", "cash_flow"):
            # FY vs the FY before, a quarter vs the same quarter a year earlier
            a, b = (re.search(r"(Q\d )?FY(\d{4})", p) for p in c.text.split(" to ")[:2])
            assert a and b and a.group(1) == b.group(1)
            assert int(b.group(2)) - int(a.group(2)) == 1


async def test_every_displayed_period_is_named(infy):
    _value, _claims, result = infy
    for line in result.summary_text.splitlines():
        assert not RAW_PERIOD.search(line), line  # never a bare 'Mar 2026'
    text = result.summary_text
    assert "balance sheet at " in text and "TTM to " in text
    assert re.search(r"\bQ\d FY\d{4}\b", text) and re.search(r"\bFY\d{4}\b", text)


async def test_ratios_are_calculated_from_their_two_cited_figures(infy):
    _value, claims, _result = infy
    ratios = [c for c in claims if c.kind == "ratio"]
    assert {c.metric_id for c in ratios} >= {"operating_margin"}
    for c in ratios:
        numerator, denominator = c.values
        assert len(c.source_observation_ids) == 2 and denominator > 0
        quotient = numerator / denominator
        shown = (f"{(quotient * 100).quantize(Decimal('0.1'), ROUND_HALF_EVEN)}%"
                 if c.metric_id == "operating_margin"
                 else f"{quotient.quantize(Decimal('0.01'), ROUND_HALF_EVEN)}×")
        assert f": {shown} (" in c.text


async def test_analysis_leads_and_the_risk_section_is_never_a_placeholder(infy):
    _value, _claims, result = infy
    text = result.summary_text
    order = [text.index(h) for h in ("Current trajectory", "Profitability and cash conversion",
                                     "Latest reported figures", "Market snapshot",
                                     "Risk coverage and data limitations")]
    assert order == sorted(order)
    assert "Additional verified observations are not shown" not in text
    assert "Risk rules evaluated in this Summary: 0" in text
    assert "no finding of 'no risk'" in text
    assert "observation date not stated by the source, so not necessarily current" in text


async def test_aggregator_concepts_are_named_as_the_aggregators(infy):
    _value, claims, result = infy
    for c in claims:
        assert not c.label.startswith("PAT")
        if c.metric_id in ("eps", "net_profit", "borrowings", "roe", "roce"):
            assert "Screener" in c.label, c.label
    assert "Net profit (Screener definition)" in result.summary_text
    assert "Definitions: figures are Screener's" in result.summary_text


async def test_summary_and_detailed_research_name_and_value_periods_alike(infy):
    value, claims, _result = infy
    report = (await asyncio.to_thread(run_layers, [(SCREENER, INFY)]))["report"]
    revenue = [c for c in claims if c.kind == "observation" and c.block == "annual"
               and c.metric_id in ("sales", "revenue") and "TTM" not in c.text]
    latest = revenue[-1]
    name = re.search(r"— (FY\d{4}):", latest.text).group(1)
    detailed = [c for c in report.claims if c.period == name and c.value == latest.values[0]
                and "REVENUE" in (c.metric or c.topic)]
    assert detailed, f"Detailed Research has no {name} revenue of {latest.values[0]}"


def test_detailed_research_explains_the_undated_snapshot_it_does_not_use():
    report = run_layers([(SCREENER, INFY)])["report"]
    valuation = report.section("VALUATION")
    texts = [c.statement for c in report.claims if c.claim_id in valuation.limitation_claim_ids
             or c.claim_id in valuation.claim_ids]
    assert any("aggregator snapshot without an observation date" in t for t in texts)


async def test_a_derived_pe_is_shown_beside_the_reported_one_never_as_it(infy):
    value, claims, result = infy
    derived = [c for c in claims if c.metric_id == "derived_price_earnings"]
    reported = [c for c in claims if c.metric_id == "price_earnings"]
    if not derived:
        pytest.skip("fixture has no price / TTM EPS pair")
    (d,) = derived
    price, eps = d.values
    assert f"{(price / eps).quantize(Decimal('0.01'), ROUND_HALF_EVEN)}×" in d.text
    assert "not the source-reported P/E" in d.label
    assert result.summary_text.count(d.text) == 1
    assert all("Screener-reported" in c.label for c in reported)
