"""P1-2: untrusted PDF/HTML/XLSX parsing runs in a killable child process with hard time
and memory bounds, and never on the asyncio event loop. Offline; synthetic local inputs;
child targets below are deliberately slow, crashing or memory-hungry."""

import asyncio
import io
import os
import subprocess
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter
from test_document_intelligence import make_pdf

from stock_research_ai.adapters.documents import isolation
from stock_research_ai.adapters.documents.document_parsers import (
    LocalDocumentParser,
    parse_document,
)
from stock_research_ai.adapters.documents.isolation import (
    MEMORY_ENFORCEMENT,
    IsolationFailure,
    IsolationLimits,
    run_isolated,
)
from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.bootstrap import ingest_document
from stock_research_ai.domain.document_intelligence import ExtractionLimits, ExtractionStatus
from stock_research_ai.domain.documents import DocumentRequest
from stock_research_ai.domain.evidence_retrieval import DetectedContentType

D = DetectedContentType
LIMITS = ExtractionLimits()
QUICK = IsolationLimits(seconds=20.0)


# --- child targets (module level: the child imports them by name) ----------------------


def echo(content: bytes, label: str) -> dict:
    return {"size": len(content), "label": label, "head": content[:4].decode()}


def sleep_forever(content: bytes) -> None:
    time.sleep(3600)


def crash(content: bytes) -> None:
    os._exit(13)


def fail(content: bytes) -> None:
    raise ValueError("/Users/someone/secret/path must never surface")


def hog_memory(content: bytes) -> None:
    blocks = []
    for _ in range(80):  # up to ~800 MB, touched so it is really resident
        blocks.append(bytearray(b"x" * 10_000_000))
        time.sleep(0.01)


def big_output(content: bytes) -> str:
    return "x" * 2_000_000


# --- the isolation primitive ------------------------------------------------------------


@pytest.fixture
def children(monkeypatch):
    """Record every child process started, to prove each one is reaped."""
    started: list[subprocess.Popen] = []
    original = subprocess.Popen

    def recording(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(isolation.subprocess, "Popen", recording)
    return started


def assert_reaped(children):
    assert children
    for process in children:
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):  # reaped: not even a zombie remains
            os.kill(process.pid, 0)


def test_successful_result_round_trips_as_json(children):
    result = run_isolated(echo, b"%PDF-data", "label", limits=QUICK)
    assert result.failure is None
    assert result.value == {"size": 9, "label": "label", "head": "%PDF"}
    assert children[0].args[:2] == [isolation.sys.executable, "-P"]  # argument list, no shell
    assert_reaped(children)


def test_timeout_kills_the_child_at_the_deadline(children):
    started = time.monotonic()
    result = run_isolated(sleep_forever, b"", limits=IsolationLimits(seconds=1.0))
    elapsed = time.monotonic() - started
    assert result.failure is IsolationFailure.TIMEOUT and result.value is None
    assert elapsed < 5.0
    assert_reaped(children)


def test_child_crash_is_a_typed_failure(children):
    result = run_isolated(crash, b"", limits=QUICK)
    assert result.failure is IsolationFailure.CRASHED
    assert_reaped(children)


def test_parser_exception_is_a_typed_code_without_text(children):
    result = run_isolated(fail, b"", limits=QUICK)
    assert result.failure is IsolationFailure.ERROR
    assert "secret" not in repr(result)
    assert_reaped(children)


@pytest.mark.skipif(MEMORY_ENFORCEMENT == "none", reason="no memory ceiling on this platform")
def test_memory_ceiling_stops_the_child(children):
    result = run_isolated(
        hog_memory, b"", limits=IsolationLimits(seconds=30.0, memory_bytes=200 * 1024 * 1024)
    )
    assert result.failure is IsolationFailure.MEMORY_LIMIT
    assert_reaped(children)


def test_output_is_capped(children):
    result = run_isolated(
        big_output, b"", limits=IsolationLimits(seconds=20.0, output_bytes=1_000_000)
    )
    assert result.failure is IsolationFailure.OUTPUT_TOO_LARGE
    assert_reaped(children)


async def test_event_loop_stays_responsive_while_a_slow_child_runs():
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        result = await asyncio.to_thread(
            run_isolated, sleep_forever, b"", limits=IsolationLimits(seconds=1.5)
        )
    finally:
        beat.cancel()
    assert result.failure is IsolationFailure.TIMEOUT
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 50 and max(gaps) < 0.25


# --- Box 3 parsing layer (LocalDocumentParser) ------------------------------------------


