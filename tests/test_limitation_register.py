"""V1 RC1 (Tech Mahindra): one limitation register, one count definition.

TECHM's report had 57 limitation rows: eight anonymous 'Provider unavailable: blocked source
(BOX12)' (four identical KITE_HISTORICAL lines for different instruments and windows), every
unevaluated rule twice (a Box 13 rule-input item and the rule's own limitation), the full list
reprinted in several sections, and 'Open diligence items: 2' while 40 were open.
"""

import re

import pytest
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.data_quality import _endpoint
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_presentation import build_presentation


@pytest.fixture(scope="module")
def layers():
    return run_layers([(SCREENER, INFY)])


def test_every_entry_has_one_canonical_id(layers):
    entries = layers["report"].risks.data_limitations
    refs = [e.ref for e in entries]
    assert refs == [f"DL-{n:03d}" for n in range(1, len(entries) + 1)]


def test_no_rule_is_listed_twice_and_nothing_is_dropped(layers):
    report, d13 = layers["report"], layers["b13"]
    entries = report.risks.data_limitations
    rule_rows = [a for e in entries if e.kind == "RULE_INPUTS_UNAVAILABLE" for a in e.affects]
    own = [a for e in entries if e.layer == "BOX13" and e.kind != "RULE_INPUTS_UNAVAILABLE"
           for a in e.affects]
    assert not set(rule_rows) & set(own)
    accounted = {x for e in entries for x in (e.entry_id, *e.merged_item_ids)}
    assert {i.item_id for i in d13.data_quality_summary.items} <= accounted


def test_endpoints_identify_a_source_without_credentials():
    assert _endpoint("https://api.kite.trade/instruments/historical/1/day?from=a&to=b") == (
        "api.kite.trade/instruments/historical/1/day?from=a&to=b")
    assert _endpoint("https://user:secret@host.example/x") == "host.example/x"


def test_full_detail_once_and_ids_elsewhere(layers):
    md = render_markdown(build_presentation(layers["report"]))
    for e in layers["report"].risks.data_limitations:
        statement = next(c.statement for c in layers["report"].claims
                         if c.claim_id == e.claim_id)
        printed = [line for line in md.splitlines()
                   if statement.replace("_", "\\_")[:60] in line]
        # the register (Section 17), plus at most the read-first overview and the executive
        # summary - each labelled with the same ID; every other section cites the ID only
        assert len(printed) <= 3, (e.ref, len(printed))
        assert all(e.ref in line for line in printed), e.ref
    assert re.search(r"DL-\d{3}(, DL-\d{3})* — data limitations stated in full", md)


def test_open_diligence_count_is_the_total_with_priorities(layers):
    report = layers["report"]
    md = render_markdown(build_presentation(report))
    total = len(report.risks.open_diligence)
    assert f"| Open diligence items | {total} total (HIGH " in md
