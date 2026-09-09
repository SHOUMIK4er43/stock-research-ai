"""Exact extractor identities only. Candidate recognition never establishes a mapping."""

from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.sector_kpis import SemanticMappingDefinition, SemanticResolution

# Each meaning intentionally remains source-reported; no regulatory reinterpretation.
_NAMES = {
    "deposits": ("Deposits", "DEPOSITS"),
    "total_assets": ("Total Assets", "TOTAL_ASSETS"),
    "net_profit": ("Net Profit", "NET_PROFIT"),
    "revenue": ("Revenue", "SOURCE_REVENUE"),
    "sales": ("Sales", "SALES"),
    "operating_profit": ("Operating Profit", "OPERATING_PROFIT"),
    "eps": ("EPS in Rs", "EPS"),
}
MAPPINGS = tuple(
    SemanticMappingDefinition(
        mapping_id=f"screener:{section}:{metric}",
        provider="screener",
        section=section,
        source_metric_id=metric,
        source_identity=f"source:{label}",
        source_definition=f"Source-displayed {label}; no calculation performed.",
        canonical_semantic=canonical,
        meaning=f"Source-reported {label}; no upgraded economic meaning.",
    )
    for metric, (label, canonical) in _NAMES.items()
    for section in (
        ("balance_sheet",)
        if metric in {"deposits", "total_assets"}
        else ("profit_loss", "quarterly")
    )
)


class ScreenerSemanticResolver:
    def __init__(self, mappings: tuple[SemanticMappingDefinition, ...] = MAPPINGS) -> None:
        self.mappings = mappings

    def resolve(
        self, source: CompanyExtractionResult, semantics: tuple[str, ...]
    ) -> tuple[SemanticResolution, ...]:
        resolved = []
        for section in ("profit_loss", "quarterly", "balance_sheet"):
            for o in getattr(source, section):
                matches = [
                    m
                    for m in self.mappings
                    if m.canonical_semantic in semantics
                    and m.section == section
                    and (m.source_metric_id == o.metric_id or m.source_identity == o.metric)
                ]
                # Candidate recognition can refuse a mapping, never establish one.
                candidate = "NIM" if o.metric == "source:NIM" and "NIM" in semantics else None
                if not matches and candidate:
                    resolved.append(
                        SemanticResolution(
                            observation_id=o.id,
                            provider=source.metadata.provider,
                            section=section,
                            source_metric_id=o.metric_id,
                            source_identity=o.metric,
                            canonical_semantic=candidate,
                            status="UNVERIFIED",
                            definition=None,
                            provenance=o.provenance,
                        )
                    )
                for m in matches:
                    definitions = [
                        d for d in source.metric_definitions if d.id == m.source_metric_id
                    ]
                    verified = (
                        source.metadata.provider == m.provider
                        and o.mapping_status == "mapped"
                        and o.metric_id == m.source_metric_id
                        and o.metric == m.source_identity
                        and o.provenance.source_location.section == section.upper()
                        and source.metadata.mapping_version == m.extractor_mapping_version
                        and len(definitions) == 1
                        and definitions[0].version == m.extractor_mapping_version
                        and definitions[0].definition == m.source_definition
                    )
                    resolved.append(
                        SemanticResolution(
                            observation_id=o.id,
                            provider=source.metadata.provider,
                            section=section,
                            source_metric_id=o.metric_id,
                            source_identity=o.metric,
                            canonical_semantic=m.canonical_semantic,
                            status="VERIFIED" if verified else "UNVERIFIED",
                            definition=m,
                            provenance=o.provenance,
                        )
                    )
        return tuple(resolved)
