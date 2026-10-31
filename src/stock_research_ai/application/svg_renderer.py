"""DETAILED_RESEARCH_V1 Box 6: a small deterministic SVG renderer (svg-renderer/1).

Pure standard library. All geometry is Decimal and printed with fixed precision, so the
same ChartSpec always produces the same bytes. Output safety: no scripts, no external
references, no foreignObject, no remote fonts; every label is XML-escaped. Missing points
are drawn as visible 'n/a' gaps and lines are never connected across them; signed series
get a zero line; LOW-confidence inputs are drawn hollow (lines) or hatched (bars), so
meaning never depends on colour alone.
"""

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from xml.sax.saxutils import escape

from stock_research_ai.domain.financial_canonical import Confidence
from stock_research_ai.domain.financial_charts import ChartPoint, ChartSpec, ChartType

D = Decimal
FONT = "Helvetica, Arial, sans-serif"
PALETTE = ("#1f4e79", "#c55a11", "#548235", "#7030a0", "#2e75b6", "#7f6000")
DASHES = ("", "7 4", "2 3", "9 3 2 3", "4 2", "1 2")
MARKERS = ("circle", "square", "diamond", "triangle", "circle", "square")
DEFAULT_MARKER = D(4)
NO_HEADROOM = D(0)
SEVERITY_SIZE = {"LOW": D(4), "MEDIUM": D(6), "HIGH": D(8)}


def esc(text: str) -> str:
    return escape(text, {'"': "&quot;", "'": "&apos;"})


def n(value: Decimal) -> str:
    """Fixed-precision coordinate."""
    text = f"{value.quantize(D('0.1')):f}"
    return "0.0" if text == "-0.0" else text


def nice_step(span: Decimal) -> Decimal:
    if span <= 0:
        return D(1)
    raw = span / 5
    magnitude = D(10) ** raw.adjusted()
    for factor in (D(1), D(2), D("2.5"), D(5), D(10)):
        if raw <= factor * magnitude:
            return factor * magnitude
    return D(10) * magnitude


def domain(
    values: Sequence[Decimal],
    include_zero: bool,
    ticks: Sequence[Decimal] = (),
    headroom: Decimal = NO_HEADROOM,
) -> tuple[Decimal, Decimal, Decimal]:
    if ticks:
        return min(ticks), max(ticks), D(1)
    lo = min(values, default=D(0))
    hi = max(values, default=D(1))
    if include_zero:
        lo, hi = min(lo, D(0)), max(hi, D(0))
    hi += (hi - lo) * headroom  # room above the data for anomaly flags
    if lo == hi:
        pad = abs(lo) / 10 if lo != 0 else D(1)
        lo, hi = lo - pad, hi + pad
    step = nice_step(hi - lo)
    lo_t = (lo / step).to_integral_value(rounding=ROUND_FLOOR) * step
    hi_t = (hi / step).to_integral_value(rounding=ROUND_CEILING) * step
    if lo_t == hi_t:
        hi_t = lo_t + step
    return lo_t, hi_t, step


def tick_text(value: Decimal, step: Decimal) -> str:
    exponent = step.normalize().as_tuple().exponent
    places = max(0, -exponent) if isinstance(exponent, int) else 0
    return f"{value:,.{places}f}"


