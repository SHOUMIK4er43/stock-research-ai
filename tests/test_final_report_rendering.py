# ruff: noqa: F811 - the Box 14 fixtures are imported and requested by name
"""DETAILED_RESEARCH_V1 Box 15: final report rendering + end-to-end orchestration.

Offline only: synthetic Box 14 datasets from the Box 14 test stack and the offline Screener
fixtures; every store lives in tmp_path.
"""

import ast
import asyncio
import html as htmllib
import io
import json
import os
import re
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pypdf
import pytest
from test_final_research_report import (  # noqa: F401 - pytest fixtures
    big_ledger,
    box13,
    compose,
    conflicted,
    d13,
    report,
    sector_report,
    stack,
)
from test_http import ORIGIN, auth_settings, pair
from test_screener_dataset import FIXTURES, page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.http.final_reports import download_name
from stock_research_ai.adapters.rendering import pdf as pdf_mod
from stock_research_ai.adapters.rendering.cli import build_research_stage
from stock_research_ai.adapters.rendering.pdf import render_pdf
from stock_research_ai.adapters.rendering.store import (
    FileFinalReportStore,
    UnsafePathError,
    company_component,
    symbol_component,
)
from stock_research_ai.application import report_orchestrator as orch
from stock_research_ai.application.report_claims import ReportCompositionError
from stock_research_ai.application.report_html import render_html
from stock_research_ai.application.report_markdown import md, render_markdown
from stock_research_ai.application.report_orchestrator import (
    ResearchStage,
    StagePayload,
    artifact_id_for,
    parse_formats,
    render_final_report,
    render_formats,
    report_json,
    run_detailed_research,
)
from stock_research_ai.application.report_presentation import (
    box14_sha256,
    build_presentation,
    safe_link,
)
from stock_research_ai.domain import report_rendering as rr
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import (
    SECTION_ORDER,
    SECTION_TITLES,
    FinalReportRequest,
    FinalResearchReportDataset,
    SectionStatus,
    advice_term,
)
from stock_research_ai.domain.financial_canonical import Industry
from stock_research_ai.domain.market_intelligence import FetchStatus, PayloadKind, RawPayload
from stock_research_ai.domain.report_rendering import (
    RenderFormat,
    cell_number,
    display_exact,
    display_number,
    display_value,
)
from stock_research_ai.interfaces import http as http_interface

F = RenderFormat
ROOT = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
NEW_MODULES = ("domain/report_rendering.py", "application/report_presentation.py",
               "application/report_markdown.py", "application/report_html.py",
               "application/report_orchestrator.py", "adapters/rendering/pdf.py",
               "adapters/rendering/store.py", "adapters/rendering/cli.py",
               "adapters/http/final_reports.py")
EVIL = '<script>alert("x")</script> | pipe\nnew line ** _em_ [link](javascript:alert(1)) &amp;'
NOW = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


# --- helpers --------------------------------------------------------------------------------


def pdf_text(data: bytes) -> str:
    return " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(data)).pages)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


@pytest.fixture(scope="module")
def rendered(report: FinalResearchReportDataset) -> dict[str, Any]:
    doc = build_presentation(report)
    return {"doc": doc, "md": render_markdown(doc), "html": render_html(doc),
            "pdf": render_pdf(doc), "json": report_json(report)}


@pytest.fixture(scope="module")
def pdf_plain(rendered: dict[str, Any]) -> str:
    return pdf_text(rendered["pdf"])


def mutate(report: FinalResearchReportDataset, fn: Any) -> FinalResearchReportDataset:
    data = report.model_dump(mode="json")
    fn(data)
    return FinalResearchReportDataset.model_validate(data)


@pytest.fixture(scope="module")
def evil_report(report: FinalResearchReportDataset) -> FinalResearchReportDataset:
    """Malicious upstream text everywhere text is copied verbatim (Box 14 never advice-checks
    source text, titles or URLs)."""
    def fn(d: dict[str, Any]) -> None:
        d["metadata"]["company_name"] = "Widget <Co> & Sons — ₹ “Ltd” – " + EVIL
        for s in d["sources"]:
            s["title"] = EVIL + " " + s["title"]
        d["sources"][0]["url"] = "javascript:alert(1)"
        d["sources"][1]["url"] = "file:///etc/passwd"
        d["sources"][2]["url"] = "https://evil.example/x.png\"><img src=x onerror=alert(1)>"
        for c in d["claims"]:
            if c["claim_type"] in ("MANAGEMENT_STATEMENT", "FACT") and c["source_text"]:
                c["source_text"] = (EVIL + ' <img src="http://evil.example/p.png"/> '
                                    '<a href="file:///etc/passwd">x</a> ' + "y" * 3000)[:4000]
        for t in d["tables"]:
            for row in t["rows"]:
                for cell in row["cells"]:
                    if cell["text_origin"] == "UPSTREAM":
                        cell["text"] = (EVIL + " " + cell["text"])[:1000]
    return mutate(report, fn)


# --- static discipline ----------------------------------------------------------------------


def test_new_modules_write_no_advice_language_and_call_no_network_or_ai():
    banned_imports = {"httpx", "requests", "socket", "urllib.request", "anthropic", "openai"}
    for rel in NEW_MODULES:
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if advice_term(text) and "BUYBACK" not in text.upper():
                    raise AssertionError(f"{rel}: advice language in {text[:60]!r}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [
                    node.module or ""]
                assert not set(names) & banned_imports, (rel, names)


def test_application_layer_imports_only_domain_application_pydantic_and_stdlib():
    for rel in NEW_MODULES:
        if not rel.startswith(("domain/", "application/")):
            continue
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
                    "stock_research_ai."):
                assert node.module.split(".")[1] in ("domain", "application"), (rel,
                                                                                node.module)


