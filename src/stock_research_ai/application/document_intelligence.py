"""DETAILED_RESEARCH_V1 Box 3: turn retrieved artifacts into one deterministic EvidenceBundle.

Each retrieved document is read back (hash-verified), parsed and analysed in isolation:
a failure becomes that document's FAILED/MALFORMED/... extraction and never stops the run.
Duplicate bytes, the Screener page (covered by the Box 1B dataset) and audio references
are skipped and counted. `verify_provenance` walks every evidence item back to the
catalogue document and its source URL.
"""

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from stock_research_ai.application.cancellation import to_thread_cancellable
from stock_research_ai.application.document_structure import (
    STRUCTURE_VERSION,
    DocumentContext,
    RawDocument,
    analyse_document,
)
from stock_research_ai.domain.document_intelligence import (
    CountByKey,
    DocumentExtraction,
    EvidenceBundle,
    ExtractionLimits,
    ExtractionStatistics,
    ExtractionStatus,
    ExtractionWarning,
)
from stock_research_ai.domain.evidence import EvidenceCatalogue, EvidenceDocumentType
from stock_research_ai.domain.evidence_retrieval import (
    DetectedContentType,
    EvidenceRetrievalManifest,
    RetrievalOutcome,
    RetrievedEvidenceArtifact,
)

_NS = UUID("6c1d4e0b-2f5a-4a9e-8b3d-7e2f1a0c9d55")
_SUCCESSFUL = {ExtractionStatus.SUCCESS}
_USABLE = {ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL}


class ArtifactReader(Protocol):
    async def read(self, reference: str, sha256: str, size_bytes: int) -> bytes: ...


class DocumentParser(Protocol):
    def parse(
        self, content: bytes, detected: DetectedContentType, limits: ExtractionLimits
    ) -> RawDocument: ...


def _failed(context: DocumentContext, code: str, status: ExtractionStatus) -> DocumentExtraction:
    return DocumentExtraction(
        extraction_id=uuid5(_NS, f"{context.document_id}|{context.artifact_sha256}|failed|{code}"),
        document_id=context.document_id,
        company_key=context.company_key,
        document_type=context.document_type,
        authority=context.authority,
        artifact_sha256=context.artifact_sha256,
        artifact_reference=context.artifact_reference,
        retrieval_id=context.retrieval_id,
        parser="none",
        parser_version=STRUCTURE_VERSION,
        extracted_at=context.extracted_at,
        format=None,
        status=status,
        page_count=None,
        warnings=(ExtractionWarning(code=code),),
        statistics=ExtractionStatistics(
            pages=0,
            characters=0,
            blocks=0,
            tables=0,
            cells=0,
            sections=0,
            evidence_items=0,
            pages_without_text=0,
        ),
    )


def _skip_reason(record: RetrievedEvidenceArtifact) -> str | None:
    if record.outcome != RetrievalOutcome.RETRIEVED or record.artifact is None:
        return "NOT_RETRIEVED"
    if record.document_type == EvidenceDocumentType.SCREENER_PAGE:
        return "SCREENER_PAGE_IN_DATASET"
    if record.document_type == EvidenceDocumentType.CONCALL_AUDIO:
        return "AUDIO_REFERENCE_ONLY"
    if record.duplicate_of is not None:
        return "DUPLICATE_CONTENT"
    if record.detected_content_type is None:
        return "UNKNOWN_CONTENT"
    return None


async def extract_evidence(
    catalogue: EvidenceCatalogue,
    manifest: EvidenceRetrievalManifest,
    reader: ArtifactReader,
    parser: DocumentParser,
    limits: ExtractionLimits,
    *,
    clock: Callable[[], datetime],
) -> EvidenceBundle:
    documents = {d.document_id: d for d in catalogue.documents}
    extracted_at = clock()
    skipped: Counter[str] = Counter()
    extractions: list[DocumentExtraction] = []
    for record in manifest.artifacts:
        reason = _skip_reason(record)
        if reason is not None:
            skipped[reason] += 1
            continue
        document = documents[record.document_id]
        artifact = record.artifact
        assert artifact is not None and record.detected_content_type is not None
        agency = document.metadata.get("rating_agency_label")
        context = DocumentContext(
            document_id=document.document_id,
            company_key=catalogue.company.company_key,
            document_type=document.document_type,
            authority=document.source_authority,
            artifact_sha256=artifact.sha256,
            artifact_reference=artifact.reference,
            retrieval_id=record.retrieval_id,
            extracted_at=extracted_at,
            tags=(f"agency:{agency.lower()}",) if agency else (),
        )
        try:
            content = await reader.read(artifact.reference, artifact.sha256, artifact.size_bytes)
        except (OSError, ValueError):
            extractions.append(
                _failed(context, "ARTIFACT_INTEGRITY_FAILED", ExtractionStatus.FAILED)
            )
            continue
        try:
            raw = await to_thread_cancellable(
                parser.parse, content, record.detected_content_type, limits
            )
            extractions.append(await asyncio.to_thread(analyse_document, raw, context, limits))
        except Exception:  # isolate one pathological document from the run
            extractions.append(_failed(context, "PARSER_ERROR", ExtractionStatus.FAILED))
    return _bundle(catalogue, manifest, limits, extracted_at, extractions, skipped)


