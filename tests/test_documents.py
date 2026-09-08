from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.bootstrap import document_engine, ingest_document
from stock_research_ai.domain.documents import DocumentReference, DocumentRequest, ParsedDocument


async def test_known_reference_keeps_discovery_provenance(
    make_document_pdf, document_settings, document_company, tmp_path
):
    reference = DocumentReference.model_validate(
        {
            "title": "Investor presentation",
            "document_type": "investor_presentation",
            "link": {
                "original_href": "../presentation.pdf",
                "resolved_url": "https://example.com/presentation.pdf",
                "source_location": {"locator": "#documents/a[1]"},
            },
            "provenance": {
                "raw_artifact_id": str(uuid4()),
                "source_location": {"locator": "#documents/a[1]"},
                "normalizer_version": "1",
            },
        }
    )
    path = tmp_path / "file.pdf"
    path.write_bytes(make_document_pdf())
    request = DocumentRequest(
        company_id=document_company.id,
        url=reference.link.resolved_url,
        reference=reference,
        replay_path=path,
    )
    document = await ingest_document(document_settings, request, tmp_path / "out")
    assert document.source_reference == reference
    assert document.document_type == "investor_presentation"
    assert document.classification_basis == "source_reference"
    with pytest.raises(ValidationError, match="must match"):
        DocumentRequest(
            company_id=document_company.id, url="https://example.com/other.pdf", reference=reference
        )
    payload = document.model_dump(mode="json")
    payload["pages"][0]["artifact_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="Page provenance"):
        ParsedDocument.model_validate(payload)
    payload = document.model_dump(mode="json")
    payload["chunks"][0]["page_number"] = 100
    with pytest.raises(ValidationError, match="Chunk provenance"):
        ParsedDocument.model_validate(payload)


async def test_unknown_company_fails_before_acquisition(document_settings, tmp_path):
    class NeverAcquire:
        async def acquire(self, request):
            raise AssertionError("No acquisition authorized for unknown company")

    with pytest.raises(ValueError, match="Register the company"):
        await ingest_document(
            document_settings,
            DocumentRequest(company_id=uuid4(), url="https://example.com/doc.pdf"),
            tmp_path / "out",
            NeverAcquire(),
        )


async def test_search_validation_and_absent_version(document_settings, document_company):
    async with document_engine(document_settings) as engine:
        assert await engine.get_document(uuid4()) is None
        with pytest.raises(ValueError):
            await engine.search_company_documents(document_company.id, "  ")
        with pytest.raises(ValueError):
            await engine.search_company_documents(document_company.id, "query", 101)


async def test_pdf_resource_limit_is_explicit(
    make_document_pdf, document_settings, document_company, tmp_path
):
    from stock_research_ai.adapters.documents.pdf import PDFNormalizer

    path = tmp_path / "file.pdf"
    data = make_document_pdf()
    path.write_bytes(data)
    request = DocumentRequest(
        company_id=document_company.id, url="https://example.com/doc.pdf", replay_path=path
    )
    original = await ingest_document(document_settings, request, tmp_path / "out")
    result = PDFNormalizer(max_stream_bytes=1).normalize(
        request, original.identity, original.retrieval, original.artifact, data
    )
    assert result.coverage.overall == "FAILED"
    assert result.coverage.pages_failed == 1
    assert result.pages[0].status == "FAILED"
    assert "extraction limit" in result.coverage.issues[0].message