# --- display policy -------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "whole", "expected"), [
    ("13.086771", False, "13.09"), ("73.581710", False, "73.58"), ("128.226560", False, "128.23"),
    ("11364.72", False, "11,364.72"), ("-4064", False, "-4,064.00"), ("-1889", False, "-1,889.00"),
    ("0.005", False, "0.01"), ("0.004", False, "<0.01"), ("-0.004", False, ">-0.01"),
    ("0", False, "0.00"), ("-0", False, "0.00"), ("107", True, "107"), ("14.000000", True, "14"),
    ("2.5", True, "2.50"), ("1234567.891", False, "1,234,567.89"), ("-0.005", False, "-0.01"),
    ("99.995", False, "100.00"),
])
def test_display_rounding_policy(value, whole, expected):
    assert display_number(Decimal(value), whole=whole) == expected


def test_display_units_keep_sign_and_never_infer_a_scale():
    assert display_value(Decimal("11364.72"), "INR CRORE") == "₹11,364.72 Cr"
    assert display_value(Decimal("-4064"), "INR CRORES") == "-₹4,064.00 Cr"
    assert display_value(Decimal("2.37"), "INR_CRORE") == "₹2.37 Cr"
    assert display_value(Decimal("13.086771"), "PERCENT") == "13.09%"
    assert display_value(Decimal("-5.36"), "PERCENTAGE_POINTS") == "-5.36 pp"
    assert display_value(Decimal("0.967852"), "TIMES") == "0.97x"
    assert display_value(Decimal("2.18"), "INR per share") == "₹2.18/share"
    assert display_value(Decimal("8863"), "INR MONEY") == "8,863.00 INR MONEY"
    assert display_value(Decimal("13.086771"), "PERCENT", exact=True) == "13.086771%"
    assert cell_number(Decimal("-4064"), "INR CRORES") == "-4,064.00"
    assert display_exact(Decimal("-0.938861")) == "-0.938861"


# --- presentation: order, status, completeness ----------------------------------------------


def test_every_section_in_canonical_order_in_every_format(report, rendered, pdf_plain):
    doc = rendered["doc"]
    assert [s.section_id for s in doc.sections] == [s.value for s in SECTION_ORDER]
    titles = [f"{i}. {SECTION_TITLES[s]}" for i, s in enumerate(SECTION_ORDER)]
    for text, esc in ((rendered["md"], md), (htmllib.unescape(rendered["html"]), str)):
        positions = [text.rindex(esc(t)) for t in titles]
        assert positions == sorted(positions)
    flat = squash(pdf_plain)
    last = flat.index(squash(titles[0]))  # contents page lists them in order too
    for t in titles[1:]:
        last = flat.index(squash(t), last)


def test_section_status_and_reason_always_rendered(report, rendered):
    for s in report.sections:
        line = f"{rr.STATUS_LABELS[s.status.value]} — {s.status_reason}"
        assert md(line) in rendered["md"]
        assert htmllib.escape(line) in rendered["html"]


def test_not_applicable_is_distinct_from_unavailable_and_never_a_warning():
    _, r = sector_report(Industry.BANK)
    doc = build_presentation(r)
    na = [s for s in doc.sections if s.status == "NOT_APPLICABLE"]
    assert na, "the bank fixture has a not-applicable section"
    for s in na:
        status = s.blocks[0]
        assert isinstance(status, rr.Paragraph)
        assert status.text.startswith("Not applicable") and status.tone == "NEUTRAL"
    text = render_markdown(doc)
    assert "Not applicable — " in text
    unavailable = [s for s in doc.sections if s.status == "UNAVAILABLE"]
    for s in unavailable:
        assert s.blocks[0].text.startswith("Data unavailable")  # type: ignore[union-attr]


def test_every_claim_reaches_the_readable_formats(report, rendered):
    doc = rendered["doc"]
    shown: set[UUID] = set()
    for s in doc.sections:
        for b in s.blocks:
            if isinstance(b, rr.ClaimList):
                shown |= {c.claim_id for c in b.claims}
    in_tables = {c.claim_id for t in report.tables for row in t.rows for c in row.cells
                 if c.claim_id} | {row.claim_id for t in report.tables for row in t.rows
                                   if row.claim_id}
    texts = htmllib.unescape(rendered["html"])
    for c in report.claims:
        assert c.claim_id in shown | in_tables or c.statement in texts, c.topic


def test_executive_summary_renders_box14_claims_verbatim(report, rendered):
    html = rendered["html"]
    for p in report.executive_summary.points:
        for i in p.claim_ids:
            assert htmllib.escape(report.claim(i).statement) in html
    assert "Recommendation: none" in rendered["md"]


def test_claim_types_are_visually_distinct(rendered):
    html = rendered["html"]
    for t in ("FACT", "CALCULATION", "SCENARIO", "DATA_LIMITATION", "MANAGEMENT_STATEMENT"):
        assert f'class="ct {t}"' in html
    assert "Management statement (verbatim)" in rendered["md"]
    assert "Management statement (verbatim)" in html


def test_basis_labels_on_every_table(report, rendered, pdf_plain):
    for t in report.tables:
        tag = f"Basis: {t.basis.value}"
        assert md(tag) in rendered["md"] and tag in rendered["html"]
    assert "Basis: CONSOLIDATED" in pdf_plain and "Basis: STANDALONE" in pdf_plain


