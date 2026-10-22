"""Governance evidence integrity: a governance conclusion exists only on identifiable evidence.

Box 3 (an issuer-verified annual report) -> the statutory auditor's report -> AuditorReport ->
the registered AUDITOR_SIGNAL rule -> the Box 13 governance evidence ledger -> completeness and
the report. No score; 'no adverse evidence in reviewed source' is never good governance;
missing evidence is never green. Filings are synthetic PDFs run through the real Box 1-3
pipeline; HUDCO / INFY reproduce the headings of their FY2026 auditor's reports.

  A auditor named -> fact only          N no regulatory source -> not evaluable
  B auditor resignation -> evaluable     O capital issuance -> factual only
  C no auditor source -> not evaluable   P no evidence -> governance UNAVAILABLE
  D qualified opinion -> flag            Q partial -> PARTIAL     R all evaluated -> COMPLETE
  E clean opinion -> no flag             S missing evidence != green
  F related-party disclosure -> fact     T one event, one flag
  G material related party -> flag       U HUDCO   V INFY   W YESBANK
  H promoter holding -> no flag          X verifier rejects unsupported conclusions
  I explicit pledge -> evaluable         Y bounded governance table renders
  J no pledge data != zero pledge        Z completeness ledger follows the governance ledger
  K resignation recorded  L routine appointment -> no flag  M regulatory action -> evaluable
"""

import asyncio
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import test_business_intelligence as tbi
from pydantic import ValidationError
from test_due_diligence import AS_OF, NOW, SRC, Q, pledge, run, sig
from test_summary_input import YESBANK

from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.governance_evidence import (
    auditor_reports_from_bundle,
    verify_governance_provenance,
)
from stock_research_ai.application.research_completeness import governance_block
from stock_research_ai.domain import due_diligence as dd
from stock_research_ai.domain.due_diligence import (
    DueDiligenceRequest,
    GovernanceEvidenceStatus,
    GovernanceRiskStatus,
    GovernanceTopic,
)
from stock_research_ai.domain.final_research_report import FinalReportRequest, SectionStatus

E, R = GovernanceEvidenceStatus, GovernanceRiskStatus
SF = Path(__file__).parent / "fixtures" / "screener"


def report_pages(company: str, *, opinion: list[str], extra: list[str] = (),
                 firm: str = "S A R C & Associates", basis: str = "Standalone") -> list[list[str]]:
    """An auditor's report as Indian annual reports print it (SA 700 headings)."""
    return [
        ["INDEPENDENT AUDITOR'S REPORT", f"TO THE MEMBERS OF {company.upper()}",
         f"Report on the Audit of the {basis} Financial Statements", *opinion, *extra],
        ["4. Key Audit Matters:", "Key audit matters are those matters of most significance;",
         "we do not provide a separate opinion on these matters.", f"For {firm}",
         "Chartered Accountants", "Firm Registration No. 006085N"],
    ]


CLEAN = ["1. Opinion:", "In our opinion and to the best of our information the aforesaid",
         "financial statements give a true and fair view of the state of affairs as at",
         "31st March, 2026.", "2. Basis for Opinion:",
         "We conducted our audit in accordance with the Standards on Auditing."]
QUALIFIED = ["1. Qualified Opinion", "In our opinion, except for the effects of the matter",
             "described below, the statements give a true and fair view as at 31st March, 2026.",
             "2. Basis for Qualified Opinion", "Receivables of Rs 120 crore were not confirmed."]
EMPHASIS = ["3. Emphasis of Matter", "We draw attention to Note 39(B) to the financial",
            "statements. Our opinion is not modified in respect of this matter."]


