"""Preserve public document links verbatim; never fetch document bodies."""

from datetime import datetime
from urllib.parse import urlsplit

from stock_research_ai.adapters.screener.html import Node
from stock_research_ai.adapters.screener.metrics import ParseContext
from stock_research_ai.domain.documents import DocumentReference, DocumentType
from stock_research_ai.domain.extraction import Section
from stock_research_ai.domain.provenance import ObservationProvenance


def parse_documents(
    node: Node, context: ParseContext, final_url: str
) -> tuple[DocumentReference, ...]:
    result: list[DocumentReference] = []
    heading = ""
    for item in node.all():
        if item.tag == "h3":
            heading = item.text.lower()
        if item.tag != "a" or "href" not in item.attrs:
            continue
        link = item.link(final_url, "DOCUMENTS")
        if urlsplit(link.resolved_url).scheme not in {"http", "https"}:
            continue
        if urlsplit(link.resolved_url).path.startswith(("/login/", "/register/")):
            context.issue(
                Section.DOCUMENTS,
                "SOURCE_UNAVAILABLE",
                "Document link requires login",
                item,
                item.text,
                error=True,
            )
            continue
        label = item.text.lower()
        kind = DocumentType.OTHER
        if "annual report" in label:
            kind = DocumentType.ANNUAL_REPORT
        elif "ppt" in label or "presentation" in label:
            kind = DocumentType.INVESTOR_PRESENTATION
        elif "transcript" in label:
            kind = DocumentType.CONCALL_TRANSCRIPT
        elif "credit" in heading or "rating" in heading:
            kind = DocumentType.CREDIT_RATING
        elif "announcement" in heading:
            kind = DocumentType.ANNOUNCEMENT
        times = item.all("time")
        publication = None
        if times and times[0].attrs.get("datetime"):
            try:
                publication = datetime.fromisoformat(times[0].attrs["datetime"])
                if publication.tzinfo is None:
                    raise ValueError("Publication timezone is unspecified")
            except ValueError:
                publication = None
                context.issue(
                    Section.DOCUMENTS,
                    "INVALID_PUBLICATION_DATE",
                    "Source date text retained without interpreting its timezone",
                    times[0],
                    value=times[0].attrs["datetime"],
                    error=True,
                )
        result.append(
            DocumentReference(
                title=item.text or item.attrs["href"],
                document_type=kind,
                link=link,
                source_date_text=times[0].text if times else None,
                publication_at=publication,
                provenance=ObservationProvenance(
                    raw_artifact_id=context.artifact.id,
                    source_location=item.location("DOCUMENTS"),
                    normalizer_version="1",
                ),
            )
        )
    return tuple(result)
