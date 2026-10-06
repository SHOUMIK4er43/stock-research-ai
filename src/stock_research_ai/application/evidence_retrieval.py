"""DETAILED_RESEARCH_V1 Box 2: retrieve catalogued evidence under an explicit policy.

Selection is deterministic: tier, then document type, then catalogue order (newest first),
then per-type and total document caps. Retrieval runs with bounded concurrency and a hard
total byte budget (bytes are reserved before each fetch, so the budget is never exceeded;
with concurrency 1 the outcome is also fully deterministic). Fetched bytes are kept only
when they are the kind of document the type allows - an HTML login page where a PDF was
expected is INVALID_CONTENT and is never stored. Identical bytes are linked, not re-stored.
"""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from stock_research_ai.domain.evidence import (
    DocumentRetrievalStatus,
    EvidenceCatalogue,
    EvidenceDocumentType,
    ResearchDocument,
)
from stock_research_ai.domain.evidence_retrieval import (
    DetectedContentType,
    EvidenceRetrievalManifest,
    FetchResult,
    RetrievalOutcome,
    RetrievalPolicy,
    RetrievedEvidenceArtifact,
    StoredArtifact,
    TypeOutcomeCount,
)

T = EvidenceDocumentType
C = DetectedContentType
R = RetrievalOutcome

_PDF_ONLY = frozenset({C.PDF})
#: What a document of each type may legitimately be. PDF is always acceptable.
_ACCEPTED: dict[EvidenceDocumentType, frozenset[DetectedContentType]] = {
    T.CONCALL_TRANSCRIPT: frozenset({C.PDF, C.HTML, C.TEXT}),
    T.AGM_TRANSCRIPT: frozenset({C.PDF, C.HTML, C.TEXT}),
    T.CREDIT_RATING_REPORT: frozenset({C.PDF, C.HTML}),
    T.SHAREHOLDING_PATTERN: frozenset({C.PDF, C.HTML, C.SPREADSHEET}),
    T.OTHER_COMPANY_DOCUMENT: frozenset({C.PDF, C.HTML, C.TEXT, C.SPREADSHEET}),
    T.SCREENER_PAGE: frozenset({C.HTML}),
    T.SCREENER_EXPORT: frozenset({C.SPREADSHEET}),
}
_EXTENSIONS = {C.PDF: ".pdf", C.HTML: ".html", C.TEXT: ".txt"}
_FAILED = frozenset(
    {R.FAILED, R.TIMEOUT, R.NOT_FOUND, R.INVALID_CONTENT, R.TOO_LARGE, R.UNSUPPORTED}
)
_TYPE_ORDER = {kind: index for index, kind in enumerate(EvidenceDocumentType)}
_OUTCOME_ORDER = {kind: index for index, kind in enumerate(RetrievalOutcome)}


class EvidenceFetcher(Protocol):
    async def fetch(
        self, url: str, *, max_bytes: int, timeout_seconds: float, max_redirects: int
    ) -> FetchResult: ...


class ArtifactWriter(Protocol):
    async def store(
        self,
        content: bytes,
        *,
        extension: str,
        final_url: str,
        retrieved_at: datetime,
        declared_content_type: str | None,
    ) -> StoredArtifact: ...


def detect_content_type(content: bytes, declared: str | None) -> DetectedContentType:
    """Byte signatures first; the declared header only settles text-like content."""
    head = content[:1024]
    if head.startswith(b"%PDF-"):
        return C.PDF
    mime = (declared or "").split(";")[0].strip().lower()
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and "excel" in mime:
        return C.SPREADSHEET
    if head.startswith(b"PK\x03\x04") and "spreadsheetml" in mime:
        return C.SPREADSHEET
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or mime in (
        "text/html",
        "application/xhtml+xml",
    ):
        return C.HTML
    if mime == "text/plain":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return C.UNKNOWN
        return C.TEXT
    return C.UNKNOWN


def accepted_content(document: ResearchDocument) -> frozenset[DetectedContentType]:
    """A URL that names a PDF must return a PDF, whatever the document type allows."""
    if urlsplit(document.source_url).path.lower().endswith(".pdf"):
        return _PDF_ONLY
    return _ACCEPTED.get(document.document_type, _PDF_ONLY)


def _extension(detected: DetectedContentType, content: bytes) -> str:
    if detected == C.SPREADSHEET:
        return ".xlsx" if content.startswith(b"PK") else ".xls"
    return _EXTENSIONS[detected]


