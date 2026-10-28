"""V1 RC1 (Tech Mahindra): source attribution and entity binding.

A peer's Screener 'key points' text (Persistent Systems) entered the subject company's
Industry section as '[Fact] Industry structure (technology change)', registered as a FILING;
and the subject's own Screener profile text read 'Business description disclosed by the
company'. A peer source never populates a subject fact, and aggregator text is labelled as
aggregator text.
"""

from uuid import UUID

import pytest
from test_business_classification_integrity import HUDCO, NOW, run_layers

from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import (
    FinalReportRequest,
    SectionId,
    SourceType,
)
from stock_research_ai.domain.industry_intelligence import (
    EvidenceRef,
    IndustryStructureObservation,
    Nature,
    SourceLayer,
)

PEER_TEXT = "The company is a global digital engineering provider specialising in AI-led work."
OWN_TEXT = "Housing finance demand depends on the government's urban infrastructure outlay."


def _observation(company_key: str, text: str, n: int) -> IndustryStructureObservation:
    return IndustryStructureObservation(
        observation_id=UUID(int=n), topic="TECHNOLOGY_CHANGE", company_key=company_key,
        statement=text, nature=Nature.AGGREGATOR_DATA, authority=None, document_type=None,
        event_date=None, period_text=None,
        source=EvidenceRef(company_key=company_key, layer=SourceLayer.BOX7_EVIDENCE,
                           record_id=UUID(int=n + 100), dataset_id=None,
                           source_url=f"https://www.screener.in/company/{n}/consolidated/",
                           locator="profile.key_points/s0"))


@pytest.fixture(scope="module")
def report():
    layers = run_layers(HUDCO)
    b4, b9 = layers["b4"], layers["b9"]
    b9 = b9.model_copy(update={"structure_observations": (
        _observation("screener:PEER", PEER_TEXT, 1), _observation(b4.company_key, OWN_TEXT, 2))})
    b13 = build_due_diligence(b4, layers["b5"], request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=b4.company_key), generated_at=NOW, box7=layers["b7"],
        box9=b9, box10=layers["b10"], box11=layers["b11"])
    return build_final_report(
        request=FinalReportRequest(as_of=NOW.date(), company_key=b4.company_key),
        generated_at=NOW, box4=b4, box5=layers["b5"], box13=b13, box7=layers["b7"],
        box9=b9, box10=layers["b10"], box11=layers["b11"])


def test_a_peer_text_never_becomes_a_subject_fact(report):
    texts = [c.source_text or "" for c in report.claims]
    assert PEER_TEXT not in texts
    own = [c for c in report.claims if c.source_text == OWN_TEXT]
    assert len(own) == 1
    assert SectionId.INDUSTRY_COMPETITION in own[0].section_ids


def test_aggregator_text_is_registered_as_an_aggregator_record(report):
    own = next(c for c in report.claims if c.source_text == OWN_TEXT)
    sources = {s.source_id: s for s in report.sources}
    assert {sources[i].source_type for i in own.source_ids} == {SourceType.PROVIDER_RECORD}
    assert "aggregator" in own.statement.lower()


def test_screener_profile_text_is_never_disclosed_by_the_company(report):
    profile = [c for c in report.claims if c.topic.startswith("BUSINESS_")]
    assert profile
    assert not [c for c in profile if "disclosed by the company" in c.statement]
