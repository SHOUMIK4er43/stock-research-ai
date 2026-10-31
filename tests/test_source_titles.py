"""V1 RC1 (Tech Mahindra): source titles a reader can use.

TECHM's register titled a call transcript '8248031f1164/p7/block92/s23' (a Box 3 locator)
and 21 shareholding filings by their raw URL; peers appeared as 'screener:1297'. The opaque
identifiers stay as the location / provenance; the title is readable.
"""

import pytest
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.report_claims import _opaque, url_title


@pytest.mark.parametrize("title,opaque", [
    ("8248031f1164/p7/block92/s23", True),
    ("https://nsearchives.nseindia.com/corporate/xbrl/SHP_1489460_21072025064930_WEB.xml", True),
    ("screener:1297", True),
    ("Q2 FY27 Earnings Conference Call (15-Oct-2026)", False),
    ("Annual Report", False),
])
def test_locators_and_bare_urls_are_not_titles(title, opaque):
    assert _opaque(title) is opaque


def test_url_titles_are_readable():
    assert url_title("https://nsearchives.nseindia.com/corporate/xbrl/"
                     "SHP_1489460_21072025064930_WEB.xml") == (
        "NSE shareholding pattern filing (XBRL), filed 21-Jul-2025")
    assert url_title("https://www.careratings.com/upload/x.pdf") == (
        "Credit rating document (www.careratings.com)")
    assert url_title("https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=a.pdf") == (
        "BSE announcement attachment (PDF)")
    assert url_title(None) is None


def test_the_register_has_no_opaque_title():
    report = run_layers([(SCREENER, INFY)])["report"]
    for s in report.sources:
        assert not _opaque(s.title) or s.title.startswith(("BOX", "BSE ", "NSE ", "KITE ")), (
            s.title)
