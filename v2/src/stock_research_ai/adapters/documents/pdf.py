"""pypdf text is derived evidence, never a replacement for original PDF bytes."""

import hashlib
import logging
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid5

import pypdf
from pypdf import PdfReader

from stock_research_ai.adapters.documents.chunking import chunk_pages
from stock_research_ai.domain.documents import (
    DocumentCoverage,
    DocumentExtractionIssue,
    DocumentIdentity,
    DocumentPage,
    DocumentRequest,
    DocumentType,
    ParsedDocument,
)
from stock_research_ai.domain.provenance import RawArtifact, RetrievalStatus, SourceRetrieval

PARSER_VERSION = f"pypdf/{pypdf.__version__};document-text/1;chunks/1"


class PDFNormalizer:
    def __init__(self, max_pages: int = 1500, max_stream_bytes: int = 25_000_000) -> None:
        self.max_pages, self.max_stream_bytes = max_pages, max_stream_bytes

    def normalize(
        self,
        request: DocumentRequest,
        identity: DocumentIdentity,
        retrieval: SourceRetrieval,
        artifact: RawArtifact | None,
        content: bytes | None,
    ) -> ParsedDocument:
        version = uuid5(
            artifact.id if artifact else retrieval.id, f"{identity.id}:{PARSER_VERSION}"
        )
        issues: list[DocumentExtractionIssue] = []
        pages: list[DocumentPage] = []
        metadata: tuple[tuple[str, str], ...] = ()
        attempted = False
        source_page_count: int | None = None
        if retrieval.status != RetrievalStatus.SUCCESS or not artifact or content is None:
            issues.append(
                DocumentExtractionIssue(
                    code="ACQUISITION_FAILED", message=retrieval.error or "No bytes"
                )
            )
        elif not artifact.complete:
            issues.append(
                DocumentExtractionIssue(
                    code="INCOMPLETE_ARTIFACT", message="Complete original file unavailable"
                )
            )
        elif hashlib.sha256(content).hexdigest() != artifact.content_sha256:
            raise ValueError("Parser bytes do not match original artifact")
        else:
            attempted = True
            try:
                if not content.startswith(b"%PDF-"):
                    raise ValueError("HTML_OR_UNSUPPORTED_CONTENT: response is not a PDF")
                if b"%%EOF" not in content[-2048:]:
                    raise ValueError("TRUNCATED_PDF: missing final PDF marker")
                reader = PdfReader(BytesIO(content), strict=True)
                if reader.is_encrypted:
                    raise ValueError("ENCRYPTED_PDF: decryption is not supported")
                count = len(reader.pages)
                source_page_count = count
                if count > self.max_pages:
                    raise ValueError("PAGE_LIMIT: PDF exceeds the configured page limit")
                if reader.metadata:
                    metadata = tuple(sorted((str(k), str(v)) for k, v in reader.metadata.items()))
                if not count:
                    issues.append(
                        DocumentExtractionIssue(code="EMPTY_DOCUMENT", message="PDF has no pages")
                    )
                for index, page in enumerate(reader.pages, 1):
                    text = ""
                    status = "FAILED"
                    page_issues: list[DocumentExtractionIssue] = []
                    try:
                        stream = page.get_contents()
                        if stream is not None and len(stream.get_data()) > self.max_stream_bytes:
                            raise ValueError("Page content stream exceeds extraction limit")
                        text = page.extract_text() or ""
                        # Inspect resource types without decoding images.
                        resources = page.get("/Resources")
                        resources = resources.get_object() if resources else {}
                        objects = resources.get("/XObject", {})
                        objects = (
                            objects.get_object() if hasattr(objects, "get_object") else objects
                        )
                        images = any(
                            o.get_object().get("/Subtype") == "/Image" for o in objects.values()
                        )
                        visible = sum(c.isalnum() for c in text)
                        if visible == 0:
                            status = "OCR_REQUIRED" if images else "EMPTY"
                        elif visible < 20:
                            status = "OCR_REQUIRED" if images else "LOW_TEXT"
                        else:
                            status = "EXTRACTED"
                        if status != "EXTRACTED":
                            page_issues.append(
                                DocumentExtractionIssue(
                                    code=status,
                                    message="Insufficient extractable text; no OCR was run",
                                    page_number=index,
                                )
                            )
                    except Exception as exc:
                        # A malformed individual page must not erase evidence from other pages.
                        logging.getLogger(__name__).debug("Page extraction failed", exc_info=True)
                        page_issues.append(
                            DocumentExtractionIssue(
                                code="PAGE_PARSE_FAILED",
                                message=f"{type(exc).__name__}: {exc}",
                                page_number=index,
                            )
                        )
                    pages.append(
                        DocumentPage.model_validate(
                            {
                                "id": uuid5(version, f"page:{index}"),
                                "version_id": version,
                                "artifact_id": artifact.id,
                                "number": index,
                                "text": text,
                                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                "parser_version": PARSER_VERSION,
                                "status": status,
                                "issues": tuple(page_issues),
                            }
                        )
                    )
                    issues.extend(page_issues)
            except Exception as exc:
                issues.append(
                    DocumentExtractionIssue(
                        code="PDF_PARSE_FAILED", message=f"{type(exc).__name__}: {exc}"
                    )
                )
        sections, chunks = chunk_pages(tuple(pages))
        extracted = sum(p.status == "EXTRACTED" for p in pages)
        overall = "FAILED" if not extracted else "PARTIAL" if issues else "COMPLETE"
        known = sum(s.label != "UNKNOWN" for s in sections)
        declared_type = request.document_type
        basis = "caller_declared" if declared_type != DocumentType.OTHER else "unknown"
        if request.reference and declared_type == DocumentType.OTHER:
            declared_type = request.reference.document_type
            basis = "source_reference"
        if declared_type == DocumentType.OTHER:
            headings = {line.strip().casefold() for p in pages[:3] for line in p.text.splitlines()}
            for phrase, kind in {
                "investor presentation": DocumentType.INVESTOR_PRESENTATION,
                "earnings presentation": DocumentType.EARNINGS_PRESENTATION,
                "annual report": DocumentType.ANNUAL_REPORT,
                "earnings conference call transcript": DocumentType.CONCALL_TRANSCRIPT,
            }.items():
                if phrase in headings:
                    declared_type, basis = kind, "explicit_heading"
                    break
        title = request.title or (request.reference.title if request.reference else None)
        title = title or dict(metadata).get("/Title") or "Untitled document"
        return ParsedDocument.model_validate(
            {
                "identity": identity,
                "subjects": request.subjects,
                "version_id": version,
                "created_at": datetime.now(UTC),
                "title": title,
                "document_type": declared_type,
                "classification_basis": basis,
                "publication_date": request.publication_date,
                "fiscal_label": request.fiscal_label,
                "source_reference": request.reference,
                "retrieval": retrieval,
                "artifact": artifact,
                "parser_version": PARSER_VERSION,
                "metadata": metadata,
                "pages": tuple(pages),
                "sections": sections,
                "chunks": chunks,
                "coverage": DocumentCoverage.model_validate(
                    {
                        "overall": overall,
                        "acquisition_status": retrieval.status,
                        "parser_status": "NOT_ATTEMPTED"
                        if not attempted
                        else "FAILED"
                        if not extracted
                        else "PARTIAL"
                        if issues
                        else "SUCCESS",
                        "page_count": len(pages),
                        "source_page_count": source_page_count,
                        "pages_extracted": extracted,
                        "pages_empty": sum(p.status == "EMPTY" for p in pages),
                        "pages_failed": sum(p.status == "FAILED" for p in pages),
                        "pages_low_text": sum(p.status == "LOW_TEXT" for p in pages),
                        "suspected_scanned_pages": tuple(
                            p.number for p in pages if p.status == "OCR_REQUIRED"
                        ),
                        "characters_extracted": sum(len(p.text) for p in pages),
                        "metadata_available": bool(metadata),
                        "section_detection": "UNKNOWN"
                        if not known
                        else "COMPLETE"
                        if known == len(sections)
                        else "PARTIAL",
                        "issues": tuple(issues),
                    }
                ),
            }
        )
