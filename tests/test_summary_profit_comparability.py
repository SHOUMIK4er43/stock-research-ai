"""V1 RC1 (live HINDUNILVR): reported-profit growth is never shown as operating growth unqualified.

HUL's Summary said 'Net profit increased from Rs 10,671 Cr in FY2025 to Rs 15,059 Cr in FY2026
(+41.1%)'. The annual report shows FY2026 profit includes Rs 4,407 Cr from discontinued
operations (the ice-cream demerger); continuing-operations profit was Rs 10,652 Cr against
Rs 10,680 Cr. The Summary reads no filings, so it cannot name the cause - but its own page
shows operating profit up only 2.3 % over the same years. A reported-profit trend that moves
far from the operating line carries that qualification inside its grounded text; a missing
comparison is marked unchecked; a comparable change is left alone. Values are never replaced.
"""

from decimal import Decimal

import pytest
from test_summary_generation import FakeModel, generate, summary_input

from stock_research_ai.application.summary_generation import build_summary_claims

QUALIFIED = "Over the same periods operating profit changed"
UNCHECKED = "Comparability not checked"


@pytest.fixture
async def base(tmp_path, fixture_dir):
    return await summary_input(tmp_path, fixture_dir)


def scaled(value, metric_id, factor):
    """The input with `metric_id`'s latest annual value = the year before x factor."""
    rows = []
    for row in value.annual.rows:
        if row.metric_id == metric_id:
            prior, latest = row.values[-2], row.values[-1]
            row = row.model_copy(update={"values": (*row.values[:-1], latest.model_copy(
                update={"value": (prior.value * factor).quantize(Decimal(1))}))})
        rows.append(row)
    annual = value.annual.model_copy(update={"rows": tuple(rows)})
    return value.model_copy(update={"annual": annual})


def without(value, metric_id):
    rows = tuple(r for r in value.annual.rows if r.metric_id != metric_id)
    return value.model_copy(update={"annual": value.annual.model_copy(update={"rows": rows})})


def latest_trend(claims, metric_id):
    return [c for c in claims if c.kind == "trend" and c.block == "annual"
            and c.metric_id == metric_id][-1]


async def test_profit_growth_far_from_operating_growth_is_qualified(base):
    value = scaled(scaled(base, "net_profit", Decimal("1.41")), "operating_profit",
                   Decimal("1.023"))
    claims = build_summary_claims(value)
    trend = latest_trend(claims, "net_profit")
    assert ", +41.0%)" in trend.text and f"{QUALIFIED} +2.3%" in trend.text
    assert "discontinued operations" in trend.text and "not a measure of the continuing" in (
        trend.text)
    # the reported values, their sources and periods are kept as they are
    row = next(r for r in value.annual.rows if r.metric_id == "net_profit")
    assert trend.values == (row.values[-2].value, row.values[-1].value)
    assert trend.source_observation_ids == (row.values[-2].source_observation_id,
                                            row.values[-1].source_observation_id)
    ratio = [c for c in claims if c.metric_id == "cash_conversion"]
    assert ratio and all("Net profit moved far from operating profit" in c.text for c in ratio)


async def test_a_comparable_change_is_left_alone(base):
    claims = build_summary_claims(base)  # INFY: profit and operating profit grow alike
    trend = latest_trend(claims, "net_profit")
    assert QUALIFIED not in trend.text and UNCHECKED not in trend.text


async def test_a_missing_operating_line_marks_comparability_unchecked(base):
    trend = latest_trend(build_summary_claims(without(base, "operating_profit")), "net_profit")
    assert UNCHECKED in trend.text and QUALIFIED not in trend.text


async def test_the_headline_never_appears_without_its_qualification(base):
    value = scaled(scaled(base, "net_profit", Decimal("1.41")), "operating_profit",
                   Decimal("1.023"))
    result = await generate(value, FakeModel())
    lines = [line for line in result.summary_text.splitlines() if "+41.0%" in line]
    assert lines and all(QUALIFIED in line for line in lines)
