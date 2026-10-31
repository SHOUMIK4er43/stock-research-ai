"""DETAILED_RESEARCH_V1 Box 3: turn retrieved artifacts into one deterministic EvidenceBundle.

Each retrieved document is read back (hash-verified), parsed and analysed in isolation:
a failure becomes that document's FAILED/MALFORMED/... extraction and never stops the run.
Duplicate bytes, the Screener page (covered by the Box 1B dataset) and audio references
are skipped and counted. `verify_provenance` walks every evidence item back to the
catalogue document and its source URL.
"""

import asyncio
import json
import re
import unicodedata
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
    IssuerBindingMethod,
    IssuerBindingStatus,
)
from stock_research_ai.domain.evidence import (
    EvidenceCatalogue,
    EvidenceCompany,
    EvidenceDocumentType,
)
from stock_research_ai.domain.evidence_retrieval import (
    DetectedContentType,
    EvidenceRetrievalManifest,
    RetrievalOutcome,
    RetrievedEvidenceArtifact,
)

_NS = UUID("6c1d4e0b-2f5a-4a9e-8b3d-7e2f1a0c9d55")
_SUCCESSFUL = {ExtractionStatus.SUCCESS}
_USABLE = {ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL}
_NSE_HEADER = re.compile(
    r"\bNSE\s*(?:SYMBOL|CODE)\s*[:\-]\s*([A-Z0-9][A-Z0-9.&_-]{0,23})"
    r"|\bNSE\s*:\s*([A-Z0-9][A-Z0-9.&_-]{0,23})",
    re.I,
)
# a scrip code ends at its digits; '2026/08_06' after 'NSE&BSE:' is a letter's reference number
_BSE_HEADER = re.compile(
    r"\bBSE\s*(?:SCRIP\s*)?(?:CODE)?\s*[:\-]\s*(\d{4,8})(?![\d/_]|[.\-]\d)", re.I
)
_ISIN_HEADER = re.compile(r"\bISIN\s*[:\-]\s*(INE[A-Z0-9]{9})\b", re.I)


def _issuer_name_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    aliases = {"ltd": "limited", "pvt": "private"}
    return tuple(aliases.get(token, token) for token in tokens)


def _contains_name(text: str, name: str) -> bool:
    expected = _issuer_name_tokens(name)
    if not expected:
        return False
    title_terms = {
        "annual", "report", "financial", "result", "results", "quarter", "quarterly",
        "ended", "year", "investor", "presentation", "earnings", "conference", "call",
        "business", "responsibility", "sustainability", "corporate", "governance", "notice",
        "statement", "of", "the", "for", "as", "at", "and", "bank", "limited",
    }

    def title_context(tokens: tuple[str, ...]) -> bool:
        return all(
            token in title_terms
            or token.isdigit()
            or re.fullmatch(r"(?:q[1-4]|fy)\d{0,4}", token) is not None
            for token in tokens
        )

    # A name mention in prose is not issuer evidence. Accept an exact standalone name
    # line or an exact name attached only to recognizable document-title/date words.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        context = " ".join(lines[max(0, index - 3):index + 1]).casefold()
        if any(term in context for term in (
            "peer", "competitor", "comparable", "benchmark", "peer group",
        )):
            continue
        observed = _issuer_name_tokens(line)
        if not observed or len(observed) < len(expected):
            continue
        for start in range(len(observed) - len(expected) + 1):
            if observed[start:start + len(expected)] != expected:
                continue
            if title_context(observed[:start]) and title_context(observed[start + len(expected):]):
                return True
    return False