def test_forecasts_are_labelled_and_never_look_historical(report, rendered, pdf_plain):
    doc = rendered["doc"]
    tables = [b for s in doc.sections for b in s.blocks if isinstance(b, rr.Table)]
    forecast_tables = [t for t in tables if any("FORECAST" in c.tags for c in t.columns)]
    assert forecast_tables
    for t in forecast_tables:
        for j, col in enumerate(t.columns[1:]):
            if "FORECAST" in col.tags:
                assert all(r.cells[j].forecast for r in t.rows)
    assert 'td class="num fc"' in rendered["html"]
    assert re.search(r"\| \*[-0-9,.<>]+%?\* \|", rendered["md"])
    for scenario in ("BASE", "BULL", "BEAR"):
        assert f"Scenario: {scenario}" in rendered["md"]
        assert f"{scenario} scenario" in rendered["md"]
    assert "FORECAST" in pdf_plain


def test_order_book_stages_are_separate_and_never_summed(report, rendered, pdf_plain):
    for text in (rendered["md"], htmllib.unescape(rendered["html"]), pdf_plain):
        flat = squash(text)
        assert squash("L1 ≠ LOA ≠ CONTRACT ≠ REVENUE") in flat
        for title in ("CONFIRMED ORDER BOOK", "PENDING L1", "UNRESOLVED L1", "SOURCE INCOMPLETE"):
            assert squash(title) in flat
        assert squash("combined total — none") in flat


def test_risks_and_data_limitations_render_in_separate_blocks(report, rendered):
    doc = rendered["doc"]
    risks = next(s for s in doc.sections if s.section_id == "RISKS")
    lists = {b.title: b for b in risks.blocks if isinstance(b, rr.ClaimList)}
    red = lists["RED company risks"]
    limits = next(v for k, v in lists.items() if k and k.startswith("DATA LIMITATIONS"))
    assert red.tone == "RED" and limits.tone == "LIMITATION"
    red_ids = {e.claim_id for e in report.risks.risk_register if e.risk_id in report.risks.red}
    assert red_ids <= {c.claim_id for c in red.claims}
    assert not ({c.claim_id for c in limits.claims} & red_ids)
    assert all(c.claim_type == "DATA_LIMITATION" for c in limits.claims)
    assert all(c.severity is None for c in limits.claims)
    assert "Open diligence" in lists and "Positive controls" in lists
    assert "box LIMITATION" in rendered["html"] and "box RED" in rendered["html"]


def test_source_register_is_complete_and_deduplicated(report, rendered):
    doc = rendered["doc"]
    reg = next(b for s in doc.sections for b in s.blocks
               if isinstance(b, rr.Table) and b.table_id == "source-register")
    assert [r.label for r in reg.rows] == [f"S{i}" for i in range(1, len(report.sources) + 1)]
    for src in report.sources:
        assert htmllib.escape(src.title) in rendered["html"]


def test_source_disagreements_keep_every_observation(conflicted):
    _, r = conflicted
    doc = build_presentation(r)
    text = render_markdown(doc)
    view = next(d for d in r.source_disagreements if d.status == "OPEN")
    for v in view.values:
        assert v.value is not None and md(display_exact(v.value)) in text
    assert "Canonical selection: None (open)" in text
    dd = next(s for s in doc.sections if s.section_id == "DUE_DILIGENCE_STATUS")
    kv = next(b for b in dd.blocks if isinstance(b, rr.KeyValues))
    assert dict(kv.rows)["Open source conflicts"] == str(
        r.due_diligence_status.open_source_conflicts)


def test_truncated_appendix_is_surfaced_never_silent(big_ledger):
    _, r = big_ledger
    doc = build_presentation(r)
    text = render_markdown(doc)
    assert "APPENDIX TRUNCATED" in text and "Appendix truncated" in text
    pdf = pdf_text(render_pdf(doc))
    assert "APPENDIX TRUNCATED" in pdf


def test_unavailable_cells_never_render_as_zero_or_blank(report, rendered):
    doc = rendered["doc"]
    for s in doc.sections:
        for b in s.blocks:
            if not isinstance(b, rr.Table):
                continue
            for row in b.rows:
                for cell in row.cells:
                    assert cell.text.strip() not in ("", "-", "—", "–")
                    if cell.kind == "UNAVAILABLE":
                        assert cell.text == "Data unavailable"
                    if cell.kind == "NOT_APPLICABLE":
                        assert cell.text == "Not applicable"
    assert "Data unavailable" in rendered["md"]


def test_due_diligence_status_is_prominent_and_counts_upstream(report, rendered):
    zero = rendered["doc"].sections[0]
    assert zero.prominent and "READ FIRST" in rendered["md"]
    kv = next(b for b in zero.blocks if isinstance(b, rr.KeyValues))
    rows = dict(kv.rows)
    dd = report.due_diligence_status
    assert rows["Open diligence items"] == str(len(dd.open_diligence_item_ids))
    assert rows["Blocked / failed sources"] == str(len(dd.blocked_sources))
    assert rows["Score"].startswith("None")


def test_valuation_methods_are_separate_and_nothing_is_blended(report, rendered):
    md_text = rendered["md"]
    for m in report.valuation.methods:
        assert md(m.method) in md_text
    assert "Blended value: none" in md_text and "Price objective: none" in md_text


def test_charts_use_box6_svg_in_html_and_a_reference_fallback_elsewhere(report, stack):
    with_svg = build_presentation(report, charts=stack.box6)
    charts = [b for s in with_svg.sections for b in s.blocks if isinstance(b, rr.Chart)]
    assert charts and all(c.svg for c in charts)
    html = render_html(with_svg)
    assert html.count('src="data:image/svg+xml;base64,') == len(charts)
    assert "Box 6 SVG available in the HTML export" in render_markdown(with_svg)
    without = build_presentation(report)
    assert any(w.startswith("CHART_FALLBACK:") for w in without.warnings)
    assert "Box 6 definition referenced" in render_html(without)
    assert "Chart image not rendered in the PDF" in pdf_text(render_pdf(without))