class Canvas:
    def __init__(self, spec: ChartSpec) -> None:
        self.spec = spec
        self.parts: list[str] = []
        self.uid = str(spec.chart_id)[:8]

    def add(self, element: str) -> None:
        self.parts.append(element)

    def text(
        self,
        x: Decimal,
        y: Decimal,
        content: str,
        size: int = 11,
        fill: str = "#333",
        anchor: str = "start",
        weight: str = "normal",
        extra: str = "",
    ) -> None:
        self.add(
            f'<text x="{n(x)}" y="{n(y)}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}"{extra}>{esc(content)}</text>'
        )

    def line(
        self,
        x1: Decimal,
        y1: Decimal,
        x2: Decimal,
        y2: Decimal,
        stroke: str = "#ddd",
        width: str = "1",
        dash: str = "",
        cls: str = "",
    ) -> None:
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        cls_attr = f' class="{cls}"' if cls else ""
        self.add(
            f'<line x1="{n(x1)}" y1="{n(y1)}" x2="{n(x2)}" y2="{n(y2)}" stroke="{stroke}" '
            f'stroke-width="{width}"{dash_attr}{cls_attr}/>'
        )

    def marker(
        self,
        shape: str,
        x: Decimal,
        y: Decimal,
        color: str,
        hollow: bool,
        size: Decimal = DEFAULT_MARKER,
        title: str = "",
    ) -> None:
        fill = "#ffffff" if hollow else color
        tip = f"<title>{esc(title)}</title>" if title else ""
        if shape == "circle":
            body = f'<circle cx="{n(x)}" cy="{n(y)}" r="{n(size)}"'
        elif shape == "square":
            body = (
                f'<rect x="{n(x - size)}" y="{n(y - size)}" width="{n(size * 2)}" '
                f'height="{n(size * 2)}"'
            )
        elif shape == "diamond":
            body = (
                f'<polygon points="{n(x)},{n(y - size)} {n(x + size)},{n(y)} '
                f'{n(x)},{n(y + size)} {n(x - size)},{n(y)}"'
            )
        else:
            body = (
                f'<polygon points="{n(x)},{n(y - size)} {n(x + size)},{n(y + size)} '
                f'{n(x - size)},{n(y + size)}"'
            )
        close = {"circle": "</circle>", "square": "</rect>"}.get(shape, "</polygon>")
        self.add(f'{body} fill="{fill}" stroke="{color}" stroke-width="1.5">{tip}{close}')

    def finish(self) -> str:
        w, h = self.spec.width, self.spec.height
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-labelledby="title-{self.uid}" '
            f'font-family="{FONT}">'
            f'<title id="title-{self.uid}">{esc(self.spec.title)}</title>'
            f'<defs><pattern id="hatch-{self.uid}" width="6" height="6" '
            f'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            f'<line x1="0" y1="0" x2="0" y2="6" stroke="#ffffff" stroke-width="2"/>'
            f"</pattern></defs>"
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>'
        )
        return head + "".join(self.parts) + "</svg>\n"


def _frame(c: Canvas) -> None:
    spec = c.spec
    c.text(D(28), D(36), spec.title, size=20, fill="#111111", weight="bold")
    c.text(D(28), D(58), spec.subtitle, size=12, fill="#444444")
    c.text(D(28), D(spec.height - 12), spec.footer, size=10, fill="#666666")


def _legend(c: Canvas, items: Sequence[tuple[str, str, int]], y: Decimal) -> None:
    """items: (kind, label, series index) - kind BAR / LINE / MARKER / NOTE."""
    x = D(28)
    for kind, label, index in items:
        color = PALETTE[index % len(PALETTE)]
        if kind == "BAR":
            c.add(f'<rect x="{n(x)}" y="{n(y - 9)}" width="12" height="10" fill="{color}"/>')
            x += 16
        elif kind in ("LINE", "MARKER"):
            c.line(
                x, y - 4, x + 18, y - 4, stroke=color, width="2", dash=DASHES[index % len(DASHES)]
            )
            c.marker(MARKERS[index % len(MARKERS)], x + 9, y - 4, color, False, D(3))
            x += 24
        c.text(x, y, label, size=11, fill="#333333")
        x += D(len(label) * 6 + 18)


def _point_title(p: ChartPoint, series_label: str) -> str:
    shown = p.display_value or "n/a"
    extra = f" | {p.status}" + (f" | {p.missing_reason}" if p.missing_reason else "")
    return f"{series_label} {p.x_label}: {shown}{extra}"[:300]


