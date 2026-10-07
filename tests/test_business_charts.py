"""DETAILED_RESEARCH_V1 Box 7: additive segments/1 charts on the Box 6 renderer (offline)."""

from datetime import date
from decimal import Decimal

from test_business_intelligence import (
    BANK_KPI,
    CAPACITY,
    GEOGRAPHY,
    NOW,
    ORDER_BOOK,
    ar,
    business,
    ip,
    prior_year,
    segment_table,
    two_year,
)

from stock_research_ai.application.business_charts import REGISTRY, build_business_charts
from stock_research_ai.application.chart_registry import CHARTS
from stock_research_ai.domain.business_intelligence import BusinessChartBundle
from stock_research_ai.domain.financial_canonical import Industry
from stock_research_ai.domain.financial_charts import (
    AxisKind,
    ChartCategory,
    ChartStatus,
    PointSourceKind,
    SourceLayer,
)

FY26, FY25, FY24 = date(2026, 3, 31), date(2025, 3, 31), date(2024, 3, 31)


def charts(data) -> BusinessChartBundle:
    return build_business_charts(data, generated_at=NOW)


def chart(bundle, code, **kw):
    found = [
        c
        for c in bundle.charts
        if c.chart_code == code and all(getattr(c, k) == v for k, v in kw.items())
    ]
    assert len(found) == 1, (code, [c.title for c in bundle.charts])
    return found[0]


async def test_points_reference_box7_provenance(tmp_path):
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(segment_table(), GEOGRAPHY)})
    bundle = charts(data)
    canonical = {v.canonical_value_id for v in data.canonical_segment_values} | {
        v.canonical_value_id for v in data.canonical_geography_values
    }
    analytics = {a.analytic_id for a in data.segment_analytics}
    observed = {o.observation_id for o in data.segment_observations} | {
        o.observation_id for o in data.geography_observations
    }
    plotted = [p for c in bundle.charts for s in c.series for p in s.points if p.value is not None]
    assert plotted
    for p in plotted:
        assert p.source.layer == SourceLayer.BOX7
        assert p.source.kind in (
            PointSourceKind.SEGMENT_VALUE,
            PointSourceKind.GEOGRAPHY_VALUE,
            PointSourceKind.SEGMENT_ANALYTIC,
        )
        assert p.source.ref_id in canonical | analytics
        assert p.source.observation_ids and set(p.source.observation_ids) <= observed
        assert p.source.source_locators
    revenue = chart(bundle, "SEGMENT_REVENUE_TREND")
    assert revenue.frequency == AxisKind.ANNUAL_FLOW and revenue.unit == "MONEY"
    assert [s.label for s in revenue.series] == ["Chemicals", "Pharma", "Agri"]
    assert [p.value for p in revenue.series[0].points] == [
        Decimal(900),
        Decimal(1000),
        Decimal(1200),
    ]
    mix = chart(bundle, "SEGMENT_REVENUE_MIX")
    assert mix.frequency == AxisKind.CATEGORY and mix.series[0].points[0].value == Decimal(50)
    assert bundle.source_box7_dataset_id == data.dataset_id


async def test_no_trend_across_an_unrestated_structure_change(tmp_path):
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
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(head + new, note)}
    )
    bundle = charts(data)
    assert not [c for c in bundle.charts if c.chart_code == "SEGMENT_REVENUE_TREND"]
    skipped = [s for s in bundle.skipped_charts if s.chart_code == "SEGMENT_REVENUE_TREND"]
    assert skipped and skipped[0].status == ChartStatus.NO_DATA
    plotted = {p.period_end for c in bundle.charts for s in c.series for p in s.points}
    assert FY24 not in plotted  # the old structure never joins the new one


async def test_restated_run_is_drawn_and_the_break_is_stated(tmp_path):
    old = ["Segment revenue", "Chemicals 1,000 900", "Agri 300 250", "Total 1,300 1,150"]
    new = [
        "Segment revenue",
        "Specialty Chemicals 700 560",
        "Bulk Chemicals 500 440",
        "Agri 400 280",
        "Total 1,600 1,280",
    ]
    note = [
        "During the year the Company reorganised its reportable segments. Comparative "
        "segment information for the previous year has been restated accordingly."
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar25.pdf": ar(prior_year(old)), "/ar26.pdf": ar(two_year(new), note)}
    )
    revenue = chart(charts(data), "SEGMENT_REVENUE_TREND")
    assert [k for k, _ in revenue.x_axis.categories] == [f"FY|{FY25}", f"FY|{FY26}"]
    assert {s.label for s in revenue.series} == {"Specialty Chemicals", "Bulk Chemicals", "Agri"}
    agri = next(s for s in revenue.series if s.label == "Agri")
    assert [p.value for p in agri.points] == [Decimal(280), Decimal(400)]  # restated FY25
    assert [w.code for w in revenue.warnings] == ["SEGMENT_STRUCTURE_CHANGED"]
    assert "history before 2025-03-31 not drawn" in revenue.subtitle