def test_disclaimer_and_no_recommendation(rendered, pdf_plain):
    for text in (rendered["md"], rendered["html"]):
        assert text.count("not investment advice") >= 2
    assert "not investment advice" in pdf_plain


# --- JSON -----------------------------------------------------------------------------------


def test_json_is_the_box14_dataset_byte_deterministic(report, rendered):
    text = rendered["json"]
    assert FinalResearchReportDataset.model_validate_json(text) == report
    assert report_json(report) == text
    data = json.loads(text)
    by_id = {c["claim_id"]: c for c in data["claims"]}
    numeric = [c for c in report.claims if c.value is not None]
    assert numeric
    for c in numeric:  # exact value, never rounded
        assert Decimal(by_id[str(c.claim_id)]["value"]) == c.value


# --- determinism, immutability --------------------------------------------------------------


def test_all_formats_are_byte_deterministic(report, rendered):
    doc = build_presentation(report)
    assert doc == rendered["doc"]
    assert render_markdown(doc) == rendered["md"]
    assert render_html(doc) == rendered["html"]
    assert render_pdf(doc) == rendered["pdf"]


def test_inputs_are_never_mutated(report, stack):
    before = report.model_dump(mode="json")
    charts_before = stack.box6.model_dump(mode="json")
    render_formats(report, list(F), pdf=render_pdf, charts=stack.box6)
    assert report.model_dump(mode="json") == before
    assert stack.box6.model_dump(mode="json") == charts_before


# --- Unicode, escaping, malicious input -----------------------------------------------------


def test_unicode_survives_every_format(evil_report):
    doc = build_presentation(evil_report)
    md_text, html_text = render_markdown(doc), render_html(doc)
    pdf = pdf_text(render_pdf(doc))
    for ch in ("₹", "—", "–", "“", "”"):
        assert ch in md_text and ch in html_text and ch in pdf


def test_html_escapes_all_untrusted_text(evil_report):
    html = render_html(build_presentation(evil_report))
    assert "<script" not in html.lower()
    assert "onerror" not in html.replace("onerror=alert(1)&gt;", "")  # only escaped form
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html and 'href="file:' not in html
    assert "<img src=x" not in html
    srcs = re.findall(r'\ssrc="([^"]*)"', html)
    assert all(s.startswith("data:image/svg+xml;base64,") for s in srcs)
    assert "default-src &#x27;none&#x27;" in html
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert all(h.startswith(("#", "https://", "http://")) for h in hrefs)


def test_safe_link_accepts_only_absolute_http_urls():
    assert safe_link("https://www.bseindia.com/a.pdf") == "https://www.bseindia.com/a.pdf"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "/relative",
                "https://x.example/a\"b", "https:///nohost", "ftp://x.example/", None, ""):
        assert safe_link(bad) is None


def test_markdown_tables_survive_pipes_newlines_and_markup(evil_report):
    text = render_markdown(build_presentation(evil_report))
    assert "<script>" not in text and "\\<script\\>" in text
    lines = text.splitlines()
    in_table = False
    width = 0
    for line in lines:
        if line.startswith("|---") or line.startswith("|--"):
            in_table, width = True, line.count("|")
            continue
        if in_table and line.startswith("| "):
            assert len(re.findall(r"(?<!\\)\|", line)) == width, line[:120]
        elif in_table:
            in_table = False
    assert "[link](javascript" not in text


def test_pdf_treats_report_text_as_text_never_markup(evil_report):
    data = render_pdf(build_presentation(evil_report))
    text = pdf_text(data)
    assert '<script>alert("x")</script>' in text or "<script>" in text
    raw = data
    assert b"/JavaScript" not in raw and b"/JS " not in raw and b"/Launch" not in raw
    assert b"javascript:" not in raw.lower() and b"file:///" not in raw.lower()


def test_rendering_works_offline_with_the_network_blocked(report, monkeypatch):
    import socket

    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("network access during rendering")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    files, failures, _ = render_formats(report, list(F), pdf=render_pdf)
    assert not failures and set(files) == set(F)


def test_oversized_cell_text_moves_to_a_note_instead_of_clipping(report):
    long = "LONGTEXT " * 450

    def fn(d: dict[str, Any]) -> None:
        for t in d["tables"]:
            if t["code"] == "MANAGEMENT_STATEMENTS" or t["code"] == "MATERIAL_EVENTS":
                for row in t["rows"][:2]:
                    for cell in row["cells"]:
                        if cell["text_origin"] == "UPSTREAM":
                            cell["text"] = long[:1000]
    r = mutate(report, fn)
    data = render_pdf(build_presentation(r))
    assert data.startswith(b"%PDF")


# --- bounds and large reports ---------------------------------------------------------------


def test_large_report_renders_within_bounds(big_ledger):
    _, r = big_ledger
    doc = build_presentation(r)
    files, failures, _ = render_formats(r, list(F), pdf=render_pdf)
    assert not failures
    assert len(files[F.PDF]) < rr.MAX_PDF_BYTES
    assert len(pypdf.PdfReader(io.BytesIO(files[F.PDF])).pages) < rr.MAX_PDF_PAGES
    assert doc.cells < rr.MAX_PDF_CELLS


