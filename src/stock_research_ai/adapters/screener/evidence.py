"""DETAILED_RESEARCH_V1 Box 1: the Screener page as an EvidenceSource.

Re-reads the SAME already-acquired, hash-verified Screener artifact used for extraction
(no second fetch - the sector.py convention), so the frozen normalizer and its
DocumentReference output stay untouched. Unlike that parser, this one keeps the context
classification needs: the section heading, each concall row's period label, and the
link kind (Transcript / PPT / REC). Classification is deterministic and rule-ordered;
dates and periods are only ever taken from explicit source text.
"""

import hashlib
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from stock_research_ai.adapters.documents.source import validate_url
from stock_research_ai.adapters.screener.html import (
    MAX_SCREENER_NODES,
    Node,
    Page,
    data_tables,
    rows,
)
from stock_research_ai.adapters.screener.metrics import clean_label
from stock_research_ai.domain.evidence import (
    CatalogueIssue,
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    DocumentRetrievalStatus,
    EvidenceCompany,
    EvidenceDocumentType,
    RetrievalState,
    SourceDiscovery,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.provenance import RawArtifact, SourceRetrieval

SOURCE_NAME = "screener"

T = EvidenceDocumentType
HIGH, MEDIUM, LOW = (
    ClassificationConfidence.HIGH,
    ClassificationConfidence.MEDIUM,
    ClassificationConfidence.LOW,
)

#: Title keyword rules, most specific first. First match wins; every rule is explicit
#: source text, never a guess from a URL. AGM documents are resolved before these.
_TITLE_RULES: tuple[tuple[tuple[str, ...], EvidenceDocumentType], ...] = (
    (("shareholding pattern",), T.SHAREHOLDING_PATTERN),
    (
        ("insider trading", "(pit)", "regulation 7(2)", "regulation 7 (2)"),
        T.INSIDER_TRADING_DISCLOSURE,
    ),
    (("business responsibility", "brsr"), T.BRSR),
    (("corporate governance",), T.CORPORATE_GOVERNANCE_REPORT),
    (("letter to shareholders", "shareholder letter", "shareholders letter"), T.SHAREHOLDER_LETTER),
    (("transcript",), T.CONCALL_TRANSCRIPT),
    (("audio recording", "call recording", "recording of"), T.CONCALL_AUDIO),
    (
        (
            "investor presentation",
            "investors presentation",
            "analyst presentation",
            "earnings presentation",
        ),
        T.INVESTOR_PRESENTATION,
    ),
    (("credit rating",), T.CREDIT_RATING_REPORT),
    (("annual report",), T.ANNUAL_REPORT),
    (("financial result", "quarterly results", "results for the quarter"), T.QUARTERLY_RESULT),
    (("regulation 30", "reg. 30", "reg 30", "(lodr)"), T.REGULATION_30_FILING),
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DIRECT_SUFFIXES = (".pdf", ".htm", ".html", ".xls", ".xlsx", ".mp3", ".mp4", ".zip")


def classify_title(title: str) -> EvidenceDocumentType | None:
    """Deterministic title-keyword classification; None when nothing explicit matches."""
    text = " ".join(title.lower().split())
    if "annual general meeting" in text or "agm" in re.findall(r"[a-z0-9]+", text):
        if "transcript" in text:
            return T.AGM_TRANSCRIPT
        if "minutes" in text or "proceedings" in text:
            return T.AGM_MINUTES
        if "notice" in text:
            return T.AGM_NOTICE
    for keywords, kind in _TITLE_RULES:
        if any(keyword in text for keyword in keywords):
            return kind
    return None


def explicit_fiscal_period(title: str) -> tuple[int | None, int | None]:
    """(fiscal_year, fiscal_quarter) only from an explicit 'FY' token in the title."""
    text = title.upper()
    quarter = re.search(r"\bQ([1-4])\s*[-']?\s*FY\s*'?(\d{4}|\d{2})\b", text)
    if quarter:
        return _year(quarter.group(2)), int(quarter.group(1))
    year = re.search(r"\bFY\s*'?(\d{4}|\d{2})\b", text)
    return (_year(year.group(1)), None) if year else (None, None)


def _year(token: str) -> int:
    return 2000 + int(token) if len(token) == 2 else int(token)


def _explicit_date(text: str) -> date | None:
    """A date only when day, month and year are all displayed (e.g. '26 Jun 2025')."""
    match = re.search(r"\b(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})\b", text)
    if not match or match.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1)))
    except ValueError:
        return None


