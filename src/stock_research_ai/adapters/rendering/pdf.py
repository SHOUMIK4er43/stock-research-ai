"""DETAILED_RESEARCH_V1 Box 15: RenderDocument -> institutional PDF (reportlab).

Consumes the same renderer-neutral RenderDocument as Markdown / HTML; it never parses another
rendered format. Safety and determinism:
  * every report string is XML-escaped before it reaches reportlab's paragraph mini-markup,
    so report text can never open an <img>, <a>, <font> or any other tag; links are emitted
    only for absolute http(s) source-register URLs (already filtered upstream);
  * reportlab's URL opener keeps no trusted host or scheme, so nothing remote or file:// is
    ever read; no JavaScript is ever written;
  * invariant mode fixes the creation date and document ID: the same document gives the same
    bytes;
  * bounded: a cell / page / time budget fails the PDF explicitly (RenderError) — it never
    drops content. Wide tables are split into column groups (the row label repeats), long
    tables into row chunks that repeat their header row, and an over-long cell is moved to a
    numbered note under its table instead of being clipped.
"""

import threading
import time
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import Paragraph as RLParagraph

from stock_research_ai.domain import report_rendering as rr
from stock_research_ai.domain.report_rendering import (
    MAX_PDF_BYTES,
    MAX_PDF_CELLS,
    MAX_PDF_PAGES,
    MAX_PDF_SECONDS,
    PDF_ROWS_PER_CHUNK,
    RenderDocument,
    RenderError,
)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT = "DejaVuSansCondensed"
FONT_BOLD = "DejaVuSansCondensed-Bold"
PDF_ENGINE = "reportlab"

rl_config.trustedHosts = None
rl_config.trustedSchemes = []
rl_config.invariant = 1

_REGISTERED = False
_LOCK = threading.Lock()  # reportlab keeps module-level state; one PDF build at a time

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
FRAME_W = PAGE_W - 2 * MARGIN
FRAME_H = PAGE_H - 2 * MARGIN - 10 * mm
LABEL_MIN = 90.0
COL_MIN = 50.0
TABLE_FONT = 7.2
MAX_CELL_HEIGHT = FRAME_H * 0.6
RECORD_ROWS = 200  # a text-heavy table this short is laid out one record per row

TONES: dict[str, tuple[colors.Color, colors.Color]] = {
    "RED": (colors.HexColor("#a4161a"), colors.HexColor("#fdecec")),
    "YELLOW": (colors.HexColor("#8a6100"), colors.HexColor("#fff6dc")),
    "POSITIVE": (colors.HexColor("#1f6f3f"), colors.HexColor("#e9f6ee")),
    "LIMITATION": (colors.HexColor("#3d4a5c"), colors.HexColor("#eef1f5")),
    "DILIGENCE": (colors.HexColor("#5b3b8c"), colors.HexColor("#f2edf9")),
    "SCENARIO": (colors.HexColor("#14527a"), colors.HexColor("#e8f2fa")),
    "NOTICE": (colors.HexColor("#7a4b00"), colors.HexColor("#fff8eb")),
    "NEUTRAL": (colors.HexColor("#5b6470"), colors.HexColor("#f5f7fa")),
}
TYPE_COLORS = {
    "FACT": "#1d3557", "CALCULATION": "#22543d", "MANAGEMENT_STATEMENT": "#5b3b8c",
    "ANALYST_INFERENCE": "#6b4f1d", "SCENARIO": "#14527a", "DATA_LIMITATION": "#3d4a5c",
}
SEVERITY_COLORS = {"RED": "#a4161a", "YELLOW": "#8a6100", "GREEN": "#1f6f3f"}
LINE = colors.HexColor("#c9cfd8")
SOFT = colors.HexColor("#eef1f5")
FORECAST_BG = colors.HexColor("#e8f2fa")


def _register_fonts() -> None:
    global _REGISTERED
    if not _REGISTERED:
        pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / f"{FONT}.ttf")))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / f"{FONT_BOLD}.ttf")))
        pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT,
                                      boldItalic=FONT_BOLD)
        _REGISTERED = True