def _anomaly_flags(c: Canvas, centre: dict[str, Decimal], top: Decimal) -> bool:
    drawn = False
    stacked: dict[str, int] = {}
    for a in c.spec.annotations:
        if a.kind != "ANOMALY" or a.x_key not in centre:
            continue
        k = stacked.get(a.x_key, 0)
        stacked[a.x_key] = k + 1
        x, y = centre[a.x_key], top + 8 + D(k * 12)
        c.add(
            f'<polygon points="{n(x)},{n(y - 5)} {n(x + 5)},{n(y + 4)} {n(x - 5)},{n(y + 4)}" '
            f'fill="#ffffff" stroke="#b00020" stroke-width="1.5" class="anomaly">'
            f"<title>{esc(a.label + ' | ' + a.rule_id)}</title></polygon>"
        )
        c.text(x + 7, y + 4, (a.severity or "")[:1], size=9, fill="#b00020")
        drawn = True
    return drawn


def render_vertical(c: Canvas) -> None:
    spec = c.spec
    left, right, top, bottom = D(90), D(spec.width - 30), D(118), D(spec.height - 92)
    categories = list(spec.x_axis.categories) or list(
        dict.fromkeys((p.x_key, p.x_label) for s in spec.series for p in s.points)
    )
    keys = [k for k, _ in categories]
    divisor = spec.y_axis.scale_divisor
    shown = [p.value / divisor for s in spec.series for p in s.points if p.value is not None]
    ticks = [t for t, _ in spec.y_axis.tick_labels]
    bars = [s for s in spec.series if s.render_as == "BAR"]
    flagged = any(a.kind == "ANOMALY" for a in spec.annotations)
    lo, hi, step = domain(
        shown,
        spec.y_axis.zero_baseline or bool(bars) or any(v < 0 for v in shown),
        ticks,
        D("0.12") if flagged else D(0),
    )

    def y(v: Decimal) -> Decimal:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    tick = lo
    labels = dict(spec.y_axis.tick_labels)
    while tick <= hi:
        c.line(left, y(tick), right, y(tick), stroke="#e5e5e5")
        c.text(
            left - 8,
            y(tick) + 4,
            labels.get(tick, tick_text(tick, step)),
            size=11,
            fill="#555555",
            anchor="end",
        )
        tick += step
    zero_needed = lo < 0 < hi or (lo <= 0 <= hi and (bars or any(v < 0 for v in shown)))
    if zero_needed:
        c.line(left, y(D(0)), right, y(D(0)), stroke="#333333", width="1.2", cls="zero-line")
    c.text(D(28), top - 10, spec.y_axis.display_unit, size=11, fill="#555555")
    band = (right - left) / max(len(keys), 1)
    centre = {k: left + band * (i + D("0.5")) for i, k in enumerate(keys)}
    every = 1 if len(keys) <= 14 else 2
    for i, (k, label) in enumerate(categories):
        if i % every == 0:
            c.text(centre[k], bottom + 18, label, size=11, fill="#444444", anchor="middle")
    c.text(
        (left + right) / 2, bottom + 40, spec.x_axis.label, size=11, fill="#555555", anchor="middle"
    )
    base = y(max(lo, min(D(0), hi)))
    group = band * D("0.72")
    width = group / max(len(bars), 1)
    hatch = f"url(#hatch-{c.uid})"
    gap_drawn: set[str] = set()
    for b_index, s in enumerate(bars):
        index = spec.series.index(s)
        color = PALETTE[index % len(PALETTE)]
        for p in s.points:
            if p.x_key not in centre:
                continue
            x = centre[p.x_key] - group / 2 + width * b_index
            if p.value is None:
                c.text(
                    x + width / 2,
                    bottom - 6,
                    "n/a",
                    size=10,
                    fill=color if len(spec.series) > 1 else "#888888",
                    anchor="middle",
                    extra=' class="gap"',
                )
                gap_drawn.add(p.x_key)
                continue
            v = y(p.value / divisor)
            top_y, height = (v, base - v) if v < base else (base, v - base)
            tip = f"<title>{esc(_point_title(p, s.label))}</title>"
            c.add(
                f'<rect x="{n(x + 1)}" y="{n(top_y)}" width="{n(max(width - 2, D(1)))}" '
                f'height="{n(max(height, D("0.5")))}" fill="{color}" class="bar">{tip}</rect>'
            )
            if p.confidence == Confidence.LOW:
                c.add(
                    f'<rect x="{n(x + 1)}" y="{n(top_y)}" width="{n(max(width - 2, D(1)))}" '
                    f'height="{n(max(height, D("0.5")))}" fill="{hatch}" class="low-confidence"/>'
                )
    for s in spec.series:
        if s.render_as == "BAR":
            continue
        index = spec.series.index(s)
        color = PALETTE[index % len(PALETTE)]
        run: list[str] = []
        for p in s.points:
            if p.x_key not in centre:
                continue
            if p.value is None:
                if len(run) > 1:
                    c.add(
                        f'<path d="{" ".join(run)}" fill="none" stroke="{color}" '
                        f'stroke-width="2" stroke-dasharray="{DASHES[index % len(DASHES)]}" '
                        'class="segment"/>'
                    )
                run = []
                c.text(
                    centre[p.x_key],
                    bottom - 6 - D(12 * index),
                    "n/a",
                    size=10,
                    fill=color if len(spec.series) > 1 else "#888888",
                    anchor="middle",
                    extra=' class="gap"',
                )
                gap_drawn.add(p.x_key)
                continue
            px, py = centre[p.x_key], y(p.value / divisor)
            run.append(f"{'M' if not run else 'L'}{n(px)},{n(py)}")
        if len(run) > 1:
            c.add(
                f'<path d="{" ".join(run)}" fill="none" stroke="{color}" stroke-width="2" '
                f'stroke-dasharray="{DASHES[index % len(DASHES)]}" class="segment"/>'
            )
        for p in s.points:
            if p.value is not None and p.x_key in centre:
                c.marker(
                    MARKERS[index % len(MARKERS)],
                    centre[p.x_key],
                    y(p.value / divisor),
                    color,
                    p.confidence == Confidence.LOW,
                    title=_point_title(p, s.label),
                )
    flags = _anomaly_flags(c, centre, top)
    items: list[tuple[str, str, int]] = [
        ("BAR" if s.render_as == "BAR" else "LINE", s.label, i) for i, s in enumerate(spec.series)
    ]
    notes = []
    if any(p.confidence == Confidence.LOW for s in spec.series for p in s.points):
        notes.append("hatched bar / hollow marker = LOW-confidence input")
    if gap_drawn:
        notes.append("n/a = not available (gap, not interpolated; coloured by series)")
    if flags:
        notes.append("triangle = Box 5 anomaly flag (L/M/H)")
    _legend(c, items, D(84))
    if notes:
        c.text(right, D(102), " · ".join(notes), size=10, fill="#666666", anchor="end")


