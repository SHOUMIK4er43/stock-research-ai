"""DETAILED_RESEARCH_V1 Box 9: peer candidates, eligibility and the canonical peer set.

Candidates come only from the target's Box 1B Screener peer table (same Screener industry)
and are enriched by explicit, resolved competitor mentions. Eligibility uses the peer's own
Box 1B classification, Box 4 industry (regulated model) and Box 7 sector family - never
market cap, name similarity or a model's opinion. Excluded peers keep their reasons.
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid5

from stock_research_ai.application.business_registry import sector_family
from stock_research_ai.application.peer_registry import FINANCIAL, NS, RANKING_CRITERIA
from stock_research_ai.domain.business_intelligence import BusinessIntelligenceDataset
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import CanonicalFinancialDataset, Industry
from stock_research_ai.domain.industry_intelligence import (
    SELECTION_VERSION,
    ClassificationRef,
    Eligibility,
    PeerCandidate,
    PeerSet,
    PeerSource,
    PeerTableRef,
)
from stock_research_ai.domain.management_intelligence import ManagementIntelligenceDataset
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset


@dataclass(frozen=True)
class CompanyLayers:
    """One company's validated upstream datasets (any may be missing)."""

    company_key: str
    screener: DetailedScreenerDataset | None = None
    box4: CanonicalFinancialDataset | None = None
    box5: FinancialAnalyticsDataset | None = None
    box7: BusinessIntelligenceDataset | None = None
    box8: ManagementIntelligenceDataset | None = None

    @property
    def name(self) -> str | None:
        if self.screener is not None:
            return self.screener.profile.name
        return self.box4.company_name if self.box4 is not None else None

    def levels(self) -> dict[str, str]:
        if self.screener is None:
            return {}
        return {c.level: c.name for c in self.screener.profile.classification}

    def classification(self) -> tuple[ClassificationRef, ...]:
        if self.screener is None:
            return ()
        return tuple(
            ClassificationRef(level=c.level, name=c.name, url=str(c.url))
            for c in self.screener.profile.classification
        )

    def family(self) -> tuple[str | None, str]:
        """Box 7's family when Box 7 exists, else Box 7's own rule on Box 4 / Box 1B."""
        if self.box7 is not None:
            return self.box7.sector.value, "Box 7 sector family"
        names = list(self.levels().values())
        if self.box4 is not None:
            family, reason = sector_family(self.box4.industry, names)
            return family.value, reason
        if names:
            family, reason = sector_family(Industry.GENERAL, names)
            return family.value, reason + " (no Box 4 industry)"
        return None, "no classification"

    def symbol(self) -> str | None:
        if self.screener is None:
            return None
        return symbol_of(str(self.screener.profile.page_url))


def symbol_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = [p for p in url.split("/") if p]
    if "company" in parts and parts.index("company") + 1 < len(parts):
        return parts[parts.index("company") + 1]
    return None


@dataclass
class Selection:
    candidates: list[PeerCandidate] = field(default_factory=list)
    peer_set: PeerSet | None = None
    warnings: list[tuple[str, str]] = field(default_factory=list)


def _eligibility(
    target: CompanyLayers, peer: CompanyLayers | None
) -> tuple[Eligibility, list[str], list[str], str | None, str | None, str]:
    """(eligibility, selection reasons, exclusion reasons, industry, family, confidence)."""
    reasons = ["SCREENER_PEER_TABLE"]
    if peer is None or peer.screener is None:
        return Eligibility.UNKNOWN, reasons, ["PEER_PROFILE_UNAVAILABLE"], None, None, "LOW"
    mine, theirs = target.levels(), peer.levels()
    target_family, _ = target.family()
    family, _ = peer.family()
    industry = peer.box4.industry.value if peer.box4 is not None else None
    if not theirs:
        return (
            Eligibility.UNKNOWN,
            reasons,
            ["PEER_CLASSIFICATION_MISSING"],
            industry,
            family,
            ("LOW"),
        )
    if mine.get("Broad Sector") != theirs.get("Broad Sector"):
        return Eligibility.EXCLUDED, reasons, ["DIFFERENT_BROAD_SECTOR"], industry, family, "HIGH"
    if target_family is not None and family is not None and target_family != family:
        return Eligibility.EXCLUDED, reasons, ["SECTOR_FAMILY_DIFFERS"], industry, family, "HIGH"
    if (
        target.box4 is not None
        and peer.box4 is not None
        and FINANCIAL in (target_family, family)
        and target.box4.industry != peer.box4.industry
    ):
        # bank vs NBFC vs insurer: different regulated business models
        return (
            Eligibility.EXCLUDED,
            reasons,
            ["REGULATED_MODEL_DIFFERS"],
            industry,
            family,
            ("HIGH"),
        )
    reasons.append("SAME_SECTOR_FAMILY")
    limits: list[str] = []
    if mine.get("Industry") and mine.get("Industry") == theirs.get("Industry"):
        reasons.append("SAME_INDUSTRY")
    elif mine.get("Broad Industry") and mine.get("Broad Industry") == theirs.get("Broad Industry"):
        reasons.append("SAME_BROAD_INDUSTRY")
        limits.append("DIFFERENT_INDUSTRY")
    else:
        return (
            Eligibility.EXCLUDED,
            reasons,
            ["DIFFERENT_BROAD_INDUSTRY"],
            industry,
            family,
            ("HIGH"),
        )
    if peer.box4 is None:
        limits.append("NO_FINANCIAL_DATASET")
    if peer.box7 is not None and target.box7 is not None:
        reasons.append("BOX7_SAME_FAMILY")
    if limits:
        return Eligibility.INCLUDED_LIMITED, reasons, limits, industry, family, "MEDIUM"
    return Eligibility.INCLUDED, reasons, [], industry, family, "HIGH"


