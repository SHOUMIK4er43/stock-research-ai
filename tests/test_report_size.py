"""V1 RC1 (Tech Mahindra): exhaustive appendices never drown the readable report.

Once TECHM's 1,703 exchange events were bound (07b0e5d), the PDF grew from ~120 to 287 pages:
every event row and each of 1,463 sources was printed. Human formats now show the first rows of
an exhaustive appendix and group sources cited only by appendix rows; the JSON keeps all.
"""

from stock_research_ai.application.report_presentation import (
    DISPLAY_APPENDIX_ROWS,
    _ref_ranges,
)


def test_reference_ranges_collapse_consecutive_numbers():
    assert _ref_ranges(["S48", "S49", "S50", "S60", "S62", "S63"]) == "S48-S50, S60, S62-S63"
    assert _ref_ranges([]) == ""


def test_a_bounded_number_of_appendix_rows_is_shown():
    assert 50 <= DISPLAY_APPENDIX_ROWS <= 500