def render_horizontal(c: Canvas) -> None:
    spec = c.spec
    left, right, top, bottom = D(300), D(spec.width - 140), D(96), D(spec.height - 80)
    points = [p for s in spec.series for p in s.points]
    divisor = spec.y_axis.scale_divisor
    shown = [p.value / divisor for p in points if p.value is not None]
    ticks = [t for t, _ in spec.y_axis.tick_labels]
    lo, hi, step = domain(shown, True, ticks)
    if ticks:
        lo, hi = min(lo, D(0)), max(hi, D(0))

    def x(v: Decimal) -> Decimal:
        return left + (v - lo) / (hi - lo) * (right - left)

    labels = dict(spec.y_axis.tick_labels)
    tick = lo
    while tick <= hi:
        c.line(x(tick), top, x(tick), bottom, stroke="#e5e5e5")
        c.text(
            x(tick),
            bottom + 16,
            labels.get(tick, tick_text(tick, step)),
            size=10,
            fill="#555555",
            anchor="middle",
        )
        tick += step
    if lo <= 0 <= hi:
        c.line(x(D(0)), top, x(D(0)), bottom, stroke="#333333", width="1.2", cls="zero-line")
    c.text(
        (left + right) / 2,
        bottom + 34,
        spec.y_axis.display_unit,
        size=11,
        fill="#555555",
        anchor="middle",
    )
    row = (bottom - top) / max(len(points), 1)
    height = min(row * D("0.6"), D(22))
    color = PALETTE[0]
    gaps = False
    for i, p in enumerate(points):
        cy = top + row * (i + D("0.5"))
        c.text(left - 10, cy + 4, p.x_label, size=11, fill="#333333", anchor="end")
        if p.value is None:
            c.text(
                x(max(lo, D(0))) + 6,
                cy + 4,
                f"n/a · {p.missing_reason or p.status}"[:90],
                size=10,
                fill="#888888",
                extra=' class="gap"',
            )
            gaps = True
            continue
        v = x(p.value / divisor)
        start = x(max(lo, min(D(0), hi)))
        x0, w = (v, start - v) if v < start else (start, v - start)
        tip = f"<title>{esc(_point_title(p, spec.series[0].label))}</title>"
        c.add(
            f'<rect x="{n(x0)}" y="{n(cy - height / 2)}" width="{n(max(w, D(2)))}" '
            f'height="{n(height)}" fill="{color}" class="bar">{tip}</rect>'
        )
        c.text(max(v, start) + 6, cy + 4, p.display_value or "", size=10, fill="#333333")
    note = spec.series[0].encoding_note if spec.series else None
    texts = [t for t in (note, "n/a = not available" if gaps else None) if t]
    if texts:
        c.text(D(28), D(82), " · ".join(texts), size=10, fill="#666666")