def _declared_issuer_names(text: str) -> set[tuple[str, ...]]:
    """Return legal-entity headings, not company names mentioned in ordinary prose."""
    title_terms = {
        "annual", "report", "financial", "results", "quarter", "quarterly", "ended",
        "year", "investor", "presentation", "earnings", "conference", "call", "business",
        "responsibility", "sustainability", "corporate", "governance", "notice", "statement",
        "of", "the", "for", "as", "at", "and", "fy", "q1", "q2", "q3", "q4",
    }
    legal_suffixes = {"limited", "ltd", "inc", "incorporated", "corporation", "corp", "plc"}
    prose_terms = {
        "this", "these", "those", "company", "compares", "compared", "comparison",
        "performance", "with", "its", "their", "versus", "against", "between", "from",
        "provides", "reported", "reports", "announced", "announces", "said", "has", "have",
        "had", "was", "were", "is", "are", "will", "would", "should", "could", "we",
        "our", "you", "your", "investors", "customers", "business", "growth", "market",
    }
    known_non_issuer_entities = {
        ("national", "stock", "exchange", "of", "india"),
        ("bse",), ("bombay", "stock", "exchange"),
        ("securities", "and", "exchange", "board", "of", "india"),
        # credit rating agencies author rating rationales; they are never the issuer
        ("care", "ratings"), ("crisil",), ("crisil", "ratings"), ("icra",),
        ("india", "ratings", "and", "research"), ("acuite", "ratings", "and", "research"),
        ("brickwork", "ratings", "india"), ("infomerics", "valuation", "and", "rating"),
    }
    address_terms = {"plaza", "complex", "tower", "towers", "street", "road", "marg", "floor",
                     "building", "bhawan", "bhavan", "nagar", "dalal", "bandra", "kurla",
                     "mumbai", "delhi", "kolkata", "chennai", "bengaluru", "sector", "phase"}
    declared: set[tuple[str, ...]] = set()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        context = " ".join(lines[max(0, index - 3):index + 1]).casefold()
        if any(term in context for term in (
            "peer", "competitor", "comparable", "benchmark", "peer group",
        )):
            continue
        tokens = _issuer_name_tokens(line)
        if not tokens or len(tokens) > 12 or not any(t in legal_suffixes for t in tokens):
            continue
        suffix_index = next(i for i, token in enumerate(tokens) if token in legal_suffixes)
        if not all(token in title_terms or token.isdigit() for token in tokens[suffix_index + 1:]):
            continue
        name_tokens = list(tokens[:suffix_index])
        while name_tokens and name_tokens[0].isdigit():
            name_tokens.pop(0)  # a page number printed before a running header ('1 CARE ...')
        while name_tokens and name_tokens[0] in {"for", "by", "the"}:
            name_tokens.pop(0)
        while name_tokens and name_tokens[-1] in {"private", "pvt"}:
            name_tokens.pop()
        if any(token in prose_terms for token in name_tokens):
            continue
        normalized_name = tuple(name_tokens)
        if not normalized_name or normalized_name in known_non_issuer_entities:
            continue
        # a cover letter's two-column exchange address read as one line ('"Exchange Plaza",
        # Bandra - Kurla Complex  BSE Limited') ends in an addressee, and an address is not
        # a legal-entity heading
        if any(normalized_name[-len(e):] == e for e in known_non_issuer_entities) or any(
                token in address_terms for token in normalized_name):
            continue
        declared.add(normalized_name)
    return declared