def x(text: str) -> str:
    """Escape report text for reportlab markup; control characters are dropped."""
    clean = "".join(ch if ch >= " " or ch in "\t" else " " for ch in text)
    return xml_escape(clean, {'"': "&quot;"})


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle("body", fontName=FONT, fontSize=8.6, leading=11.2, spaceAfter=2)
    return {
        "body": base,
        "title": ParagraphStyle("title", parent=base, fontName=FONT_BOLD, fontSize=18,
                                leading=22, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=13, leading=16,
                             spaceBefore=8, spaceAfter=4, keepWithNext=1),
        "h3": ParagraphStyle("h3", parent=base, fontName=FONT_BOLD, fontSize=10, leading=13,
                             spaceBefore=6, spaceAfter=2, keepWithNext=1),
        "h4": ParagraphStyle("h4", parent=base, fontName=FONT_BOLD, fontSize=9, leading=12,
                             spaceBefore=6, spaceAfter=1, keepWithNext=1),
        "meta": ParagraphStyle("meta", parent=base, fontSize=7.4, leading=9.4,
                               textColor=colors.HexColor("#5b6470")),
        "claim": ParagraphStyle("claim", parent=base, leftIndent=6, borderPadding=2,
                                spaceBefore=1, spaceAfter=3),
        "quote": ParagraphStyle("quote", parent=base, fontSize=8, leading=10.4, leftIndent=16,
                                textColor=colors.HexColor("#333333"), spaceAfter=3),
        "cell": ParagraphStyle("cell", parent=base, fontSize=TABLE_FONT,
                               leading=TABLE_FONT + 1.8, spaceAfter=0),
        "num": ParagraphStyle("num", parent=base, fontSize=TABLE_FONT,
                              leading=TABLE_FONT + 1.8, spaceAfter=0, alignment=TA_RIGHT),
        "head": ParagraphStyle("head", parent=base, fontName=FONT_BOLD, fontSize=TABLE_FONT,
                               leading=TABLE_FONT + 1.8, spaceAfter=0),
    }


class _Bookmark(Flowable):
    """A zero-size anchor that registers a PDF outline entry for a section."""

    def __init__(self, key: str, title: str) -> None:
        super().__init__()
        self.key, self.title = key, title
        self.width = self.height = 0

    def draw(self) -> None:
        self.canv.bookmarkPage(self.key)
        self.canv.addOutlineEntry(self.title, self.key, level=0)


class _Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + MAX_PDF_SECONDS
        self.exceeded: RenderError | None = None

    def check(self, page: int) -> None:
        if page > MAX_PDF_PAGES:
            self.exceeded = RenderError("RENDER_LIMIT_EXCEEDED",
                                        f"more than {MAX_PDF_PAGES} PDF pages")
        elif time.monotonic() > self.deadline:
            self.exceeded = RenderError("RENDER_LIMIT_EXCEEDED",
                                        f"PDF rendering exceeded {MAX_PDF_SECONDS:.0f} seconds")
        if self.exceeded is not None:
            raise self.exceeded


