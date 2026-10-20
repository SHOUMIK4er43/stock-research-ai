"""DETAILED_RESEARCH_V1 Box 6: deterministic financial charts and SVG artifacts (offline).

Fixtures: A 5-year revenue/PAT, B negative PAT, C quarterly revenue with a missing quarter,
D margin trend, E ROE/ROCE, F CFO vs PAT, G debt vs cash, H receivable days, I anomaly
marker, J conflicted / low-confidence input, K bank, L insufficient data, M FY/TTM mixing
attempt, N standalone/consolidated mixing attempt, O signed negative series.
"""

import ast
import hashlib
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_financial_analytics import (
    FY26,
    NOW,
    TTM,
    B,
    M,
    V,
    company,
    company_year,
    fy,
    make,
    pit,
    quarter,
    replace,
)

from stock_research_ai.adapters.chart_artifacts import write_chart_artifacts
from stock_research_ai.application import (
    chart_planner,
    chart_registry,
    financial_charts,
    svg_renderer,
)
from stock_research_ai.application.chart_registry import CHARTS
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_charts import (
    build_chart_bundle,
    canonical_chart_json,
)
from stock_research_ai.application.svg_renderer import render_svg
from stock_research_ai.domain import financial_charts as charts_domain
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Currency,
    Industry,
    Outcome,
    PeriodType,
)
from stock_research_ai.domain.financial_charts import (
    CHART_REGISTRY_VERSION,
    AxisKind,
    ChartLimits,
    ChartPoint,
    ChartSpec,
    ChartStatus,
    FinancialChartBundle,
    PointSourceKind,
    SourceLayer,
)

YEARS = range(2021, 2027)
SVG_NS = "{http://www.w3.org/2000/svg}"


def bundle_for(rows, industry=Industry.GENERAL, **kw) -> FinancialChartBundle:
    box4 = make(rows, industry)
    box5 = build_financial_analytics(box4, generated_at=NOW)
    return build_chart_bundle(box4, box5, generated_at=NOW, **kw)


def chart(bundle, code, basis=B.CONSOLIDATED) -> ChartSpec:
    found = bundle.chart(code, basis)
    assert found is not None, (
        code,
        [s.reason for s in bundle.skipped_charts if s.chart_code == code],
    )
    return found


def skipped(bundle, code, basis=B.CONSOLIDATED):
    return next(
        s for s in bundle.skipped_charts if s.chart_code == code and s.reporting_basis == basis
    )


def svg_of(bundle, spec) -> str:
    return bundle.artifact(spec.artifact_sha256).svg


def point(spec, series_code, period_end):
    s = next(s for s in spec.series if s.metric_code == series_code)
    return next(p for p in s.points if p.period_end == period_end)


A = bundle_for(company())


# --- contracts, registry, determinism ----------------------------------------------------------


def test_registry_is_versioned_and_unique():
    assert CHART_REGISTRY_VERSION == "charts/1" == chart_registry.REGISTRY_VERSION
    assert len({c.code for c in CHARTS}) == len(CHARTS)
    for definition in CHARTS:
        assert definition.sectors and definition.min_points >= 1
        assert definition.series or definition.special
    assert A.chart_registry_version == "charts/1" and A.renderer_version == "svg-renderer/1"


def test_bundle_statistics_and_skips_are_complete():
    st = A.statistics
    assert st.definitions_considered == len(CHARTS)  # one basis
    assert (
        st.charts_ready
        + st.charts_partial
        + st.no_data
        + st.not_applicable
        + st.unsupported_sector
        + st.render_failed
    ) == st.definitions_considered
    assert len(A.charts) + len(A.skipped_charts) == st.definitions_considered
    assert st.svg_artifacts == len(A.artifacts) and st.svg_bytes == sum(
        a.byte_size for a in A.artifacts
    )
    assert st.total_points == sum(len(s.points) for c in A.charts for s in c.series)
    for s in A.skipped_charts:
        assert s.reason  # never silently omitted
    assert {s.chart_code for s in A.skipped_charts} >= {"DEPOSITS", "NPA_RATIOS"}