def bundle(monkeypatch, key: str, name: str, *pages: list[str], verified: bool = True):
    company = tbi.COMPANY.model_copy(update={"company_key": key, "name": name})
    monkeypatch.setattr(tbi, "COMPANY", company)
    front = ["Annual Report 2025-26"]

    async def go():
        with tempfile.TemporaryDirectory() as t:
            return (await tbi.filings(Path(t), {"/ar.pdf": tbi.ar(front, *pages)}))[0]
    b = asyncio.run(go())
    if not verified:  # the printed issuer is another company: never this company's evidence
        b = b.model_copy(update={"extractions": tuple(
            x.model_copy(update={"issuer_binding_status": "UNVERIFIED"}) for x in b.extractions)})
    return b


def topic(data, code):
    return next(t for t in data.governance_risks.topics if t.topic == code)


def key_of():
    data = run()
    return data.company_key


# --- A-E: the statutory auditor's report ---------------------------------------------------------


def test_a_e_clean_opinion_is_a_fact_and_no_flag_never_good_governance(monkeypatch):
    b = bundle(monkeypatch, key_of(), "Widget Co Ltd", *report_pages("Widget Co Limited",
                                                                     opinion=CLEAN))
    (r,) = auditor_reports_from_bundle(b)
    assert (r.fiscal_year, r.basis, r.opinion.value, r.auditor) == (
        2026, "STANDALONE", "UNMODIFIED", "S A R C & Associates")
    assert r.source.evidence_ids and r.source.document_id and r.source.page == 2
    data = run(bundle=b)
    s = sig(data, "AUDITOR_SIGNAL")
    assert (s.status.value, s.severity.value) == ("EVALUATED", "GREEN")
    t = topic(data, "AUDITOR_OPINION")
    assert (t.evidence_status, t.risk_status) == (E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE,
                                                  R.EVALUATED_NO_FLAG)  # E
    assert "auditor S A R C & Associates" in t.finding  # A: the fact, with its evidence
    assert set(r.source.evidence_ids) <= set(t.evidence_ids)
    for word in ("good", "strong", "weak", "score", "rating"):
        assert word not in t.finding.lower()


def test_key_audit_matters_contents_pages_and_unverified_issuers_never_make_a_report(
        monkeypatch):
    kam_only = [["INDEPENDENT AUDITOR'S REPORT", "Report on the Audit of the Standalone "
                 "Financial Statements", "Key Audit Matters", "Qualified Opinion matters of "
                 "most significance are discussed below."]]  # no Opinion heading, no year
    assert auditor_reports_from_bundle(bundle(monkeypatch, key_of(), "Widget Co Ltd",
                                              *kam_only)) == ()
    other = bundle(monkeypatch, key_of(), "Widget Co Ltd", *report_pages(
        "Widget Co Limited", opinion=QUALIFIED), verified=False)
    assert auditor_reports_from_bundle(other) == ()


def test_b_d_qualified_opinion_and_auditor_resignation_are_flags(monkeypatch):
    q = run(bundle=bundle(monkeypatch, key_of(), "Widget Co Ltd", *report_pages(
        "Widget Co Limited", opinion=QUALIFIED)))
    s = sig(q, "AUDITOR_SIGNAL")
    assert s.severity.value == "RED" and "qualified opinion" in s.explanation  # D
    assert topic(q, "AUDITOR_OPINION").risk_status == R.FLAGGED
    resign = dd.AuditorChange(change_date=date(2025, 9, 1), reason="RESIGNATION", mid_term=True,
                              concerns_unresolved=True, source=SRC)
    b = run(auditor_changes=(resign,), auditor_reports=(dd.AuditorReport(
        fiscal_year=2026, basis="CONSOLIDATED", auditor="X & Co", opinion="UNMODIFIED",
        source=SRC),))
    assert sig(b, "AUDITOR_SIGNAL").severity.value == "RED"  # B: evaluable, registered RED
    assert topic(b, "AUDITOR_OPINION").risk_status == R.FLAGGED


def test_c_no_auditor_source_is_not_evaluable():
    t = topic(run(), "AUDITOR_OPINION")
    assert (t.evidence_status, t.risk_status, t.reason) == (
        E.SOURCE_NOT_AVAILABLE, R.NOT_EVALUABLE, "AUDITOR_REPORT_NOT_SUPPLIED")


