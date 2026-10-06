import pytest

from stock_research_ai.bootstrap import ingest_document
from stock_research_ai.domain.documents import DocumentRequest


@pytest.mark.parametrize(
    "kind,expected,code",
    [
        ("invalid", "FAILED", "PDF_PARSE_FAILED"),
        ("html", "FAILED", "PDF_PARSE_FAILED"),
        ("truncated", "FAILED", "PDF_PARSE_FAILED"),
        ("empty", "FAILED", "EMPTY_DOCUMENT"),
        ("blank", "FAILED", "EMPTY"),
        ("scanned", "FAILED", "OCR_REQUIRED"),
        ("mixed", "PARTIAL", "OCR_REQUIRED"),
        ("unsupported", "FAILED", "PDF_PARSE_FAILED"),
    ],
)
async def test_explicit_document_failures(
    kind, expected, code, make_document_pdf, document_settings, document_company, tmp_path
):
    data = {
        "invalid": b"%PDF-1.7\nnot valid\n%%EOF",
        "html": b"<html>Login required</html>",
        "truncated": make_document_pdf()[:-30],
        "empty": make_document_pdf(texts=()),
        "blank": make_document_pdf(texts=("",)),
        "scanned": make_document_pdf(texts=("",), scanned=True),
        "mixed": make_document_pdf(
            texts=("Extractable text with enough characters for this page.", ""), scanned=True
        ),
        "unsupported": b"PK\x03\x04 this is not a PDF",
    }[kind]
    path = tmp_path / "input.pdf"
    path.write_bytes(data)
    result = await ingest_document(
        document_settings,
        DocumentRequest(
            company_id=document_company.id, url="https://example.com/document.pdf", replay_path=path
        ),
        tmp_path / "output",
    )
    assert result.coverage.overall == expected
    assert code in {issue.code for issue in result.coverage.issues}
    if kind in {"scanned", "mixed"}:
        assert result.coverage.suspected_scanned_pages
    assert result.artifact is not None
    assert (
        document_settings.artifact_path / result.artifact.content_reference
    ).read_bytes() == data


async def test_metadata_missing_and_publication_not_inferred(
    make_document_pdf, document_settings, document_company, tmp_path
):
    path = tmp_path / "input.pdf"
    path.write_bytes(make_document_pdf(metadata=False))
    result = await ingest_document(
        document_settings,
        DocumentRequest(
            company_id=document_company.id,
            url="https://example.com/annual-report-2025.pdf",
            replay_path=path,
        ),
        tmp_path / "output",
    )
    assert result.coverage.overall == "COMPLETE"
    assert not result.coverage.metadata_available
    assert result.document_type == "other"  # URL wording is not a classification rule.
    assert result.publication_date is None and result.fiscal_label is None
    assert result.sections[0].label == "MANAGEMENT_REMARKS"
    assert result.pages[0].number == 1


async def test_page_chunks_are_lossless_stable_and_validated(
    make_document_pdf, document_settings, document_company, tmp_path
):
    from pydantic import ValidationError

    from stock_research_ai.adapters.documents.pdf import PDFNormalizer
    from stock_research_ai.domain.documents import ParsedDocument

    text = "Management remarks\n" + "Revenue 12345 and operating cash flow 67890. " * 100
    data = make_document_pdf(
        texts=(text, "Q&A\nThe second page contains a separate discussion and context.")
    )
    path = tmp_path / "input.pdf"
    path.write_bytes(data)
    request = DocumentRequest(
        company_id=document_company.id, url="https://example.com/doc.pdf", replay_path=path
    )
    result = await ingest_document(document_settings, request, tmp_path / "output")
    for page in result.pages:
        chunks = [c for c in result.chunks if c.page_id == page.id]
        assert "".join(c.text for c in chunks) == page.text
        assert all(c.character_count <= 1600 for c in chunks)
        assert all(c.page_number == page.number for c in chunks)
    repeated = PDFNormalizer().normalize(
        request, result.identity, result.retrieval, result.artifact, data
    )
    assert repeated.pages == result.pages and repeated.chunks == result.chunks
    payload = result.model_dump(mode="json")
    payload["chunks"][0]["text"] = "tampered"
    with pytest.raises(ValidationError, match="exact page slice"):
        ParsedDocument.model_validate(payload)
    payload = result.model_dump(mode="json")
    payload["pages"][0]["text_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="hash mismatch"):
        ParsedDocument.model_validate(payload)