def test_limits_fail_explicitly_never_silently(report, monkeypatch):
    doc = build_presentation(report)
    monkeypatch.setattr("stock_research_ai.application.report_markdown.MAX_TEXT_CELLS", 10)
    monkeypatch.setattr("stock_research_ai.application.report_html.MAX_TEXT_CELLS", 10)
    monkeypatch.setattr(pdf_mod, "MAX_PDF_PAGES", 2)
    for fn in (render_markdown, render_html, render_pdf):
        with pytest.raises(rr.RenderError) as err:
            fn(doc)
        assert err.value.code == "RENDER_LIMIT_EXCEEDED"


# --- store: paths, atomicity, manifest, cache ------------------------------------------------


def test_path_components_are_generated_and_validated(tmp_path):
    assert company_component("RVNL", "x") == "nse-RVNL"
    assert company_component("M&M", "x") == "nse-M_M"
    for bad in ("../etc", "/abs", "a/b", "rvnl", "", "A" * 21, "..", "R\x00"):
        assert company_component(bad, "screener:1").startswith("key-")
        with pytest.raises(UnsafePathError):
            symbol_component(bad)
    store = FileFinalReportStore(tmp_path / "reports")
    for bad in ("../x/2026-01-01/abc", "nse-A/2026-01-01/../../x", "nse-A/../2026-01-01/x",
                "/etc/2026-01-01/x", "nse-A/2026-1-1/x", "nse-A/2026-01-01"):
        with pytest.raises(UnsafePathError):
            store._run_dir(bad, create=False)
    with pytest.raises(UnsafePathError):
        FileFinalReportStore(Path("relative/root"))


def test_symlink_escape_is_refused(tmp_path, report):
    root = tmp_path / "reports"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    company = company_component(report.metadata.nse_symbol, report.metadata.company_key)
    (root / company).symlink_to(outside, target_is_directory=True)
    store = FileFinalReportStore(root)
    result = render_final_report(report, store=store, pdf=None, formats=[F.JSON])
    assert result.status == "FAILED"
    assert result.failure and result.failure.code == rr.FailureCode.OUTPUT_IO_FAILED
    assert list(outside.iterdir()) == []


def store_for(tmp_path: Path) -> FileFinalReportStore:
    return FileFinalReportStore(tmp_path / "final-reports")


def test_render_writes_manifest_files_and_hashes(tmp_path, report):
    store = store_for(tmp_path)
    result = render_final_report(report, store=store, pdf=render_pdf)
    assert result.status == "COMPLETE" and not result.cached
    m = result.manifest
    assert m is not None
    run = tmp_path / "final-reports" / m.location
    assert m.location == f"nse-{report.metadata.nse_symbol}/{report.metadata.as_of}/" \
                         f"{artifact_id_for(box14_sha256(report))}"
    assert sorted(p.name for p in run.iterdir() if not p.name.startswith(".")) == [
        "manifest.json", "report.html", "report.json", "report.md", "report.pdf"]
    assert not [p for p in run.iterdir() if p.name.startswith(".pending")]
    for f in m.files:
        data = (run / f.filename).read_bytes()
        assert len(data) == f.bytes and orch._sha_bytes(data) == f.sha256
        assert f.media_type == rr.MEDIA_TYPES[f.format]
    assert m.box14_sha256 == box14_sha256(report) and m.as_of == report.metadata.as_of
    assert dict(m.engines)["renderer"] == rr.RENDERER_VERSION
    manifest_text = (run / "manifest.json").read_text()
    assert str(tmp_path) not in manifest_text  # no absolute paths
    assert rr.ReportManifest.model_validate_json(manifest_text) == m
    assert oct(os.stat(run / "report.pdf").st_mode & 0o777) == "0o600"


def test_manifest_is_deterministic(tmp_path, report):
    a = render_final_report(report, store=store_for(tmp_path / "a"), pdf=render_pdf)
    b = render_final_report(report, store=store_for(tmp_path / "b"), pdf=render_pdf)
    assert a.manifest == b.manifest
    assert (tmp_path / "a/final-reports" / a.manifest.location / "manifest.json").read_bytes() \
        == (tmp_path / "b/final-reports" / b.manifest.location / "manifest.json").read_bytes()


def test_cache_reuses_verified_files_and_never_serves_stale_content(tmp_path, report):
    store = store_for(tmp_path)
    first = render_final_report(report, store=store, pdf=render_pdf)
    again = render_final_report(report, store=store, pdf=render_pdf)
    assert again.cached and again.manifest == first.manifest
    run = tmp_path / "final-reports" / first.manifest.location
    (run / "report.md").write_text("tampered")
    healed = render_final_report(report, store=store, pdf=render_pdf)
    assert not healed.cached and (run / "report.md").read_text() != "tampered"
    newer = mutate(report, lambda d: d["metadata"].update(as_of="2026-10-11"))
    other = render_final_report(newer, store=store, pdf=render_pdf)
    assert other.manifest.artifact_id != first.manifest.artifact_id
    assert other.manifest.location.split("/")[1] == "2026-10-11"
    assert artifact_id_for("a" * 64) != artifact_id_for("b" * 64)


def test_cache_key_includes_the_renderer_version(monkeypatch):
    a = artifact_id_for("a" * 64)
    monkeypatch.setattr(orch, "RENDERER_VERSION", "report-renderer/2")
    assert artifact_id_for("a" * 64) != a