def test_deterministic_ids_json_and_bytes():
    again = bundle_for(company())
    assert canonical_chart_json(A) == canonical_chart_json(again)
    assert FinancialChartBundle.model_validate_json(canonical_chart_json(A)) == A
    spec = chart(A, "REVENUE_ANNUAL")
    assert render_svg(spec) == render_svg(spec) == svg_of(A, spec)
    assert chart(again, "REVENUE_ANNUAL").chart_id == spec.chart_id


def test_svg_snapshot_hash():
    """Renderer changes must update this hash intentionally (svg-renderer/1)."""
    spec = chart(A, "REVENUE_ANNUAL")
    digest = hashlib.sha256(svg_of(A, spec).encode()).hexdigest()
    assert digest == spec.artifact_sha256 == SNAPSHOT_REVENUE_ANNUAL


# 2026-10-20 (user-approved): Box 4 now records its business classification, so the
# content-addressed chart id prefix changed (f9b70fb0 -> 1749b147); every other SVG byte is
# identical to the previous snapshot e7bf9e2e....
SNAPSHOT_REVENUE_ANNUAL = "256ceaf7a0a2a5cf091968397830605ca8d529ff8a4071b624e5783fdf013e8e"


def test_contract_is_strict():
    spec = chart(A, "REVENUE_ANNUAL")
    with pytest.raises(ValidationError):
        ChartSpec.model_validate({**spec.model_dump(), "surprise": 1})
    with pytest.raises(ValidationError):
        FinancialChartBundle.model_validate({**A.model_dump(), "charts": (*A.charts, A.charts[0])})
    p = spec.series[0].points[0]
    with pytest.raises(ValidationError):  # a gap must say why
        ChartPoint.model_validate({**p.model_dump(), "value": None, "missing_reason": None})
    with pytest.raises(ValidationError):  # a value must reference its source
        ChartPoint.model_validate(
            {**p.model_dump(), "source": {**p.source.model_dump(), "ref_id": None}}
        )


def test_box5_must_come_from_this_box4():
    box4 = make(company())
    other = make(company(range(2022, 2027)))
    box5 = build_financial_analytics(other, generated_at=NOW)
    with pytest.raises(ValueError):
        build_chart_bundle(box4, box5, generated_at=NOW)


# --- A, B, O: values, negatives, zero line --------------------------------------------------


def test_a_annual_revenue_and_pat_come_from_box4_unchanged():
    box4 = make(company())
    revenue = chart(A, "REVENUE_ANNUAL")
    assert revenue.status == ChartStatus.READY and revenue.unit == "MONEY"
    p = point(revenue, "REVENUE_FROM_OPERATIONS", FY26)
    canonical = next(v for v in box4.canonical_values if v.canonical_value_id == p.source.ref_id)
    assert p.value == canonical.value == Decimal(1500)
    assert p.source.layer == SourceLayer.BOX4 and p.display_value == "1,500 ₹ crore"
    assert chart(A, "REVENUE_AND_PAT").status == ChartStatus.READY
    assert len(chart(A, "REVENUE_AND_PAT").series) == 2
    assert chart(A, "CAGR_SUMMARY").status == ChartStatus.PARTIAL  # 10Y CAGR has no data