# --- F, G: related parties -----------------------------------------------------------------------


def test_f_g_related_party_fact_vs_registered_materiality(monkeypatch):
    rpt = [["Related Party Disclosures", "Note 38: Related party transactions",
            "Sale of services to Widget Holdings Rs 10 crore"]]
    data = run(bundle=bundle(monkeypatch, key_of(), "Widget Co Ltd", *rpt))
    t = topic(data, "RELATED_PARTY")
    if t.evidence_status == E.EVIDENCE_FOUND:  # Box 3 located the note: a fact, never a risk
        assert t.risk_status == R.NOT_EVALUABLE and "recorded as fact" in t.finding
    else:
        assert t.evidence_status == E.SOURCE_NOT_AVAILABLE
    assert t.risk_status != R.FLAGGED  # F
    loans = (dd.RelatedPartyItem(fiscal_year=2026, basis="CONSOLIDATED", kind="LOAN_GIVEN",
                                 counterparty="Promoter Co", relation="PROMOTER_ENTITY",
                                 amount_crore=Decimal(500), source=SRC),)
    flagged = run(related_party=loans)
    assert sig(flagged, "RELATED_PARTY_LOANS").severity.value in ("YELLOW", "RED")
    assert topic(flagged, "RELATED_PARTY").risk_status == R.FLAGGED  # G: the registered rule


# --- H-J: promoter holding / pledge --------------------------------------------------------------


def test_h_i_j_promoter_holding_pledge_and_missing_pledge():
    holding = dd.HoldingPoint(period_end=Q[-1], category=dd.HolderCategory.PROMOTER,
                              pct=Decimal(75), source=SRC)
    h = run(holdings=(holding,))
    assert not [t for t in h.governance_risks.topics if t.risk_status == R.FLAGGED]  # H
    i = run(pledges=(pledge(Q[-1], 25),))
    assert topic(i, "PROMOTER_PLEDGE").risk_status == R.FLAGGED  # I: registered RED
    zero = run(pledges=(pledge(Q[-1], 0),))
    assert topic(zero, "PROMOTER_PLEDGE").evidence_status == (
        E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE)
    j = topic(run(), "PROMOTER_PLEDGE")  # J: no data is not a zero pledge
    assert (j.evidence_status, j.risk_status) == (E.SOURCE_NOT_AVAILABLE, R.NOT_EVALUABLE)


# --- K, L: management changes --------------------------------------------------------------------


def test_k_l_resignations_recorded_routine_appointments_not_flagged():
    def change(role, kind, day, planned=None):
        return dd.ManagementChange(change_date=day, role=role, kind=kind,
                                   planned_succession=planned, source=SRC)
    k = run(management_changes=(change("CEO_MD", "RESIGNATION", date(2025, 5, 1)),))
    assert any(g.kind == "KMP_CHANGE" for g in k.governance_risks.catalogue)  # recorded
    assert topic(k, "MANAGEMENT_CHANGE").risk_status in (R.FLAGGED, R.EVALUATED_NO_FLAG)
    routine = run(management_changes=(change("CFO", "APPOINTMENT", date(2025, 6, 1)),))
    assert topic(routine, "MANAGEMENT_CHANGE").risk_status == R.EVALUATED_NO_FLAG  # L


# --- M, N: regulatory action ---------------------------------------------------------------------


def test_m_n_regulatory_action_needs_a_reviewed_source():
    fine = dd.PenaltyInput(penalty_date=date(2026, 3, 1), authority="SEBI",
                           amount_inr=Decimal(100_000_000), reason="disclosure lapse",
                           source=SRC)
    m = run(penalties=(fine,))
    assert sig(m, "EXCHANGE_PENALTIES").status.value == "EVALUATED"
    assert topic(m, "REGULATORY_ACTION").risk_status == R.FLAGGED  # M
    n = topic(run(), "REGULATORY_ACTION")
    assert (n.evidence_status, n.risk_status) == (E.SOURCE_NOT_AVAILABLE, R.NOT_EVALUABLE)  # N
    assert n.reason == "NO_EVENT_LEDGER"


