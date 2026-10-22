"""A report claim states its period WITH the period type.

Forensic audit (2026-10-22): a quarter and the fiscal year ending the same month share the
label 'Mar 2026'. Claim statements read 'REVENUE reported 3563 INR CRORES for Mar 2026
(consolidated)' (Q4) next to '... 13150 ... for Mar 2026 (consolidated)' (FY2026), so the
report's claim lists showed two different revenues for one apparent period. The statement now
names the period type; the structured `period` field is unchanged.
"""

import pytest
from test_business_classification_integrity import HUDCO, run_layers


@pytest.fixture(scope="module")
def report():
    return run_layers(HUDCO)["report"]


def test_quarter_and_fiscal_year_claims_are_distinguishable(report):
    revenue = [c for c in report.claims if c.metric == "REVENUE" and c.period == "Mar 2026"
               and c.basis.value == "CONSOLIDATED" and c.claim_type.value == "FACT"]
    by_value = {str(c.value): c.statement for c in revenue}
    assert by_value["13150"] == ("REVENUE reported 13150 INR CRORES for the fiscal year ended "
                                 "Mar 2026 (consolidated).")
    assert by_value["3563"] == ("REVENUE reported 3563 INR CRORES for the quarter ended "
                                "Mar 2026 (consolidated).")
    assert {c.period for c in revenue} == {"Mar 2026"}  # the structured field is unchanged


def test_no_two_fact_values_share_one_statement_context(report):
    seen: dict[str, str] = {}
    for c in report.claims:
        if c.claim_type.value != "FACT" or c.value is None or c.metric is None:
            continue
        context = c.statement.replace(str(c.value), "{value}", 1)
        assert seen.setdefault(context, str(c.value)) == str(c.value), context


def test_balance_sheet_facts_read_as_at(report):
    assets = [c for c in report.claims if c.metric == "TOTAL_ASSETS" and c.period == "Mar 2026"
              and c.basis.value == "CONSOLIDATED"]
    assert assets and all("as at Mar 2026" in c.statement for c in assets)
