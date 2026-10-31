"""A report claim states its period WITH the period type.

Forensic audit (2026-10-22): a quarter and the fiscal year ending the same month share the
label 'Mar 2026'. Claim statements read 'REVENUE reported 3563 INR CRORES for Mar 2026
(consolidated)' (Q4) next to '... 13150 ... for Mar 2026 (consolidated)' (FY2026), so the
report's claim lists showed two different revenues for one apparent period. The statement now
names the period type.

V1 RC1 (2026-10-28): the period is named canonically in the statement AND the structured
field - FY2026, Q4 FY2026, a balance sheet at 31-Mar-2026 - never the source's ambiguous
'Mar 2026' (or a filing header such as 'year ended March 31 2026 March 31 2026').
"""

import pytest
from test_business_classification_integrity import HUDCO, run_layers


@pytest.fixture(scope="module")
def report():
    return run_layers(HUDCO)["report"]


def test_quarter_and_fiscal_year_claims_are_distinguishable(report):
    revenue = [c for c in report.claims if c.metric == "REVENUE" and str(c.period_end) ==
               "2026-03-31" and c.basis.value == "CONSOLIDATED" and c.claim_type.value == "FACT"]
    by_value = {str(c.value): c for c in revenue}
    assert by_value["13150"].statement == "REVENUE reported ₹13,150.00 Cr for FY2026 " \
        "(consolidated)."
    assert by_value["3563"].statement == "REVENUE reported ₹3,563.00 Cr for Q4 FY2026 " \
        "(consolidated)."
    assert (by_value["13150"].period, by_value["3563"].period) == ("FY2026", "Q4 FY2026")


def test_no_two_fact_values_share_one_statement_context(report):
    seen: dict[str, str] = {}
    for c in report.claims:
        if c.claim_type.value != "FACT" or c.value is None or c.metric is None:
            continue
        context = c.statement.replace(str(c.value), "{value}", 1)
        assert seen.setdefault(context, str(c.value)) == str(c.value), context


def test_balance_sheet_facts_read_as_at(report):
    assets = [c for c in report.claims if c.metric == "TOTAL_ASSETS" and c.period == "31-Mar-2026"
              and c.basis.value == "CONSOLIDATED"]
    assert assets and all("as at 31-Mar-2026" in c.statement for c in assets)
