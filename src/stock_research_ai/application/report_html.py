"""DETAILED_RESEARCH_V1 Box 15: RenderDocument -> self-contained HTML.

Pure and byte-deterministic. Safety:
  * every piece of report text is HTML-escaped (quotes included); no report string is ever
    emitted as markup;
  * a Content-Security-Policy meta tag forbids scripts, frames, forms, fonts and every remote
    resource (`default-src 'none'`); only inline styles and data: images are allowed;
  * links are emitted only for absolute http(s) source-register URLs (see
    report_presentation.safe_link), with rel="noopener noreferrer nofollow";
  * Box 6 charts are embedded only as <img src="data:image/svg+xml;base64,..."> — an SVG
    loaded as an image can neither run script nor fetch anything.
"""

import base64
from html import escape

from stock_research_ai.domain.report_rendering import (
    MAX_HTML_BYTES,
    MAX_TEXT_CELLS,
    Block,
    Chart,
    ClaimList,
    ClaimView,
    KeyValues,
    Paragraph,
    RenderDocument,
    RenderError,
    Section,
    Table,
)

CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")

CSS = """
:root{--ink:#1b1f24;--muted:#5b6470;--line:#d5dae1;--bg:#ffffff;--soft:#f5f7fa;
--red:#a4161a;--red-bg:#fdecec;--yellow:#8a6100;--yellow-bg:#fff6dc;--pos:#1f6f3f;
--pos-bg:#e9f6ee;--lim:#3d4a5c;--lim-bg:#eef1f5;--dil:#5b3b8c;--dil-bg:#f2edf9;
--scn:#14527a;--scn-bg:#e8f2fa;--notice:#7a4b00;--notice-bg:#fff8eb}
.st-COMPLETE{color:var(--pos);background:var(--pos-bg)}
.st-PARTIAL{color:var(--yellow);background:var(--yellow-bg)}
.st-UNAVAILABLE{color:var(--lim);background:var(--lim-bg)}
.st-NOT_APPLICABLE{color:var(--muted);background:var(--soft)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,
"Segoe UI",Roboto,"DejaVu Sans",Arial,sans-serif;overflow-wrap:anywhere}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:24px;margin:8px 0}h2{font-size:19px;margin:28px 0 8px;padding-top:8px;
border-top:2px solid var(--ink)}h3{font-size:15px;margin:16px 0 6px}
h4{font-size:14px;margin:14px 0 4px}
.disclaimer{background:var(--soft);border-left:4px solid var(--muted);padding:8px 12px}
.kv{border-collapse:collapse;margin:6px 0;width:100%}.kv th{text-align:left;width:32%;
font-weight:600}.kv th,.kv td{border:1px solid var(--line);padding:4px 8px;vertical-align:top}
.wrap{overflow-x:auto;margin:6px 0 4px}
table.data{border-collapse:collapse;font-size:12.5px;min-width:60%}
table.data th,table.data td{border:1px solid var(--line);padding:3px 6px;vertical-align:top}
table.data thead th{background:var(--soft);position:sticky;top:0}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.fc{background:var(--scn-bg);font-style:italic}
td.na,td.unavail,td.nocell{color:var(--muted);font-style:italic}
td.unavail{background:var(--lim-bg)}
.tag{display:inline-block;font-size:10.5px;font-weight:600;padding:0 5px;border-radius:3px;
border:1px solid var(--line);background:var(--soft);color:var(--muted);margin:0 2px;
white-space:nowrap;font-style:normal}
.tag.FORECAST,.tag.SCENARIO,.tag.BASE,.tag.BULL,.tag.BEAR{background:var(--scn-bg);
color:var(--scn);border-color:var(--scn)}
.tag.CONSOLIDATED,.tag.STANDALONE{background:#fff;color:var(--ink);border-color:var(--ink)}
.claims{list-style:none;padding:0;margin:4px 0}
.claims li{border-left:3px solid var(--line);padding:4px 8px;margin:4px 0;background:#fff}
.ct{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;
padding:1px 5px;border-radius:3px;margin-right:4px;white-space:nowrap}
.ct.FACT{background:#e7ecf3;color:#1d3557}.ct.CALCULATION{background:#e6f0ec;color:#22543d}
.ct.MANAGEMENT_STATEMENT{background:var(--dil-bg);color:var(--dil)}
.ct.ANALYST_INFERENCE{background:#f3efe6;color:#6b4f1d}
.ct.SCENARIO{background:var(--scn-bg);color:var(--scn)}
.ct.DATA_LIMITATION{background:var(--lim-bg);color:var(--lim)}
.sev{font-weight:700;padding:1px 5px;border-radius:3px;margin-right:4px}
.sev.RED{background:var(--red);color:#fff}.sev.YELLOW{background:#e0a800;color:#1b1f24}
.sev.GREEN{background:var(--pos);color:#fff}
.quote{margin:4px 0 0;padding:3px 8px;border-left:3px solid var(--dil);color:#333;
background:var(--soft);font-style:italic}
.meta{color:var(--muted);font-size:12px}
.box{border:1px solid var(--line);border-radius:4px;padding:6px 10px;margin:10px 0}
.box.RED{border-color:var(--red);background:var(--red-bg)}
.box.YELLOW{border-color:#e0a800;background:var(--yellow-bg)}
.box.POSITIVE{border-color:var(--pos);background:var(--pos-bg)}
.box.LIMITATION{border-color:var(--lim);background:var(--lim-bg)}
.box.DILIGENCE{border-color:var(--dil);background:var(--dil-bg)}
.box.SCENARIO{border-color:var(--scn);background:var(--scn-bg)}
.box.NOTICE{border-color:var(--notice);background:var(--notice-bg)}
.box h3{margin-top:2px}
.status{padding:4px 10px;border-radius:4px;background:var(--soft);margin:4px 0}
.status.NOTICE{background:var(--notice-bg)}
.prominent{border:2px solid var(--notice);background:var(--notice-bg);padding:8px 12px;
border-radius:6px}
.toc{columns:2;font-size:13px;list-style:none;padding-left:0}.toc li{break-inside:avoid}
table.data tbody th{text-align:left;font-weight:500}
figure{margin:10px 0}figure img{max-width:100%;height:auto;border:1px solid var(--line)}
footer{margin-top:28px;border-top:1px solid var(--line);padding-top:8px}
@media (max-width:640px){.toc{columns:1}.kv th{width:45%}}
@media print{main{max-width:none}h2{break-after:avoid}table.data thead{
display:table-header-group}.wrap{overflow:visible}tr,li{break-inside:avoid}}
"""


