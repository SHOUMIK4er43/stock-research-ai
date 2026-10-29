"""DETAILED_RESEARCH_V1 Box 15: FinalResearchReportDataset -> RenderDocument (pure).

The one place where the Box 14 dataset is read for presentation. It copies text verbatim,
formats numbers through domain.report_rendering's single display policy and keeps Box 14's
section order, statuses and reasons. It never computes an economic value, never re-grades a
risk, never resolves a disagreement and never writes narrative of its own beyond fixed labels.

Every section is rendered (none is omitted); every claim a section lists, every table and
chart it references and every structured entry (risk, limitation, diligence item, order
bucket, valuation method, scenario case, monitoring item, view change, disagreement,
appendix) reaches the document.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import replace
from urllib.parse import urlsplit
from uuid import UUID

from stock_research_ai.application.final_report_composer import canonical_report_json
from stock_research_ai.application.research_completeness import summary_lines
from stock_research_ai.domain.final_research_report import (
    Appendix,
    CellStatus,
    ColumnKind,
    CompletenessBlock,
    FinalResearchReportDataset,
    OrderBucketKind,
    ReportMetadata,
    ReportSection,
    ReportSource,
    ReportTable,
    RiskEntry,
    RowKind,
    SectionId,
    SectionStatus,
    ThresholdRef,
)
from stock_research_ai.domain.financial_charts import FinancialChartBundle
from stock_research_ai.domain.report_rendering import (
    CLAIM_TYPE_LABELS,
    MAX_REFS_SHOWN,
    NO_CELL_TEXT,
    NOT_APPLICABLE_TEXT,
    STATUS_LABELS,
    UNAVAILABLE_TEXT,
    Block,
    Cell,
    CellKind,
    Chart,
    ClaimList,
    ClaimView,
    Column,
    KeyValues,
    Paragraph,
    RenderDocument,
    Row,
    Section,
    Table,
    Tone,
    cell_number,
    display_exact,
    display_value,
    unit_label,
)

S = SectionId

ORDER_INVARIANT = "L1 ≠ LOA ≠ CONTRACT ≠ REVENUE"

_ROW_TAGS = {
    RowKind.FORECAST: "FORECAST",
    RowKind.ASSUMPTION: "ASSUMPTION",
    RowKind.VALUATION: "SCENARIO OUTPUT",
    RowKind.CALCULATION: "CALCULATION",
    RowKind.MANAGEMENT_STATEMENT: "MANAGEMENT STATEMENT",
    RowKind.DATA_LIMITATION: "DATA LIMITATION",
}

_BUCKET_ORDER = (OrderBucketKind.CONFIRMED_ORDER_BOOK, OrderBucketKind.PENDING_L1,
                 OrderBucketKind.UNRESOLVED_L1, OrderBucketKind.SOURCE_INCOMPLETE)
_BUCKET_TITLES = {
    OrderBucketKind.CONFIRMED_ORDER_BOOK: "CONFIRMED ORDER BOOK",
    OrderBucketKind.PENDING_L1: "PENDING L1",
    OrderBucketKind.UNRESOLVED_L1: "UNRESOLVED L1",
    OrderBucketKind.SOURCE_INCOMPLETE: "SOURCE INCOMPLETE",
}


def classification_text(m: ReportMetadata) -> str:
    """The ONE business classification (Box 4): its class, subtype and reason."""
    cls = m.business_class or {"GENERAL": "NON_FINANCIAL", "UNKNOWN": "UNKNOWN"}.get(
        m.industry.value, "FINANCIAL")
    subtype = "subtype unresolved" if m.industry.value == "UNKNOWN" else m.industry.value
    if cls == "UNKNOWN":
        return "UNKNOWN (business classification unresolved)"
    return f"{cls} ({subtype})" if cls == "FINANCIAL" else f"{cls} ({m.industry.value})"


def box14_sha256(report: FinalResearchReportDataset) -> str:
    """The Box 14 content hash (canonical JSON; generated_at excluded by Box 14)."""
    return hashlib.sha256(canonical_report_json(report).encode("utf-8")).hexdigest()


def safe_link(url: str | None) -> str | None:
    """Only an absolute http(s) URL with a host is ever linked; anything else (file:,
    javascript:, data:, relative) is shown as text and never becomes a link."""
    if not url or any(c in url for c in "\r\n\t \"'<>\\`") or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


def _label(code: str) -> str:
    return code.replace("_", " ").strip().capitalize()


class _Builder:
    def __init__(self, report: FinalResearchReportDataset,
                 charts: FinancialChartBundle | None) -> None:
        self.r = report
        self.claims = {c.claim_id: c for c in report.claims}
        self.tables = {t.table_id: t for t in report.tables}
        self.chart_refs = {c.chart_id: c for c in report.charts}
        self.refs = {s.source_id: f"S{i}" for i, s in enumerate(report.sources, start=1)}
        self.risks = {e.risk_id: e for e in report.risks.risk_register}
        self.risk_by_claim = {e.claim_id: e for e in report.risks.risk_register}
        self.omitted_charts = 0
        self.limitation_ref = {d.claim_id: d.ref for d in report.risks.data_limitations
                               if d.ref is not None}
        self.svg: dict[UUID, str] = {}
        if charts is not None:
            art = {a.sha256: a.svg for a in charts.artifacts}
            self.svg = {c.chart_id: art[c.artifact_sha256] for c in charts.charts
                        if c.artifact_sha256 in art}
        self.cells = 0
        self.warnings: list[str] = []
        self.rendered: set[UUID] = set()

    # --- primitives -------------------------------------------------------------------------

    def ref_list(self, source_ids: Iterable[UUID]) -> tuple[tuple[str, ...], int]:
        refs = list(dict.fromkeys(self.refs[s] for s in source_ids if s in self.refs))
        return tuple(refs[:MAX_REFS_SHOWN]), max(0, len(refs) - MAX_REFS_SHOWN)

    def claim(self, claim_id: UUID, *, exact: bool = False) -> ClaimView:
        c = self.claims[claim_id]
        self.rendered.add(claim_id)
        type_label = CLAIM_TYPE_LABELS[c.claim_type.value]
        if claim_id in self.limitation_ref:  # one register entry, the same ID everywhere
            type_label = f"{self.limitation_ref[claim_id]} · {type_label}"
        if c.scenario is not None:
            type_label = f"{type_label} · {c.scenario.value}"
        refs, more = self.ref_list(c.source_ids)
        risk = self.risk_by_claim.get(claim_id)
        return ClaimView(
            claim_id=c.claim_id, claim_type=c.claim_type.value, type_label=type_label,
            statement=c.statement, source_text=c.source_text,
            value=None if c.value is None else display_value(c.value, c.unit, exact=exact),
            exact_value=None if c.value is None else display_exact(c.value),
            basis=c.basis.value, period=c.period,
            scenario=c.scenario.value if c.scenario else None,
            severity=risk.severity if risk is not None else None,
            refs=refs, more_refs=more, limitations=c.limitations)

    def requirement(self, block: str) -> CompletenessBlock | None:
        rc = self.r.research_completeness
        return next((b for b in rc.blocks if b.block == block), None) if rc else None

    def claim_list(self, title: str | None, tone: Tone, ids: Sequence[UUID], *,
                   empty: str | None = None, exact: bool = False,
                   shown: set[UUID] | None = None) -> ClaimList:
        known = [i for i in dict.fromkeys(ids) if i in self.claims]
        if shown is not None:
            shown.update(known)
        return ClaimList(title, tone, tuple(self.claim(i, exact=exact) for i in known),
                         empty_text=empty)

    def text_table(self, table_id: str, title: str, columns: Sequence[str],
                   rows: Sequence[tuple[str, Sequence[str]]], *,
                   tags: tuple[str, ...] = (), footnotes: tuple[str, ...] = ()) -> Table:
        """A presentation table of Box 14 fields (text only: copied codes / labels / exact
        values). The first column is the row label."""
        out = tuple(Row(label, (), tuple(Cell(v, "TEXT") for v in values))
                    for label, values in rows)
        self.cells += sum(len(r.cells) + 1 for r in out)
        return Table(table_id, title, tags, tuple(Column(c) for c in columns), out,
                     footnotes=footnotes)

    def thresholds(self, items: Sequence[ThresholdRef]) -> str:
        if not items:
            return "None defined upstream"
        return "; ".join(f"{t.severity} if {t.comparison} {display_exact(t.value)} "
                         f"({t.rule_id})" for t in items)

    def claim_texts(self, ids: Sequence[UUID]) -> str:
        texts = [self.claims[i].statement for i in ids if i in self.claims]
        self.rendered.update(i for i in ids if i in self.claims)
        return " | ".join(texts) if texts else "None cited"

    # --- Box 14 typed tables --------------------------------------------------------------------

    def table(self, t: ReportTable) -> Table:
        tags = [f"Basis: {t.basis.value}"]
        if t.period:
            tags.append(f"Period: {t.period}")
        if t.scenario is not None:
            tags.append(f"Scenario: {t.scenario.value}")
        if any(c.kind == ColumnKind.FORECAST for c in t.columns):
            tags.append("Contains FORECAST columns")
        if t.appendix:
            tags.append("Appendix")
        columns = [Column("Item")]
        for col in t.columns:
            ctags: list[str] = []
            if col.kind == ColumnKind.FORECAST:
                ctags.append("FORECAST")
            elif col.kind == ColumnKind.HISTORICAL:
                ctags.append("HISTORICAL")
            if col.scenario is not None:
                ctags.append(col.scenario.value)
            if col.basis is not None:
                ctags.append(col.basis.value)
            columns.append(Column(col.label, tuple(ctags), numeric=col.kind in (
                ColumnKind.HISTORICAL, ColumnKind.FORECAST, ColumnKind.VALUE)))
        shared = {c.tags for c in columns[1:]}
        if len(columns) > 2 and len(shared) == 1 and next(iter(shared)):
            # every column carries the same tags: say it once for the table (narrow period
            # columns otherwise break their header words)
            tags.append("Columns: " + ", ".join(next(iter(shared))))
            columns = [columns[0], *(replace(c, tags=()) for c in columns[1:])]
        rows: list[Row] = []
        self.rendered.update(c.claim_id for row in t.rows for c in row.cells
                             if c.claim_id is not None)
        self.rendered.update(row.claim_id for row in t.rows if row.claim_id is not None)
        for row in t.rows:
            by_col = {c.column: c for c in row.cells}
            cells: list[Cell] = []
            for col in t.columns:
                forecast = col.kind == ColumnKind.FORECAST
                cell = by_col.get(col.key)
                if cell is None:
                    cells.append(Cell(NO_CELL_TEXT, "NO_CELL", forecast=forecast))
                elif cell.status == CellStatus.AVAILABLE and cell.value is not None:
                    cells.append(Cell(cell_number(cell.value, row.unit), "NUMBER",
                                      forecast=forecast))
                elif cell.status == CellStatus.TEXT and cell.text is not None:
                    kind: CellKind = ("UPSTREAM_TEXT" if cell.text_origin == "UPSTREAM"
                                      else "TEXT")
                    cells.append(Cell(cell.text, kind, forecast=forecast))
                elif cell.status == CellStatus.NOT_APPLICABLE:
                    cells.append(Cell(NOT_APPLICABLE_TEXT, "NOT_APPLICABLE", cell.note,
                                      forecast=forecast))
                else:
                    cells.append(Cell(UNAVAILABLE_TEXT, "UNAVAILABLE", cell.note,
                                      forecast=forecast))
            shown_unit = unit_label(row.unit)
            label = row.label + (f" ({shown_unit})" if shown_unit else "")
            gaps = [c for c in cells if c.kind in ("UNAVAILABLE", "NOT_APPLICABLE")]
            notes = {(c.kind, c.note) for c in gaps}
            if len(gaps) > 1 and len(notes) == 1:
                # one reason for every gap in the row: stated once on the row, not per cell
                kind, note = next(iter(notes))
                if note:
                    word = UNAVAILABLE_TEXT if kind == "UNAVAILABLE" else NOT_APPLICABLE_TEXT
                    label += f" — {word.lower()} where shown ({note})"
                    cells = [replace(c, note=None) if c.kind == kind else c for c in cells]
            rtags = (_ROW_TAGS[row.kind],) if row.kind in _ROW_TAGS else ()
            rows.append(Row(label, rtags, tuple(cells)))
        self.cells += sum(len(r.cells) + 1 for r in rows)
        notices: list[str] = []
        if t.truncated:
            notices.append("Appendix truncated: Box 14 kept only part of the upstream rows "
                           "(see the appendix limitation).")
        refs, more = self.ref_list(t.source_ids)
        return Table(str(t.table_id), t.title, tuple(tags), tuple(columns), tuple(rows),
                     footnotes=t.footnotes, notices=tuple(notices), refs=refs, more_refs=more)

    def chart(self, chart_id: UUID) -> Chart | None:
        ref = self.chart_refs[chart_id]
        svg = self.svg.get(ref.upstream_chart_id)
        if svg is None:
            self.omitted_charts += 1
            return None
        return Chart(str(ref.chart_id), ref.title, ref.chart_code, ref.purpose,
                     ref.basis.value, ref.upstream_status, ref.metric_codes, svg)

    # --- section scaffolding ----------------------------------------------------------------

    def status_block(self, s: ReportSection) -> Paragraph:
        tone: Tone = "NOTICE" if s.status in (SectionStatus.UNAVAILABLE,
                                               SectionStatus.PARTIAL) else "NEUTRAL"
        # section 0 reports THE research status (the one shown in the contents, the cover
        # and the side panel); its own status only says its overview inputs exist
        rc = self.r.research_completeness
        if s.section_id == S.DUE_DILIGENCE_STATUS and rc is not None:
            return Paragraph(f"{STATUS_LABELS[rc.overall.value]} — the material-requirement "
                             "ledger below states what is complete, partial and unavailable "
                             f"(status overview inputs: {STATUS_LABELS[s.status.value]})",
                             "NOTICE" if rc.overall != SectionStatus.COMPLETE else "NEUTRAL",
                             label="Research completeness")
        label = ("Status overview inputs" if s.section_id == S.DUE_DILIGENCE_STATUS
                 else "Section status")
        return Paragraph(f"{STATUS_LABELS[s.status.value]} — {s.status_reason}", tone,
                         label=label)

    def common_tail(self, s: ReportSection, shown: set[UUID],
                    tables: Sequence[UUID] | None = None) -> list[Block]:
        out: list[Block] = []
        rest = [i for i in s.claim_ids if i not in shown]
        if rest:
            out.append(self.claim_list("Findings", "NEUTRAL", rest, shown=shown))
        for tid in (s.table_ids if tables is None else tables):
            if tid in self.tables:
                out.append(self.table(self.tables[tid]))
        # a chart is shown only with its Box 6 image: no placeholder for an undrawn chart
        out.extend(c for c in (self.chart(i) for i in s.chart_ids if i in self.chart_refs)
                   if c is not None)
        limits = [i for i in s.limitation_claim_ids if i not in shown]
        # a register limitation is printed in full once (Section 17); elsewhere it is cited
        cited = [i for i in limits if i in self.limitation_ref] if (
            s.section_id != S.RISKS) else []
        if cited:
            shown.update(cited)
            out.append(Paragraph(", ".join(dict.fromkeys(self.limitation_ref[i] for i in cited))
                                 + " — data limitations stated in full in Section 17's register "
                                 "(not company risks).", "LIMITATION",
                                 label="Data limitations"))
        limits = [i for i in limits if i not in cited]
        if limits:
            out.append(self.claim_list("Data limitations (not company risks)", "LIMITATION",
                                       limits, shown=shown))
        if s.notes:
            out.append(Paragraph(", ".join(s.notes), "NEUTRAL", label="Notes"))
        return out

    # --- special sections -------------------------------------------------------------------

    def due_diligence(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        dd = self.r.due_diligence_status
        blocked = [x for x in self.r.sources if x.source_id in set(dd.blocked_sources)]
        missing = [f.family.value for f in dd.input_families if f.status.value == "MISSING"]
        by_status = ", ".join(f"{STATUS_LABELS[st.value]}: {n}"
                              for st, n in dd.sections_by_status)
        rows = (
            ("Open source conflicts", str(dd.open_source_conflicts)),
            ("Missing evidence (unavailable information)",
             str(len(dd.unavailable_information_claim_ids))),
            ("Blocked / failed sources", str(len(dd.blocked_sources))),
            ("Valuation availability",
             f"{dd.valuation_status} (inputs complete: "
             f"{'yes' if dd.valuation_inputs_complete else 'no'})"),
            ("Forecast availability",
             f"{dd.forecast_status} (inputs complete: "
             f"{'yes' if dd.forecast_inputs_complete else 'no'})"),
            ("Open diligence items", self.diligence_count()),
            ("Data limitations (register)", f"{len(self.r.risks.data_limitations)} entries "
             "(DL-001 ...), each listed once in Section 17"),
            *((("Risk rules evaluated", f"{cov.evaluated} / {cov.applicable} (NOT EVALUATED "
                                        f"{cov.not_evaluated})"),)
              if (cov := self.r.risks.coverage) else ()),
            ("Input families available", f"{dd.families_available} of {dd.families_checked}"),
            ("Missing input families", ", ".join(missing) or "None"),
            ("Sections by status", f"{by_status} (of {dd.sections_total})"),
            ("Score", "None (Box 14 assigns no score)"),
        )
        out: list[Block] = [KeyValues("What the reader must know first", "NOTICE", rows)]
        rc = self.r.research_completeness
        out.append(KeyValues("Research completeness", "NOTICE", tuple(summary_lines(rc)) if rc
                             else (("Overall", "Not assessed (report composed before the "
                                               "completeness ledger)"),)))
        if dd.conflict_claim_ids:
            out.append(self.claim_list("Open source conflicts", "LIMITATION",
                                       dd.conflict_claim_ids, shown=shown))
        if blocked:
            out.append(self.text_table(
                "dd-blocked", "Blocked / failed sources",
                ("Source", "Ref", "Status", "Limitations"),
                [(x.title, (self.refs[x.source_id], x.status, ", ".join(x.limitations)
                            or "None")) for x in blocked]))
        out.append(self.claim_list("Missing evidence (data limitations, not company risks)",
                                   "LIMITATION", dd.unavailable_information_claim_ids,
                                   shown=shown, empty="None recorded upstream."))
        return out

    def executive_summary(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        es = self.r.executive_summary
        monitoring = {m.item_id: m for m in self.r.monitoring}
        out: list[Block] = [Paragraph(
            "Rendered exactly as Box 14 composed it from existing claims of the completed "
            "sections; Box 15 adds no thesis or narrative. Recommendation: none.", "NEUTRAL")]
        for p in es.points:
            tone: Tone = "NEUTRAL"
            if p.kind.value == "MAJOR_RISKS":
                tone = "RED"
            elif p.kind.value == "DATA_LIMITATIONS":
                tone = "LIMITATION"
            elif p.kind.value in ("FORECAST_RANGE", "VALUATION_RANGE"):
                tone = "SCENARIO"
            only_limits = bool(p.claim_ids) and all(
                self.claims[i].claim_type.value == "DATA_LIMITATION"
                for i in p.claim_ids if i in self.claims)
            if p.status == "AVAILABLE" and p.kind.value != "DATA_LIMITATIONS":
                # a point whose only content is a data limitation is not 'available'
                title = _label(p.kind.value) + (
                    f" — {STATUS_LABELS['UNAVAILABLE']}" if only_limits else "")
            elif p.status == "AVAILABLE":
                title = f"{_label(p.kind.value)} ({len(p.claim_ids)})"
            else:
                title = f"{_label(p.kind.value)} — {STATUS_LABELS.get(p.status, p.status)}"
            block = self.claim_list(title, tone, p.claim_ids, shown=shown)
            extra: list[tuple[str, str]] = []
            if p.reason:
                extra.append(("Reason", p.reason))
            for mid in p.monitoring_item_ids:
                m = monitoring.get(mid)
                if m is not None:
                    extra.append(("Monitoring", f"{m.topic} / {m.subject}: {m.reason}"))
            out.append(ClaimList(block.title, block.tone, block.claims,
                                 empty_text=None if extra else "No claims cited.",
                                 extra=tuple(extra)))
        if es.yellow_risks_not_listed:
            out.append(Paragraph(f"{es.yellow_risks_not_listed} further YELLOW company "
                                 "risk(s) are listed in Section 17 (not repeated here).",
                                 "YELLOW"))
        return out

    def risk_entry_view(self, e: RiskEntry) -> ClaimView:
        view = self.claim(e.claim_id, exact=True)
        detail = [f"{e.code} ({e.category}; {e.status}"
                  + (f"; reason {e.reason}" if e.reason else "") + ")"]
        if e.likelihood or e.impact:
            detail.append(f"likelihood {e.likelihood or 'not stated'}, impact "
                          f"{e.impact or 'not stated'}")
        if e.mitigating_risk_ids:
            codes = [self.risks[m].code for m in e.mitigating_risk_ids if m in self.risks]
            detail.append("mitigating: " + ", ".join(codes))
        return _with_label(view, f"{view.type_label} · {'; '.join(detail)}", e.severity)

    def diligence_count(self) -> str:
        """THE open-diligence count: every open item, then by priority (one definition)."""
        items = self.r.risks.open_diligence
        by = {k: sum(1 for o in items if o.importance == k) for k in ("HIGH", "MEDIUM", "LOW")}
        return f"{len(items)} total (HIGH {by['HIGH']}, MEDIUM {by['MEDIUM']}, LOW {by['LOW']})"

    def risk_coverage(self) -> list[Block]:
        """The canonical coverage first: how many rules ran, what they found, and - by name,
        with the missing evidence - every rule that could not run."""
        cov = self.r.risks.coverage
        if cov is None:
            return []
        out: list[Block] = [KeyValues(
            f"Risk coverage — Risk rules evaluated: {cov.evaluated} / {cov.applicable}",
            "NOTICE", (
                ("RED", str(cov.red)), ("YELLOW", str(cov.yellow)), ("GREEN", str(cov.green)),
            *((("Informational (no grade)", str(cov.informational)),)
              if cov.informational else ()),
            ("NOT EVALUATED", str(cov.not_evaluated)),
            ("NOT APPLICABLE", str(cov.not_applicable))))]
        out.append(Paragraph(cov.headline, "NOTICE", label="Coverage"))
        clear = tuple((f"{r.measured or r.code} — GREEN",
                       f"no adverse evidence found; risk tested: {r.title.lower()} ({r.code})")
                      for r in cov.rules if r.outcome == "GREEN")
        if clear:
            out.append(KeyValues("Rules evaluated with no adverse evidence found", "POSITIVE",
                                 clear))
        missing = tuple((r.title, f"NOT EVALUATED - {r.reason_text or r.reason} "
                                  f"(rule {r.code})")
                        for r in cov.rules if r.outcome == "NOT_EVALUATED")
        if missing:
            out.append(KeyValues("Rules NOT EVALUATED (evidence unavailable - not a finding "
                                 "of no risk)", "LIMITATION", missing))
        return out

    def risks_section(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        rp = self.r.risks
        out: list[Block] = self.risk_coverage()
        groups: tuple[tuple[str, Tone, tuple[UUID, ...], str], ...] = (
                ("RED company risks", "RED", rp.red, "No RED company risk graded upstream."),
                ("YELLOW company risks", "YELLOW", rp.yellow,
                 "No YELLOW company risk graded upstream."),
                ("Positive controls", "POSITIVE", rp.positive_controls,
                 "No positive control evaluated upstream."))
        for title, tone, ids, empty in groups:
            entries = [self.risks[i] for i in ids if i in self.risks]
            views: list[ClaimView] = []
            for e in entries:
                views.append(self.risk_entry_view(e))
                for i in e.input_claim_ids:
                    if i in self.claims:
                        v = self.claim(i)
                        views.append(_with_label(v, f"Input to {e.code} · {v.type_label}"))
                        shown.add(i)
            shown.update(e.claim_id for e in entries)
            out.append(ClaimList(title, tone, tuple(views), empty_text=empty))
        lim_views: list[ClaimView] = []
        for d in rp.data_limitations:
            if d.claim_id in self.claims:
                v = self.claim(d.claim_id)
                lim_views.append(_with_label(v, f"{v.type_label} · {d.kind} ({d.layer})"
                                             + (f"; affects {', '.join(d.affects)}"
                                                if d.affects else "")))
                shown.add(d.claim_id)
        out.append(ClaimList("DATA LIMITATIONS (gaps in the data — not statements about the "
                             "company)", "LIMITATION", tuple(lim_views),
                             empty_text="No data limitation recorded upstream."))
        dil: list[ClaimView] = []
        for o in rp.open_diligence:
            if o.claim_id in self.claims:
                v = self.claim(o.claim_id)
                related = [self.risks[r].code for r in o.related_risk_ids if r in self.risks]
                lim = f"{v.type_label} · {o.importance} importance; needed source: " \
                      f"{o.needed_source}" + (f"; related: {', '.join(related)}"
                                              if related else "")
                dil.append(_with_label(v, lim))
                shown.add(o.claim_id)
        out.append(ClaimList("Open diligence", "DILIGENCE", tuple(dil),
                             empty_text="No open diligence item recorded upstream."))
        return out

    def order_book(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        ob = self.r.order_book
        out: list[Block] = [Paragraph(
            f"{ORDER_INVARIANT}. Order-book stages are shown separately and are never added "
            "together: combined total — none. L1 (lowest bidder) is not an award, not backlog "
            "and not revenue.", "NOTICE", label="Hard invariant"),
            Paragraph(f"{'Applicable' if ob.applicable else 'Not applicable'} — "
                      f"{ob.applicability}", "NEUTRAL", label="Applicability")]
        buckets = {b.bucket: b for b in ob.buckets}
        for kind in _BUCKET_ORDER:
            b = buckets.get(kind)
            if b is None:
                out.append(Paragraph("No bucket supplied upstream for this stage.", "NEUTRAL",
                                     label=_BUCKET_TITLES[kind]))
                continue
            count = self.claims.get(b.count_claim_id) if b.count_claim_id else None
            ids = ([b.count_claim_id] if b.count_claim_id else []) + list(b.value_claim_ids)
            block = self.claim_list(f"{_BUCKET_TITLES[kind]} — {b.label}", "SCENARIO" if (
                kind != OrderBucketKind.CONFIRMED_ORDER_BOOK) else "NEUTRAL", ids, shown=shown)
            count_text = (display_value(count.value, count.unit)
                          if count is not None and count.value is not None else UNAVAILABLE_TEXT)
            out.append(ClaimList(block.title, block.tone, block.claims, extra=(
                ("Order chains", f"{count_text} (chain IDs listed: {len(b.chain_ids)})"),
                ("Note", b.note))))
        if ob.reported_order_book_claim_ids:
            out.append(self.claim_list("Company-reported order book (as disclosed)", "NEUTRAL",
                                       ob.reported_order_book_claim_ids, shown=shown))
        if ob.metric_claim_ids:
            out.append(self.claim_list("Order-book metrics (upstream calculations)", "NEUTRAL",
                                       ob.metric_claim_ids, shown=shown))
        return out

    def guidance(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        if not self.r.guidance_tracker:
            return []
        rows = []
        for g in self.r.guidance_tracker:
            st = self.claims.get(g.statement_claim_id)
            actual = self.claims.get(g.actual_claim_id) if g.actual_claim_id else None
            actual_text = "Not linked"
            if actual is not None:
                actual_text = actual.statement
            shown.add(g.statement_claim_id)
            for i in g.earlier_statement_claim_ids:
                shown.add(i)
            rows.append((g.metric, (
                str(g.date_made) if g.date_made else "Date not stated",
                g.target, g.deadline or "Not stated", g.result, g.history or g.lifecycle,
                st.statement if st else "Statement not found", actual_text)))
        return [self.text_table(
            "guidance-tracker", "Guidance tracker (MANAGEMENT STATEMENTS until an outcome is "
            "linked)", ("Metric", "Date made", "Target (upstream text)", "Deadline", "Result",
                        "Lifecycle", "Management statement", "Linked actual"), rows,
            tags=("Claim type: MANAGEMENT STATEMENT",))]

    def scenarios(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        ic = self.r.investment_case
        out: list[Block] = [Paragraph(
            "Scenarios as Box 10 / Box 11 define them. No probability is assigned upstream, so "
            "no expected value is derived. Recommendation: none.", "NOTICE")]
        for case in ic.cases:
            risks = [self.risks[r].code for r in case.risk_ids if r in self.risks]
            out.append(KeyValues(f"{case.scenario.value} scenario ({case.case}) — "
                                 f"{STATUS_LABELS[case.status.value]}", "SCENARIO", (
                                     ("Thesis (Box 14 template)", case.thesis),
                                     ("Conditions required", " | ".join(
                                         case.conditions_required) or "None stated"),
                                     ("Invalidation conditions", " | ".join(
                                         case.invalidation_conditions) or "None stated"),
                                     ("Company risks bearing on every case",
                                      ", ".join(risks) or "None"),
                                     ("Probability", case.probability))))
            for title, ids in (("drivers (assumptions)", case.driver_claim_ids),
                               ("forecast outputs", case.forecast_claim_ids),
                               ("valuation outputs", case.valuation_claim_ids)):
                out.append(self.claim_list(f"{case.scenario.value} {title}", "SCENARIO", ids,
                                           shown=shown, empty="None supplied upstream."))
        return out

    def valuation(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        v = self.r.valuation
        out: list[Block] = []
        block = self.requirement("VALUATION")
        if block is not None and block.status.value != "COMPLETE":
            out.append(Paragraph(
                f"Valuation: {STATUS_LABELS[block.status.value]}. "
                + "; ".join(reason_text(r) for r in block.reasons) + ". No valuation multiple "
                "or assumption is ever invented: methods need explicitly supplied inputs.",
                "LIMITATION", label="Valuation availability"))
        out.append(Paragraph(
            "Each Box 11 method and scenario is shown separately. Blended value: none. "
            "Price objective: none. Recommendation: none.", "NOTICE", label="Valuation policy"))
        rows = []
        for m in v.methods:
            results = [self.claims[i] for i in m.result_claim_ids if i in self.claims]
            values = " | ".join(f"{c.statement}" for c in results) or "None"
            assumptions = self.claim_texts(m.assumption_claim_ids)
            limits = self.claim_texts(m.limitation_claim_ids)
            shown.update((*m.assumption_claim_ids, *m.result_claim_ids,
                          *m.limitation_claim_ids))
            rows.append((m.method, (m.scenario.value, m.status, m.reason or "None",
                                    "; ".join(m.missing_inputs) or "None", assumptions,
                                    values, limits)))
        out.append(self.text_table(
            "valuation-methods-detail", "Valuation methods (Box 11, per method and scenario)",
            ("Method", "Scenario", "Status", "Reason", "Missing inputs", "Assumptions",
             "Outputs (EV / equity / per share as Box 11 states)", "Limitations"), rows,
            tags=("Claim type: SCENARIO",)))
        if v.market_context_claim_ids:
            out.append(self.claim_list("Market context (not a valuation)", "NEUTRAL",
                                       v.market_context_claim_ids, shown=shown))
        return out

    def what_changes(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        if not self.r.what_changes_the_view:
            return []
        rows = []
        for e in self.r.what_changes_the_view:
            shown.update(e.related_claim_ids)
            risks = [self.risks[r].code for r in e.related_risk_ids if r in self.risks]
            rows.append((e.subject, (e.direction.value, e.condition,
                                     self.thresholds(e.thresholds), e.source_required,
                                     e.status, ", ".join(risks) or "None",
                                     self.claim_texts(e.related_claim_ids))))
        return [self.text_table(
            "what-changes", "What changes the view (upstream thresholds, exact values)",
            ("Subject", "Direction", "Condition", "Thresholds (exact)", "Source required",
             "Status", "Related risks", "Latest related claims"), rows)]

    def monitoring(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        if not self.r.monitoring:
            return []
        rows = []
        for m in self.r.monitoring:
            shown.update(m.latest_claim_ids)
            risks = [self.risks[r].code for r in m.related_risk_ids if r in self.risks]
            rows.append((m.topic, (m.subject, m.reason, m.trigger, m.source_to_check,
                                   self.thresholds(m.thresholds), ", ".join(risks) or "None",
                                   self.claim_texts(m.latest_claim_ids))))
        return [self.text_table(
            "monitoring", "Monitoring checklist", ("Topic", "Subject", "Reason", "Trigger",
                                                   "Source to check", "Thresholds (exact)",
                                                   "Related risks", "Latest claims"), rows)]

    def data_quality(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        out: list[Block] = []
        open_ = [d for d in self.r.source_disagreements if d.status == "OPEN"]
        out.append(Paragraph(
            f"{len(open_)} open and {len(self.r.source_disagreements) - len(open_)} resolved "
            "source disagreement(s). Every observation is shown, including the ones not "
            "selected.", "NOTICE" if open_ else "NEUTRAL", label="Source disagreements"))
        for d in self.r.source_disagreements:
            shown.add(d.claim_id)
            rows = [(o.source, (o.authority, display_exact(o.value) if o.value is not None
                                else UNAVAILABLE_TEXT, str(o.record_id) if o.record_id
                                else "Not stated")) for o in d.values]
            out.append(self.text_table(
                f"disagreement-{d.entry_id}", f"{d.topic} — {d.period} ({d.status})",
                ("Source", "Authority", "Observed value (exact)", "Record"), rows,
                tags=(f"Basis: {d.basis}", f"Origin: {d.origin}"),
                footnotes=(f"Canonical selection: {d.canonical_selection or 'None (open)'}",
                           f"Reason: {d.reason}",
                           f"Box 14: {self.claims[d.claim_id].statement}"
                           if d.claim_id in self.claims else "")))
        if self.r.issues:
            out.append(self.text_table(
                "report-issues", "Box 14 composition issues", ("Code", "Severity", "Detail"),
                [(i.code, (i.severity, i.detail or "None")) for i in self.r.issues]))
        return out

    def source_register(self) -> list[Block]:
        rows: list[Row] = []
        for src in self.r.sources:
            texts = self._source_cells(src)
            cells = [Cell(texts[0], "UPSTREAM_TEXT")]
            cells += [Cell(t, "TEXT", link=self._link(src) if i == 4 else None)
                      for i, t in enumerate(texts[1:])]
            rows.append(Row(self.refs[src.source_id], (), tuple(cells)))
        self.cells += sum(len(r.cells) + 1 for r in rows)
        return [Paragraph(f"{len(self.r.sources)} deduplicated sources (each document or "
                          "record listed once; claims cite them as S1, S2, ...).", "NEUTRAL"),
                Table("source-register", "Source register", ("Deduplicated by Box 14",),
                      tuple(Column(c) for c in ("Ref", "Title", "Type", "Authority", "Status",
                                                "Dates", "Location", "Layers", "Claims")),
                      tuple(rows))]

    def _link(self, src: ReportSource) -> str | None:
        return safe_link(src.url)

    def _source_cells(self, src: ReportSource) -> tuple[str, ...]:
        dates = []
        if src.document_date:
            dates.append(f"document {src.document_date}")
        if src.retrieval_date:
            dates.append(f"retrieved {src.retrieval_date}")
        status = src.status + (f" ({', '.join(src.limitations)})" if src.limitations else "")
        return (src.title, src.source_type.value, ", ".join(src.authority) or "Not stated",
                status, "; ".join(dates) or "Not stated", src.url or src.key,
                ", ".join(x.value for x in src.layers), str(src.claim_count))

    def appendices(self, s: ReportSection, shown: set[UUID]) -> list[Block]:
        out: list[Block] = []
        used: set[UUID] = set()
        for a in self.r.appendices:
            out.extend(self.appendix(a, shown, used))
        rest = [t for t in s.table_ids if t not in used]
        out.extend(self.common_tail(s, shown, tables=rest))
        return out

    def appendix(self, a: Appendix, shown: set[UUID], used: set[UUID]) -> list[Block]:
        tone: Tone = "NOTICE" if a.truncated else "NEUTRAL"
        head = (f"{STATUS_LABELS[a.status.value]}; {a.entries} of {a.upstream_entries} "
                "upstream entries" + ("; APPENDIX TRUNCATED" if a.truncated else ""))
        out: list[Block] = [Paragraph(head, tone, label=f"Appendix — {a.title}")]
        if a.truncated and a.limitation_claim_id is not None:
            out.append(self.claim_list("Appendix truncated (Box 14 limitation)", "LIMITATION",
                                       [a.limitation_claim_id], shown=shown))
        table_claims: set[UUID] = set()
        for tid in a.table_ids:
            if tid in self.tables:
                used.add(tid)
                t = self.tables[tid]
                out.append(self.table(t))
                table_claims.update(t.claim_ids)
                table_claims.update(c.claim_id for row in t.rows for c in row.cells
                                    if c.claim_id is not None)
                table_claims.update(row.claim_id for row in t.rows if row.claim_id)
        if a.appendix_id.value == "SOURCE_REGISTER":
            table_claims.update(a.claim_ids)
        rest = [i for i in a.claim_ids if i not in table_claims and i not in shown]
        if rest:
            out.append(self.claim_list(f"{a.title} — claims", "NEUTRAL", rest, shown=shown))
        return out

    # --- assembly ---------------------------------------------------------------------------

    def section(self, s: ReportSection) -> Section:
        shown: set[UUID] = set()
        blocks: list[Block] = [self.status_block(s)]
        special = {
            S.DUE_DILIGENCE_STATUS: self.due_diligence,
            S.EXECUTIVE_SUMMARY: self.executive_summary,
            S.CONCALL_GUIDANCE: self.guidance,
            S.ORDER_BOOK: self.order_book,
            S.SCENARIO_ANALYSIS: self.scenarios,
            S.VALUATION: self.valuation,
            S.RISKS: self.risks_section,
            S.WHAT_CHANGES_THE_VIEW: self.what_changes,
            S.MONITORING: self.monitoring,
            S.DATA_QUALITY_RECONCILIATION: self.data_quality,
        }.get(s.section_id)
        if s.section_id == S.APPENDICES:
            blocks.extend(self.appendices(s, shown))
        else:
            if special is not None:
                blocks.extend(special(s, shown))
            if s.section_id == S.SOURCE_REGISTER:
                blocks.extend(self.source_register())
            blocks.extend(self.common_tail(s, shown))
        if len(blocks) == 1 and s.status in (SectionStatus.COMPLETE, SectionStatus.PARTIAL):
            blocks.append(Paragraph("No section content was supplied by Box 14.", "NOTICE"))
        status, label = s.status.value, STATUS_LABELS[s.status.value]
        rc = self.r.research_completeness
        if s.section_id == S.DUE_DILIGENCE_STATUS and rc is not None:
            # the status overview's badge is the research conclusion, never 'Complete' for
            # a report whose material research is partial
            status, label = rc.overall.value, f"Research {STATUS_LABELS[rc.overall.value]}"
        return Section(s.order, s.section_id.value, s.title, status, label, s.status_reason,
                       tuple(f.value for f in s.completeness.missing), tuple(blocks),
                       prominent=s.section_id == S.DUE_DILIGENCE_STATUS)

    def document(self, sha: str) -> RenderDocument:
        m = self.r.metadata
        sections = [self.section(s) for s in self.r.sections]
        rest = [c.claim_id for c in self.r.claims if c.claim_id not in self.rendered]
        if rest:
            last = sections[-1]
            block = self.claim_list(
                "Supporting claims not shown elsewhere (cited as inputs, derivations or "
                "references)", "NEUTRAL", rest)
            sections[-1] = replace(last, blocks=(*last.blocks, block))
        identifiers = tuple((k, v) for k, v in (
            ("NSE", m.nse_symbol), ("ISIN", m.isin), ("BSE", m.bse_code),
            ("Company key", m.company_key)) if v)
        rc = self.r.research_completeness
        glance: list[tuple[str, str]] = [
            ("As of", str(m.as_of)),
            ("Latest fiscal year end", str(m.latest_fiscal_year_end or "Not stated")),
            ("Analytical basis", m.analytical_basis.value),
            ("Control basis", m.control_basis.value if m.control_basis
             else "None (one basis analysed)"),
        ]
        note = basis_note(m.basis_selection)
        if note:
            glance.append(("Basis selection", note))
        glance.append(("Business classification", classification_text(m)))
        if rc is not None:
            glance.append(("Research completeness", f"{STATUS_LABELS[rc.overall.value]} (see "
                           "section 0 for what is complete, partial and unavailable)"))
            for key, label in (("FORECAST", "Forecast"), ("VALUATION", "Valuation")):
                req = self.requirement(key)
                if req is not None:
                    glance.append((label, STATUS_LABELS[req.status.value] + (
                        " — " + "; ".join(reason_text(r) for r in req.reasons)
                        if req.reasons else "")))
        else:
            glance.append(("Research completeness", "Not assessed (report composed before the "
                           "completeness ledger)"))
        facts = (*glance,
            ("Recommendation", "None (Box 14 recommendation: NONE)"),
            ("Currency", m.currency.value),
            ("Units", m.unit_note),
            ("Order-book model", "order-book driven" if m.business_model == "ORDER_BOOK_DRIVEN"
             else "not order-book driven"),
            ("Identity status", m.identity_status),
            ("Report ID", str(self.r.report_id)),
            ("Box 14 SHA-256", sha),
            ("Engines", f"{m.engine_version}; {m.selection_rules_version}"),
        )
        return RenderDocument(
            report_id=str(self.r.report_id),
            title=f"Detailed Research — {m.company_name}", company_name=m.company_name,
            identifiers=identifiers, as_of=str(m.as_of), generated_at=m.generated_at.isoformat(),
            box14_sha256=sha, header_facts=facts, sections=tuple(sections),
            warnings=tuple(dict.fromkeys(self.warnings)) + ((
                f"CHARTS_OMITTED: {self.omitted_charts} Box 14 chart reference(s) without a "
                "Box 6 image are not shown (no placeholder is drawn)",)
                if self.omitted_charts else ()), cells=self.cells)


#: readable text for structured reason codes (the code itself is always kept)
REASON_TEXT = {
    "BOX10_PARTIAL": "core lines are forecast, but the Box 10 forecast itself is partial",
    "MULTIPLE_MISSING": "no valuation multiple was supplied (none is assumed)",
    "ATTRIBUTION_UNKNOWN": "profit attributable to ordinary shareholders is not established",
    "CASH_UNAVAILABLE": "cash and debt inputs for the equity bridge are unavailable",
    "INPUT_MISSING": "a required input is not reported",
    "NO_APPLICABLE_METHOD": "no valuation method applies to this business model",
    "LAYER_NOT_SUPPLIED": "the upstream layer was not supplied",
    "CLASSIFICATION_UNRESOLVED": "the business model is unresolved",
    "NO_ELIGIBLE_PEERS": "no comparable peer is eligible",
    "PAT_NOT_FORECAST": "PAT is not forecast by this model",
}


def reason_text(code: str) -> str:
    text = REASON_TEXT.get(code)
    if text is None and code.endswith("_NOT_FORECAST"):
        text = f"{code[: -len('_NOT_FORECAST')].replace('_', ' ').lower()} is not forecast"
    return f"{text} ({code})" if text else code


def basis_note(selection: str | None) -> str | None:
    """A readable note only when the basis is not the default consolidated choice."""
    if not selection or "not current" not in selection:
        return None
    detail = selection.split("(", 1)[1].rstrip(")") if "(" in selection else ""
    return ("Standalone selected because the consolidated statements are older"
            + (f" ({detail})" if detail else "") + ".")


def _with_label(view: ClaimView, label: str, severity: str | None = None) -> ClaimView:
    return replace(view, type_label=label, severity=severity or view.severity)


def build_presentation(report: FinalResearchReportDataset, *,
                       charts: FinancialChartBundle | None = None,
                       sha256: str | None = None) -> RenderDocument:
    """Pure and deterministic: the same dataset (and chart bundle) gives the same document."""
    return _Builder(report, charts).document(sha256 or box14_sha256(report))
