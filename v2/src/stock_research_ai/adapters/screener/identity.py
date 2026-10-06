"""Identity comes from explicit page attributes and displayed exchange identifiers."""

import re
from uuid import NAMESPACE_URL, UUID, uuid5

from stock_research_ai.adapters.screener.html import Node
from stock_research_ai.domain.company import Listing, SourceCompanyIdentity
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.provenance import ObservationProvenance, RawArtifact, SourceRetrieval


def parse_identity(
    root: Node, artifact: RawArtifact, retrieval: SourceRetrieval, company_id: UUID
) -> SourceCompanyIdentity | None:
    info = root.by_id("company-info")
    top = root.by_id("top")
    if info is None or top is None:
        return None
    names = top.all("h1")
    key = info.attrs.get("data-company-id")
    if not names or not names[0].text or not key:
        return None
    modes = set(re.findall(r"(Standalone|Consolidated) Figures", root.text))
    explicit = info.attrs.get("data-consolidated")
    if explicit == "true":
        modes.add("Consolidated")
    elif explicit == "false":
        modes.add("Standalone")
    scope = ReportingScope(next(iter(modes)).lower()) if len(modes) == 1 else ReportingScope.UNKNOWN
    provenance = ObservationProvenance(
        raw_artifact_id=artifact.id,
        source_location=info.location("COMPANY_IDENTITY"),
        normalizer_version="1",
    )
    listings: list[Listing] = []
    for anchor in top.all("a"):
        match = re.fullmatch(r"(NSE|BSE):\s*([\w.-]+)", anchor.text)
        if not match or "href" not in anchor.attrs:
            continue
        exchange, identifier = match.groups()
        if any(item.exchange == exchange for item in listings):
            continue
        listings.append(
            Listing(
                id=uuid5(NAMESPACE_URL, f"{company_id}:{exchange}:{identifier}"),
                company_id=company_id,
                exchange=exchange,
                symbol=identifier if exchange == "NSE" else None,
                exchange_identifier=identifier if exchange == "BSE" else None,
                provenance=ObservationProvenance(
                    raw_artifact_id=artifact.id,
                    source_location=anchor.location("COMPANY_IDENTITY"),
                    normalizer_version="1",
                ),
                link=anchor.link(retrieval.final_url, "COMPANY_IDENTITY"),
            )
        )
    return SourceCompanyIdentity(
        company_id=company_id,
        name=names[0].text,
        provider=retrieval.provider,
        source_company_key=key,
        requested_url=retrieval.requested_url,
        final_url=retrieval.final_url,
        reporting_scope=scope,
        listings=tuple(listings),
        provenance=provenance,
    )
