"""Read the Screener 'Broad Industry' breadcrumb from the SAME already-acquired,
validated Screener artifact used for extraction. No second web fetch; no change to
CompanyExtractionResult/SourceCompanyIdentity or any frozen normalizer output - this
module only re-parses bytes the pipeline already retrieved and persisted.

`ScreenerBroadIndustrySource` implements application.ports.BroadIndustrySource so
application.sector_resolution can consume it without ever importing adapters.* directly
(the frozen layering rule enforced by tests/test_engine.py's core-import-boundary test).
"""

from stock_research_ai.adapters.screener.html import Page
from stock_research_ai.domain.provenance import SourceLocation

_BROAD_INDUSTRY_TITLE = "Broad Industry"


def extract_broad_industry(content: bytes, encoding: str) -> tuple[str, SourceLocation] | None:
    """The exact observed 'Broad Industry' label text and its source location, or None
    if the page has no peers/industry breadcrumb (e.g. gated/loading placeholder, or a
    page shape without a peers section)."""
    try:
        root = Page(content.decode(encoding, errors="strict")).root
    except (UnicodeError, LookupError):
        return None
    peers = root.by_id("peers")
    if peers is None:
        return None
    for anchor in peers.all("a"):
        if anchor.attrs.get("title") == _BROAD_INDUSTRY_TITLE and anchor.text:
            return anchor.text, anchor.location("SECTOR_CLASSIFICATION")
    return None


class ScreenerBroadIndustrySource:
    """Stateless adapter object conforming to application.ports.BroadIndustrySource."""

    def extract(self, content: bytes, encoding: str) -> tuple[str, SourceLocation] | None:
        return extract_broad_industry(content, encoding)
