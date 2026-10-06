"""DETAILED_RESEARCH_V1 Box 1: combine EvidenceSources into one deterministic catalogue.

Identity (documented rules, strongest first):
  1. BSE attachment file name - the same filing PDF is served both as
     /xml-data/corpfiling/Attach{His,Live}/<name>.pdf and
     /stockinfo/AnnPdfOpen.aspx?Pname=<name>.pdf
  2. Canonical URL - lower-cased scheme/host, default port and fragment dropped,
     consistent percent-encoding, tracking parameters dropped, query parameters sorted.
  document_id = uuid5(company_key + identity key): stable across runs and title changes.

Deduplication: candidates with the same identity key merge into one document (all
sightings kept in `discovered_from`). Documents at different URLs are never merged on
title similarity; once retrieved, identical content hashes are LINKED via
`duplicate_of` (highest authority is canonical) rather than merged, so IDs stay stable.

Ordering: newest published_date first (undated last; among undated documents an explicit
period label such as '2026' or 'Jun 2026' sorts newest first), then source authority,
then document type (enum order), then document_id.
"""

import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid5

from stock_research_ai.domain.evidence import (
    AUTHORITY_RANK,
    CHANNEL_DOCUMENT_TYPES,
    CatalogueIssue,
    ClassificationConfidence,
    DocumentCandidate,
    DocumentRetrievalStatus,
    EvidenceCatalogue,
    EvidenceCompany,
    EvidenceDocumentType,
    EvidenceSourceError,
    ResearchDocument,
    SourceAuthority,
    SourceDiscovery,
    SourceOutcome,
)

_NAMESPACE = UUID("6f1d3c52-2f0e-4b8a-9d55-3c1a0e7b9a41")
_TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_BSE_FILE = re.compile(r"^[A-Za-z0-9-]+\.pdf$")

#: Host suffix -> authority. Source-class knowledge only; never company-specific.
_REGULATORY_HOSTS = ("bseindia.com", "nseindia.com", "sebi.gov.in", "mca.gov.in")
_AGGREGATOR_HOSTS = ("screener.in",)
_RATING_AGENCY_HOSTS = (
    "crisil.com",
    "icra.in",
    "careratings.com",
    "careedge.in",
    "indiaratings.co.in",
    "brickworkratings.com",
    "acuite.in",
    "infomerics.com",
)
_TYPE_ORDER = {kind: index for index, kind in enumerate(EvidenceDocumentType)}
_CONFIDENCE_ORDER = {kind: index for index, kind in enumerate(ClassificationConfidence)}


class EvidenceSource(Protocol):
    """One discovery adapter (Screener, BSE, NSE, company IR, rating agency, ...)."""

    @property
    def name(self) -> str: ...

    async def discover(self, company: EvidenceCompany) -> SourceDiscovery: ...