def test_b_and_o_negative_values_render_below_a_zero_line():
    pats = {y: Decimal(270 + 40 * (y - 2021)) * Decimal("0.75") for y in YEARS}
    bundle = bundle_for(replace(company(), M.PAT, pats | {2026: -40}))
    spec = chart(bundle, "PAT_ANNUAL")
    assert point(spec, "PAT", FY26).value == Decimal(-40)
    svg = svg_of(bundle, spec)
    root = ET.fromstring(svg)
    zero = [e for e in root.iter(f"{SVG_NS}line") if e.get("class") == "zero-line"]
    assert len(zero) == 1
    zero_y = Decimal(zero[0].get("y1"))
    bars = [e for e in root.iter(f"{SVG_NS}rect") if e.get("class") == "bar"]
    assert any(Decimal(b.get("y")) == zero_y for b in bars)  # a bar hanging down from zero
    growth = chart(bundle, "PAT_GROWTH")
    assert point(growth, "PAT_GROWTH_YOY", FY26).value < 0
    assert "zero-line" in svg_of(bundle, growth)


# --- C: quarters, gaps ---------------------------------------------------------------------------


def quarters_rows(skip_revenue_at: date | None = None):
    rows = []
    ends = [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
        date(2026, 3, 31),
        date(2026, 6, 30),
    ]
    for i, end in enumerate(ends):
        if end != skip_revenue_at:
            rows.append(V(M.REVENUE_FROM_OPERATIONS, quarter(end), 300 + 10 * i))
        rows.append(V(M.PAT, quarter(end), 40 + i))
    return rows


def test_c_quarterly_revenue_keeps_a_missing_quarter_as_a_gap():
    gap = date(2025, 9, 30)
    bundle = bundle_for(company() + quarters_rows(skip_revenue_at=gap))
    spec = chart(bundle, "QUARTERLY_REVENUE")
    assert spec.status == ChartStatus.PARTIAL and spec.frequency == AxisKind.QUARTERLY
    missing = point(spec, "REVENUE_FROM_OPERATIONS", gap)
    assert (missing.value, missing.status) == (None, "INPUT_MISSING")
    assert "no canonical REVENUE_FROM_OPERATIONS" in missing.missing_reason
    assert all(p.period_type == PeriodType.QUARTER for s in spec.series for p in s.points)
    assert 'class="gap"' in svg_of(bundle, spec)
    full = bundle_for(company() + quarters_rows())
    assert chart(full, "QUARTERLY_REVENUE").status == ChartStatus.READY


def test_lines_are_never_connected_across_gaps():
    rows = replace(
        company(), M.OPERATING_PROFIT, {y: 300 + 40 * (y - 2021) for y in YEARS if y != 2023}
    )
    bundle = bundle_for(rows)
    spec = chart(bundle, "OPERATING_MARGIN")
    gap = point(spec, "OPERATING_MARGIN", date(2023, 3, 31))
    assert gap.value is None and gap.status == "INPUT_MISSING" and gap.missing_reason
    assert spec.connect_gaps is False
    root = ET.fromstring(svg_of(bundle, spec))
    segments = [e for e in root.iter(f"{SVG_NS}path") if e.get("class") == "segment"]
    assert len(segments) == 2  # FY21-FY22 and FY24-FY26, never bridged
    assert all(seg.get("d").count("M") == 1 for seg in segments)


# --- D, E, F, G, H: analytical charts copy Box 5 exactly -----------------------------------------


def test_d_margin_chart_carries_the_box5_trend_label():
    box4 = make(company())
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle = build_chart_bundle(box4, box5, generated_at=NOW)
    spec = chart(bundle, "PAT_MARGIN")
    trend = next(t for t in box5.trends if t.series_code == "PAT_MARGIN")
    (annotation,) = [a for a in spec.annotations if a.kind == "TREND"]
    assert annotation.ref_id == trend.trend_id and trend.classification.value in annotation.label
    assert trend.classification.value in spec.subtitle


def test_e_returns_are_box5_values_not_recalculated():
    box4 = make(company())
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle = build_chart_bundle(box4, box5, generated_at=NOW)
    spec = chart(bundle, "RETURNS_COMPARISON")
    metrics = {m.metric_id: m for m in box5.all_metrics()}
    for s in spec.series:
        for p in s.points:
            if p.value is not None:
                m = metrics[p.source.ref_id]
                assert (p.value, m.code, p.source.formula_or_rule_id) == (
                    m.value,
                    s.metric_code,
                    m.formula_id,
                )