def encrypted_pdf() -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(make_pdf([["Secret page"]])))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content,detected,status",
    [
        (make_pdf([["Revenue grew 12% to Rs. 500 crore."], ["Page two."]]), D.PDF, "SUCCESS"),
        (b"%PDF-1.7 not really a pdf", D.PDF, "MALFORMED"),
        (encrypted_pdf(), D.PDF, "ENCRYPTED"),
        (b"<html><body><h1>Results</h1><p>Revenue rose.</p></body></html>", D.HTML, "SUCCESS"),
    ],
)
def test_isolated_parsing_matches_in_process_parsing(content, detected, status):
    isolated = LocalDocumentParser().parse(content, detected, LIMITS)
    assert isolated == parse_document(content, detected, LIMITS)
    assert isolated.status is ExtractionStatus(status)
    assert isolated == LocalDocumentParser().parse(content, detected, LIMITS)  # deterministic


def nested_html(depth: int) -> bytes:
    return b"<html><body>" + b"<div>" * depth + b"x" + b"</div>" * depth + b"</body></html>"


def test_deeply_nested_html_fails_typed_inside_the_child():
    # Deeper than the recursion limit: the tree walk raises inside the child only.
    document = LocalDocumentParser().parse(nested_html(5_000), D.HTML, LIMITS)
    assert document.status is ExtractionStatus.FAILED
    assert [code for code, _, _ in document.warnings] == ["PARSER_ERROR"]


@pytest.mark.skipif(MEMORY_ENFORCEMENT == "none", reason="no memory ceiling on this platform")
def test_memory_pathological_html_hits_the_child_ceiling():
    # 100k-deep nesting: every node's locator copies its parent's, so the tree needs
    # O(depth^2) memory (tens of GB). Only the child is stopped, at its 1 GiB ceiling.
    document = LocalDocumentParser().parse(nested_html(100_000), D.HTML, LIMITS)
    assert document.status is ExtractionStatus.TOO_LARGE
    assert [code for code, _, _ in document.warnings] == ["PARSER_MEMORY_LIMIT"]


def test_cpu_pathological_html_is_killed_at_the_hard_deadline():
    # 60k siblings: the tree builder's sibling indexing is quadratic (minutes of CPU).
    content = b"<html><body><div>" + b"<span>a</span>" * 60_000 + b"</div></body></html>"
    parser = LocalDocumentParser(grace_seconds=0.0)
    started = time.monotonic()
    document = parser.parse(content, D.HTML, ExtractionLimits(max_seconds=1.0))
    assert time.monotonic() - started < 6.0
    assert document.status is ExtractionStatus.FAILED
    assert [code for code, _, _ in document.warnings] == ["PARSER_TIMEOUT"]


def test_text_stays_in_process(monkeypatch):
    monkeypatch.setattr(
        "stock_research_ai.adapters.documents.document_parsers.run_isolated",
        lambda *a, **k: pytest.fail("plain text must not start a child"),
    )
    document = LocalDocumentParser().parse(b"plain text", D.TEXT, LIMITS)
    assert document.status is ExtractionStatus.SUCCESS


# --- runtime PDF ingestion (PDFNormalizer, engine.ingest_document) ------------------------


async def test_runtime_pdf_timeout_is_reported_not_raised(
    make_document_pdf, document_settings, document_company, tmp_path
):
    path = tmp_path / "file.pdf"
    data = make_document_pdf()
    path.write_bytes(data)
    request = DocumentRequest(
        company_id=document_company.id, url="https://example.com/doc.pdf", replay_path=path
    )
    original = await ingest_document(document_settings, request, tmp_path / "out")
    assert original.coverage.overall == "COMPLETE"
    result = PDFNormalizer(max_seconds=0.001).normalize(
        request, original.identity, original.retrieval, original.artifact, data
    )
    assert result.coverage.overall == "FAILED" and result.pages == ()
    (issue,) = result.coverage.issues
    assert issue.code == "PDF_PARSE_FAILED" and issue.message.startswith("PARSER_TIMEOUT")
    assert "/" not in issue.message


class _SlowNormalizer:
    """Wraps the real normalizer behind a deliberately slow isolated child."""

    def normalize(self, *args):
        run_isolated(sleep_forever, b"", limits=IsolationLimits(seconds=1.0))
        return PDFNormalizer().normalize(*args)


async def test_engine_document_ingestion_does_not_block_the_event_loop(
    make_document_pdf, document_settings, document_company, tmp_path, monkeypatch
):
    from stock_research_ai import bootstrap

    monkeypatch.setattr(bootstrap, "PDFNormalizer", _SlowNormalizer)
    path: Path = tmp_path / "file.pdf"
    path.write_bytes(make_document_pdf())
    request = DocumentRequest(
        company_id=document_company.id, url="https://example.com/doc.pdf", replay_path=path
    )
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        document = await ingest_document(document_settings, request, tmp_path / "out")
    finally:
        beat.cancel()
    assert document.coverage.overall == "COMPLETE"
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) > 50 and max(gaps) < 0.25
