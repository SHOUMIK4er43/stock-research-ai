import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.bootstrap import document_engine, ingest_document
from stock_research_ai.domain.documents import DocumentRequest, DocumentType, ParsedDocument

FIXTURES = Path(__file__).parent / "fixtures/documents"
GOLDEN = json.loads((FIXTURES / "expected_yesbank.json").read_text())


@pytest.mark.parametrize("filename", list(GOLDEN))
async def test_golden_document_entire_pipeline(
    filename, document_settings, document_company, tmp_path
):
    expected = GOLDEN[filename]
    original = (FIXTURES / filename).read_bytes()
    assert hashlib.sha256(original).hexdigest() == expected["sha256"]
    request = DocumentRequest(
        company_id=document_company.id,
        url=expected["url"],
        title=expected["title"],
        document_type=DocumentType(expected["type"]),
        replay_path=FIXTURES / filename,
    )
    output = tmp_path / "proof"
    document = await ingest_document(document_settings, request, output)
    assert document.document_type == expected["type"]
    assert document.classification_basis == "caller_declared"
    assert len(document.pages) == document.coverage.page_count == expected["page_count"]
    assert document.coverage.overall == expected["coverage"], document.coverage.issues
    assert document.coverage.pages_extracted == expected["page_count"]
    assert document.artifact.content_sha256 == expected["sha256"]
    assert document.retrieval.requested_url == document.retrieval.final_url == expected["url"]
    assert len(document.chunks) >= len(document.pages)
    for check in expected["pages"]:
        page = document.pages[check["number"] - 1]
        assert page.number == check["number"] and page.artifact_id == document.artifact.id
        assert page.version_id == document.version_id
        flattened = " ".join(page.text.split())
        for phrase in check["phrases"]:
            assert phrase in flattened
        chunks = [c for c in document.chunks if c.page_id == page.id]
        assert "".join(c.text for c in chunks) == page.text
        assert hashlib.sha256(page.text.encode()).hexdigest() == page.text_sha256
    if "section" in expected:
        check = expected["section"]
        page = document.pages[check["page"] - 1]
        assert any(
            s.page_id == page.id and s.label == check["label"] and s.heading == check["heading"]
            for s in document.sections
        )
    restored = ParsedDocument.model_validate_json((output / "normalized/document.json").read_text())
    assert restored == document
    store = FileRawArtifactStore(document_settings.artifact_path)
    assert await store.read(document.artifact) == original
    manifest = json.loads((output / "raw/manifest.json").read_text())
    assert (output / manifest["export_path"]).read_bytes() == original
    assert json.loads((output / "coverage.json").read_text()) == document.coverage.model_dump(
        mode="json"
    )
    async with document_engine(document_settings) as engine:
        assert await engine.get_document(document.version_id) == document
        hits = await engine.search_company_documents(document_company.id, expected["query"])
        assert hits
        for hit in hits:
            assert hit.document_id == document.identity.id and hit.version_id == document.version_id
            assert (
                hit.artifact_sha256 == expected["sha256"]
                and hit.retrieval_id == document.retrieval.id
            )
            chunk = next(c for c in document.chunks if c.id == hit.chunk_id)
            page = next(p for p in document.pages if p.id == hit.page_id)
            assert hit.page_number == page.number
            assert chunk.page_id == page.id and hit.text == chunk.text
            assert chunk.artifact_id == page.artifact_id == document.artifact.id
        assert await engine.search_company_documents(uuid4(), expected["query"]) == ()


async def test_document_versions_and_history(
    make_document_pdf, document_settings, document_company, tmp_path
):
    path = tmp_path / "fixture.pdf"
    path.write_bytes(make_document_pdf())
    request = DocumentRequest(
        company_id=document_company.id, url="https://example.com/report.pdf", replay_path=path
    )
    first = await ingest_document(document_settings, request, tmp_path / "first")
    second = await ingest_document(document_settings, request, tmp_path / "second")
    path.write_bytes(
        make_document_pdf(texts=("Changed evidence: capital allocation is described here.",))
    )
    third = await ingest_document(document_settings, request, tmp_path / "third")
    assert first.identity == second.identity == third.identity
    assert len({first.version_id, second.version_id, third.version_id}) == 3
    assert (
        first.artifact.content_reference
        == second.artifact.content_reference
        != third.artifact.content_reference
    )
    assert first.retrieval.id != second.retrieval.id
    assert first.pages[0].id != third.pages[0].id
    db = SQLiteDatabase(document_settings.database_path)
    try:
        assert await db.documents.document_history(first.identity.id) == (first, second, third)
        assert await db.documents.get_document(first.version_id) == first
        with pytest.raises(IntegrityError):
            await db.documents.save_document(first)
    finally:
        await db.close()
    assert (
        await FileRawArtifactStore(document_settings.artifact_path).read(first.artifact)
        != path.read_bytes()
    )


async def test_document_cli_acceptance_and_search(document_settings, document_company, tmp_path):
    expected = GOLDEN["yesbank_transcript.pdf"]
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def blocked(*a,**k): raise RuntimeError('offline CLI')\n"
        "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
    )
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(guard),
        "STOCK_RESEARCH_AI_ARTIFACT_PATH": str(document_settings.artifact_path),
    }
    base = [
        sys.executable,
        "-B",
        "-m",
        "stock_research_ai",
        "--database",
        str(document_settings.database_path),
    ]
    command = [
        *base,
        "ingest-document",
        "--company",
        str(document_company.id),
        "--url",
        expected["url"],
        "--type",
        "concall-transcript",
        "--file",
        str(FIXTURES / "yesbank_transcript.pdf"),
        "--output",
        str(tmp_path / "cli"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stderr
    assert "16 pages" in completed.stdout
    assert (tmp_path / "cli/raw/manifest.json").exists()
    assert {p.name for p in (tmp_path / "cli/normalized").iterdir()} == {
        "document.json",
        "pages.json",
        "sections.json",
        "chunks.json",
    }
    searched = subprocess.run(
        [
            *base,
            "search-documents",
            "--company",
            str(document_company.id),
            "--query",
            "INR 952 crores",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert searched.returncode == 0, searched.stderr
    hits = json.loads(searched.stdout)
    assert hits and hits[0]["page_number"] == 3
    repeated = subprocess.run(command, capture_output=True, text=True, env=env)
    assert repeated.returncode == 1 and "empty directory" in repeated.stderr


def test_public_document_fixture_hashes():
    for record in json.loads((FIXTURES / "manifest.json").read_text()):
        content = (FIXTURES / record["file"]).read_bytes()
        assert len(content) == record["bytes"]
        assert (
            hashlib.sha256(content).hexdigest()
            == record["sha256"]
            == GOLDEN[record["file"]]["sha256"]
        )
