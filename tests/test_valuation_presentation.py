"""V1 RC1 (Tech Mahindra): valuation shows what exists, what is missing, and no noise.

TECHM's valuation section printed 15 identical 'unavailable' method rows and a second 15-row
table of 'Data unavailable', and the reason text said 'cash and debt inputs ... are
unavailable' whatever was actually missing.
"""

import pytest
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import (
    REASON_TEXT,
    build_presentation,
)


@pytest.fixture(scope="module")
def report():
    return run_layers([(SCREENER, INFY)])["report"]


def section(md: str) -> str:
    return md[md.index("## 16. Valuation"):md.index("## 17. ")]


def test_reason_texts_name_only_what_is_missing():
    assert "debt" not in REASON_TEXT["CASH_UNAVAILABLE"].split("(")[0]
    assert "cash" not in REASON_TEXT["DEBT_UNAVAILABLE"]


def test_no_value_means_one_concise_statement(report):
    methods = report.valuation.methods
    assert methods and all(m.status == "UNAVAILABLE" for m in methods)
    text = section(render_markdown(build_presentation(report)))
    assert "value produced: none" in text
    assert "Equity-bridge inputs (what exists, what is missing)" in text
    assert "Valuation by method and scenario" not in text  # the all-unavailable table
    distinct = {m.method for m in methods}
    rows = [line for line in text.splitlines() if line.startswith("| ")
            and line.split(" | ")[0].strip("| ").replace("\\_", "_") in distinct]
    assert len(rows) <= len(methods) and len({r.split(" | ")[0] for r in rows}) == len(distinct)
    assert text.count("Data unavailable: BASE ") == 0  # per-scenario lines summarised
    for m in methods:
        assert m.method.replace("_", "\\_") in text
