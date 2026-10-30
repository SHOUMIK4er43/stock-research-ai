"""V1 RC1 (Tech Mahindra): KPIs management states on calls become structured, cited evidence.

TECHM's calls stated deal wins (US$1,078 million), DSO (84 -> 88 days), free cash flow (115 %
of PAT), the company's own EBIT margin (14.4 % Q1 FY27, 14.8 % Q2 FY27) and constant-currency
growth; none reached the report as data. Nothing is inferred: no unit -> no KPI; a missing
currency is stated as not stated; the period is the sentence's own words or 'not stated'.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from stock_research_ai.application.transcript_kpis import stated_kpis


def box8(*quotes, role="MANAGEMENT", forward=False):
    event = uuid4()
    return SimpleNamespace(statements=[SimpleNamespace(
        speaker_role=SimpleNamespace(value=role), quote=q, event_id=event,
        statement_id=uuid4(), forward_looking=forward) for q in quotes])


@pytest.mark.parametrize("quote,metric,value,unit,period", [
    ("Our total deal wins for the quarter stood at US$1,078 million, up 33.3% Y-o-Y.",
     "DEAL_WINS_TCV", "1078", "USD MILLION", "for the quarter"),
    ("EBIT for the quarter stood at US$250 million with an EBIT margin of 14.8%.",
     "EBIT_MARGIN", "14.8", "PERCENT", "for the quarter"),
    ("DSO stands at 88 days, up four days Q-o-Q.", "DSO", "88", "DAYS", None),
    ("Free cash flow for the year stood at 616 million, which is 115% of our reported PAT.",
     "FREE_CASH_FLOW", "616", "MILLION (currency not stated)", "for the year"),
    ("Free cash flow for the year stood at 616 million, which is 115% of our reported PAT.",
     "FCF_TO_PAT", "115", "PERCENT", "for the year"),
    ("On a last-12-month basis, deal wins stand at US$4,270 million.",
     "DEAL_WINS_TCV", "4270", "USD MILLION", "last-12-month"),
])
def test_explicit_kpis_keep_value_unit_and_stated_period(quote, metric, value, unit, period):
    found = [k for k in stated_kpis(box8(quote)) if k.metric == metric]
    assert len(found) == 1
    k = found[0]
    assert (str(k.value), k.unit, k.period_text) == (value, unit, period)
    assert k.statement.quote == quote  # provenance: the verbatim statement


@pytest.mark.parametrize("quote,role,forward", [
    ("What was the DSO this quarter, 88 days?", "ANALYST", False),
    ("We aim to achieve an EBIT margin of 15% in FY27.", "MANAGEMENT", True),  # guidance
    ("This is our 12th consecutive quarter of deal wins over $1 billion.", "MANAGEMENT", False),
    ("Deal wins were strong this quarter.", "MANAGEMENT", False),  # no number, no unit
    ("We reduced technical debt by 30% for the client.", "MANAGEMENT", False),
])
def test_questions_guidance_bounds_and_unitless_numbers_are_not_kpis(quote, role, forward):
    assert stated_kpis(box8(quote, role=role, forward=forward)) == []


def test_one_figure_restated_in_a_call_is_one_kpi_with_its_currency():
    kpis = stated_kpis(box8("Our total deal wins in the quarter stood at 1,023 million.",
                            "Coming to deal wins, our total deal wins were US$1.023 billion."))
    assert [(k.value, k.unit) for k in kpis] == [(k.value, "USD BILLION") for k in kpis]
    assert len(kpis) == 1
