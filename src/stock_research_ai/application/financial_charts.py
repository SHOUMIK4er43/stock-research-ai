"""DETAILED_RESEARCH_V1 Box 6: FinancialChartBundle assembly.

Box 4 CanonicalFinancialDataset + Box 5 FinancialAnalyticsDataset -> plan every chart
definition per reporting basis -> render each emitted chart to SVG in isolation (one
failure never breaks the bundle) -> store each SVG once by content hash.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from uuid import uuid5

from stock_research_ai.application.chart_planner import NS, plan_all
from stock_research_ai.application.financial_analytics import canonical_analytics_json
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.svg_renderer import render_svg
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import CanonicalFinancialDataset
from stock_research_ai.domain.financial_charts import (
    CHART_REGISTRY_VERSION,
    RENDERER_VERSION,
    ChartArtifact,
    ChartLimits,
    ChartSpec,
    ChartStatistics,
    ChartStatus,
    ChartWarning,
    FinancialChartBundle,
    SkippedChart,
    SourceLayer,
    SourceReference,
)

Renderer = Callable[[ChartSpec], str]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_chart_bundle(
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    *,
    generated_at: datetime,
    limits: ChartLimits | None = None,
    renderer: Renderer = render_svg,
) -> FinancialChartBundle:
    bounds = limits or ChartLimits()
    if box5.source_box4.dataset_id != box4.dataset_id:
        raise ValueError("the Box 5 analytics were not built from this Box 4 dataset")
    box4_sha = _sha(canonical_dataset_json(box4))
    if box5.source_box4.canonical_sha256 != box4_sha:
        raise ValueError("the Box 4 dataset changed after the Box 5 analytics were built")
    box5_sha = _sha(canonical_analytics_json(box5))
    bundle_id = uuid5(
        NS,
        f"{box4_sha}|{box5_sha}|{CHART_REGISTRY_VERSION}|{RENDERER_VERSION}|"
        f"{bounds.model_dump_json()}",
    )
    warnings: list[ChartWarning] = []
    partial = False
    charts: list[ChartSpec] = []
    skipped: list[SkippedChart] = []
    artifacts: dict[str, ChartArtifact] = {}
    planned = plan_all(box4, box5, bounds)
    for item in planned:
        if item.skipped is not None:
            skipped.append(item.skipped)
            continue
        spec = item.spec
        assert spec is not None
        if len(charts) >= bounds.max_charts:
            partial = True
            warnings.append(
                ChartWarning(
                    code="CHART_LIMIT_REACHED",
                    detail=f"{spec.chart_code} {spec.reporting_basis.value}",
                )
            )
            continue
        try:
            svg = renderer(spec)
            size = len(svg.encode("utf-8"))
            if size > bounds.max_svg_bytes:
                raise ValueError(f"SVG of {size} bytes exceeds {bounds.max_svg_bytes}")
        except Exception as exc:  # isolation: one chart never breaks the bundle
            partial = True
            warnings.append(
                ChartWarning(
                    code="RENDER_FAILED",
                    detail=f"{spec.chart_code} {spec.reporting_basis.value}: "
                    f"{type(exc).__name__}: {str(exc)[:300]}",
                )
            )
            charts.append(
                spec.model_copy(
                    update={
                        "status": ChartStatus.RENDER_FAILED,
                        "status_reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
            )
            continue
        digest = _sha(svg)
        if digest not in artifacts:
            artifacts[digest] = ChartArtifact(
                sha256=digest,
                byte_size=size,
                width=spec.width,
                height=spec.height,
                name=f"{spec.chart_code.lower()}-{spec.reporting_basis.value.lower()}-"
                f"{digest[:12]}.svg",
                content_reference=f"sha256/{digest[:2]}/{digest}.svg",
                svg=svg,
            )
        charts.append(spec.model_copy(update={"artifact_sha256": digest}))
    charts.sort(key=lambda c: (c.reporting_basis.value, c.category.value, c.chart_code))
    skipped.sort(key=lambda s: (s.reporting_basis.value, s.category.value, s.chart_code))
    by_status = Counter(c.status for c in charts) + Counter(s.status for s in skipped)
    points = [p for c in charts for s in c.series for p in s.points]
    return FinancialChartBundle(
        bundle_id=bundle_id,
        company_key=box4.company_key,
        company_name=box4.company_name,
        sector=box5.sector.value,
        generated_at=generated_at,
        source_box4=SourceReference(
            layer=SourceLayer.BOX4,
            dataset_id=box4.dataset_id,
            sha256=box4_sha,
            schema_version=box4.schema_version,
        ),
        source_box5=SourceReference(
            layer=SourceLayer.BOX5,
            dataset_id=box5.analytics_id,
            sha256=box5_sha,
            schema_version=box5.schema_version,
        ),
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        charts=tuple(charts),
        skipped_charts=tuple(skipped),
        artifacts=tuple(sorted(artifacts.values(), key=lambda a: a.sha256)),
        warnings=tuple(sorted(warnings, key=lambda w: (w.code, w.detail or ""))),
        statistics=ChartStatistics(
            definitions_considered=len(planned),
            charts_ready=by_status[ChartStatus.READY],
            charts_partial=by_status[ChartStatus.PARTIAL],
            no_data=by_status[ChartStatus.NO_DATA],
            not_applicable=by_status[ChartStatus.NOT_APPLICABLE],
            unsupported_sector=by_status[ChartStatus.UNSUPPORTED_SECTOR],
            render_failed=by_status[ChartStatus.RENDER_FAILED],
            svg_artifacts=len(artifacts),
            svg_bytes=sum(a.byte_size for a in artifacts.values()),
            total_points=len(points),
            total_gaps=sum(1 for p in points if p.value is None),
            anomaly_annotations=sum(
                1 for c in charts for a in c.annotations if a.kind == "ANOMALY"
            ),
            charts_by_category=dict(sorted(Counter(c.category.value for c in charts).items())),
        ),
    )


def canonical_chart_json(bundle: FinancialChartBundle) -> str:
    return json.dumps(
        bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