def test_f_pat_vs_cfo_and_g_debt_vs_cash():
    spec = chart(A, "PAT_VS_CFO")
    assert spec.status == ChartStatus.READY and [s.metric_code for s in spec.series] == [
        "PAT",
        "CFO",
    ]
    debt_cash = chart(A, "DEBT_VS_CASH")
    assert debt_cash.frequency == AxisKind.ANNUAL_BALANCE
    debt = point(debt_cash, "GROSS_DEBT", FY26)
    assert debt.source.layer == SourceLayer.BOX5 and debt.value == Decimal(300)
    cash = point(debt_cash, "CASH_AND_CASH_EQUIVALENTS", FY26)
    assert cash.source.layer == SourceLayer.BOX4 and cash.value == Decimal(250)
    assert all(
        p.period_type == PeriodType.POINT_IN_TIME for s in debt_cash.series for p in s.points
    )


def test_h_receivable_days():
    spec = chart(A, "RECEIVABLE_DAYS")
    assert spec.unit == "DAYS" and spec.y_axis.display_unit == "days"
    first = spec.series[0].points[0]
    assert first.value is None and first.status == "INSUFFICIENT_HISTORY"  # FY2021 has no opening


def test_fcf_limitation_is_shown_not_calculated():
    bundle = bundle_for(company(**{"PURCHASE_OF_PPE": None}))
    fcf = skipped(bundle, "FCF_ANNUAL")
    assert fcf.status == ChartStatus.NO_DATA and "INPUT_MISSING" in fcf.reason
    both = chart(bundle, "CFO_VS_FCF")
    assert both.status == ChartStatus.PARTIAL
    fcf_series = next(s for s in both.series if s.metric_code == "FCF")
    assert fcf_series.valid_points == 0 and all(p.missing_reason for p in fcf_series.points)


# --- I: anomalies --------------------------------------------------------------------------------


def test_i_box5_anomalies_become_annotations_without_new_detection():
    rows = replace(
        company(),
        M.TRADE_RECEIVABLES,
        {2021: 200, 2022: 225, 2023: 250, 2024: 275, 2025: 300, 2026: 450},
    )
    box4 = make(rows)
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle = build_chart_bundle(box4, box5, generated_at=NOW)
    spec = chart(bundle, "RECEIVABLE_DAYS")
    (mark,) = [a for a in spec.annotations if a.kind == "ANOMALY"]
    source = next(a for a in box5.anomalies if a.signal_id == mark.ref_id)
    assert (mark.rule_id, mark.severity, mark.x_key) == (
        source.rule_id,
        source.severity.value,
        source.periods[0].key(),
    )
    assert point(spec, "RECEIVABLE_DAYS", FY26).anomaly_ids == (source.signal_id,)
    assert 'class="anomaly"' in svg_of(bundle, spec)
    timeline = chart(bundle, "ANOMALY_TIMELINE")
    box5_annual = {a.signal_id for a in box5.anomalies if a.periods[0].period_type == PeriodType.FY}
    shown = {p.source.ref_id for p in timeline.series[0].points}
    assert shown == box5_annual  # exactly Box 5's anomalies, nothing new
    assert {p.source.kind for p in timeline.series[0].points} == {PointSourceKind.ANOMALY}
    text = svg_of(bundle, timeline).lower()
    assert "fraud" not in text and "may require review" in text


# --- J: conflicts and low confidence --------------------------------------------------------