def h(text: str) -> str:
    return escape(text, quote=True)


def _tags(tags: tuple[str, ...]) -> str:
    return "".join(f'<span class="tag {h(t.split(":")[0].replace(" ", "_"))}">{h(t)}</span>'
                   for t in tags)


def _claim(c: ClaimView) -> str:
    parts = ['<li>']
    if c.severity:
        parts.append(f'<span class="sev {h(c.severity)}">{h(c.severity)}</span>')
    parts.append(f'<span class="ct {h(c.claim_type)}">{h(c.type_label)}</span>')
    if c.basis != "NOT_APPLICABLE":
        parts.append(f'<span class="tag {h(c.basis)}">{h(c.basis)}</span>')
    if c.period:
        parts.append(f'<span class="tag">{h(c.period)}</span>')
    parts.append(f" {h(c.statement)}")
    if c.value is not None:
        exact = (f' <span class="meta">(exact {h(c.exact_value)})</span>'
                 if c.exact_value and c.exact_value.lstrip("-") not in c.value else "")
        parts.append(f' <strong class="val">{h(c.value)}</strong>{exact}')
    if c.refs:
        more = f" +{c.more_refs} more" if c.more_refs else ""
        parts.append(f' <span class="meta">[{h(", ".join(c.refs))}{h(more)}]</span>')
    if c.source_text:
        label = "Management statement (verbatim)" if c.claim_type == (
            "MANAGEMENT_STATEMENT") else "Source text (verbatim)"
        parts.append(f'<div class="quote"><span class="meta">{h(label)}:</span> '
                     f'“{h(c.source_text)}”</div>')
    if c.limitations:
        parts.append(f'<div class="meta">Limitations: {h(", ".join(c.limitations))}</div>')
    parts.append("</li>")
    return "".join(parts)


def _cell(cell_text: str, kind: str, note: str | None, forecast: bool,
          link: str | None) -> str:
    classes = {"NUMBER": "num", "UNAVAILABLE": "unavail", "NOT_APPLICABLE": "na",
               "NO_CELL": "nocell"}.get(kind, "")
    if forecast and kind == "NUMBER":
        classes += " fc"
    body = h(cell_text)
    if link is not None and kind != "NUMBER":
        body = f'<a href="{h(link)}" rel="noopener noreferrer nofollow">{body}</a>'
    if note:
        body += f' <span class="meta">({h(note)})</span>'
    cls = f' class="{classes.strip()}"' if classes.strip() else ""
    return f"<td{cls}>{body}</td>"


def _table(t: Table) -> str:
    out = [f'<h4 id="t-{h(t.table_id)}">{h(t.title)}</h4>']
    if t.tags:
        out.append(f'<div class="meta">{_tags(t.tags)}</div>')
    out += [f'<div class="box NOTICE"><strong>{h(n)}</strong></div>' for n in t.notices]
    head = "".join(f"<th>{h(c.label)}{' ' + _tags(c.tags) if c.tags else ''}</th>"
                   for c in t.columns)
    rows = []
    for row in t.rows:
        label = h(row.label) + (" " + _tags(row.tags) if row.tags else "")
        cells = "".join(_cell(c.text, c.kind, c.note, c.forecast, c.link) for c in row.cells)
        rows.append(f"<tr><th scope=\"row\">{label}</th>{cells}</tr>")
    out.append(f'<div class="wrap"><table class="data"><thead><tr>{head}</tr></thead>'
               f"<tbody>{''.join(rows)}</tbody></table></div>")
    notes = [f"<li>{h(f)}</li>" for f in t.footnotes if f]
    if t.refs:
        more = f" (+{t.more_refs} more)" if t.more_refs else ""
        notes.append(f"<li>Sources: {h(', '.join(t.refs))}{h(more)}</li>")
    if notes:
        out.append(f'<ul class="meta">{"".join(notes)}</ul>')
    return "".join(out)


