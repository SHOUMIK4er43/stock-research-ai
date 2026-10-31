"""V1 RC1 (Tech Mahindra): a keyword is not evidence of a financial concept.

TECHM's transcripts produced a TARGET statement from 'deployed at Target Group', DEBT
statements from 'technical debt', and a PRICING statement plus an industry 'pricing signal:
increasing' from 'memory prices and chip prices are increasing' (an input cost the company
will not absorb, not its pricing power). Each pair below holds the adversarial sentence and a
genuine one, so the guard can neither leak nor over-block.
"""

import pytest

from stock_research_ai.application.management_calls import topics_of
from stock_research_ai.application.management_statements import classify
from stock_research_ai.application.semantic_guards import mask
from stock_research_ai.domain.management_intelligence import StatementType as ST

CASES = [
    (ST.TARGET,
     "We launched a Center of Excellence whose first solution, Collections Guru, is already "
     "deployed at Target Group.",
     "We remain on track to achieve our FY27 EBIT margin target of 15%."),
    (ST.TARGET, "Target Corporation renewed its contract with us.",
     "Target EBIT margin for the year stays at 15%."),
    (ST.TARGET, "Our target markets in Europe recovered.", "We are targeting 15% margins."),
    (ST.DEBT, "Benefits include a 30% to 35% reduction in technical debt.",
     "Our debt came down by 200 crore this quarter."),
    (ST.DEBT, "The programme retires tech debt across the estate.",
     "We expect to refinance borrowings next year."),
    (ST.PRICING,
     "As you know, memory prices and chip prices are increasing significantly, and we are not "
     "willing to guarantee those for the customer.",
     "Pricing in our renewal deals remained stable."),
    (ST.PRICING, "The share price fell after the announcement.",
     "We took price increases in the quarter."),
]


@pytest.mark.parametrize("kind,trap,genuine", CASES)
def test_statement_types_read_meaning_not_keywords(kind, trap, genuine):
    assert kind not in classify(trap)[0], trap
    assert kind in classify(genuine)[0], genuine


def test_topics_ignore_input_cost_prices():
    assert "PRICING" not in topics_of(CASES[5][1])
    assert "PRICING" in topics_of(CASES[5][2])


def test_masking_keeps_positions_and_quotes():
    text = CASES[3][1]
    assert len(mask(text)) == len(text) and "debt" not in mask(text)
    assert mask("Our debt fell.") == "Our debt fell."  # nothing to mask, nothing changed


def test_an_input_cost_price_is_not_an_industry_pricing_signal():
    from types import SimpleNamespace

    from stock_research_ai.application import industry_structure as ix
    from stock_research_ai.domain.industry_intelligence import Nature

    def unit(text: str) -> SimpleNamespace:
        return SimpleNamespace(text=text, nature=Nature.COMPANY_ASSERTION,
                               layer=SimpleNamespace(value="BOX8"),
                               record_id="r", locator="l", company_key="c", event_date=None,
                               period_text=None, ref=lambda: None)

    _demand, pricing = ix.market_signals([unit(CASES[5][1])])  # type: ignore[list-item]
    assert pricing == []
