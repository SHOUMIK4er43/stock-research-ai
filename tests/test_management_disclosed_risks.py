"""V1 RC1 (Tech Mahindra): management's own risk disclosures reach the Negatives view.

TECHM's management discussed client concentration, FX headwinds, large-deal margin dilution,
IT-spend caution, tariffs and a large US telecom client's volatility; the Negatives view held
only the registered rules. Such statements are now listed - verbatim, cited, ungraded - as
MANAGEMENT-DISCLOSED RISK / HEADWIND. An analyst's question, a denial ('it won't have a margin
headwind') or a keyword in another sense ('chip prices') never qualifies.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_business_classification_integrity import SCREENER, run_layers
from test_summary_input import INFY

from stock_research_ai.application.disclosed_risks import disclosed_risks
from stock_research_ai.domain.final_research_report import DisclosedRiskEntry


def box8(*quotes: tuple[str, str]):
    event = SimpleNamespace(event_id=uuid4(), call_date=None)
    statements = [SimpleNamespace(speaker_role=SimpleNamespace(value=role), quote=q,
                                  event_id=event.event_id, statement_id=uuid4())
                  for role, q in quotes]
    return SimpleNamespace(events=[event], statements=statements)


@pytest.mark.parametrize("role,quote,category", [
    ("MANAGEMENT", "There is clearly volatility in a large US telecoms client.", "GEOGRAPHIC"),
    ("MANAGEMENT", "We've been supported by the FX tailwind but there have been a lot of "
                   "headwinds as well.", "CURRENCY"),
    ("MANAGEMENT", "Clients exercise caution in their discretionary spend.",
     "DISCRETIONARY_SPENDING"),
    ("MANAGEMENT", "Large deals involve execution challenges that could dilute margins.",
     "LARGE_DEAL_EXECUTION"),
])
def test_a_management_risk_statement_is_listed_under_its_category(role, quote, category):
    (found,) = disclosed_risks(box8((role, quote)))
    assert found.category == category


@pytest.mark.parametrize("role,quote", [
    ("ANALYST", "Are you seeing pressure on margins from the large deals?"),
    ("MODERATOR", "Thank you. The next question is about currency headwinds."),
    ("MANAGEMENT", "No, it won't have a margin headwind."),
    ("MANAGEMENT", "We do not expect unreasonable seasonality or delays in Q3."),
    ("MANAGEMENT", "As you know, memory prices and chip prices are increasing."),
    ("MANAGEMENT", "Margins expanded by 290 basis points year on year."),
])
def test_questions_denials_and_other_senses_never_qualify(role, quote):
    assert disclosed_risks(box8((role, quote))) == []


def test_a_disclosed_risk_must_be_a_management_statement():
    report = run_layers([(SCREENER, INFY)])["report"]
    fact = next(c for c in report.claims if c.claim_type.value == "FACT")
    forged = report.risks.model_copy(update={"management_disclosed": (DisclosedRiskEntry(
        category="CURRENCY", label="Currency / FX", claim_id=fact.claim_id),)})
    with pytest.raises(ValidationError, match="management statement"):
        type(report).model_validate({**dict(report), "risks": forged})