# --- O, U: HUDCO (capital event, emphasis of matter) ---------------------------------------------


@pytest.fixture(scope="module")
def hudco_data():
    import test_financial_forecasting as tff
    data, _forecast = tff.hudco.__wrapped__()
    return data, build_financial_analytics(data, generated_at=NOW)


def test_o_u_hudco_exact_governance_regression(monkeypatch, hudco_data):
    box4, box5 = hudco_data
    b = bundle(monkeypatch, box4.company_key, box4.company_name, *report_pages(
        "Housing and Urban Development Corporation Limited", opinion=CLEAN, extra=EMPHASIS))
    data = build_due_diligence(box4, box5, request=DueDiligenceRequest(
        as_of=AS_OF, company_key=box4.company_key), generated_at=NOW, bundle=b)
    s = sig(data, "AUDITOR_SIGNAL")  # the registered rule: emphasis of matter is YELLOW
    assert (s.severity.value, s.explanation) == ("YELLOW", "FY2026 standalone: emphasis of matter")
    auditor = topic(data, "AUDITOR_OPINION")
    assert (auditor.evidence_status, auditor.risk_status) == (E.EVIDENCE_FOUND, R.FLAGGED)
    assert auditor.finding.startswith("FY2026 standalone: unmodified opinion, auditor S A R C")
    capital = topic(data, "CAPITAL_ISSUANCE")  # O: the 7.87% perpetual is a fact, not a flag
    assert "1442" in capital.finding and "not a governance flag" in capital.finding
    assert capital.risk_status != R.FLAGGED and capital.record_ids
    assert {t.topic: t.risk_status for t in data.governance_risks.topics if t.material} == {
        "AUDITOR_OPINION": R.FLAGGED, "RELATED_PARTY": R.NOT_EVALUABLE,
        "PROMOTER_PLEDGE": R.NOT_EVALUABLE, "MANAGEMENT_CHANGE": R.NOT_EVALUABLE,
        "BOARD_INDEPENDENCE": R.NOT_EVALUABLE, "REGULATORY_ACTION": R.NOT_EVALUABLE}
    assert verify_governance_provenance(data, b) == []
    g = governance_block(data)  # Q: one of six material topics evaluated
    assert g.status == SectionStatus.PARTIAL and "1 of 6" in (g.detail or "")


# --- V, W: INFY and YESBANK ----------------------------------------------------------------------


def test_v_infy_clean_opinion_and_disclosures_stay_factual(monkeypatch):
    infy = [["INDEPENDENT AUDITOR'S REPORT", "TO THE MEMBERS OF INFOSYS LIMITED",
             "Report on the Audit of the Standalone Financial Statements", "Opinion",
             "In our opinion and to the best of our information the aforesaid Standalone",
             "Financial Statements give a true and fair view of the state of affairs as at",
             "March 31, 2026.", "Basis for Opinion", "We are independent of the Company."],
            ["Key Audit Matters", "Revenue recognition on fixed-price contracts.",
             "For DELOITTE HASKINS & SELLS LLP", "Chartered Accountants"]]
    rpt = [["Related Party Disclosures", "Investments in subsidiaries and non-controlling",
            "interests are disclosed in Note 2.25."]]
    data = run(bundle=bundle(monkeypatch, key_of(), "Infosys Ltd", *infy, *rpt))
    t = topic(data, "AUDITOR_OPINION")
    assert t.risk_status == R.EVALUATED_NO_FLAG and "DELOITTE HASKINS & SELLS LLP" in t.finding
    assert not [x for x in data.governance_risks.topics if x.risk_status == R.FLAGGED]
    owner = topic(data, "PROMOTER_OWNERSHIP")  # NCI / investments are never promoter issues
    assert owner.risk_status == R.NOT_EVALUABLE and not owner.evidence_ids