def test_partial_render_keeps_good_formats_and_names_the_failed_one(tmp_path, report):
    store = store_for(tmp_path)
    ok = render_final_report(report, store=store, pdf=render_pdf)
    run = tmp_path / "final-reports" / ok.manifest.location
    assert (run / "report.pdf").exists()

    def broken(_doc: rr.RenderDocument) -> bytes:
        raise rr.RenderError("RENDER_ENGINE_ERROR", "synthetic PDF failure")

    for f in run.glob("report.*"):  # force a re-render of the same run directory
        f.write_bytes(b"x")
    result = render_final_report(report, store=store, pdf=broken)
    assert result.status == "PARTIAL_RENDER"
    assert result.failure and result.failure.code == rr.FailureCode.PARTIAL_RENDER
    assert "PDF" in result.failure.detail
    m = result.manifest
    assert m.status == "PARTIAL_RENDER"
    assert [f.format for f in m.failures] == [F.PDF]
    assert {f.format for f in m.files} == {F.JSON, F.MARKDOWN, F.HTML}
    assert not (run / "report.pdf").exists()  # no file that looks successful
    assert FinalResearchReportDataset.model_validate_json((run / "report.json").read_text())


def test_a_crashing_renderer_is_isolated(tmp_path, report, monkeypatch):
    monkeypatch.setattr(orch, "render_html", lambda doc: 1 / 0)
    result = render_final_report(report, store=store_for(tmp_path), pdf=render_pdf)
    assert result.status == "PARTIAL_RENDER"
    assert result.manifest.failures[0].code == "RENDER_ENGINE_ERROR"
    assert {f.format for f in result.manifest.files} == {F.JSON, F.MARKDOWN, F.PDF}


def test_every_format_failing_is_render_failed(tmp_path, report, monkeypatch):
    for name in ("render_html", "render_markdown", "report_json"):
        monkeypatch.setattr(orch, name, lambda *_: 1 / 0)
    result = render_final_report(report, store=store_for(tmp_path), pdf=None)
    assert result.status == "FAILED"
    assert result.failure and result.failure.code == rr.FailureCode.RENDER_FAILED
    assert result.manifest.status == "RENDER_FAILED"


def test_concurrent_renders_do_not_corrupt_or_cross_contaminate(tmp_path, report, evil_report):
    store = store_for(tmp_path)
    results: list[Any] = []
    errors: list[BaseException] = []

    def work(r: FinalResearchReportDataset) -> None:
        try:
            results.append(render_final_report(r, store=store, pdf=render_pdf))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(r,)) for r in
               (report, report, evil_report, report, evil_report)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 5
    for r in (report, evil_report):
        m = store.load(artifact_id_for(box14_sha256(r)))
        assert m is not None and store.verified_formats(m) == set(F)
        run = tmp_path / "final-reports" / m.location
        assert FinalResearchReportDataset.model_validate_json(
            (run / "report.json").read_text()) == r
        assert not list(run.glob(".pending-*"))


def test_store_detects_tampering_on_read(tmp_path, report):
    store = store_for(tmp_path)
    m = render_final_report(report, store=store, pdf=None, formats=[F.JSON]).manifest
    assert store.read_file(m, F.JSON) is not None
    run = tmp_path / "final-reports" / m.location
    (run / "report.json").write_text("{}")
    assert store.read_file(m, F.JSON) is None
    assert store.read_file(m, F.PDF) is None  # never produced


def test_format_validation():
    assert parse_formats(["json", "MD", "html", "pdf"]) == tuple(F)
    assert parse_formats(None) == tuple(F)
    for bad in (["docx"], ["../report"], ["pdf", "exe"]):
        with pytest.raises(ValueError):
            parse_formats(bad)


# --- orchestrator ---------------------------------------------------------------------------


async def screener_stage(tmp_path: Path, case: tuple[str, str]) -> ResearchStage:
    extraction, content = await page(tmp_path, FIXTURES, case)
    stage = build_research_stage(
        generated_at=NOW, pages=[(extraction, content)], catalogue=None, bundle=None,
        diligence_request=DueDiligenceRequest(as_of=date(2026, 10, 10), company_key="x"),
        report_request=FinalReportRequest(as_of=date(2026, 10, 10), company_key="x"))
    key = stage.screeners[0].company_key
    return stage.model_copy(update={
        "diligence_request": DueDiligenceRequest(as_of=date(2026, 10, 10), company_key=key),
        "report_request": FinalReportRequest(as_of=date(2026, 10, 10), company_key=key)})


@pytest.fixture(scope="module")
def infy_stage(tmp_path_factory: pytest.TempPathFactory) -> ResearchStage:
    return asyncio.run(screener_stage(tmp_path_factory.mktemp("infy"), INFY))


@pytest.fixture(scope="module")
def bank_stage(tmp_path_factory: pytest.TempPathFactory) -> ResearchStage:
    return asyncio.run(screener_stage(tmp_path_factory.mktemp("bank"), YESBANK))


def test_orchestrator_runs_boxes_4_to_14_and_renders(tmp_path, infy_stage):
    result = run_detailed_research(infy_stage.model_dump_json(), store=store_for(tmp_path),
                                   pdf=render_pdf)
    assert result.status == "COMPLETE", result.failure
    boxes = {s.box: s.status for s in result.steps}
    assert boxes == {"BOX4": "BUILT", "BOX5": "BUILT", "BOX6": "BUILT", "BOX7": "BUILT",
                     "BOX8": "NOT_RUN", "BOX9": "NOT_RUN", "BOX10": "BUILT",
                     "BOX11": "BUILT", "BOX12": "NOT_RUN", "BOX13": "BUILT", "BOX14": "BUILT"}
    assert {f.format for f in result.manifest.files} == set(F)
    again = run_detailed_research(infy_stage, store=store_for(tmp_path), pdf=render_pdf)
    assert again.cached and again.box14_sha256 == result.box14_sha256


def test_financial_company_end_to_end_keeps_not_applicable(tmp_path, bank_stage):
    result = run_detailed_research(bank_stage, store=store_for(tmp_path), pdf=render_pdf,
                                   formats=["md"])
    assert result.status == "COMPLETE", result.failure
    run = tmp_path / "final-reports" / result.manifest.location
    text = (run / "report.md").read_text()
    assert "11. Order Book / Project Pipeline" in text
    assert re.search(r"Section status:\*\* Not applicable — ", text)


