"""V1 RC1 (live TECHM): a segment note's line items are not segments.

Tech Mahindra prints its segments across the columns ('IT BPS Total' per year) and the line
items down the rows (Revenue from Operations / Direct Expenses / Segmental Results). The
report stated 'Segment Direct Expenses: revenue 38250.2 INR CRORES' and a 'segment revenue
mix' of 80 % / 20 % between two line items - a fabricated segment split. A line item is never
a segment, and an unresolved column header leaves the table unmapped instead.
"""

from test_business_intelligence import ar, business

from stock_research_ai.application.business_registry import segment_metric_heading
from stock_research_ai.domain.business_intelligence import SegmentMetric

HEAD = [
    "Notes to the Consolidated Financial Statements",
    "Information on reportable segments for the year ended March 31, 2026 along with "
    "comparatives is given below:",
    "A. Business Segments",
    "(Rs. in crore)",
    "Particulars For the year ended",
    "March 31, 2026 March 31, 2025",
    "IT BPS Total IT BPS Total",
]
REVENUE = "Revenue from Operations 477,670 90,484 568,154 444,767 85,116 529,883"
EXPENSES = "Direct Expenses 382,502 75,494 457,996 362,058 73,193 435,251"
RESULTS = "Segmental Results 95,168 14,990 110,158 82,709 11,923 94,632"
LINE_ITEMS = {"direct expenses", "segmental results", "revenue from operations"}


def test_segmental_results_is_a_segment_result_heading():
    assert segment_metric_heading("Segmental Results") is SegmentMetric.SEGMENT_RESULT


async def test_line_items_across_segment_columns_are_never_segments(tmp_path):
    for n, rows in enumerate(([REVENUE, EXPENSES, RESULTS], [REVENUE, EXPENSES])):
        data, _b, _x = await business(tmp_path / str(n), {"/ar.pdf": ar([*HEAD, *rows])})
        named = {v.segment_key for v in data.canonical_segment_values}
        assert not named & LINE_ITEMS, (n, named)
        assert not {o.segment_key for o in data.segment_observations} & LINE_ITEMS