def test_w_yesbank_without_governance_evidence_is_said_so():
    import test_business_classification_integrity as bci
    h = bci.run_layers([(SF, YESBANK)])
    topics = h["b13"].governance_risks.topics
    assert topics and not [t for t in topics if t.risk_status in (R.FLAGGED,
                                                                 R.EVALUATED_NO_FLAG)
                           and t.material]
    assert topic(h["b13"], "AUDITOR_OPINION").evidence_status == E.SOURCE_NOT_AVAILABLE
    assert governance_block(h["b13"]).status == SectionStatus.UNAVAILABLE  # P


# --- P-T: completeness and signal semantics ------------------------------------------------------


def _topic(code, risk, *, material=True):
    evidence = {R.FLAGGED: E.EVIDENCE_FOUND, R.EVALUATED_NO_FLAG:
                E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE, R.NOT_EVALUABLE:
                E.SOURCE_NOT_AVAILABLE, R.NOT_APPLICABLE: E.NOT_APPLICABLE}[risk]
    return GovernanceTopic(topic=code, title=code, material=material, evidence_status=evidence,
                           risk_status=risk, finding="x",
                           reason="SOURCE_ABSENT" if risk == R.NOT_EVALUABLE else None,
                           record_ids=(uuid4(),) if evidence in (
                               E.EVIDENCE_FOUND, E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE)
                           else ())


class _Box13:
    def __init__(self, *topics):
        self.governance_risks = dd.Governance(catalogue=(), signal_ids=(), topics=topics)
        self.signals = ()


def test_p_q_r_z_governance_completeness_follows_material_topics():
    assert governance_block(_Box13(_topic("A", R.NOT_EVALUABLE))).status == (
        SectionStatus.UNAVAILABLE)  # P
    assert governance_block(_Box13(_topic("A", R.EVALUATED_NO_FLAG),
                                   _topic("B", R.NOT_EVALUABLE))).status == SectionStatus.PARTIAL
    full = governance_block(_Box13(_topic("A", R.EVALUATED_NO_FLAG), _topic("B", R.FLAGGED),
                                   _topic("C", R.NOT_APPLICABLE),
                                   _topic("D", R.NOT_EVALUABLE, material=False)))
    assert full.status == SectionStatus.COMPLETE  # R: N/A and optional topics never count
    assert "2 of 2 material governance topics evaluated" in (full.detail or "")  # Z


def test_s_missing_evidence_is_never_green():
    with pytest.raises(ValidationError, match="never an evaluated|no adverse evidence"):
        GovernanceTopic(topic="X", title="X", material=True, evidence_status=E.SOURCE_NOT_AVAILABLE,
                        risk_status=R.EVALUATED_NO_FLAG, finding="x")
    with pytest.raises(ValidationError, match="no adverse evidence"):
        GovernanceTopic(topic="X", title="X", material=True,
                        evidence_status=E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE,
                        risk_status=R.NOT_EVALUABLE, reason="Y", finding="x",
                        record_ids=(uuid4(),))
    data = run()
    for t in data.governance_risks.topics:
        signals = {s.risk_id: s for s in data.signals}
        if any(signals[i].status.value not in ("EVALUATED", "INFORMATIONAL", "NOT_APPLICABLE",
                                               "UNSUPPORTED_SECTOR_RULE") for i in t.signal_ids):
            assert t.risk_status != R.EVALUATED_NO_FLAG, t.topic


def test_executive_pay_missing_is_unavailable_never_not_applicable():
    s = sig(run(), "EXECUTIVE_PAY_VS_PROFIT")
    assert (s.status.value, s.reason) == ("UNAVAILABLE", "PAY_HISTORY_NOT_SUPPLIED")
    assert topic(run(), "EXECUTIVE_PAY").risk_status == R.NOT_EVALUABLE