def test_j_conflicts_are_gaps_and_low_confidence_is_marked():
    rows = [r for r in company() if not (r.metric == M.PAT and r.period == fy(2026))]
    rows += [V(M.PAT, fy(2026), None, outcome=Outcome.CONFLICT)]
    rows = [r for r in rows if not (r.metric == M.TOTAL_ASSETS and r.period == pit(2026))]
    rows += [
        V(
            M.TOTAL_ASSETS,
            pit(2026),
            2750,
            outcome=Outcome.SOURCE_PRECEDENCE,
            max_difference=Decimal(500),
        )
    ]
    bundle = bundle_for(rows)
    pat = point(chart(bundle, "PAT_ANNUAL"), "PAT", FY26)
    assert (pat.value, pat.status) == (None, "INPUT_CONFLICT")
    roe = point(chart(bundle, "ROE"), "ROE", FY26)
    assert (roe.value, roe.status) == (None, "INPUT_CONFLICT")
    asset_turn = chart(bundle, "DUPONT_TURNOVER_AND_LEVERAGE")
    p = point(asset_turn, "DUPONT_ASSET_TURNOVER", FY26)
    assert p.confidence == Confidence.LOW and "MATERIAL_INPUT_CONFLICT" in p.warnings
    assert 'fill="#ffffff" stroke=' in svg_of(bundle, asset_turn)  # hollow marker


# --- K: banks ----------------------------------------------------------------------------------


def test_k_bank_charts_are_sector_aware():
    rows = company() + [V(M.DEPOSITS, pit(y), 5000 + 400 * (y - 2021)) for y in YEARS]
    bundle = bundle_for(rows, industry=Industry.BANK)
    assert bundle.sector == "FINANCIAL"
    for code in (
        "EBITDA_MARGIN",
        "INVENTORY_DAYS",
        "PAYABLE_DAYS",
        "CASH_CONVERSION_CYCLE",
        "DEBT_TO_EQUITY",
        "DEBT_VS_CASH",
        "PIOTROSKI_COMPONENTS",
        "ALTMAN_COMPONENTS",
        "BENEISH_COMPONENTS",
        "ROIC",
    ):
        s = skipped(bundle, code)
        assert s.status == ChartStatus.UNSUPPORTED_SECTOR and "FINANCIAL" in s.reason
        assert bundle.chart(code, B.CONSOLIDATED) is None
    assert chart(bundle, "DEPOSITS").status == ChartStatus.READY
    assert chart(bundle, "DEPOSIT_GROWTH").series[0].source_layer == SourceLayer.BOX5
    assert chart(bundle, "ROE").status in (ChartStatus.READY, ChartStatus.PARTIAL)
    npa = skipped(bundle, "NPA_RATIOS")
    assert npa.status == ChartStatus.NO_DATA and "NPA" in npa.reason


# --- L, M, N: insufficient data and mixing attempts -------------------------------------------


def test_l_insufficient_data():
    bundle = bundle_for([V(M.REVENUE_FROM_OPERATIONS, fy(2026), 1000)])
    s = skipped(bundle, "REVENUE_ANNUAL")
    assert s.status == ChartStatus.NO_DATA and "fewer than 2 valid points" in s.reason
    assert bundle.statistics.no_data >= 1 and not [
        c for c in bundle.charts if c.chart_code == "REVENUE_ANNUAL"
    ]


def test_m_fy_and_ttm_never_share_an_axis():
    bundle = bundle_for(company() + [V(M.REVENUE_FROM_OPERATIONS, TTM, 1550), V(M.PAT, TTM, 360)])
    for spec in bundle.charts:
        assert all(p.period_type != PeriodType.TTM for s in spec.series for p in s.points)
    spec = chart(bundle, "REVENUE_ANNUAL")
    good = spec.series[0].points[0]
    ttm_point = good.model_copy(update={"period_type": PeriodType.TTM})
    series = spec.series[0].model_copy(update={"points": (*spec.series[0].points, ttm_point)})
    with pytest.raises(ValidationError):
        ChartSpec.model_validate({**spec.model_dump(), "series": (series.model_dump(),)})
    quarterly = chart(bundle_for(company() + quarters_rows()), "QUARTERLY_REVENUE")
    ytd = quarterly.series[0].points[0].model_copy(update={"period_type": PeriodType.NINE_MONTH})
    with pytest.raises(ValidationError):  # quarter never with YTD
        ChartSpec.model_validate(
            {
                **quarterly.model_dump(),
                "series": (quarterly.series[0].model_copy(update={"points": (ytd,)}).model_dump(),),
            }
        )


