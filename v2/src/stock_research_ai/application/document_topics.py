"""Deterministic, versioned document-chunk selection for FULL_RESEARCH_V1.

No LLM retrieval decisions, no embeddings/vector infrastructure - the simplest existing
deterministic primitive this codebase already has (plain case-insensitive substring
match, exactly the algorithm application.sqlite's DocumentRepository.search already
uses) applied directly to the ParsedDocument objects THIS research run itself just
ingested. Operating on that already-in-hand, run-scoped set - rather than querying the
repository across a company's whole history - is what makes "closed evidence, no
prior-run leakage" true by construction rather than by an added filter.

A selected chunk is never promoted to a ManagementStatement or otherwise reinterpreted:
this module only ever produces (document_version_id, chunk_id) pairs, the exact shape
domain.analysis.AnalysisRequest.document_chunk_refs already accepts. Everything else
(citation validation, epistemic classification, management attribution) remains
entirely owned by the existing, frozen Part 5B validator.
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from stock_research_ai.domain.analysis import AnalysisSection
from stock_research_ai.domain.documents import DocumentType, ParsedDocument

TOPIC_REGISTRY_VERSION = "DOCUMENT_RESEARCH_TOPICS_V1"


class DocumentResearchTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic_id: str
    section: AnalysisSection
    allowed_document_types: tuple[DocumentType, ...]
    query_terms: tuple[str, ...]
    max_chunks: int
    per_document_cap: int


#: The smallest useful set: three topics, each mapped 1:1 onto a real, existing
#: AnalysisSection that is genuinely document-shaped. EXPECTATIONS/CATALYSTS are
#: deliberately excluded - selecting chunks for them via keyword search would look like
#: guidance extraction, which this milestone does not implement (see module docstring
#: and application/research_plan.py's management/guidance-safety notes).
DOCUMENT_RESEARCH_TOPICS_V1: tuple[DocumentResearchTopic, ...] = (
    DocumentResearchTopic(
        topic_id="BUSINESS_MODEL_V1",
        section="BUSINESS_MODEL",
        allowed_document_types=(DocumentType.ANNUAL_REPORT, DocumentType.INVESTOR_PRESENTATION),
        query_terms=("business model", "revenue model", "products and services"),
        max_chunks=4,
        per_document_cap=2,
    ),
    DocumentResearchTopic(
        topic_id="RISKS_V1",
        section="RISKS",
        allowed_document_types=(DocumentType.ANNUAL_REPORT,),
        query_terms=("risk factors", "key risks"),
        max_chunks=4,
        per_document_cap=2,
    ),
    DocumentResearchTopic(
        topic_id="MGMT_DOC_INTEL_V1",
        section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        allowed_document_types=(
            DocumentType.CONCALL_TRANSCRIPT,
            DocumentType.INVESTOR_PRESENTATION,
        ),
        query_terms=("management", "strategy"),
        max_chunks=6,
        per_document_cap=2,
    ),
)

#: Hard ceiling across every topic combined, independent of each topic's own max_chunks.
TOTAL_CHUNK_BUDGET_V1 = 14


class SelectedChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_version_id: UUID
    chunk_id: UUID
    document_type: DocumentType
    publication_date: date | None
    fiscal_label: str | None
    topic_id: str
    matched_term: str
    rank: int


def match_document_topics(
    documents: tuple[ParsedDocument, ...],
    topics: tuple[DocumentResearchTopic, ...] = DOCUMENT_RESEARCH_TOPICS_V1,
    total_chunk_budget: int = TOTAL_CHUNK_BUDGET_V1,
) -> tuple[SelectedChunk, ...]:
    """Deterministic and reproducible given the same `documents`: topics are scanned in
    registry order, documents in the order passed in, chunks in each document's own
    stored (contiguous, page-ordered) order - first match wins, ties broken by that same
    order. `documents` must be exactly the set ingested for this research run; nothing
    here reaches into a repository or any other run's evidence."""
    selected: list[SelectedChunk] = []
    seen: set[tuple[UUID, UUID]] = set()
    for topic in topics:
        topic_selected = 0
        for document in documents:
            if len(selected) >= total_chunk_budget or topic_selected >= topic.max_chunks:
                break
            if document.document_type not in topic.allowed_document_types:
                continue
            per_document_selected = 0
            for chunk in document.chunks:
                if len(selected) >= total_chunk_budget or topic_selected >= topic.max_chunks:
                    break
                if per_document_selected >= topic.per_document_cap:
                    break
                key = (document.version_id, chunk.id)
                if key in seen:
                    continue
                text = chunk.text.casefold()
                matched_term = next(
                    (term for term in topic.query_terms if term.casefold() in text), None
                )
                if matched_term is None:
                    continue
                selected.append(
                    SelectedChunk(
                        document_version_id=document.version_id,
                        chunk_id=chunk.id,
                        document_type=document.document_type,
                        publication_date=document.publication_date,
                        fiscal_label=document.fiscal_label,
                        topic_id=topic.topic_id,
                        matched_term=matched_term,
                        rank=len(selected),
                    )
                )
                seen.add(key)
                topic_selected += 1
                per_document_selected += 1
    return tuple(selected)