def select_documents(
    catalogue: EvidenceCatalogue, policy: RetrievalPolicy
) -> tuple[list[ResearchDocument], dict[UUID, str]]:
    """(selected in retrieval order, {document_id: reason} for everything not selected)."""
    skipped: dict[UUID, str] = {}
    eligible: list[tuple[int, int, int, ResearchDocument]] = []
    for position, document in enumerate(catalogue.documents):
        tier = policy.tiers.get(document.document_type, policy.max_tier + 1)
        if document.retrieval.status == DocumentRetrievalStatus.RETRIEVED:
            skipped[document.document_id] = "ALREADY_RETRIEVED"
        elif document.document_type == T.CONCALL_AUDIO:
            skipped[document.document_id] = "AUDIO_REFERENCE_ONLY"
        elif document.duplicate_of is not None:
            skipped[document.document_id] = "CATALOGUE_DUPLICATE"
        elif document.retrieval.status == DocumentRetrievalStatus.INELIGIBLE:
            skipped[document.document_id] = "INELIGIBLE_URL"
        elif tier > policy.max_tier:
            skipped[document.document_id] = "BELOW_POLICY_TIER"
        else:
            eligible.append((tier, _TYPE_ORDER[document.document_type], position, document))
    selected: list[ResearchDocument] = []
    per_type: Counter[EvidenceDocumentType] = Counter()
    for _tier, _type, _position, document in sorted(eligible, key=lambda e: e[:3]):
        limit = policy.per_type_limit.get(document.document_type)
        if limit is not None and per_type[document.document_type] >= limit:
            skipped[document.document_id] = "TYPE_LIMIT"
        elif len(selected) >= policy.max_documents:
            skipped[document.document_id] = "DOCUMENT_LIMIT"
        else:
            selected.append(document)
            per_type[document.document_type] += 1
    return selected, skipped


class _Budget:
    """Bytes are reserved before a fetch and settled after, so the cap always holds."""

    def __init__(self, total: int) -> None:
        self.remaining = total
        self.lock = asyncio.Lock()

    async def reserve(self, wanted: int) -> int:
        async with self.lock:
            granted = max(0, min(wanted, self.remaining))
            self.remaining -= granted
            return granted

    async def settle(self, reserved: int, used: int) -> None:
        async with self.lock:
            self.remaining += reserved - used


async def retrieve_evidence(
    catalogue: EvidenceCatalogue,
    fetcher: EvidenceFetcher,
    writer: ArtifactWriter,
    policy: RetrievalPolicy,
    *,
    run_id: UUID,
    clock: Callable[[], datetime],
) -> EvidenceRetrievalManifest:
    started_at = clock()
    selected, skipped = select_documents(catalogue, policy)
    budget = _Budget(policy.max_total_bytes)
    semaphore = asyncio.Semaphore(policy.concurrency)

    async def run(rank: int, document: ResearchDocument) -> RetrievedEvidenceArtifact:
        async with semaphore:
            return await _retrieve_one(
                document, rank, fetcher, writer, policy, budget, run_id, clock
            )

    retrieved = await asyncio.gather(*(run(rank, d) for rank, d in enumerate(selected, start=1)))
    records = list(retrieved) + [
        _not_selected(d, skipped[d.document_id], run_id)
        for d in catalogue.documents
        if d.document_id in skipped
    ]
    records = _link_duplicates(records)
    return _manifest(catalogue, policy, run_id, started_at, clock(), records)


async def _retrieve_one(
    document: ResearchDocument,
    rank: int,
    fetcher: EvidenceFetcher,
    writer: ArtifactWriter,
    policy: RetrievalPolicy,
    budget: _Budget,
    run_id: UUID,
    clock: Callable[[], datetime],
) -> RetrievedEvidenceArtifact:
    url = document.download_url or document.source_url
    base: dict[str, object] = {
        "document_id": document.document_id,
        "document_type": document.document_type,
        "retrieval_id": uuid5(run_id, str(document.document_id)),
        "selection_rank": rank,
        "requested_url": url,
    }
    allowance = await budget.reserve(policy.max_file_bytes)
    if allowance == 0:
        return _record(**base, outcome=R.NOT_ATTEMPTED, code="BYTE_BUDGET_EXHAUSTED")
    used = 0
    try:
        result = await _fetch_with_policy(fetcher, url, allowance, policy)
        retrieved_at = clock()
        transport: dict[str, object] = {
            "final_url": result.final_url,
            "redirects": result.redirects,
            "retrieved_at": retrieved_at,
            "http_status": result.http_status,
            "declared_content_type": result.declared_content_type,
        }
        if result.outcome != "OK" or result.content is None:
            code = result.code
            if result.outcome == "TOO_LARGE":
                code = (
                    "FILE_SIZE_LIMIT"
                    if allowance >= policy.max_file_bytes
                    else ("TOTAL_BYTE_BUDGET")
                )
            return _record(**base, **transport, outcome=R(result.outcome), code=code)
        content = result.content
        digest = hashlib.sha256(content).hexdigest()
        detected = detect_content_type(content, result.declared_content_type)
        received: dict[str, object] = {
            "detected_content_type": detected,
            "size_bytes": len(content),
            "sha256": digest,
        }
        if detected not in accepted_content(document):
            return _record(
                **base,
                **transport,
                **received,
                outcome=R.INVALID_CONTENT,
                code=f"UNEXPECTED_{detected.value}",
            )
        extension = _extension(detected, content)
        stored = await writer.store(
            content,
            extension=extension,
            final_url=result.final_url or url,
            retrieved_at=retrieved_at,
            declared_content_type=result.declared_content_type,
        )
        used = len(content)
        return _record(
            **base,
            **transport,
            **received,
            file_extension=extension,
            outcome=R.RETRIEVED,
            artifact=stored,
        )
    finally:
        await budget.settle(allowance, used)