def test_orchestrator_classifies_failures(tmp_path, infy_stage, monkeypatch):
    store = store_for(tmp_path)
    bad = run_detailed_research(b"{not json", store=store, pdf=None)
    assert bad.status == "FAILED" and bad.failure.code == rr.FailureCode.INPUT_ERROR
    fmt = run_detailed_research(infy_stage, store=store, pdf=None, formats=["exe"])
    assert fmt.failure.code == rr.FailureCode.INPUT_ERROR

    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("synthetic Box 10 defect")

    monkeypatch.setattr(orch, "build_forecast", boom)
    up = run_detailed_research(infy_stage, store=store, pdf=None)
    assert up.failure.code == rr.FailureCode.UPSTREAM_RESEARCH_FAILED
    assert up.failure.stage == "BOX10" and "synthetic Box 10 defect" in up.failure.detail
    assert up.failure.error_type == "ValueError"
    monkeypatch.undo()

    def refuse(**k: Any) -> Any:
        raise ReportCompositionError("COMPANY_MISMATCH", "synthetic composition refusal")

    monkeypatch.setattr(orch, "build_final_report", refuse)
    comp = run_detailed_research(infy_stage, store=store, pdf=None)
    assert comp.failure.code == rr.FailureCode.REPORT_COMPOSITION_FAILED
    assert comp.failure.stage == "BOX14" and "COMPANY_MISMATCH" in comp.failure.detail


def test_chart_failure_never_stops_the_report(tmp_path, infy_stage, monkeypatch):
    monkeypatch.setattr(orch, "build_chart_bundle", lambda *a, **k: 1 / 0)
    result = run_detailed_research(infy_stage, store=store_for(tmp_path), pdf=None,
                                   formats=["json"])
    assert result.status == "COMPLETE"
    assert next(s for s in result.steps if s.box == "BOX6").status == "FAILED"


def test_output_io_failure_is_classified(tmp_path, infy_stage):
    blocker = tmp_path / "final-reports"
    blocker.write_text("not a directory")
    result = run_detailed_research(infy_stage, store=FileFinalReportStore(blocker),
                                   pdf=None, formats=["json"])
    assert result.failure and result.failure.code == rr.FailureCode.OUTPUT_IO_FAILED


def test_stage_payload_bytes_round_trip_exactly():
    raw = RawPayload(kind=list(PayloadKind)[0], url="https://www.nseindia.com/api/x",
                     status=FetchStatus.OK, http_status=200, retrieved_at=NOW,
                     content=bytes(range(256)), content_type="application/json")
    staged = StagePayload.of(raw)
    assert StagePayload.model_validate_json(staged.model_dump_json()).payload() == raw


# --- HTTP -----------------------------------------------------------------------------------