def test_n_bases_currencies_and_units_never_mix():
    rows = (
        company() + company_year(2026, basis=B.STANDALONE) + company_year(2025, basis=B.STANDALONE)
    )
    bundle = bundle_for(rows)
    consolidated, standalone = (
        chart(bundle, "REVENUE_ANNUAL"),
        chart(bundle, "REVENUE_ANNUAL", B.STANDALONE),
    )
    assert consolidated.chart_id != standalone.chart_id
    box4 = make(rows)
    canonical = {v.canonical_value_id: v for v in box4.canonical_values}
    for spec in bundle.charts:
        for s in spec.series:
            for p in s.points:
                for cid in p.source.canonical_value_ids:
                    assert canonical[cid].reporting_basis == spec.reporting_basis
    s = consolidated.series[0]
    for update in (
        {"reporting_basis": B.STANDALONE},
        {"currency": Currency.USD},
        {"unit": "PERCENT"},
    ):
        with pytest.raises(ValidationError):
            ChartSpec.model_validate(
                {**consolidated.model_dump(), "series": (s.model_copy(update=update).model_dump(),)}
            )


# --- provenance ----------------------------------------------------------------------------------


def test_every_plotted_point_reaches_box4_or_box5():
    box4 = make(company())
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle = build_chart_bundle(box4, box5, generated_at=NOW)
    canonical = {v.canonical_value_id: v for v in box4.canonical_values}
    observations = {o.observation_id: o for o in box4.observations}
    box5_ids = (
        {m.metric_id for m in box5.all_metrics()}
        | {r.result_id for r in box5.forensic_scores}
        | {c.check_id for c in box5.earnings_quality}
        | {t.trend_id for t in box5.trends}
        | {a.signal_id for a in box5.anomalies}
    )
    checked = 0
    for spec in bundle.charts:
        assert set(spec.source_dataset_ids) == {box4.dataset_id, box5.analytics_id}
        for s in spec.series:
            for p in s.points:
                if p.value is None:
                    continue
                checked += 1
                src = p.source
                if src.layer == SourceLayer.BOX4:
                    value = canonical[src.ref_id]
                    assert value.value == p.value
                    observation = observations[value.selected_observation_id]
                    assert src.source_locators == (observation.source.locator,)
                elif src.kind == PointSourceKind.HEALTH_SIGNAL:
                    assert src.component and src.formula_or_rule_id
                else:
                    assert src.ref_id in box5_ids
                assert set(src.canonical_value_ids) <= set(canonical)
    assert checked > 200


# --- SVG safety -------------------------------------------------------------------------


def test_svg_is_safe_valid_and_bounded():
    for artifact in A.artifacts:
        root = ET.fromstring(artifact.svg)  # valid XML
        assert root.tag == f"{SVG_NS}svg"
        assert root.get("width") == str(artifact.width) and root.get("height") == str(
            artifact.height
        )
        lowered = artifact.svg.lower()
        for forbidden in (
            "<script",
            "javascript:",
            "foreignobject",
            "href=",
            "@import",
            "http://" + "x",
            "https://",
        ):
            assert forbidden not in lowered
        assert lowered.count("http://") == 1  # only the SVG namespace declaration
        assert artifact.byte_size < A.limits.max_svg_bytes
        assert artifact.name.endswith(".svg") and artifact.media_type == "image/svg+xml"
        assert artifact.content_reference == f"sha256/{artifact.sha256[:2]}/{artifact.sha256}.svg"