async def _fetch_with_policy(
    fetcher: EvidenceFetcher, url: str, allowance: int, policy: RetrievalPolicy
) -> FetchResult:
    """At most `max_attempts` (default 1); only TIMEOUT/FAILED are ever retried."""
    for attempt in range(policy.max_attempts):
        result = await fetcher.fetch(
            url,
            max_bytes=allowance,
            timeout_seconds=policy.timeout_seconds,
            max_redirects=policy.max_redirects,
        )
        if result.outcome not in ("TIMEOUT", "FAILED") or attempt == policy.max_attempts - 1:
            return result
    raise AssertionError("unreachable")


def _record(**fields: object) -> RetrievedEvidenceArtifact:
    return RetrievedEvidenceArtifact.model_validate(fields)


def _not_selected(
    document: ResearchDocument, reason: str, run_id: UUID
) -> RetrievedEvidenceArtifact:
    base: dict[str, object] = {
        "document_id": document.document_id,
        "document_type": document.document_type,
        "retrieval_id": uuid5(run_id, str(document.document_id)),
        "selection_rank": None,
        "requested_url": document.download_url or document.source_url,
    }
    state = document.retrieval
    if reason == "ALREADY_RETRIEVED" and state.content_hash and state.local_artifact_reference:
        # Preserved earlier (e.g. the Screener page by extraction): reference, don't refetch.
        return _record(
            **base,
            retrieved_at=state.retrieved_at,
            declared_content_type=state.mime_type,
            detected_content_type=C.HTML if document.document_type == T.SCREENER_PAGE else None,
            size_bytes=state.file_size_bytes,
            sha256=state.content_hash,
            outcome=R.RETRIEVED,
            code="PRESERVED_BY_EXTRACTION",
            artifact=StoredArtifact(
                reference=state.local_artifact_reference,
                sha256=state.content_hash,
                size_bytes=state.file_size_bytes or 0,
            ),
        )
    detected = C.AUDIO_REFERENCE if reason == "AUDIO_REFERENCE_ONLY" else None
    return _record(**base, detected_content_type=detected, outcome=R.NOT_ATTEMPTED, code=reason)


def _link_duplicates(records: list[RetrievedEvidenceArtifact]) -> list[RetrievedEvidenceArtifact]:
    """The first record (selection order) with given bytes is canonical; later ones link."""
    first: dict[str, UUID] = {}
    linked: list[RetrievedEvidenceArtifact] = []
    for record in records:
        if record.outcome == R.RETRIEVED and record.sha256:
            canonical = first.setdefault(record.sha256, record.document_id)
            if canonical != record.document_id:
                record = record.model_copy(update={"duplicate_of": canonical})
        linked.append(record)
    return linked


def _manifest(
    catalogue: EvidenceCatalogue,
    policy: RetrievalPolicy,
    run_id: UUID,
    started_at: datetime,
    completed_at: datetime,
    records: Sequence[RetrievedEvidenceArtifact],
) -> EvidenceRetrievalManifest:
    fetched = [r for r in records if r.outcome == R.RETRIEVED and r.selection_rank is not None]
    counts = Counter((r.document_type, r.outcome) for r in records)
    return EvidenceRetrievalManifest(
        run_id=run_id,
        company_key=catalogue.company.company_key,
        catalogued_at=catalogue.catalogued_at,
        started_at=started_at,
        completed_at=completed_at,
        policy=policy,
        documents_discovered=len(catalogue.documents),
        documents_selected=sum(r.selection_rank is not None for r in records),
        documents_retrieved=sum(r.outcome == R.RETRIEVED for r in records),
        documents_failed=sum(r.outcome in _FAILED for r in records),
        documents_blocked=sum(r.outcome == R.BLOCKED for r in records),
        total_bytes_retrieved=sum(r.size_bytes or 0 for r in fetched),
        unique_content_hashes=len({r.sha256 for r in records if r.outcome == R.RETRIEVED}),
        duplicate_content_documents=sum(r.duplicate_of is not None for r in records),
        outcomes_by_type=tuple(
            TypeOutcomeCount(document_type=kind, outcome=outcome, count=count)
            for (kind, outcome), count in sorted(
                counts.items(), key=lambda i: (_TYPE_ORDER[i[0][0]], _OUTCOME_ORDER[i[0][1]])
            )
        ),
        artifacts=tuple(records),
    )


def canonical_manifest_json(manifest: EvidenceRetrievalManifest) -> str:
    return json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
