"""Narrow RBI release-table reader and issuer PDF publication reader."""

import hashlib
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid4, uuid5

from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.domain.documents import DocumentIdentity, DocumentRequest
from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    SourceLink,
    SourceLocation,
    SourceRetrieval,
)
from stock_research_ai.domain.publications import (
    PublicationCoverage,
    PublicationIdentity,
    PublicationPassage,
    PublicationRequest,
    PublicationSnapshot,
)

VERSION = "publication/1"


def passage(text: str, artifact: RawArtifact, locator: str) -> PublicationPassage:
    return PublicationPassage(
        text=text,
        provenance=ObservationProvenance(
            raw_artifact_id=artifact.id,
            source_location=SourceLocation(locator=locator),
            normalizer_version=VERSION,
        ),
    )


class RBIRelease(HTMLParser):
    """Only collects tableheader cells, paragraphs and links inside table.tablebg."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.headers: list[str] = []
        self.paragraphs: list[str] = []
        self.links: list[str] = []
        self.header: list[str] | None = None
        self.paragraph: list[str] | None = None
        self.closed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and (self.depth or a.get("class") == "tablebg"):
            self.depth += 1
        if not self.depth:
            return
        if tag == "td" and a.get("class") == "tableheader":
            self.header = []
        if tag == "p":
            self.paragraph = []
        href = a.get("href")
        if tag == "a" and href and not href.startswith("#"):
            self.links.append(href)

    def handle_data(self, data: str) -> None:
        if self.header is not None:
            self.header.append(data)
        if self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.header is not None:
            self.headers.append("".join(self.header).strip())
            self.header = None
        if tag == "p" and self.paragraph is not None:
            self.paragraphs.append("".join(self.paragraph).strip())
            self.paragraph = None
        if tag == "table" and self.depth:
            self.depth -= 1
            if not self.depth:
                self.closed = True


class PublicationNormalizer:
    def normalize(
        self,
        request: PublicationRequest,
        retrieval: SourceRetrieval,
        artifact: RawArtifact | None,
        content: bytes | None,
    ) -> PublicationSnapshot:
        publisher = "RBI" if request.format == "RBI_HTML" else "YES BANK"
        identity = PublicationIdentity(
            id=uuid5(NAMESPACE_URL, f"{publisher}:{request.source_key}"),
            publisher=publisher,
            source_key=request.source_key,
            source_url=request.source_url,
        )
        issues: list[str] = []
        passages: list[PublicationPassage] = []
        metadata: list[PublicationPassage] = []
        attachments: list[SourceLink] = []
        title = None
        published = None
        reference = None
        if (
            not artifact
            or content is None
            or retrieval.status != "success"
            or not artifact.complete
        ):
            issues.append("SOURCE_UNAVAILABLE")
        else:
            if hashlib.sha256(content).hexdigest() != artifact.content_sha256:
                raise ValueError("Raw evidence integrity failed")
            try:
                if request.format == "RBI_HTML":
                    reader = RBIRelease()
                    reader.feed(content.decode(retrieval.encoding))
                    reader.close()
                    if not reader.closed:
                        issues.append("MALFORMED_HTML")
                    for i, header in enumerate(reader.headers, 1):
                        if "Sectoral Deployment of Bank Credit" in header:
                            title = header
                            metadata.append(passage(header, artifact, f"table.tablebg/header[{i}]"))
                            match = re.search(r"([A-Z][a-z]+ \d{4})$", header)
                            reference = match.group(1) if match else None
                        if header.startswith("Date :"):
                            published = datetime.strptime(
                                header.split(":", 1)[1].strip(), "%b %d, %Y"
                            ).date()
                            metadata.append(passage(header, artifact, f"table.tablebg/header[{i}]"))
                    passages = [
                        passage(t, artifact, f"table.tablebg/p[{i}]")
                        for i, t in enumerate(reader.paragraphs, 1)
                        if t
                    ]
                    attachments = [
                        SourceLink(
                            original_href=h,
                            resolved_url=urljoin(retrieval.final_url, h),
                            source_location=SourceLocation(locator=f"table.tablebg/a[{i}]"),
                        )
                        for i, h in enumerate(reader.links, 1)
                    ]
                else:
                    document = PDFNormalizer().normalize(
                        DocumentRequest(url=request.url, subjects=request.subjects),
                        DocumentIdentity(
                            id=identity.id,
                            provider=retrieval.provider,
                            original_url=request.source_url,
                        ),
                        retrieval,
                        artifact,
                        content,
                    )
                    if document.coverage.overall != "COMPLETE":
                        issues.append("PDF_BODY_PARTIAL_OR_FAILED")
                    passages = [
                        passage(p.text, artifact, f"pdf:page[{p.number}]")
                        for p in document.pages
                        if p.text
                    ]
                    for p in passages:
                        if "YES BANK ANNOUNCES FINANCIAL RESULTS" in p.text:
                            title = "YES BANK ANNOUNCES FINANCIAL RESULTS"
                            metadata.append(p)
                            match = re.search(r"\b([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b", p.text)
                            if match:
                                published = datetime.strptime(
                                    " ".join(match.group(1).split()), "%B %d, %Y"
                                ).date()
                            break
            except (ValueError, TypeError, UnicodeError):
                issues.append("MALFORMED_PUBLICATION")
        if not title:
            issues.append("TITLE_MISSING")
        if not published:
            issues.append("PUBLICATION_DATE_UNKNOWN")
        if not passages:
            issues.append("BODY_MISSING")
        subject_state: Any = "ESTABLISHED" if request.subjects else "ABSENT"
        identities = [(s.kind, s.identifier) for s in request.subjects]
        if len(set(identities)) != len(identities):
            subject_state = "AMBIGUOUS"
        if subject_state != "ESTABLISHED":
            issues.append(f"SUBJECT_{subject_state}")
        return PublicationSnapshot(
            id=uuid4(),
            identity=identity,
            request=request,
            retrieved_at=retrieval.retrieved_at,
            title=title,
            publication_date=published,
            reference_period=reference,
            passages=tuple(passages),
            metadata_evidence=tuple(metadata),
            subjects=request.subjects,
            attachments=tuple(attachments),
            retrieval=retrieval,
            artifact=artifact,
            coverage=PublicationCoverage(
                overall="FAILED" if not passages else "PARTIAL" if issues else "COMPLETE",
                source_reachable=retrieval.status == "success",
                metadata_present=bool(title),
                body="MISSING"
                if not passages
                else "PARTIAL"
                if any(i.startswith(("PDF_", "MALFORMED")) for i in issues)
                else "AVAILABLE",
                attachments="LINKED_NOT_ACQUIRED" if attachments else "NOT_PRESENT",
                publication_date_known=published is not None,
                subject_linkage=subject_state,
                source_locator_available=bool(passages),
                issues=tuple(issues),
            ),
            normalizer_version=VERSION,
        )
