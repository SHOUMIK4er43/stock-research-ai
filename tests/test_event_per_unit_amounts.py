"""V1 RC1 (live YESBANK): a per-share figure is not an event's value.

YES Bank's QIP filings ('equity shares of face value Rs 2 each') were reported as 'Fund raise
... stated value 0.000000 crore'; dividends took 'Rs. 9 per equity share' and allotments the
'face value of Rs 10/- each' as their amount. A per-unit price never becomes the stated value;
a real amount (an RBI penalty of Rs. 10,000) still does.
"""

from decimal import Decimal

import pytest

from stock_research_ai.application.corporate_events import stated_amounts


@pytest.mark.parametrize("text", [
    "Qualified institutions placement of equity shares of face value Rs 2 each",
    "allotted 1,02,500 equity shares of face value of Rs. 10/- each on August 22, 2015",
    "recommended the payment of final dividend @ Rs. 9 per equity share",
    "Dividend of Rs. 2/-per equity share i.e. 100%",
    "having nominal value of Rs. 10 /- per share of Valecha Engineering Ltd",
])
def test_a_per_share_figure_is_not_a_stated_amount(text):
    assert stated_amounts(text) == []


def test_a_real_amount_is_still_read():
    (penalty,) = stated_amounts("imposition of monetary penalty of Rs. 10,000 imposed by RBI")
    assert penalty.crore == Decimal(10000) / Decimal(10**7)
    (order,) = stated_amounts("received an order worth Rs. 1,250 crore from NHAI")
    assert order.crore == Decimal(1250)