async def test_missing_periods_stay_gaps(tmp_path):
    gap = [
        "Consolidated segment information",
        "(Rs. in crore)",
        "Particulars Year ended March 31,",
        "2026 2024",
        "Segment revenue",
        "Chemicals 1,200 900",
        "Pharma 800 600",
        "Total 2,000 1,500",
    ]
    data, _b, _x = await business(tmp_path, {"/a.pdf": ar(gap)})
    revenue = chart(charts(data), "SEGMENT_REVENUE_TREND")
    chem = revenue.series[0]
    assert [p.value for p in chem.points] == [Decimal(900), None, Decimal(1200)]
    gap_point = chem.points[1]
    assert gap_point.missing_reason == "not presented for this period"
    assert gap_point.source.ref_id is None and revenue.status == ChartStatus.PARTIAL
    assert revenue.connect_gaps is False


async def test_units_currencies_and_statuses_never_share_an_axis(tmp_path):
    usd = [
        "Consolidated segment information",
        "(US$ in million)",
        "Particulars Year ended March 31,",
        "2026 2025",
        "Segment revenue",
        "Chemicals 144 120",
        "Pharma 96 84",
        "Total 240 204",
    ]
    data, _b, _x = await business(
        tmp_path, {"/ar.pdf": ar(segment_table(), usd), "/ip.pdf": ip(CAPACITY, ORDER_BOOK)}
    )
    bundle = charts(data)
    revenue = [c for c in bundle.charts if c.chart_code == "SEGMENT_REVENUE_TREND"]
    assert sorted(c.currency.value for c in revenue if c.currency) == ["INR", "USD"]
    for c in bundle.charts:
        assert len({s.unit for s in c.series}) == 1
        assert len({s.currency for s in c.series}) <= 1
    capacity = chart(bundle, "CAPACITY_TREND")
    assert capacity.frequency == AxisKind.ANNUAL_BALANCE and capacity.unit == "MTPA"
    assert {s.label for s in capacity.series} == {"installed", "under construction"}
    book = chart(bundle, "ORDER_BOOK_TREND")
    assert book.unit == "MONEY" and book.series[0].points[-1].value == Decimal(12500)


async def test_insufficient_data_is_skipped_not_drawn(tmp_path):
    lines = [
        "Operational highlights",
        "Particulars As at March 31, 2026",
        "Installed capacity (MTPA) 12.5",
        "Capacity utilisation (%) 78",
        "Production (MT) 9.1",
    ]
    data, _b, _x = await business(tmp_path, {"/ip.pdf": ip(lines)})
    bundle = charts(data)
    assert not bundle.charts
    assert {s.chart_code for s in bundle.skipped_charts} >= {
        "CAPACITY_TREND",
        "SEGMENT_REVENUE_TREND",
    }


async def test_bank_gets_only_its_own_kpi_charts(tmp_path):
    data, _b, _x = await business(tmp_path, {"/ar.pdf": ar(BANK_KPI)}, industry=Industry.BANK)
    bundle = charts(data)
    codes = {c.chart_code for c in bundle.charts}
    assert codes <= {"KPI_TREND"}
    titles = {c.title for c in bundle.charts}
    assert "Deposits" in titles and not any("Capacity" in t for t in titles)


async def test_svg_output_is_deterministic(tmp_path):
    files = {"/ar.pdf": ar(segment_table(), GEOGRAPHY)}
    first, _b, _x = await business(tmp_path / "a", files)
    second, _b, _x = await business(tmp_path / "b", files)
    one, two = charts(first), charts(second)
    assert [a.sha256 for a in one.artifacts] == [a.sha256 for a in two.artifacts]
    assert one.bundle_id == two.bundle_id and one.status == "COMPLETE"
    assert all(a.svg.startswith("<svg") for a in one.artifacts)


def test_existing_box6_definitions_unchanged():
    assert len(CHARTS) == 58  # the committed charts/1 registry (per basis), untouched
    assert {c.category for c in CHARTS}.isdisjoint(
        {ChartCategory.SEGMENTS, ChartCategory.GEOGRAPHY, ChartCategory.OPERATING_KPIS}
    )
    assert len(REGISTRY) == 10 and all(
        d.category
        in (ChartCategory.SEGMENTS, ChartCategory.GEOGRAPHY, ChartCategory.OPERATING_KPIS)
        for d in REGISTRY
    )