def test_t_one_emphasis_in_both_reports_is_one_flag(monkeypatch):
    pages = [*report_pages("Widget Co Limited", opinion=CLEAN, extra=EMPHASIS),
             *report_pages("Widget Co Limited", opinion=CLEAN, extra=EMPHASIS,
                           basis="Consolidated")]
    data = run(bundle=bundle(monkeypatch, key_of(), "Widget Co Ltd", *pages))
    assert {r.basis for r in data.request.auditor_reports} == {"STANDALONE", "CONSOLIDATED"}
    flags = [i for i in data.red_flags if data.signal(i).code == "AUDITOR_SIGNAL"]
    assert len(flags) == 1 and len(data.by_code("AUDITOR_SIGNAL")) == 1


# --- X, Y: provenance and presentation -----------------------------------------------------------


def test_x_verifier_rejects_unsupported_governance_conclusions(monkeypatch):
    b = bundle(monkeypatch, key_of(), "Widget Co Ltd", *report_pages(
        "Widget Co Limited", opinion=CLEAN))
    data = run(bundle=b)
    assert verify_governance_provenance(data, b) == []
    with pytest.raises(ValidationError, match="cites its evidence"):
        GovernanceTopic(topic="X", title="X", material=True, evidence_status=E.EVIDENCE_FOUND,
                        risk_status=R.NOT_EVALUABLE, reason="Y", finding="claimed")

    def forge(**update):
        topics = tuple(t.model_copy(update=update) if t.topic == "AUDITOR_OPINION" else t
                       for t in data.governance_risks.topics)
        return data.model_copy(update={"governance_risks": data.governance_risks.model_copy(
            update={"topics": topics})})
    stray = uuid4()
    assert any("not in Box 3" in p for p in verify_governance_provenance(
        forge(evidence_ids=(stray,)), b))
    assert any("FLAGGED without" in p for p in verify_governance_provenance(
        forge(risk_status=R.FLAGGED, evidence_status=E.EVIDENCE_FOUND), b))
    pledge_topic = topic(data, "PROMOTER_PLEDGE")
    forged = data.model_copy(update={"governance_risks": data.governance_risks.model_copy(
        update={"topics": tuple(t.model_copy(update={
            "risk_status": R.EVALUATED_NO_FLAG,
            "evidence_status": E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE})
            if t is pledge_topic else t for t in data.governance_risks.topics)})})
    assert any("over an unevaluated rule" in p for p in verify_governance_provenance(forged, b))


def test_y_report_renders_a_bounded_governance_ledger(monkeypatch):
    b = bundle(monkeypatch, key_of(), "Widget Co Ltd", *report_pages(
        "Widget Co Limited", opinion=CLEAN, extra=EMPHASIS))
    data = run(bundle=b)
    from test_due_diligence import datasets, rows_for
    box4, box5 = datasets(rows_for())
    report = build_final_report(request=FinalReportRequest(
        as_of=AS_OF, company_key=box4.company_key), generated_at=NOW, box4=box4, box5=box5,
        box13=data, bundle=b)
    table = report.table("GOVERNANCE_EVIDENCE")
    assert len(table.rows) == len(data.governance_risks.topics) <= 10  # one row per topic
    assert [c.key for c in table.columns] == ["EVIDENCE", "RISK", "FINDING", "SOURCE"]
    auditor = next(r for r in table.rows if r.key == "GOV|AUDITOR_OPINION")
    cells = {c.column: c.text for c in auditor.cells}
    assert (cells["EVIDENCE"], cells["RISK"]) == ("EVIDENCE_FOUND", "FLAGGED")
    assert "No governance score" in table.footnotes[0]
    rc = report.research_completeness
    gov = next(x for x in rc.blocks if x.block == "GOVERNANCE")
    assert gov.status == SectionStatus.PARTIAL and "AUDITOR_OPINION" in (gov.detail or "")