def canonical_url(url: str) -> str:
    """Normalized absolute http(s) URL; raises ValueError for anything else."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Unsupported URL")
    if parts.username or parts.password:
        raise ValueError("URL embeds credentials")
    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    path = quote(unquote(parts.path), safe=_PATH_SAFE) or "/"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
    )
    return urlunsplit((scheme, netloc, path, urlencode(query, quote_via=quote), ""))


def identity_key(url: str) -> str:
    """The strongest URL-level identity available (see module docstring)."""
    canonical = canonical_url(url)
    parts = urlsplit(canonical)
    host = parts.hostname or ""
    if host == "bseindia.com" or host.endswith(".bseindia.com"):
        segments = parts.path.split("/")
        name = None
        if len(segments) >= 5 and segments[1:4] in (
            ["xml-data", "corpfiling", "AttachHis"],
            ["xml-data", "corpfiling", "AttachLive"],
        ):
            name = segments[4]
        elif parts.path.lower() == "/stockinfo/annpdfopen.aspx":
            name = dict(parse_qsl(parts.query)).get("Pname")
        if name and _BSE_FILE.fullmatch(name):
            return f"bse-attachment:{name.lower()}"
    return f"url:{canonical}"


def document_id(company: EvidenceCompany, key: str) -> UUID:
    return uuid5(_NAMESPACE, f"evidence-document:v1:{company.company_key}:{key}")


def _host_matches(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def source_authority(url: str, company: EvidenceCompany) -> SourceAuthority:
    host = (urlsplit(url).hostname or "").lower()
    if _host_matches(host, _REGULATORY_HOSTS):
        return SourceAuthority.PRIMARY_REGULATORY
    if company.website_url:
        site = (urlsplit(company.website_url).hostname or "").lower().removeprefix("www.")
        # Only the company's own verified site and its subdomains, never look-alikes.
        if site and _host_matches(host, (site,)):
            return SourceAuthority.PRIMARY_COMPANY
    if _host_matches(host, _AGGREGATOR_HOSTS):
        return SourceAuthority.SECONDARY_AGGREGATOR
    if _host_matches(host, _RATING_AGENCY_HOSTS):
        return SourceAuthority.THIRD_PARTY
    return SourceAuthority.UNVERIFIED


def _file_extension(url: str) -> str | None:
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    match = re.search(r"\.([A-Za-z0-9]{1,8})$", last)
    return "." + match.group(1).lower() if match else None


def _primary(candidates: Sequence[DocumentCandidate]) -> DocumentCandidate:
    """Most informative sighting: content type over channel type, then confidence."""
    return min(
        candidates,
        key=lambda c: (
            c.document_type in CHANNEL_DOCUMENT_TYPES,
            _CONFIDENCE_ORDER[c.confidence],
            c.discovery.source_name,
            c.discovery.locator,
        ),
    )


def _agreed[T](values: Iterable[T | None]) -> tuple[T | None, bool]:
    """The single known value, or (None, conflict) when sightings disagree."""
    known = {value for value in values if value is not None}
    if len(known) == 1:
        return next(iter(known)), False
    return None, len(known) > 1


def _merge(
    company: EvidenceCompany, key: str, candidates: Sequence[DocumentCandidate]
) -> tuple[ResearchDocument, list[CatalogueIssue]]:
    primary = _primary(candidates)
    issues: list[CatalogueIssue] = []
    merged_values: dict[str, object] = {}
    for field in (
        "published_date",
        "published_date_text",
        "period_label",
        "fiscal_year",
        "fiscal_quarter",
        "reporting_basis",
        "language",
    ):
        value, conflict = _agreed(getattr(c, field) for c in candidates)
        merged_values[field] = value
        if conflict:
            issues.append(
                CatalogueIssue(
                    source_name=primary.discovery.source_name,
                    code=f"CONFLICTING_{field.upper()}",
                    url=primary.url,
                )
            )
    if merged_values["fiscal_year"] is None:
        merged_values["fiscal_quarter"] = None
    retrieved = [c for c in candidates if c.retrieval.status == DocumentRetrievalStatus.RETRIEVED]
    retrieval = retrieved[0].retrieval if retrieved else primary.retrieval
    urls = sorted({canonical_url(c.url) for c in candidates})
    source_url = canonical_url(primary.url)
    observed = sorted({c.document_type.value for c in candidates})
    metadata = dict(sorted(primary.metadata.items()))
    if len(observed) > 1:
        metadata["observed_types"] = ",".join(observed)
    discoveries = tuple(
        sorted(
            {c.discovery for c in candidates},
            key=lambda d: (d.source_name, d.locator, d.discovered_at),
        )
    )
    document = ResearchDocument(
        document_id=document_id(company, key),
        identity_key=key,
        company_key=company.company_key,
        company_id=company.company_id,
        document_type=primary.document_type,
        confidence=primary.confidence,
        title=primary.title,
        source_url=source_url,
        download_url=source_url if any(c.is_direct_document for c in candidates) else None,
        alternate_urls=tuple(url for url in urls if url != source_url),
        source_domain=urlsplit(source_url).hostname or "",
        source_authority=min(
            (source_authority(url, company) for url in urls), key=AUTHORITY_RANK.__getitem__
        ),
        file_extension=_file_extension(source_url),
        retrieval=retrieval,
        discovered_from=discoveries,
        discovered_at=min(d.discovered_at for d in discoveries),
        metadata=metadata,
        **merged_values,  # type: ignore[arg-type]
    )
    return document, issues


def _link_content_duplicates(documents: list[ResearchDocument]) -> list[ResearchDocument]:
    groups: dict[str, list[ResearchDocument]] = {}
    for document in documents:
        if document.retrieval.content_hash:
            groups.setdefault(document.retrieval.content_hash, []).append(document)
    canonical: dict[UUID, UUID] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        head = min(members, key=lambda d: (AUTHORITY_RANK[d.source_authority], str(d.document_id)))
        canonical.update({m.document_id: head.document_id for m in members if m is not head})
    return [
        d.model_copy(update={"duplicate_of": canonical[d.document_id]})
        if d.document_id in canonical
        else d
        for d in documents
    ]


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _period_rank(label: str | None) -> int:
    """Sort-only rank from an explicit period label ('2026' or 'Jun 2026'); 0 if neither.

    Never stored and never turned into a date - it only keeps undated documents of the
    same kind newest-first instead of in identifier order.
    """
    if not label:
        return 0
    match = re.fullmatch(r"(?:([A-Za-z]{3})[a-z]* )?(\d{4})", label.strip())
    if not match:
        return 0
    month = _MONTHS.get((match.group(1) or "").lower(), 13 if match.group(1) is None else 0)
    return int(match.group(2)) * 100 + month


def _order_key(document: ResearchDocument) -> tuple[object, ...]:
    dated = document.published_date
    return (
        dated is None,
        -dated.toordinal() if dated else 0,
        -_period_rank(document.period_label) if dated is None else 0,
        AUTHORITY_RANK[document.source_authority],
        _TYPE_ORDER[document.document_type],
        str(document.document_id),
    )


def assemble_catalogue(
    company: EvidenceCompany,
    discoveries: Sequence[SourceDiscovery],
    outcomes: Sequence[SourceOutcome],
    *,
    catalogued_at: datetime,
) -> EvidenceCatalogue:
    """Pure: identity, merging, duplicate linking and ordering over source results."""
    issues: list[CatalogueIssue] = [i for d in discoveries for i in d.issues]
    groups: dict[str, list[DocumentCandidate]] = {}
    for discovery in discoveries:
        for candidate in discovery.candidates:
            try:
                key = identity_key(candidate.url)
            except ValueError:
                issues.append(
                    CatalogueIssue(source_name=discovery.source_name, code="UNSUPPORTED_URL")
                )
                continue
            groups.setdefault(key, []).append(candidate)
    documents: list[ResearchDocument] = []
    for key in sorted(groups):
        document, merge_issues = _merge(company, key, groups[key])
        documents.append(document)
        issues.extend(merge_issues)
    documents = sorted(_link_content_duplicates(documents), key=_order_key)
    return EvidenceCatalogue(
        company=company,
        catalogued_at=catalogued_at,
        documents=tuple(documents),
        sources=tuple(sorted(outcomes, key=lambda o: o.source_name)),
        issues=tuple(
            sorted(issues, key=lambda i: (i.source_name, i.code, i.locator or "", i.url or ""))
        ),
    )


async def build_evidence_catalogue(
    company: EvidenceCompany,
    sources: Sequence[EvidenceSource],
    *,
    catalogued_at: datetime,
) -> EvidenceCatalogue:
    """Ask each source once; a failed source is recorded and the others still count."""
    discoveries: list[SourceDiscovery] = []
    outcomes: list[SourceOutcome] = []
    for source in sources:
        try:
            discovery = await source.discover(company)
        except EvidenceSourceError as exc:
            outcomes.append(
                SourceOutcome(
                    source_name=source.name,
                    status="FAILED",
                    failure_code=exc.code,
                    candidates_found=0,
                )
            )
            continue
        discoveries.append(discovery)
        outcomes.append(
            SourceOutcome(
                source_name=source.name,
                status="SUCCESS",
                candidates_found=len(discovery.candidates),
            )
        )
    return assemble_catalogue(company, discoveries, outcomes, catalogued_at=catalogued_at)


def canonical_catalogue_json(catalogue: EvidenceCatalogue) -> str:
    """One canonical serialization: sorted keys, compact, UTF-8 safe."""
    return json.dumps(
        catalogue.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