def render_scatter(c: Canvas) -> None:
    spec = c.spec
    left, right, top, bottom = D(300), D(spec.width - 40), D(96), D(spec.height - 80)
    categories = list(spec.x_axis.categories)
    points = [p for s in spec.series for p in s.points]
    rows = sorted({p.y_category or "" for p in points})
    band = (right - left) / max(len(categories), 1)
    centre = {k: left + band * (i + D("0.5")) for i, (k, _) in enumerate(categories)}
    row = (bottom - top) / max(len(rows), 1)
    middle = {r: top + row * (i + D("0.5")) for i, r in enumerate(rows)}
    for r, cy in middle.items():
        c.line(left, cy, right, cy, stroke="#eeeeee")
        c.text(
            left - 10, cy + 4, r.replace("_", " ").lower(), size=11, fill="#333333", anchor="end"
        )
    for k, label in categories:
        c.line(centre[k], top, centre[k], bottom, stroke="#f2f2f2")
        c.text(centre[k], bottom + 18, label, size=10, fill="#444444", anchor="middle")
    shapes = {"LOW": "circle", "MEDIUM": "square", "HIGH": "triangle"}
    for p in points:
        if p.x_key not in centre:
            continue
        cx, cy = centre[p.x_key], middle[p.y_category or ""]
        severity = p.severity or "LOW"
        c.marker(
            shapes.get(severity, "circle"),
            cx,
            cy,
            "#b00020",
            severity == "LOW",
            SEVERITY_SIZE.get(severity, D(4)),
            title=_point_title(p, p.y_category or ""),
        )
        c.text(cx + 10, cy + 4, severity[:1], size=9, fill="#b00020")
    c.text(
        D(28),
        D(82),
        "circle = LOW, square = MEDIUM, triangle = HIGH severity · rule-based "
        "flags from Box 5 that may require review",
        size=10,
        fill="#666666",
    )


def render_svg(spec: ChartSpec) -> str:
    c = Canvas(spec)
    _frame(c)
    if spec.chart_type == ChartType.HORIZONTAL_BAR:
        render_horizontal(c)
    elif spec.chart_type == ChartType.SCATTER:
        render_scatter(c)
    else:
        render_vertical(c)
    return c.finish()