def test_svg_escapes_labels():
    spec = chart(A, "REVENUE_ANNUAL").model_copy(update={"title": 'A & B <script>"x"</script>'})
    svg = render_svg(spec)
    root = ET.fromstring(svg)
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    texts = [e.text for e in root.iter(f"{SVG_NS}text")]
    assert 'A & B <script>"x"</script>' in texts


def test_render_failure_is_isolated():
    def flaky(spec):
        if spec.chart_code == "ROE":
            raise RuntimeError("boom")
        return render_svg(spec)

    box4 = make(company())
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle = build_chart_bundle(box4, box5, generated_at=NOW, renderer=flaky)
    roe = chart(bundle, "ROE")
    assert roe.status == ChartStatus.RENDER_FAILED and roe.artifact_sha256 is None
    assert bundle.status == "PARTIAL" and bundle.statistics.render_failed == 1
    assert any(w.code == "RENDER_FAILED" for w in bundle.warnings)
    assert chart(bundle, "ROA").artifact_sha256 is not None


def test_resource_bounds():
    small = bundle_for(company(), limits=ChartLimits(max_charts=5))
    assert small.status == "PARTIAL" and len(small.charts) == 5
    assert any(w.code == "CHART_LIMIT_REACHED" for w in small.warnings)
    short = bundle_for(company(), limits=ChartLimits(max_points_per_series=3))
    assert all(
        len(s.points) <= 3
        for c in short.charts
        for s in c.series
        if c.frequency != AxisKind.CATEGORY
    )
    tiny = bundle_for(company(), limits=ChartLimits(max_svg_bytes=2_000))
    assert tiny.statistics.render_failed == len(tiny.charts)


def test_artifacts_are_written_once_by_content_hash(tmp_path):
    first = write_chart_artifacts(A, tmp_path)
    again = write_chart_artifacts(A, tmp_path)
    assert first == again and len(first) == len(A.artifacts)
    for path, artifact in zip(first, A.artifacts, strict=True):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256


def test_actual_chart_catalogue_is_emitted():
    rows = company() + quarters_rows()
    bundle = bundle_for(rows)
    for code in (
        "REVENUE_ANNUAL",
        "PAT_ANNUAL",
        "REVENUE_AND_PAT",
        "REVENUE_GROWTH",
        "PAT_MARGIN",
        "MARGIN_COMPARISON",
        "QUARTERLY_REVENUE",
        "QUARTERLY_PAT",
        "ROE",
        "ROCE",
        "CFO_TO_PAT",
        "DEBT_VS_CASH",
        "RECEIVABLE_DAYS",
        "INVENTORY_DAYS",
        "CASH_CONVERSION_CYCLE",
        "PIOTROSKI_COMPONENTS",
        "ALTMAN_COMPONENTS",
        "BENEISH_COMPONENTS",
        "TREND_SUMMARY",
        "HEALTH_SIGNALS",
        "EARNINGS_QUALITY_SIGNALS",
    ):
        spec = chart(bundle, code)
        assert spec.status in (ChartStatus.READY, ChartStatus.PARTIAL), code
        assert spec.artifact_sha256
    piotroski = chart(bundle, "PIOTROSKI_COMPONENTS")
    assert piotroski.series[0].encoding_note == "PASS = 1, FAIL = 0 (display only)"
    assert "score" in piotroski.subtitle


def test_no_raw_data_ai_or_network_imports():
    forbidden = (
        "adapters",
        "screener",
        "pypdf",
        "document",
        "evidence_retrieval",
        "aicredits",
        "analyst",
        "httpx",
        "socket",
        "urllib",
        "kite",
        "anthropic",
        "openai",
        "summary",
        "report",
    )
    for module in (chart_planner, chart_registry, financial_charts, svg_renderer, charts_domain):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not any(word in name for word in forbidden), (module.__name__, name)