def _block(b: Block) -> str:
    if isinstance(b, Paragraph):
        lead = f"<strong>{h(b.label)}:</strong> " if b.label else ""
        if b.tone == "NEUTRAL":
            return f'<p class="status">{lead}{h(b.text)}</p>'
        return f'<div class="box {h(b.tone)}">{lead}{h(b.text)}</div>'
    if isinstance(b, ClaimList):
        title = f"<h3>{h(b.title)}</h3>" if b.title else ""
        items = "".join(_claim(c) for c in b.claims)
        if not b.claims and b.empty_text:
            items = f'<li class="meta">{h(b.empty_text)}</li>'
        extra = "".join(f"<li><strong>{h(k)}:</strong> {h(v)}</li>" for k, v in b.extra)
        body = f'{title}<ul class="claims">{items}{extra}</ul>'
        return body if b.tone == "NEUTRAL" else f'<div class="box {h(b.tone)}">{body}</div>'
    if isinstance(b, KeyValues):
        title = f"<h3>{h(b.title)}</h3>" if b.title else ""
        rows = "".join(f"<tr><th>{h(k)}</th><td>{h(v)}</td></tr>" for k, v in b.rows)
        return f'<div class="box {h(b.tone)}">{title}<table class="kv">{rows}</table></div>'
    if isinstance(b, Table):
        return _table(b)
    if isinstance(b, Chart):
        meta = (f'<figcaption class="meta">{h(b.code)} · Basis {h(b.basis)} · upstream status '
                f'{h(b.status)} · {h(b.purpose)} · metrics: '
                f'{h(", ".join(b.metrics) or "not stated")}</figcaption>')
        if b.svg:
            data = base64.b64encode(b.svg.encode("utf-8")).decode("ascii")
            img = f'<img alt="{h(b.title)}" src="data:image/svg+xml;base64,{data}">'
            return f"<figure><h4>{h(b.title)}</h4>{img}{meta}</figure>"
        return (f'<figure class="box"><h4>Chart: {h(b.title)}</h4><p class="meta">Chart image '
                f"not available in this export; Box 6 definition referenced.</p>{meta}</figure>")
    raise TypeError(f"unknown block {type(b).__name__}")


def _section(s: Section) -> str:
    cls = ' class="prominent"' if s.prominent else ""
    out = [f'<section id="s{s.order}"{cls}><h2>{s.order}. {h(s.title)} '
           f'<span class="tag st-{h(s.status)}">{h(s.status_label)}</span></h2>']
    if s.missing:
        out.append(f'<p class="meta">Missing input families: {h(", ".join(s.missing))}</p>')
    out += [_block(b) for b in s.blocks]
    out.append("</section>")
    return "".join(out)


def render_html(doc: RenderDocument) -> str:
    if doc.cells > MAX_TEXT_CELLS:
        raise RenderError("RENDER_LIMIT_EXCEEDED",
                          f"{doc.cells} table cells exceed the HTML limit {MAX_TEXT_CELLS}")
    facts = "".join(f"<tr><th>{h(k)}</th><td>{h(v)}</td></tr>" for k, v in (
        *doc.identifiers, *doc.header_facts, ("Composed at", doc.generated_at)))
    toc = "".join(f'<li><a href="#s{s.order}">{s.order}. {h(s.title)}</a> '
                  f'<span class="meta st-{h(s.status)}">{h(s.status_label)}</span></li>'
                  for s in doc.sections)
    notes = ""
    if doc.warnings:
        notes = ("<section><h2>Rendering notes</h2><ul>" + "".join(
            f"<li>{h(w)}</li>" for w in doc.warnings) + "</ul></section>")
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{h(CSP)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{h(doc.title)} — as of {h(doc.as_of)}</title><style>{CSS}</style></head>"
        f"<body><main><header><h1>{h(doc.title)}</h1>"
        f'<p class="disclaimer">{h(doc.disclaimer)}</p>'
        f'<table class="kv">{facts}</table></header>'
        f'<nav><h2>Contents</h2><ul class="toc">{toc}</ul></nav>'
        + "".join(_section(s) for s in doc.sections) + notes +
        f'<footer><p class="disclaimer">{h(doc.disclaimer)}</p>'
        f'<p class="meta">Report {h(doc.report_id)} · as of {h(doc.as_of)} · Box 14 SHA-256 '
        f"{h(doc.box14_sha256)}</p></footer></main></body></html>\n")
    if len(page.encode("utf-8")) > MAX_HTML_BYTES:
        raise RenderError("RENDER_LIMIT_EXCEEDED", "the HTML export exceeds its size limit")
    return page
