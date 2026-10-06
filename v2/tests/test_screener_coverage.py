import re

import pytest
from pydantic import ValidationError

from stock_research_ai.bootstrap import extract_snapshot, initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.extraction import CoverageReport, ExtractionRequest, SectionCoverage


@pytest.mark.parametrize(
    "mutation,section,code",
    [
        ("missing", "CASH_FLOW", "MISSING_SECTION"),
        ("unknown", "QUARTERLY", "UNMAPPED_METRIC"),
        ("malformed", "QUARTERLY", "MALFORMED_TABLE"),
        ("loading", "QUARTERLY", "SOURCE_UNAVAILABLE"),
    ],
)
async def test_layout_drift_through_pipeline(mutation, section, code, fixture_dir, tmp_path):
    html = (fixture_dir / "yesbank_consolidated.html").read_text()
    if mutation == "missing":
        html = re.sub(r'<section id="cash-flow".*?</section>', "", html, flags=re.S)
    elif mutation == "unknown":
        html = html.replace("Financing Profit", "Uncatalogued Source Measure", 1)
    elif mutation == "malformed":
        html = re.sub(r"<td[^>]*>\s*6,445\s*</td>", "", html, count=1)
    else:
        html = re.sub(
            r'<section id="quarters".*?</section>',
            '<section id="quarters">Loading results table ...</section>',
            html,
            flags=re.S,
        )
    path = tmp_path / "mutated.html"
    path.write_text(html)
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "evidence")
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
    coverage = next(c for c in result.coverage.sections if c.section == section)
    assert code in {i.code for i in coverage.issues}
    if mutation == "unknown":
        record = next(
            r for r in result.quarterly if r.original_label == "Uncatalogued Source Measure"
        )
        assert record.value == -730
        assert record.original_value.strip() == "-730"
        assert record.mapping_status == "unmapped" and record.metric_id is None
        assert (
            coverage.parse_status == "SUCCESS"
        )  # Coverage succeeds with an explicit mapping warning.
    else:
        assert coverage.parse_status != "SUCCESS"
    assert result.coverage.overall == "PARTIAL"


def test_coverage_cannot_claim_success_for_empty_arrays():
    with pytest.raises(ValidationError, match="nonempty"):
        SectionCoverage(section="PEERS", presence="FOUND", parse_status="SUCCESS", records=0)
    with pytest.raises(ValidationError, match="every expected"):
        CoverageReport(overall="PARTIAL", sections=())
