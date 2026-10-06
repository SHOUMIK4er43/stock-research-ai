"""Keep percentages and the separately displayed shareholder count distinct."""

from stock_research_ai.adapters.screener.html import Node
from stock_research_ai.adapters.screener.metrics import ParseContext, clean_label
from stock_research_ai.adapters.screener.statements import parse_statements
from stock_research_ai.domain.extraction import Section
from stock_research_ai.domain.shareholding import (
    HolderCategory,
    ShareholdingObservation,
    ShareholdingSnapshot,
)


def parse_shareholding(node: Node, context: ParseContext) -> ShareholdingSnapshot:
    records = parse_statements(node, Section.SHAREHOLDING, context)
    categories = {
        "Promoters": HolderCategory.PROMOTERS,
        "FIIs": HolderCategory.FII,
        "DIIs": HolderCategory.DII,
        "Government": HolderCategory.GOVERNMENT,
        "Public": HolderCategory.PUBLIC,
    }
    return ShareholdingSnapshot(
        holdings=tuple(
            ShareholdingObservation(
                category=categories.get(clean_label(r.original_label or ""), HolderCategory.OTHER),
                observation=r,
            )
            for r in records
            if r.metric_id != "shareholder_count"
        ),
        shareholder_counts=tuple(r for r in records if r.metric_id == "shareholder_count"),
    )