def _numbered_canvas(doc: RenderDocument) -> type[Canvas]:
    head = f"{doc.company_name} — Detailed Research — as of {doc.as_of}"
    foot = f"Report {doc.report_id[:8]} · Research and information only — not investment advice"

    class NumberedCanvas(Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["invariant"] = 1
            super().__init__(*args, **kwargs)
            self._pages: list[dict[str, Any]] = []
            self.setTitle(f"{doc.title} — as of {doc.as_of}")
            self.setAuthor("Stock Research AI (Box 15 renderer)")
            self.setSubject(f"Box 14 SHA-256 {doc.box14_sha256}")
            self.setCreator(rr.RENDERER_VERSION)
            self.setProducer(PDF_ENGINE)

        def showPage(self) -> None:  # noqa: N802 - reportlab API
            self._pages.append(dict(self.__dict__))
            self._startPage()  # type: ignore[attr-defined]

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._chrome(total)
                Canvas.showPage(self)
            Canvas.save(self)

        def _chrome(self, total: int) -> None:
            self.saveState()
            self.setStrokeColor(LINE)
            self.setLineWidth(0.5)
            self.line(MARGIN, PAGE_H - MARGIN + 2, PAGE_W - MARGIN, PAGE_H - MARGIN + 2)
            self.line(MARGIN, MARGIN - 4, PAGE_W - MARGIN, MARGIN - 4)
            self.setFont(FONT, 7.2)
            self.setFillColor(colors.HexColor("#5b6470"))
            self.drawString(MARGIN, PAGE_H - MARGIN + 5, head[:150])
            self.drawString(MARGIN, MARGIN - 12, foot)
            self.drawRightString(PAGE_W - MARGIN, MARGIN - 12,
                                 f"Page {self.getPageNumber()} of {total}")
            self.restoreState()

    return NumberedCanvas


class _Renderer:
    def __init__(self, doc: RenderDocument) -> None:
        self.doc = doc
        self.st = _styles()
        self.story: list[Flowable] = []

    def p(self, markup: str, style: str = "body") -> RLParagraph:
        return RLParagraph(markup, self.st[style])

    # --- blocks --------------------------------------------------------------------------------

    def tone_bar(self, title: str, tone: str) -> RLParagraph:
        fg, bg = TONES.get(tone, TONES["NEUTRAL"])
        style = ParagraphStyle(f"bar-{tone}", parent=self.st["h3"], textColor=fg,
                               backColor=bg, borderPadding=3, borderColor=fg,
                               borderWidth=0.6, spaceBefore=8, spaceAfter=5)
        return RLParagraph(x(title), style)

    def claim(self, c: rr.ClaimView, tone: str) -> list[Flowable]:
        color = TYPE_COLORS.get(c.claim_type, "#333333")
        badges = []
        if c.severity:
            sev = SEVERITY_COLORS.get(c.severity, "#333333")
            badges.append(f'<font name="{FONT_BOLD}" color="{sev}">[{x(c.severity)}]</font>')
        badges.append(f'<font name="{FONT_BOLD}" color="{color}">[{x(c.type_label)}]</font>')
        if c.basis != "NOT_APPLICABLE":
            badges.append(f'<font name="{FONT_BOLD}">[{x(c.basis)}]</font>')
        if c.period:
            badges.append(f'<font color="#5b6470">[{x(c.period)}]</font>')
        text = " ".join(badges) + " " + x(c.statement)
        if c.value is not None:
            text += f' <font name="{FONT_BOLD}">{x(c.value)}</font>'
            if c.exact_value and c.exact_value.lstrip("-") not in c.value:
                text += f' <font color="#5b6470">(exact {x(c.exact_value)})</font>'
        if c.refs:
            more = f" +{c.more_refs} more" if c.more_refs else ""
            text += f' <font color="#5b6470">[{x(", ".join(c.refs))}{x(more)}]</font>'
        fg, bg = TONES.get(tone, TONES["NEUTRAL"])
        style = ParagraphStyle(f"claim-{tone}", parent=self.st["claim"],
                               backColor=bg if tone != "NEUTRAL" else None,
                               borderColor=fg if tone != "NEUTRAL" else LINE,
                               borderWidth=0.4)
        out: list[Flowable] = [RLParagraph(text, style)]
        if c.source_text:
            label = "Management statement (verbatim)" if c.claim_type == (
                "MANAGEMENT_STATEMENT") else "Source text (verbatim)"
            out.append(self.p(f'<font color="#5b3b8c">{x(label)}:</font> '
                              f"“{x(c.source_text)}”", "quote"))
        if c.limitations:
            out.append(self.p(f"Limitations: {x(', '.join(c.limitations))}", "meta"))
        return out

    def claims(self, b: rr.ClaimList) -> None:
        if b.title:
            self.story.append(self.tone_bar(b.title, b.tone))
        for c in b.claims:
            self.story.extend(self.claim(c, b.tone))
        if not b.claims and b.empty_text:
            self.story.append(self.p(x(b.empty_text), "meta"))
        for label, text in b.extra:
            self.story.append(self.p(f"<b>{x(label)}:</b> {x(text)}"))

    def key_values(self, b: rr.KeyValues) -> None:
        if b.title:
            self.story.append(self.tone_bar(b.title, b.tone))
        rows = [[self.p(f"<b>{x(k)}</b>", "cell"), self.p(x(v), "cell")] for k, v in b.rows]
        t = Table(rows, colWidths=[FRAME_W * 0.34, FRAME_W * 0.66], hAlign="LEFT")
        fg, bg = TONES.get(b.tone, TONES["NEUTRAL"])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINE),
                               ("BACKGROUND", (0, 0), (0, -1), bg),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        self.story.append(t)

    def paragraph(self, b: rr.Paragraph) -> None:
        lead = f"<b>{x(b.label)}:</b> " if b.label else ""
        if b.tone == "NEUTRAL":
            if b.label:  # a lead line never sits alone at the foot of a page
                self.story.append(CondPageBreak(120))
            self.story.append(RLParagraph(lead + x(b.text), self.st["body"]))
            return
        fg, bg = TONES.get(b.tone, TONES["NEUTRAL"])
        style = ParagraphStyle(f"para-{b.tone}", parent=self.st["body"], backColor=bg,
                               borderColor=fg, borderWidth=0.6, borderPadding=3,
                               spaceBefore=4, spaceAfter=6)
        self.story.append(RLParagraph(lead + x(b.text), style))

    def chart(self, b: rr.Chart) -> None:
        self.story.append(self.tone_bar(f"Chart: {b.title}", "NEUTRAL"))
        self.story.append(self.p(
            f"Chart image not rendered in the PDF (see the HTML export); Box 6 definition "
            f"{x(b.code)} · basis {x(b.basis)} · upstream status {x(b.status)} · "
            f"{x(b.purpose)} · metrics: {x(', '.join(b.metrics) or 'not stated')}", "meta"))

    # --- tables --------------------------------------------------------------------------------

    def _widths(self, cols: Sequence[int], t: rr.Table) -> list[float]:
        """Every column first gets room for its longest word (capped), so codes are not broken
        mid-word; the rest of the frame goes to the columns whose full text is longest."""
        need, want = self._needs(cols, t)
        total = sum(need)
        if total >= FRAME_W:
            return [w * FRAME_W / total for w in need]
        spare = FRAME_W - total
        extra = [w - n for w, n in zip(want, need, strict=True)]
        pool = sum(extra)
        if pool <= 0:
            return [n * FRAME_W / total for n in need]
        widths = [n + min(e, spare * e / pool) for n, e in zip(need, extra, strict=True)]
        return [w * FRAME_W / sum(widths) for w in widths]

    def _needs(self, cols: Sequence[int], t: rr.Table) -> tuple[list[float], list[float]]:
        need: list[float] = []
        want: list[float] = []
        for j in cols:
            texts = [t.columns[j].label, " ".join(t.columns[j].tags)] + [
                (r.label if j == 0 else r.cells[j - 1].text) for r in t.rows[:400]]
            words = [w for s in texts for w in s[:400].split()]
            word = max((stringWidth(w, FONT_BOLD, TABLE_FONT) for w in words), default=20)
            full = max((stringWidth(s[:300], FONT, TABLE_FONT) for s in texts), default=20)
            floor = LABEL_MIN if j == 0 else 34.0
            need.append(max(floor, min(word, 95.0) + 8))
            want.append(max(need[-1], min(full + 8, 330.0)))
        return need, want

    def _text_heavy(self, t: rr.Table) -> bool:
        text_cols = sum(1 for c in t.columns[1:] if not c.numeric)
        need, _ = self._needs(range(len(t.columns)), t)
        return text_cols >= 3 and sum(need) > FRAME_W and 0 < len(t.rows) <= RECORD_ROWS

    def records(self, t: rr.Table) -> None:
        """A text-heavy table too wide for the page: one key/value card per row, every
        column kept, in the table's own column order."""
        self.story.append(self.p("Laid out as one record per row (too wide for the page as a "
                                 "grid); every column is shown.", "meta"))
        labels = [c.label + (f" [{' · '.join(c.tags)}]" if c.tags else "") for c in t.columns]
        for row in t.rows:
            tags = f" [{' · '.join(row.tags)}]" if row.tags else ""
            data: list[list[Any]] = [[self.p(f"<b>{x(labels[0])}</b>", "cell"),
                                      self.p(f"<b>{x(row.label)}{x(tags)}</b>", "cell")]]
            for j, cell in enumerate(row.cells, start=1):
                body = x(cell.text) + (f' <font color="#5b6470">({x(cell.note)})</font>'
                                       if cell.note else "")
                if cell.link is not None:
                    body = f'<a href="{x(cell.link)}" color="#14527a">{body}</a>'
                data.append([self.p(x(labels[j]), "cell"), self.p(body, "cell")])
            tbl = Table(data, colWidths=[FRAME_W * 0.26, FRAME_W * 0.74], hAlign="LEFT",
                        splitByRow=1)
            tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.35, LINE),
                                     ("BACKGROUND", (0, 0), (-1, 0), SOFT),
                                     ("BACKGROUND", (0, 1), (0, -1), SOFT),
                                     ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
            self.story.append(tbl)
            self.story.append(Spacer(1, 3))

    def _groups(self, t: rr.Table) -> list[list[int]]:
        others = list(range(1, len(t.columns)))
        per = max(1, int((FRAME_W - LABEL_MIN) // COL_MIN))
        if not others:
            return [[0]]
        return [[0, *others[i:i + per]] for i in range(0, len(others), per)]

    def _cell(self, text: str, kind: str, forecast: bool, width: float, note: str | None,
              link: str | None, overflow: list[str], tag: str) -> RLParagraph:
        body = x(text)
        if note:
            body += f' <font color="#5b6470">({x(note)})</font>'
        if link is not None:
            body = f'<a href="{x(link)}" color="#14527a">{body}</a>'
        if kind in ("UNAVAILABLE", "NOT_APPLICABLE", "NO_CELL"):
            body = f'<font color="#5b6470">{body}</font>'
        per_line = max(1.0, (width - 6) / (TABLE_FONT * 0.5))
        lines = len(text) / per_line + 1
        if lines * (TABLE_FONT + 1.8) > MAX_CELL_HEIGHT:
            overflow.append(f"[{tag}] {text}")
            body = f"<i>Long text — see note {x(tag)} below the table</i>"
        return self.p(body, "num" if kind == "NUMBER" else "cell")

    def table(self, t: rr.Table) -> None:
        self.story.append(CondPageBreak(90))  # title + header + first rows stay together
        head: list[Flowable] = [self.tone_bar(t.title, "NEUTRAL")]
        if t.tags:
            head.append(self.p(x(" · ".join(t.tags)), "meta"))
        for n in t.notices:
            head.append(self.tone_bar(n, "NOTICE"))
        self.story.append(KeepTogether(head))
        overflow: list[str] = []
        if self._text_heavy(t):
            self.records(t)
            groups: list[list[int]] = []
        else:
            groups = self._groups(t)
        for g_index, cols in enumerate(groups, start=1):
            widths = self._widths(cols, t)
            header = []
            for j in cols:
                c = t.columns[j]
                label = x(c.label) + (f'<br/><font color="#14527a">[{x(" · ".join(c.tags))}]'
                                      "</font>" if c.tags else "")
                header.append(self.p(label, "head"))
            body_rows: list[list[Any]] = []
            for row in t.rows:
                cells: list[Any] = []
                for k, j in enumerate(cols):
                    if j == 0:
                        tags = (f' <font color="#14527a">[{x(" · ".join(row.tags))}]</font>'
                                if row.tags else "")
                        cells.append(self.p(f"{x(row.label)}{tags}", "cell"))
                        continue
                    cell = row.cells[j - 1]
                    cells.append(self._cell(cell.text, cell.kind, cell.forecast, widths[k],
                                            cell.note, cell.link, overflow,
                                            f"{len(overflow) + 1}"))
                body_rows.append(cells)
            caption = (f"Columns {cols[1] if len(cols) > 1 else 1}–{cols[-1]} of "
                       f"{len(t.columns) - 1} (row label repeated)" if len(groups) > 1 else "")
            if caption:
                self.story.append(self.p(f"{x(t.title)} — part {g_index} of {len(groups)}: "
                                         f"{x(caption)}", "meta"))
            if not body_rows:
                self.story.append(self.p("No rows supplied upstream.", "meta"))
            for start in range(0, max(1, len(body_rows)), PDF_ROWS_PER_CHUNK):
                chunk = body_rows[start:start + PDF_ROWS_PER_CHUNK]
                if not chunk:
                    break
                tbl = Table([header, *chunk], colWidths=widths, repeatRows=1, hAlign="LEFT",
                            splitByRow=1)
                ts: list[tuple[Any, ...]] = [
                    ("GRID", (0, 0), (-1, -1), 0.35, LINE),
                    ("BACKGROUND", (0, 0), (-1, 0), SOFT),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                ]
                ts += self._forecast_styles(cols, t, start, len(chunk))
                tbl.setStyle(TableStyle(ts))
                if start:
                    self.story.append(self.p(f"{x(t.title)} (continued)", "meta"))
                self.story.append(tbl)
                self.story.append(Spacer(1, 3))
        for f in t.footnotes:
            if f:
                self.story.append(self.p(f"<i>{x(f)}</i>", "meta"))
        if t.refs:
            more = f" (+{t.more_refs} more)" if t.more_refs else ""
            self.story.append(self.p(f"Sources: {x(', '.join(t.refs))}{x(more)}", "meta"))
        for note in overflow:
            self.story.append(self.p(f"Note {x(note)}", "meta"))
        self.story.append(Spacer(1, 6))

    def _forecast_styles(self, cols: Sequence[int], t: rr.Table, start: int,
                         count: int) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        for k, j in enumerate(cols):
            if j and any(r.cells[j - 1].forecast for r in t.rows[start:start + count]):
                out.append(("BACKGROUND", (k, 1), (k, count), FORECAST_BG))
        return out

    # --- document ------------------------------------------------------------------------------

    def block(self, b: rr.Block) -> None:
        if isinstance(b, rr.Paragraph):
            self.paragraph(b)
        elif isinstance(b, rr.ClaimList):
            self.claims(b)
        elif isinstance(b, rr.KeyValues):
            self.key_values(b)
        elif isinstance(b, rr.Table):
            self.table(b)
        elif isinstance(b, rr.Chart):
            self.chart(b)
        else:  # pragma: no cover - the model is closed
            raise TypeError(type(b).__name__)

    def build(self) -> list[Flowable]:
        d = self.doc
        self.story.append(self.p(x(d.title), "title"))
        self.story.append(self.paragraph_flow(d.disclaimer, "NOTICE"))
        facts = [*d.identifiers, *d.header_facts, ("Composed at", d.generated_at)]
        self.key_values(rr.KeyValues(None, "NEUTRAL", tuple(facts)))
        self.story.append(self.p("Contents", "h2"))
        for s in d.sections:
            self.story.append(self.p(f'<a href="#s{s.order}" color="#14527a">{s.order}. '
                                     f"{x(s.title)}</a> — {x(s.status_label)}"))
        for s in d.sections:
            self.story.append(CondPageBreak(60 * mm))
            self.story.append(_Bookmark(f"s{s.order}", f"{s.order}. {s.title}"))
            title = f'<a name="s{s.order}"/>{s.order}. {x(s.title)} ' \
                    f'<font size="9" color="#5b6470">[{x(s.status_label)}]</font>'
            self.story.append(self.p(title, "h2"))
            if s.prominent:
                self.story.append(self.paragraph_flow(
                    "READ FIRST — due-diligence status", "NOTICE"))
            if s.missing:
                self.story.append(self.p(f"Missing input families: {x(', '.join(s.missing))}",
                                         "meta"))
            for b in s.blocks:
                self.block(b)
        if d.warnings:
            self.story.append(self.p("Rendering notes", "h2"))
            for w in d.warnings:
                self.story.append(self.p(x(w), "meta"))
        self.story.append(Spacer(1, 8))
        self.story.append(self.paragraph_flow(d.disclaimer, "NOTICE"))
        return self.story

    def paragraph_flow(self, text: str, tone: str) -> RLParagraph:
        fg, bg = TONES[tone]
        return RLParagraph(x(text), ParagraphStyle(
            f"pf-{tone}", parent=self.st["body"], backColor=bg, borderColor=fg,
            borderWidth=0.6, borderPadding=3, spaceBefore=4, spaceAfter=6))


def render_pdf(doc: RenderDocument) -> bytes:
    """Deterministic PDF bytes for one RenderDocument, or RenderError."""
    if doc.cells > MAX_PDF_CELLS:
        raise RenderError("RENDER_LIMIT_EXCEEDED",
                          f"{doc.cells} table cells exceed the PDF limit {MAX_PDF_CELLS}")
    budget = _Budget()
    with _LOCK:
        _register_fonts()
        story = _Renderer(doc).build()
        out = BytesIO()

        def on_page(canvas: Canvas, _doc: Any) -> None:
            budget.check(canvas.getPageNumber())

        template = BaseDocTemplate(
            out, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN + 4 * mm,
            bottomMargin=MARGIN + 2 * mm, title=f"{doc.title} — as of {doc.as_of}",
            author="Stock Research AI", invariant=1)
        frame = Frame(template.leftMargin, template.bottomMargin, template.width,
                      template.height, id="body")
        template.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=on_page)])
        try:
            template.build(story, canvasmaker=_numbered_canvas(doc))
        except Exception as exc:  # reportlab raises many types (LayoutError, ValueError ...)
            if budget.exceeded is not None:  # reportlab may re-wrap the budget error
                raise budget.exceeded from exc
            if isinstance(exc, RenderError):
                raise
            raise RenderError("RENDER_ENGINE_ERROR", type(exc).__name__) from exc
    data = out.getvalue()
    if len(data) > MAX_PDF_BYTES:
        raise RenderError("RENDER_LIMIT_EXCEEDED", "the PDF exceeds its size limit")
    return data
