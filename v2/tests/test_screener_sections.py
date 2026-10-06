import re

from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import ExtractionRequest


async def test_peers_fragment_and_relative_document_urls(fixture_dir, tmp_path):
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    fragment = (fixture_dir / "peers_fragment.html").read_text()
    html = re.sub(r'<section id="peers".*?</section>', fragment, html, flags=re.S)
    html = html.replace(
        '<h2 class="margin-0">Documents</h2>',
        '<h2 class="margin-0">Documents</h2><a href="../report.pdf?x=1&amp;y=2#page=4">Report</a>',
    )
    path = tmp_path / "links.html"
    path.write_text(html)
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    result = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/OLD/",
            replay_path=path,
            final_url="https://www.screener.in/company/YESBANK/consolidated/",
        ),
        tmp_path / "out",
    )
    peer = result.peers.entries[0]
    assert peer.name == "Example Bank" and peer.company_id is None
    assert peer.link.original_href == "/company/EXAMPLE/"
    assert peer.link.resolved_url == "https://www.screener.in/company/EXAMPLE/"
    assert str(peer.metrics[0].parsed.value) == "12.3"
    doc = next(d for d in result.documents if d.title == "Report")
    assert doc.link.original_href == "../report.pdf?x=1&y=2#page=4"
    assert (
        doc.link.resolved_url == "https://www.screener.in/company/YESBANK/report.pdf?x=1&y=2#page=4"
    )
    assert doc.link.source_location.locator.startswith("#documents")
    assert result.coverage.overall == "COMPLETE"
