import httpx
import pytest

from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.bootstrap import ingest_document
from stock_research_ai.domain.documents import DocumentRequest


async def public_lookup(host):
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/doc.pdf",
        "http://example.com/doc.pdf",
        "https://localhost/doc.pdf",
        "https://127.0.0.1/doc.pdf",
        "https://10.0.0.1/doc.pdf",
        "https://user:password@example.com/doc.pdf",
    ],
)
async def test_invalid_document_urls_are_explicit_failures(url, document_company):
    source = DocumentSource(lookup=public_lookup)
    payload = await source.acquire(DocumentRequest(company_id=document_company.id, url=url))
    assert payload.retrieval.status == "failed"
    assert payload.content is None


async def test_private_dns_redirect_is_not_fetched(document_company):
    visited = []

    async def lookup(host):
        return ["127.0.0.1"] if host == "private.example" else ["93.184.216.34"]

    def handle(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example/document.pdf"})

    source = DocumentSource(httpx.MockTransport(handle), lookup)
    payload = await source.acquire(
        DocumentRequest(company_id=document_company.id, url="https://example.com/doc.pdf")
    )
    assert payload.retrieval.status == "failed" and len(visited) == 1


@pytest.mark.parametrize("mode", ["http_failure", "timeout", "oversize", "html"])
async def test_acquisition_failures_use_real_pipeline(
    mode, make_document_pdf, document_settings, document_company, tmp_path
):
    data = make_document_pdf()

    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "http_failure":
            return httpx.Response(403, content=b"Denied")
        if mode == "html":
            return httpx.Response(
                200, text="<html>Sign in</html>", headers={"content-type": "application/pdf"}
            )
        return httpx.Response(200, content=data, headers={"content-type": "application/pdf"})

    source = DocumentSource(
        httpx.MockTransport(handler), public_lookup, max_bytes=100 if mode == "oversize" else 100000
    )
    result = await ingest_document(
        document_settings,
        DocumentRequest(company_id=document_company.id, url="https://example.com/document.pdf"),
        tmp_path / "out",
        source,
    )
    assert result.coverage.overall == "FAILED"
    assert result.coverage.issues
    if mode == "oversize":
        assert not result.artifact.complete
        assert result.artifact.size_bytes == 101
        assert result.pages == ()
    if mode == "html":
        assert result.coverage.parser_status == "FAILED"


async def test_safe_redirect_and_exact_pdf_preservation(
    make_document_pdf, document_settings, document_company, tmp_path
):
    data = make_document_pdf()
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if len(visited) == 1:
            return httpx.Response(302, headers={"location": "/final.pdf"})
        return httpx.Response(
            200, content=data, headers={"content-type": "application/octet-stream"}
        )

    result = await ingest_document(
        document_settings,
        DocumentRequest(company_id=document_company.id, url="https://example.com/original"),
        tmp_path / "out",
        DocumentSource(httpx.MockTransport(handler), public_lookup),
    )
    assert result.coverage.overall == "COMPLETE"
    assert result.retrieval.final_url == "https://example.com/final.pdf"
    assert result.retrieval.requested_url == "https://example.com/original"
    assert (
        document_settings.artifact_path / result.artifact.content_reference
    ).read_bytes() == data