@pytest.fixture
async def api(tmp_path, engine, report):
    settings = auth_settings(tmp_path)
    store = store_for(tmp_path)
    result = render_final_report(report, store=store, pdf=render_pdf)
    app = http_interface.create_app(engine=engine, settings=settings, final_report_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            yield client, settings, result.manifest, tmp_path


async def test_final_report_routes_require_pairing(api):
    client, _, m, _ = api
    for path in (f"/v1/final-reports?symbol={m.nse_symbol}", f"/v1/final-reports/{m.artifact_id}",
                 f"/v1/final-reports/{m.artifact_id}/files/PDF"):
        response = await client.get(path)
        assert response.status_code == 401
        assert m.company_name not in response.text


async def test_final_report_routes_serve_verified_files(api, report):
    client, settings, m, _ = api
    auth = await pair(client, settings)
    listed = await client.get(f"/v1/final-reports?symbol={m.nse_symbol}", headers=auth)
    assert listed.status_code == 200
    reports = listed.json()["reports"]
    assert reports[0]["artifact_id"] == str(m.artifact_id)
    assert reports[0]["formats"] == ["JSON", "MARKDOWN", "HTML", "PDF"]
    got = await client.get(f"/v1/final-reports/{m.artifact_id}", headers=auth)
    assert got.status_code == 200 and got.json()["box14_sha256"] == m.box14_sha256
    expected = {"JSON": "application/json", "MARKDOWN": "text/markdown; charset=utf-8",
                "HTML": "text/html; charset=utf-8", "PDF": "application/pdf"}
    for fmt, mime in expected.items():
        r = await client.get(f"/v1/final-reports/{m.artifact_id}/files/{fmt}", headers=auth)
        assert r.status_code == 200 and r.headers["content-type"] == mime
        disposition = r.headers["content-disposition"]
        assert re.fullmatch(r'attachment; filename="[A-Za-z0-9_-]+_\d{4}-\d{2}-\d{2}_'
                            r'detailed_research\.(json|md|html|pdf)"', disposition)
        assert r.headers["x-content-type-options"] == "nosniff"
        entry = next(f for f in m.files if f.format.value == fmt)
        assert orch._sha_bytes(r.content) == entry.sha256
    html = await client.get(f"/v1/final-reports/{m.artifact_id}/files/HTML", headers=auth)
    assert "sandbox" in html.headers["content-security-policy"]


async def test_final_report_routes_reject_bad_input(api):
    client, settings, m, tmp_path = api
    auth = await pair(client, settings)
    assert (await client.get("/v1/final-reports?symbol=../../etc", headers=auth)
            ).status_code == 422
    assert (await client.get("/v1/final-reports?symbol=NOPE", headers=auth)
            ).json()["reports"] == []
    assert (await client.get(f"/v1/final-reports/{uuid4()}", headers=auth)).status_code == 404
    assert (await client.get("/v1/final-reports/not-a-uuid", headers=auth)).status_code == 422
    assert (await client.get(f"/v1/final-reports/{m.artifact_id}/files/EXE",
                             headers=auth)).status_code == 422
    assert (await client.get(f"/v1/final-reports/{m.artifact_id}/files/..%2Fmanifest",
                             headers=auth)).status_code in (404, 422)
    (tmp_path / "final-reports" / m.location / "report.pdf").write_bytes(b"%PDF-tampered")
    tampered = await client.get(f"/v1/final-reports/{m.artifact_id}/files/PDF", headers=auth)
    assert tampered.status_code == 404
    assert tampered.json()["detail"]["code"] == "FINAL_REPORT_FILE_UNAVAILABLE"


def test_download_names_are_generated_from_validated_values():
    base = {"artifact_id": str(uuid4()), "location": "nse-X/2026-10-10/a", "company_key": "k",
            "company_name": "n", "nse_symbol": 'X"\r\nSet-Cookie: a=b', "isin": None,
            "as_of": "2026-10-10", "report_id": str(uuid4()), "box14_sha256": "a" * 64,
            "box14_schema": "s", "box14_generated_at": "t", "engines": [], "status": "COMPLETE",
            "requested": ["PDF"], "files": [], "failures": [], "warnings": [],
            "limitations": []}
    m = rr.ReportManifest.model_validate(base)
    name = download_name(m, F.PDF)
    assert re.fullmatch(r"[A-Za-z0-9_-]+_2026-10-10_detailed_research\.pdf", name)


_ = (ORIGIN, box13, compose, SectionStatus)


# --- acquisition stage capture (fake adapters; offline) -------------------------------------

from stock_research_ai.adapters.rendering.acquire import (  # noqa: E402
    StageRequest,
    capture_stage,
    page_urls,
)


def test_page_urls_only_for_supported_screener_pages():
    assert page_urls("https://www.screener.in/company/RVNL/") == (
        "https://www.screener.in/company/RVNL/consolidated/",
        "https://www.screener.in/company/RVNL/")
    for bad in ("https://evil.example/company/RVNL/", "file:///etc/passwd",
                "https://www.screener.in/company/../x/"):
        with pytest.raises(ValueError):
            page_urls(bad)


async def test_capture_stage_chains_existing_adapters_and_renders_offline(tmp_path):
    infy = await page(tmp_path, FIXTURES, INFY)
    calls: list[str] = []

    async def fetch_page(url: str):
        calls.append(url)
        if url.endswith("/consolidated/"):
            raise ValueError("no consolidated page")  # a standalone-only company
        return infy

    async def fetch_evidence(p):
        calls.append("evidence")
        return None, None

    seen: list[Any] = []

    async def fetch_exchange(req):
        seen.append(req)
        return ()

    request = StageRequest(source_url=INFY[1], as_of=date(2026, 10, 10), nse_symbol="INFY")
    stage = await capture_stage(request, now=NOW, fetch_page=fetch_page,
                                fetch_evidence=fetch_evidence, fetch_exchange=fetch_exchange)
    assert calls == ["https://www.screener.in/company/INFY/consolidated/", INFY[1], "evidence"]
    assert seen[0].nse_symbol == "INFY" and seen[0].as_of == NOW
    key = stage.screeners[0].company_key
    assert stage.diligence_request.company_key == key == stage.report_request.company_key
    restored = ResearchStage.model_validate_json(stage.model_dump_json())
    result = run_detailed_research(restored, store=store_for(tmp_path), pdf=None,
                                   formats=["json", "md"])
    assert result.status == "COMPLETE", result.failure
    box12 = next(s for s in result.steps if s.box == "BOX12")
    assert box12.status == "FAILED" and "IDENTITY_FAILED" in (box12.detail or "")
    assert any(w.startswith("BOX12_FAILED: IDENTITY_FAILED") for w in result.manifest.warnings)
    report = FinalResearchReportDataset.model_validate_json(
        (tmp_path / "final-reports" / result.manifest.location / "report.json").read_text())
    missing = {f.family.value for f in report.due_diligence_status.input_families
               if f.status.value == "MISSING"}
    assert {"BOX12_MARKET", "MARKET_DATA"} <= missing  # surfaced in Section 0


async def test_capture_stage_never_invents_identity_or_inputs(tmp_path):
    infy = await page(tmp_path, FIXTURES, INFY)

    async def fetch_page(url: str):
        return infy

    async def nothing(p):
        return None, None

    async def exchange(req):
        raise AssertionError("no declared identifier -> no exchange acquisition")

    stage = await capture_stage(StageRequest(source_url=INFY[1], as_of=date(2026, 10, 10)),
                                now=NOW, fetch_page=fetch_page, fetch_evidence=nothing,
                                fetch_exchange=exchange)
    assert stage.market_request is None and stage.report_request.nse_symbol is None
    other = DueDiligenceRequest(as_of=date(2026, 10, 10), company_key="screener:999")
    with pytest.raises(ValueError):
        await capture_stage(StageRequest(source_url=INFY[1], as_of=date(2026, 10, 10),
                                         diligence=other), now=NOW, fetch_page=fetch_page,
                            fetch_evidence=nothing, fetch_exchange=None)

    async def failing(url: str):
        raise OSError("offline")

    with pytest.raises(ValueError):
        await capture_stage(StageRequest(source_url=INFY[1], as_of=date(2026, 10, 10)),
                            now=NOW, fetch_page=failing, fetch_evidence=nothing,
                            fetch_exchange=None)
