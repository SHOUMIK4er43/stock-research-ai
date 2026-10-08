"""P1-2: untrusted PDF/HTML/XLSX parsing runs in a killable child process with hard time
and memory bounds, and never on the asyncio event loop. Offline; synthetic local inputs;
child targets below are deliberately slow, crashing or memory-hungry."""

import asyncio
import io
import json
import os
import subprocess
import sys
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
from stock_research_ai.application.cancellation import to_thread_cancellable
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


@pytest.mark.parametrize("depth", [5_000, 100_000])
def test_deeply_nested_html_is_rejected_by_the_structure_bound(depth):
    # Formerly a RecursionError (5k) or O(depth^2) locator memory (100k) inside the
    # child; the shared tree now refuses nesting past its depth cap, quickly.
    started = time.monotonic()
    document = LocalDocumentParser().parse(nested_html(depth), D.HTML, LIMITS)
    assert time.monotonic() - started < 10.0
    assert document.status is ExtractionStatus.TOO_LARGE
    assert [code for code, _, _ in document.warnings] == ["HTML_STRUCTURE_LIMIT_REACHED"]


def test_many_sibling_html_parses_quickly():
    # 60k siblings: formerly quadratic sibling indexing (minutes of CPU, killed at the
    # hard deadline); now linear, well inside the parser's own time limit.
    content = b"<html><body><div>" + b"<span>a</span>" * 60_000 + b"</div></body></html>"
    parser = LocalDocumentParser(grace_seconds=0.0)
    started = time.monotonic()
    document = parser.parse(content, D.HTML, ExtractionLimits(max_seconds=10.0))
    assert time.monotonic() - started < 10.0
    assert document.status is ExtractionStatus.SUCCESS


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


# --- cancellation (R4) ---------------------------------------------------------------------


def alive(process: subprocess.Popen) -> bool:
    try:
        os.kill(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_cancelling_the_caller_kills_and_reaps_the_child(children):
    ticks: list[float] = []

    async def heartbeat():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    task = asyncio.create_task(
        to_thread_cancellable(
            run_isolated, sleep_forever, b"", limits=IsolationLimits(seconds=60.0)
        )
    )
    try:
        while not children:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        assert alive(children[0])
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2.0  # not the 60 s deadline
    finally:
        beat.cancel()
    assert_reaped(children)  # killed and reaped: no process, no zombie
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.25


async def test_a_cancelled_scope_starts_no_further_children(children):
    started_children = []

    def two_parses() -> list:
        first = run_isolated(sleep_forever, b"", limits=IsolationLimits(seconds=60.0))
        started_children.append(len(children))
        second = run_isolated(echo, b"", "x", limits=QUICK)
        return [first.failure, second.failure]

    task = asyncio.create_task(to_thread_cancellable(two_parses))
    while not children:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started_children == [1] and len(children) == 1
    assert_reaped(children)


async def test_cancellable_thread_returns_and_raises_like_to_thread():
    assert await to_thread_cancellable(lambda a, b=0: a + b, 1, b=2) == 3
    with pytest.raises(ValueError, match="boom"):
        await to_thread_cancellable(lambda: (_ for _ in ()).throw(ValueError("boom")))
    result = await to_thread_cancellable(run_isolated, echo, b"%PDF", "y", limits=QUICK)
    assert result.failure is None and result.value["label"] == "y"


# --- memory-enforcement reporting (R5) -------------------------------------------------------


def test_platform_constant_never_claims_a_kernel_limit():
    assert MEMORY_ENFORCEMENT in {"watchdog", "none"}


def test_reported_enforcement_is_what_this_child_actually_had(children):
    result = run_isolated(echo, b"%PDF", "z", limits=QUICK)
    expected = "rlimit" if sys.platform.startswith("linux") else MEMORY_ENFORCEMENT
    assert result.failure is None and result.memory_enforcement == expected


def test_rlimit_is_reported_only_when_setrlimit_succeeds(monkeypatch):
    import resource

    monkeypatch.setattr(isolation.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda *a: calls.append(a))
    assert isolation._install_rlimit(123) is True
    assert calls == [(resource.RLIMIT_AS, (123, 123))]

    def refuse(*args):
        raise OSError("not permitted")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    assert isolation._install_rlimit(123) is False
    monkeypatch.setattr(resource, "setrlimit", lambda *a: (_ for _ in ()).throw(ValueError()))
    assert isolation._install_rlimit(123) is False
    monkeypatch.setattr(isolation.sys, "platform", "darwin")
    assert isolation._install_rlimit(123) is False


def _child_reply(monkeypatch, installed: bool) -> bytes:
    """Run the child entry point in-process with a stubbed rlimit outcome."""
    header = json.dumps(
        {
            "target": f"{echo.__module__}:echo",
            "args": ["w"],
            "memory_bytes": 1,
            "path": list(sys.path),
        }
    ).encode()
    frame = isolation._LENGTH.pack
    request = frame(len(header)) + header + frame(4) + b"%PDF"
    stdin, stdout = io.TextIOWrapper(io.BytesIO(request)), io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(isolation.sys, "stdin", stdin)
    monkeypatch.setattr(isolation.sys, "stdout", stdout)
    monkeypatch.setattr(isolation.sys, "path", list(sys.path))
    monkeypatch.setattr(isolation, "_install_rlimit", lambda limit: installed)
    isolation._child_main()
    return stdout.buffer.getvalue()


def test_child_reports_a_failed_setrlimit(monkeypatch):
    failed = _child_reply(monkeypatch, installed=False)
    assert failed.startswith(b"-OK")  # never "R" when the limit was not installed
    installed = _child_reply(monkeypatch, installed=True)
    assert installed.startswith(b"ROK")