def _counts(counter: Counter[str]) -> tuple[CountByKey, ...]:
    return tuple(CountByKey(key=k, count=v) for k, v in sorted(counter.items()) if v > 0)


def _bundle(
    catalogue: EvidenceCatalogue,
    manifest: EvidenceRetrievalManifest,
    limits: ExtractionLimits,
    extracted_at: datetime,
    extractions: list[DocumentExtraction],
    skipped: Counter[str],
) -> EvidenceBundle:
    versions = sorted({e.parser_version for e in extractions})
    evidence = [item for e in extractions for item in e.evidence]
    return EvidenceBundle(
        bundle_id=uuid5(
            _NS,
            "|".join(
                [
                    catalogue.company.company_key,
                    str(manifest.run_id),
                    *versions,
                    limits.model_dump_json(),
                ]
                + sorted(f"{e.document_id}:{e.artifact_sha256}" for e in extractions)
            ),
        ),
        company_key=catalogue.company.company_key,
        retrieval_run_id=manifest.run_id,
        extracted_at=extracted_at,
        parser_version=";".join(versions) or STRUCTURE_VERSION,
        limits=limits,
        documents_attempted=len(extractions),
        documents_extracted=sum(e.status in _SUCCESSFUL for e in extractions),
        documents_partial=sum(e.status == ExtractionStatus.PARTIAL for e in extractions),
        documents_failed=sum(e.status not in _USABLE for e in extractions),
        documents_skipped=_counts(skipped),
        total_pages=sum(e.statistics.pages for e in extractions),
        total_characters=sum(e.statistics.characters for e in extractions),
        total_tables=sum(e.statistics.tables for e in extractions),
        total_evidence_items=len(evidence),
        by_document_type=_counts(Counter(e.document_type.value for e in extractions)),
        by_evidence_kind=_counts(Counter(item.kind.value for item in evidence)),
        warnings=_counts(Counter(w.code for e in extractions for w in e.warnings)),
        extractions=tuple(extractions),
    )


def canonical_bundle_json(bundle: EvidenceBundle) -> str:
    return json.dumps(
        bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def verify_provenance(
    bundle: EvidenceBundle,
    manifest: EvidenceRetrievalManifest,
    catalogue: EvidenceCatalogue,
) -> list[str]:
    """Every evidence item must walk back: extraction -> retrieval record -> catalogue
    document -> source URL and discovery; tables and quotes must carry pages and blocks."""
    problems: list[str] = []
    records = {r.document_id: r for r in manifest.artifacts}
    documents = {d.document_id: d for d in catalogue.documents}
    for extraction in bundle.extractions:
        record = records.get(extraction.document_id)
        document = documents.get(extraction.document_id)
        if record is None or document is None:
            problems.append(f"orphan extraction {extraction.document_id}")
            continue
        if (
            record.sha256 != extraction.artifact_sha256
            or record.retrieval_id != extraction.retrieval_id
        ):
            problems.append(f"extraction {extraction.document_id} does not match its retrieval")
        if not document.source_url or not document.discovered_from:
            problems.append(f"document {document.document_id} lacks source provenance")
        pages = {p.page_number for p in extraction.pages}
        blocks = {b.block_id for b in extraction.blocks}
        tables = {t.table_id: t for t in extraction.tables}
        for item in extraction.evidence:
            if item.document_id != document.document_id or item.company_key != bundle.company_key:
                problems.append(f"evidence {item.evidence_id} is not owned by its document")
            if not {item.page_start, item.page_end} <= pages:
                problems.append(f"evidence {item.evidence_id} has no page provenance")
            if item.table_id is not None and item.table_id not in tables:
                problems.append(f"evidence {item.evidence_id} references a missing table")
            if item.table_id is None and not (item.block_ids and set(item.block_ids) <= blocks):
                problems.append(f"evidence {item.evidence_id} lacks block provenance")
    return problems
