"""Coordinate source parsers and account for every expected section."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from stock_research_ai.adapters.screener.documents import parse_documents
from stock_research_ai.adapters.screener.html import (
    HtmlLimitExceeded,
    Node,
    Page,
    data_tables,
    rows,
)
from stock_research_ai.adapters.screener.identity import parse_identity
from stock_research_ai.adapters.screener.metrics import ParseContext, parse_top
from stock_research_ai.adapters.screener.peers import parse_peers
from stock_research_ai.adapters.screener.shareholding import parse_shareholding
from stock_research_ai.adapters.screener.statements import parse_statements
from stock_research_ai.domain.company import SourceCompanyIdentity
from stock_research_ai.domain.documents import DocumentReference
from stock_research_ai.domain.extraction import (
    CompanyExtractionResult,
    CoverageIssue,
    CoverageReport,
    ExtractionMetadata,
    Section,
    SectionCoverage,
)
from stock_research_ai.domain.financial import FinancialObservation, ReportingScope
from stock_research_ai.domain.peers import PeerSnapshot
from stock_research_ai.domain.provenance import RawArtifact, RetrievalStatus, SourceRetrieval
from stock_research_ai.domain.shareholding import ShareholdingSnapshot

IDS = {
    Section.COMPANY_IDENTITY: "top",
    Section.TOP_METRICS: "top-ratios",
    Section.QUARTERLY: "quarters",
    Section.PROFIT_LOSS: "profit-loss",
    Section.BALANCE_SHEET: "balance-sheet",
    Section.CASH_FLOW: "cash-flow",
    Section.RATIOS: "ratios",
    Section.SHAREHOLDING: "shareholding",
    Section.PEERS: "peers",
    Section.DOCUMENTS: "documents",
}


class ScreenerNormalizer:
    def identify(
        self, content: bytes, artifact: RawArtifact, retrieval: SourceRetrieval, company_id: UUID
    ) -> SourceCompanyIdentity | None:
        if retrieval.status != RetrievalStatus.SUCCESS:
            return None
        try:
            root = Page(content.decode(artifact.encoding, errors="strict")).root
        except (UnicodeError, LookupError, HtmlLimitExceeded):
            return None
        return parse_identity(root, artifact, retrieval, company_id)

    def normalize(
        self,
        content: bytes | None,
        artifact: RawArtifact | None,
        retrieval: SourceRetrieval,
        company_id: UUID,
    ) -> CompanyExtractionResult:
        identity = None
        root = Node("document")
        failure = retrieval.error
        if (
            artifact is not None
            and content is not None
            and retrieval.status == RetrievalStatus.SUCCESS
        ):
            try:
                root = Page(content.decode(artifact.encoding, errors="strict")).root
                identity = parse_identity(root, artifact, retrieval, company_id)
            except (UnicodeError, LookupError) as exc:
                failure = f"HTML decoding failed: {exc}"
            except HtmlLimitExceeded as exc:
                # Fail closed: no section is read from a page past the structural bounds.
                root, identity, failure = Node("document"), None, str(exc)
        else:
            failure = failure or "No usable acquired HTML"
        statements: dict[Section, tuple[FinancialObservation, ...]] = {}
        top: tuple[FinancialObservation, ...] = ()
        holdings = ShareholdingSnapshot()
        peers = PeerSnapshot()
        documents: tuple[DocumentReference, ...] = ()
        coverage: list[SectionCoverage] = []
        context = (
            ParseContext(
                artifact,
                company_id,
                identity.reporting_scope if identity else ReportingScope.UNKNOWN,
            )
            if artifact
            else None
        )
        for section in Section:
            node = root.by_id(IDS[section])
            if node is None or context is None or failure:
                coverage.append(
                    SectionCoverage(
                        section=section,
                        presence="UNAVAILABLE" if failure else "MISSING",
                        parse_status="NOT_ATTEMPTED",
                        issues=(
                            CoverageIssue(
                                code="ACQUISITION_FAILED" if failure else "MISSING_SECTION",
                                severity="error",
                                section=section,
                                message=failure or "Section not found",
                                raw_artifact_id=artifact.id if artifact else None,
                                retrieval_id=retrieval.id,
                            ),
                        ),
                    )
                )
                continue
            count = 0
            if section == Section.COMPANY_IDENTITY:
                count = int(identity is not None)
                if identity is not None and identity.reporting_scope == ReportingScope.UNKNOWN:
                    context.issue(
                        section,
                        "UNKNOWN_SCOPE",
                        "Reporting scope absent or conflicting",
                        node,
                        error=True,
                    )
            elif identity is None:
                context.issue(
                    section, "IDENTITY_REQUIRED", "No usable company identity", node, error=True
                )
            elif section == Section.TOP_METRICS:
                top = parse_top(node, context)
                count = len(top)
            elif section == Section.SHAREHOLDING:
                holdings = parse_shareholding(node, context)
                count = len(holdings.holdings) + len(holdings.shareholder_counts)
            elif section == Section.PEERS:
                peers = parse_peers(node, context, retrieval.final_url)
                count = len(peers.entries)
            elif section == Section.DOCUMENTS:
                documents = parse_documents(node, context, retrieval.final_url)
                count = len(documents)
            else:
                statements[section] = parse_statements(node, section, context)
                count = len(statements[section])
            presence: Literal["FOUND", "MISSING", "UNAVAILABLE"] = "FOUND"
            status: Literal["SUCCESS", "PARTIAL", "FAILED", "NOT_ATTEMPTED"] = "SUCCESS"
            if count == 0:
                loading = any(
                    s in node.text.lower() for s in ("loading", "log in", "login", "premium")
                )
                presence = "UNAVAILABLE" if loading else "FOUND"
                status = "NOT_ATTEMPTED" if loading else "FAILED"
                context.issue(
                    section,
                    "SOURCE_UNAVAILABLE" if loading else "EMPTY_SECTION",
                    "Source exposes a loading or gated placeholder; no table acquired"
                    if loading
                    else "Present section yielded no usable records",
                    node,
                    error=True,
                )
            elif any(i.section == section and i.severity == "error" for i in context.issues):
                status = "PARTIAL"
            tables = data_tables(node)
            coverage.append(
                SectionCoverage(
                    section=section,
                    presence=presence,
                    parse_status=status,
                    records=count,
                    tables=len(tables),
                    rows=sum(max(0, len(rows(t)) - 1) for t in tables),
                    cells=sum(len(r) - 1 for t in tables for r in rows(t)[1:] if r),
                    issues=tuple(i for i in context.issues if i.section == section),
                )
            )
        overall: Literal["COMPLETE", "PARTIAL", "FAILED"] = "COMPLETE"
        if identity is None or failure:
            overall = "FAILED"
        elif any(s.parse_status != "SUCCESS" for s in coverage):
            overall = "PARTIAL"
        return CompanyExtractionResult(
            metadata=ExtractionMetadata(
                id=uuid4(), provider=retrieval.provider, created_at=datetime.now(UTC)
            ),
            company=identity,
            metric_definitions=tuple(context.definitions[k] for k in sorted(context.definitions))
            if context
            else (),
            top_metrics=top,
            quarterly=statements.get(Section.QUARTERLY, ()),
            profit_loss=statements.get(Section.PROFIT_LOSS, ()),
            balance_sheet=statements.get(Section.BALANCE_SHEET, ()),
            cashflow=statements.get(Section.CASH_FLOW, ()),
            ratios=statements.get(Section.RATIOS, ()),
            shareholding=holdings,
            peers=peers,
            documents=documents,
            retrievals=(retrieval,),
            raw_artifacts=(artifact,) if artifact else (),
            coverage=CoverageReport(overall=overall, sections=tuple(coverage)),
        )