def _direct_text(node: Node) -> str:
    return " ".join(" ".join(c.split()) for c in node.children if isinstance(c, str)).strip()


def _is_direct_document(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.lower()
    if path.endswith(_DIRECT_SUFFIXES):
        return True
    return path == "/stockinfo/annpdfopen.aspx" and parts.query.lower().endswith(".pdf")


class ScreenerEvidenceSource:
    """EvidenceSource over one acquired Screener company page."""

    name = SOURCE_NAME

    def __init__(
        self,
        extraction: CompanyExtractionResult,
        content: bytes,
    ) -> None:
        identity = extraction.company
        if identity is None or len(extraction.raw_artifacts) != 1:
            raise ValueError("Evidence discovery requires one verified Screener extraction")
        artifact = extraction.raw_artifacts[0]
        if hashlib.sha256(content).hexdigest() != artifact.content_sha256:
            raise ValueError("Content does not match the extraction's preserved artifact")
        retrieval = next(r for r in extraction.retrievals if r.id == artifact.retrieval_id)
        self._identity = identity
        self._artifact: RawArtifact = artifact
        self._retrieval: SourceRetrieval = retrieval
        self._root = Page(
            content.decode(artifact.encoding, errors="strict"), MAX_SCREENER_NODES
        ).root

    def company(self) -> EvidenceCompany:
        website = next(
            (
                a.link(self._retrieval.final_url, "TOP").resolved_url
                for a in self._root.all("a")
                if a.text == "Website" and "href" in a.attrs
            ),
            None,
        )
        return EvidenceCompany(
            company_key=f"{self._identity.provider}:{self._identity.source_company_key}",
            company_id=self._identity.company_id,
            name=self._identity.name,
            source_company_key=self._identity.source_company_key,
            reporting_scope=self._identity.reporting_scope,
            website_url=website,
        )

    async def discover(self, company: EvidenceCompany) -> SourceDiscovery:
        candidates = [self._page_candidate()]
        issues: list[CatalogueIssue] = []
        documents = self._root.by_id("documents")
        if documents is None:
            issues.append(CatalogueIssue(source_name=self.name, code="DOCUMENTS_SECTION_MISSING"))
        else:
            # Every anchor in the section is visited in document order (none can be
            # silently skipped); a concall row's period label applies to its own links.
            heading = None
            row_periods: dict[int, str] = {}
            for node in documents.all():
                if node.tag == "h3":
                    heading = node.text or None
                elif node.tag == "li" and (heading or "").lower() == "concalls":
                    period = next(
                        (d.text for d in node.all("div") if "nowrap" in d.attrs.get("class", "")),
                        None,
                    )
                    if period:
                        row_periods.update({id(a): period for a in node.all("a")})
                elif node.tag == "a" and "href" in node.attrs:
                    self._read_anchor(node, heading, row_periods.get(id(node)), candidates, issues)
        self._read_quarterly_sources(candidates, issues)
        return SourceDiscovery(
            source_name=self.name, candidates=tuple(candidates), issues=tuple(issues)
        )

    def _read_quarterly_sources(
        self, candidates: list[DocumentCandidate], issues: list[CatalogueIssue]
    ) -> None:
        """The quarterly table's 'Raw PDF' row: one source link per quarter column.

        The period label is the column header as displayed; Screener's quarter-end date key
        is kept as metadata only (it is not a publication date). The link resolves through
        Screener, and which statement it leads to is not stated, so the reporting basis
        stays unknown - the table's own basis is recorded as metadata.
        """
        quarters = self._root.by_id("quarters")
        tables = data_tables(quarters) if quarters is not None else []
        if not tables:
            return
        table_rows = rows(tables[0])
        if not table_rows:
            return
        headers = table_rows[0][1:]
        caption = quarters.text if quarters is not None else ""
        basis = (
            "consolidated"
            if "Consolidated Figures" in caption
            else "standalone"
            if "Standalone Figures" in caption
            else "unknown"
        )
        for row in table_rows[1:]:
            if not row or clean_label(row[0].text) != "Raw PDF":
                continue
            if len(row) != len(headers) + 1:
                issues.append(
                    CatalogueIssue(source_name=self.name, code="MALFORMED_QUARTERLY_SOURCE_ROW")
                )
                continue
            for header, cell in zip(headers, row[1:], strict=True):
                for anchor in (a for a in cell.all("a") if "href" in a.attrs):
                    url = anchor.link(self._retrieval.final_url, "QUARTERS").resolved_url
                    locator = anchor.location("QUARTERS").locator
                    retrieval = self._retrieval_state(url)
                    if retrieval is None or not header.text:
                        issues.append(
                            CatalogueIssue(
                                source_name=self.name, code="UNSAFE_URL", locator=locator
                            )
                        )
                        continue
                    metadata = {"screener_table_basis": basis, "link_kind": "Raw PDF"}
                    if header.attrs.get("data-date-key"):
                        metadata["period_end_key"] = header.attrs["data-date-key"]
                    candidates.append(
                        DocumentCandidate(
                            document_type=T.QUARTERLY_RESULT,
                            confidence=HIGH,
                            title=f"Quarterly results - {header.text}",
                            url=url,
                            is_direct_document=False,
                            period_label=header.text,
                            retrieval=retrieval,
                            discovery=self._discovery(
                                locator, T.QUARTERLY_RESULT, "Quarterly Results", "Raw PDF"
                            ),
                            metadata=metadata,
                        )
                    )

    def _discovery(
        self, locator: str, kind: EvidenceDocumentType, section: str | None, label: str | None
    ) -> DocumentDiscovery:
        return DocumentDiscovery(
            source_name=self.name,
            discovered_via_url=self._retrieval.final_url,
            locator=locator,
            source_section=section,
            source_label=label[:500] if label else None,
            classified_as=kind,
            discovered_at=self._retrieval.retrieved_at,
            raw_artifact_id=self._artifact.id,
        )

    def _page_candidate(self) -> DocumentCandidate:
        titles = self._root.all("title")
        title = (titles[0].text if titles and titles[0].text else self._identity.name)[:500]
        mime = (self._retrieval.content_type or "text/html").split(";")[0].strip()
        return DocumentCandidate(
            document_type=T.SCREENER_PAGE,
            confidence=HIGH,
            title=title,
            url=self._retrieval.final_url,
            is_direct_document=True,
            reporting_basis=self._identity.reporting_scope,
            retrieval=RetrievalState(
                status=DocumentRetrievalStatus.RETRIEVED,
                retrieved_at=self._retrieval.retrieved_at,
                content_hash=self._artifact.content_sha256,
                file_size_bytes=self._artifact.size_bytes,
                mime_type=mime,
                local_artifact_reference=self._artifact.content_reference,
            ),
            discovery=self._discovery("document", T.SCREENER_PAGE, None, None),
        )

    def _read_anchor(
        self,
        anchor: Node,
        heading: str | None,
        row_period: str | None,
        candidates: list[DocumentCandidate],
        issues: list[CatalogueIssue],
    ) -> None:
        section = (heading or "").lower()
        url = anchor.link(self._retrieval.final_url, "DOCUMENTS").resolved_url
        locator = anchor.location("DOCUMENTS").locator
        retrieval = self._retrieval_state(url)
        if retrieval is None:
            issues.append(CatalogueIssue(source_name=self.name, code="UNSAFE_URL", locator=locator))
            return
        label = _direct_text(anchor) or anchor.text
        if section == "announcements" and label.lower() == "all":
            # An exchange listing page, not a document: a lead for a future BSE source.
            issues.append(
                CatalogueIssue(
                    source_name=self.name, code="INDEX_LINK_SKIPPED", locator=locator, url=url
                )
            )
            return
        if (urlsplit(url).hostname or "").endswith("screener.in"):
            issues.append(
                CatalogueIssue(
                    source_name=self.name, code="AGGREGATOR_LINK_SKIPPED", locator=locator
                )
            )
            return
        candidates.append(
            self._classify(anchor, section, heading, label, row_period, url, retrieval)
        )

    @staticmethod
    def _retrieval_state(url: str) -> RetrievalState | None:
        """NOT_ATTEMPTED when safely fetchable later; INELIGIBLE for public non-HTTPS;
        None (skip) for anything else, e.g. private-network or non-http(s) targets."""
        try:
            validate_url(url)
            return RetrievalState()
        except ValueError:
            pass
        parts = urlsplit(url)
        if parts.scheme == "http":
            try:
                validate_url(parts._replace(scheme="https").geturl())
            except ValueError:
                return None
            return RetrievalState(
                status=DocumentRetrievalStatus.INELIGIBLE, status_code="NOT_HTTPS"
            )
        return None

    def _classify(
        self,
        anchor: Node,
        section: str,
        heading: str | None,
        label: str,
        row_period: str | None,
        url: str,
        retrieval: RetrievalState,
    ) -> DocumentCandidate:
        sub_text = " ".join(d.text for d in anchor.all("div") if d.text)
        times = anchor.all("time")
        published: date | None = None
        published_text: str | None = None
        metadata: dict[str, str] = {}
        if times and times[0].attrs.get("datetime"):
            published_text = times[0].attrs["datetime"]
            try:
                stamp = datetime.fromisoformat(published_text)
                published = stamp.date() if stamp.tzinfo is not None else None
            except ValueError:
                published = None
        link_kind = label.lower()
        period_label: str | None = None
        title = label or url
        if section == "concalls":
            kind, confidence = {
                "transcript": (T.CONCALL_TRANSCRIPT, HIGH),
                "ppt": (T.INVESTOR_PRESENTATION, HIGH),
                "rec": (T.CONCALL_AUDIO, HIGH),
            }.get(link_kind, (T.OTHER_COMPANY_DOCUMENT, LOW))
            period_label = row_period
            metadata["link_kind"] = label
            title = f"Concall {label} - {row_period}" if row_period else f"Concall {label}"
        elif section == "annual reports":
            kind, confidence = (T.ANNUAL_REPORT, HIGH)
            year = re.fullmatch(r"(?i)annual report\s+(\d{4})", label)
            period_label = year.group(1) if year else None
        elif section == "credit ratings":
            kind, confidence = (T.CREDIT_RATING_REPORT, HIGH)
            match = re.search(r"\bfrom\s+(\S+)\s*$", sub_text)
            if match:
                metadata["rating_agency_label"] = match.group(1)
            date_text = sub_text[: match.start()].strip() if match else sub_text
            published_text = date_text or None
            published = _explicit_date(date_text)
            title = f"{label} - {sub_text}" if sub_text else label
        elif section == "announcements":
            kind = classify_title(label) or T.EXCHANGE_ANNOUNCEMENT
            confidence = MEDIUM
            if sub_text:
                metadata["summary_text"] = sub_text[:500]
        else:
            explicit = classify_title(label)
            kind = explicit or T.OTHER_COMPANY_DOCUMENT
            confidence = MEDIUM if explicit else LOW
        if section == "annual reports" and sub_text:
            metadata["source_label"] = sub_text
        fiscal_year, fiscal_quarter = explicit_fiscal_period(label)
        return DocumentCandidate(
            document_type=kind,
            confidence=confidence,
            title=title[:500],
            url=url,
            is_direct_document=_is_direct_document(url),
            published_date=published,
            published_date_text=published_text,
            period_label=period_label,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            retrieval=retrieval,
            discovery=self._discovery(anchor.location("DOCUMENTS").locator, kind, heading, label),
            metadata=metadata,
        )