def _issuer_binding(
    company: EvidenceCompany, raw: RawDocument
) -> tuple[IssuerBindingStatus, IssuerBindingMethod | None]:
    """Bind from identity printed at the front of the retrieved document, never its URL alone.

    Only the first three text pages are identity-bearing. A clear conflict with an exchange
    identifier fails; absent identity evidence remains unverified. Name normalization is
    deliberately exact apart from Unicode, punctuation, whitespace, Ltd/Limited and PVT/Private.
    """
    pages = [page.text for page in raw.pages if page.number <= 3]
    # Only the page header and issuer sign-off are identity-bearing. The remaining
    # first-three-page text includes peer tables, benchmark references and prose.
    identity_lines: list[str] = []
    for page_text in pages:
        lines = page_text.splitlines()
        identity_lines.extend(lines[:25])
        identity_lines.extend(
            line for line in lines[25:] if line.strip().casefold().startswith("for ")
        )
    front_matter = "\n".join(identity_lines)
    name_match = _contains_name(front_matter, company.name)
    expected_name = tuple(
        token for token in _issuer_name_tokens(company.name)
        if token not in {"limited", "ltd", "private", "pvt"}
    )
    declared_names = _declared_issuer_names(front_matter)
    conflicting_names = {
        name for name in declared_names
        if name != expected_name and not any(
            name[i:i + len(expected_name)] == expected_name
            for i in range(len(name) - len(expected_name) + 1)
        )
    }
    nse_values = {
        (match.group(1) or match.group(2)).strip(".&_- ").upper()
        for match in _NSE_HEADER.finditer(front_matter)
    }
    bse_values = {match.group(1) for match in _BSE_HEADER.finditer(front_matter)}
    isin_values = {match.group(1).upper() for match in _ISIN_HEADER.finditer(front_matter)}
    expected_nse = company.nse_symbol.strip().upper() if company.nse_symbol else None
    expected_bse = company.bse_code.strip() if company.bse_code else None
    expected_isin = company.isin.strip().upper() if company.isin else None

    # A matching exchange code in prose, a peer row, or a footer is not identity proof.
    # Require a matching legal-name declaration; any distinct legal-entity heading on the
    # inspected pages is a strong contradiction and fails closed.
    if conflicting_names:
        return IssuerBindingStatus.MISMATCH, None
    if expected_nse and any(value != expected_nse for value in nse_values):
        return IssuerBindingStatus.MISMATCH, None
    if expected_bse and any(value != expected_bse for value in bse_values):
        return IssuerBindingStatus.MISMATCH, None
    if expected_isin and any(value != expected_isin for value in isin_values):
        return IssuerBindingStatus.MISMATCH, None
    if isin_values and expected_isin is None:
        return IssuerBindingStatus.UNVERIFIED, None
    if (nse_values and expected_nse is None) or (bse_values and expected_bse is None):
        return IssuerBindingStatus.UNVERIFIED, None
    identifiers_match = bool(
        (expected_nse and expected_nse in nse_values)
        or (expected_bse and expected_bse in bse_values)
        or (expected_isin and expected_isin in isin_values)
    )
    if identifiers_match and name_match:
        return IssuerBindingStatus.VERIFIED, IssuerBindingMethod.NAME_AND_EXCHANGE
    if identifiers_match:
        return IssuerBindingStatus.UNVERIFIED, None
    if name_match:
        return IssuerBindingStatus.VERIFIED, IssuerBindingMethod.NORMALIZED_LEGAL_NAME
    return IssuerBindingStatus.UNVERIFIED, None


class ArtifactReader(Protocol):
    async def read(self, reference: str, sha256: str, size_bytes: int) -> bytes: ...


class DocumentParser(Protocol):
    def parse(
        self, content: bytes, detected: DetectedContentType, limits: ExtractionLimits
    ) -> RawDocument: ...


def _failed(
    context: DocumentContext,
    code: str,
    status: ExtractionStatus,
    *,
    issuer_binding_status: IssuerBindingStatus = IssuerBindingStatus.UNVERIFIED,
) -> DocumentExtraction:
    return DocumentExtraction(
        extraction_id=uuid5(_NS, f"{context.document_id}|{context.artifact_sha256}|failed|{code}"),
        document_id=context.document_id,
        company_key=context.company_key,
        document_type=context.document_type,
        authority=context.authority,
        issuer_binding_status=issuer_binding_status,
        issuer_binding_target_company_key=context.company_key,
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
            binding_status, binding_method = _issuer_binding(catalogue.company, raw)
            binding_fields = {
                "issuer_binding_status": binding_status,
                "issuer_binding_target_company_key": catalogue.company.company_key,
                "issuer_binding_method": binding_method,
            }
            has_text = any(page.text.strip() for page in raw.pages)
            if binding_status != IssuerBindingStatus.VERIFIED and raw.status in _USABLE \
                    and has_text:
                code = (
                    "ISSUER_IDENTITY_MISMATCH"
                    if binding_status == IssuerBindingStatus.MISMATCH
                    else "ISSUER_IDENTITY_UNVERIFIED"
                )
                extractions.append(_failed(
                    context, code, ExtractionStatus.FAILED,
                    issuer_binding_status=binding_status,
                ))
                continue
            extraction = await asyncio.to_thread(analyse_document, raw, context, limits)
            warnings = extraction.warnings
            if binding_status != IssuerBindingStatus.VERIFIED:
                code = (
                    "ISSUER_IDENTITY_MISMATCH"
                    if binding_status == IssuerBindingStatus.MISMATCH
                    else "ISSUER_IDENTITY_UNVERIFIED"
                )
                warnings = (*warnings, ExtractionWarning(code=code))
            extractions.append(extraction.model_copy(update={
                **binding_fields,
                "warnings": warnings,
            }))
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
