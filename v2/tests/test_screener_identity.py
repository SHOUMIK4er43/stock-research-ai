import re

from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import ExtractionRequest


async def test_url_slug_is_not_an_exchange_identifier(fixture_dir, tmp_path):
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    html = re.sub(r"(NSE|BSE):\s*([\w.-]+)", "Exchange link", html)
    html = html.replace('data-consolidated="true"', "")
    html = html.replace("Consolidated Figures", "Figures")
    path = tmp_path / "unidentified.html"
    path.write_text(html)
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    result = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/YESBANK/consolidated/",
            replay_path=path,
        ),
        tmp_path / "out",
    )
    assert result.company.listings == ()
    assert result.company.reporting_scope == "unknown"
    assert result.company.source_company_key == "3783"
    assert result.coverage.sections[0].parse_status == "PARTIAL"