def select_peers(
    target: CompanyLayers,
    peers: dict[str, CompanyLayers],
    mentioned: dict[str, list[UUID]],
    max_peers: int,
    max_candidates: int,
) -> Selection:
    """`mentioned`: company key -> mention IDs of explicit, resolved competitor mentions."""
    out = Selection()
    table = target.screener.peers if target.screener is not None else None
    if table is None:
        status = target.screener.peer_status.status if target.screener else "NO_BOX1B"
        out.warnings.append(("PEER_TABLE_MISSING", f"Box 1B peer table: {status}"))
    rows = [] if table is None else list(enumerate(table.peers))
    seen: set[str] = set()
    for index, row in rows:
        if row.is_subject_company or row.screener_company_id is None:
            continue
        key = f"screener:{row.screener_company_id}"
        if key in seen or key == target.company_key:
            continue
        seen.add(key)
        assert table is not None
        if len(out.candidates) >= max_candidates:
            out.warnings.append(("RESOURCE_LIMIT_REACHED", f"peer candidates > {max_candidates}"))
            break
        layers = peers.get(key)
        eligibility, reasons, exclusions, industry, family, confidence = _eligibility(
            target, layers
        )
        sources = [PeerSource.SCREENER_PEER_TABLE]
        mention_ids = tuple(sorted(mentioned.get(key, ()), key=str))
        if mention_ids:
            sources.append(PeerSource.COMPETITOR_MENTION)
            reasons.append("NAMED_COMPETITOR")
        out.candidates.append(
            PeerCandidate(
                peer_candidate_id=uuid5(NS, f"candidate|{target.company_key}|{key}"),
                target_company_key=target.company_key,
                peer_company_key=key,
                peer_company_name=(layers.name if layers and layers.name else row.name),
                peer_symbol=symbol_of(str(row.company_url) if row.company_url else None),
                sources=tuple(sources),
                raw_label=row.name,
                source_classification=layers.classification() if layers else (),
                industry=industry,
                sector_family=family,
                eligibility=eligibility,
                selection_reasons=tuple(reasons),
                exclusion_reasons=tuple(exclusions),
                table_ref=PeerTableRef(
                    source_url=str(table.source_url),
                    content_sha256=table.content_sha256,
                    retrieved_at=table.retrieved_at,
                    row_index=index,
                    displayed_rank=row.rank,
                    displayed_name=row.name,
                    screener_company_id=row.screener_company_id,
                    company_url=str(row.company_url) if row.company_url else None,
                ),
                mention_ids=mention_ids,
                confidence=confidence,  # type: ignore[arg-type]
            )
        )

    def order(c: PeerCandidate) -> tuple[int, int, int, int, str]:
        assert c.table_ref is not None
        return (
            0 if c.eligibility == Eligibility.INCLUDED else 1,
            0 if "SAME_INDUSTRY" in c.selection_reasons else 1,
            0 if "NO_FINANCIAL_DATASET" not in c.exclusion_reasons else 1,
            c.table_ref.row_index,
            c.peer_company_key,
        )

    eligible = sorted(
        (
            c
            for c in out.candidates
            if c.eligibility in (Eligibility.INCLUDED, Eligibility.INCLUDED_LIMITED)
        ),
        key=order,
    )
    ranked = {c.peer_candidate_id: i + 1 for i, c in enumerate(eligible)}
    out.candidates = [
        c.model_copy(update={"selection_rank": ranked.get(c.peer_candidate_id)})
        for c in out.candidates
    ]
    canonical, beyond = eligible[:max_peers], eligible[max_peers:]
    if beyond:
        out.warnings.append(
            ("RESOURCE_LIMIT_REACHED", f"canonical peers: {len(eligible)} eligible > {max_peers}")
        )
    by_id = {c.peer_candidate_id: c for c in out.candidates}
    canonical_ids = [c.peer_candidate_id for c in canonical]
    out.peer_set = PeerSet(
        peer_set_id=uuid5(
            NS,
            f"peerset|{target.company_key}|{SELECTION_VERSION}|"
            + "|".join(str(i) for i in canonical_ids),
        ),
        target_company_key=target.company_key,
        methodology_version=SELECTION_VERSION,
        max_peers=max_peers,
        candidate_ids=tuple(c.peer_candidate_id for c in out.candidates),
        included_ids=tuple(
            i for i in canonical_ids if by_id[i].eligibility == Eligibility.INCLUDED
        ),
        limited_ids=tuple(
            i for i in canonical_ids if by_id[i].eligibility == Eligibility.INCLUDED_LIMITED
        ),
        excluded_ids=tuple(
            c.peer_candidate_id for c in out.candidates if c.eligibility == Eligibility.EXCLUDED
        ),
        unknown_ids=tuple(
            c.peer_candidate_id for c in out.candidates if c.eligibility == Eligibility.UNKNOWN
        ),
        beyond_limit_ids=tuple(c.peer_candidate_id for c in beyond),
        ranking_criteria=RANKING_CRITERIA,
    )
    return out
