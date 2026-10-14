"""DETAILED_RESEARCH_V1 Box 15: RenderDocument -> Markdown (a text export, never a source).

Pure and byte-deterministic. Every piece of report text is escaped so that a source title, a
management quote or a filing name can never open a link, emphasis, raw HTML or break a table:
newlines (and the Unicode line / paragraph separators) collapse to spaces and Markdown
punctuation is backslash-escaped. Forecast cells are italic under a FORECAST column tag.
"""

import re

from stock_research_ai.domain.report_rendering import (
    MAX_MARKDOWN_BYTES,
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

_BREAKS = re.compile(r"[\r\n  \x0b\x0c\x85]+")
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_SPECIAL = re.compile(r"([\\`*_\[\]<>|~&#])")

_TONE_TAGS = {"RED": "[RED] ", "YELLOW": "[YELLOW] ", "POSITIVE": "[POSITIVE] ",
              "LIMITATION": "[DATA LIMITATION] ", "DILIGENCE": "[OPEN DILIGENCE] ",
              "SCENARIO": "[SCENARIO] ", "NOTICE": "", "NEUTRAL": ""}


def md(text: str) -> str:
    """Inline-safe Markdown text: one line, punctuation escaped, control characters dropped."""
    one = _BREAKS.sub(" ", _CONTROL.sub("", text))
    return _SPECIAL.sub(r"\\\1", one)


def _claim(c: ClaimView) -> list[str]:
    meta = [c.type_label]
    if c.severity:
        meta.insert(0, c.severity)
    if c.basis != "NOT_APPLICABLE":
        meta.append(c.basis)
    if c.period:
        meta.append(c.period)
    head = f"- **{md(' · '.join(meta))}** — {md(c.statement)}"
    if c.value is not None:
        shown = c.exact_value and c.exact_value.lstrip("-") not in c.value
        exact = f" (exact {md(c.exact_value)})" if shown and c.exact_value else ""
        head += f" Value: {md(c.value)}{exact}."
    if c.refs:
        head += f" Sources: {md(', '.join(c.refs))}" + (
            f" (+{c.more_refs} more)" if c.more_refs else "")
    lines = [head]
    if c.source_text:
        label = "Management statement (verbatim)" if c.claim_type == (
            "MANAGEMENT_STATEMENT") else "Source text (verbatim)"
        lines.append(f"  - {label}: “{md(c.source_text)}”")
    if c.limitations:
        lines.append(f"  - Limitations: {md(', '.join(c.limitations))}")
    return lines


def _table(t: Table) -> list[str]:
    out = [f"#### {md(t.title)}", ""]
    if t.tags:
        out += [f"*{md(' · '.join(t.tags))}*", ""]
    for n in t.notices:
        out += [f"> **{md(n)}**", ""]
    heads = [md(c.label) + (f" \\[{md(' · '.join(c.tags))}\\]" if c.tags else "")
             for c in t.columns]
    out.append("| " + " | ".join(heads) + " |")
    out.append("|" + "|".join("---:" if c.numeric else "---" for c in t.columns) + "|")
    for row in t.rows:
        label = md(row.label) + (f" \\[{md(' · '.join(row.tags))}\\]" if row.tags else "")
        cells = [label]
        for cell in row.cells:
            text = md(cell.text)
            if cell.note:
                text += f" ({md(cell.note)})"
            if cell.kind == "NUMBER" and cell.forecast:
                text = f"*{text}*"
            elif cell.link:
                text = f"<{cell.link}>" if not any(ch in cell.link for ch in "<> |") else text
            cells.append(text)
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    for f in t.footnotes:
        if f:
            out.append(f"- *{md(f)}*")
    if t.refs:
        out.append(f"- Sources: {md(', '.join(t.refs))}" + (
            f" (+{t.more_refs} more)" if t.more_refs else ""))
    out.append("")
    return out


def _block(b: Block) -> list[str]:
    if isinstance(b, Paragraph):
        lead = f"**{md(b.label)}:** " if b.label else ""
        prefix = "> " if b.tone in ("NOTICE", "RED", "YELLOW") else ""
        return [f"{prefix}{_TONE_TAGS.get(b.tone, '')}{lead}{md(b.text)}", ""]
    if isinstance(b, ClaimList):
        out = []
        if b.title:
            out += [f"### {_TONE_TAGS.get(b.tone, '')}{md(b.title)}", ""]
        for c in b.claims:
            out += _claim(c)
        if not b.claims and b.empty_text:
            out.append(f"- {md(b.empty_text)}")
        for label, text in b.extra:
            out.append(f"- **{md(label)}:** {md(text)}")
        return [*out, ""]
    if isinstance(b, KeyValues):
        out = [f"### {_TONE_TAGS.get(b.tone, '')}{md(b.title)}", ""] if b.title else []
        out += ["| Item | Value |", "|---|---|"]
        out += [f"| {md(k)} | {md(v)} |" for k, v in b.rows]
        return [*out, ""]
    if isinstance(b, Table):
        return _table(b)
    if isinstance(b, Chart):
        image = ("Box 6 SVG available in the HTML export" if b.svg else
                 "Chart image not rendered in this format; Box 6 definition referenced")
        return [f"#### Chart: {md(b.title)}", "",
                f"- Code: {md(b.code)} · Basis: {md(b.basis)} · Upstream status: "
                f"{md(b.status)}",
                f"- Purpose: {md(b.purpose)}",
                f"- Metrics: {md(', '.join(b.metrics) or 'Not stated')}",
                f"- {md(image)}", ""]
    raise TypeError(f"unknown block {type(b).__name__}")


def _section(s: Section) -> list[str]:
    out = [f"## {s.order}. {md(s.title)}", ""]
    if s.prominent:
        out += ["> **READ FIRST — due-diligence status**", ""]
    if s.missing:
        out += [f"*Missing input families: {md(', '.join(s.missing))}*", ""]
    for b in s.blocks:
        out += _block(b)
    return out


def render_markdown(doc: RenderDocument) -> str:
    if doc.cells > MAX_TEXT_CELLS:
        raise RenderError("RENDER_LIMIT_EXCEEDED",
                          f"{doc.cells} table cells exceed the Markdown limit {MAX_TEXT_CELLS}")
    out = [f"# {md(doc.title)}", "", f"> {md(doc.disclaimer)}", "",
           "| Item | Value |", "|---|---|"]
    out += [f"| {md(k)} | {md(v)} |" for k, v in (*doc.identifiers, *doc.header_facts,
                                                  ("Composed at", doc.generated_at))]
    out += ["", "## Contents", ""]
    out += [f"{s.order}. {md(s.title)} — {md(s.status_label)}" for s in doc.sections]
    out.append("")
    for s in doc.sections:
        out += _section(s)
    if doc.warnings:
        out += ["## Rendering notes", ""] + [f"- {md(w)}" for w in doc.warnings] + [""]
    out += ["---", "", f"*{md(doc.disclaimer)}*", ""]
    text = "\n".join(out)
    if len(text.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        raise RenderError("RENDER_LIMIT_EXCEEDED", "the Markdown export exceeds its size limit")
    return text
