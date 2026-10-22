"""Research completeness integrity: COMPLETE means every MATERIAL, APPLICABLE requirement of the
company's business model is met by what the upstream layers produced - never that the
pipeline ran. Execution (rendering) status and research completeness are separate.

  A all material complete -> COMPLETE      M rule evaluated, no risk -> evaluated
  B one material unavailable -> PARTIAL    N rule not evaluable != 'no risk'
  C all material unavailable -> UNAVAIL.   O governance evidence absent -> honest
  D optional unavailable -> no penalty     P Summary / Detailed share one conclusion
  E NOT_APPLICABLE -> no penalty           Q COMPLETE never over a material UNAVAILABLE
  F industrial DCF N/A for a lender        R PARTIAL exposes reason codes
  G lender P/B MULTIPLE_MISSING            S HUDCO   T INFY   U YESBANK
  H ATTRIBUTION_UNKNOWN                    V execution SUCCESS + analytical PARTIAL
  I zero eligible peers (material)         W forged COMPLETE refused
  J optional peer metric unavailable       X bounded completeness summary renders
  K material lender forecast line missing
  L industrial forecast line N/A for a lender
"""

from pathlib import Path

import pytest
from pydantic import ValidationError
from test_business_classification_integrity import HUDCO, run_layers
from test_financial_analytics import B
from test_financial_valuation import NOW, VM, lender, lender_rows, multiple, shares
from test_peer_comparability_integrity import FINANCE, NBFC, build, layers, table
from test_peer_comparability_integrity import lender_rows as peer_lender_rows
from test_summary_input import INFY, YESBANK

from stock_research_ai.application.fundamental_valuation import DEFAULT_METHODS, build_valuation
from stock_research_ai.application.report_markdown import render_markdown
from stock_research_ai.application.report_panel import build_panel
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.application.research_completeness import (
    forecast_block,
    governance_block,
    peer_block,
    risk_block,
    summary_lines,
    valuation_block,
)
from stock_research_ai.domain.due_diligence import (
    RiskCategory,
    RiskSignal,
    SignalClass,
    SignalStatus,
)
from stock_research_ai.domain.final_research_report import (
    CompletenessBlock,
    FinalResearchReportDataset,
    ResearchCompleteness,
    SectionId,
    SectionStatus,
    overall_completeness,
)
from stock_research_ai.domain.financial_canonical import BusinessClass, Industry
from stock_research_ai.domain.fundamental_valuation import ValuationRequest
from stock_research_ai.domain.report_rendering import ReportManifest
from stock_research_ai.domain.summary import SummaryResult

ST = SectionStatus
SF = Path(__file__).parent / "fixtures" / "screener"


def blk(name, status, material=True, reasons=None):
    reasons = reasons if reasons is not None else (
        ("INPUT_MISSING",) if status in (ST.PARTIAL, ST.UNAVAILABLE) else ())
    return CompletenessBlock(block=name, title=name.title(), material=material, status=status,
                             reasons=reasons)


# --- A-E, Q, R, W: the contract ------------------------------------------------------------------


def test_a_b_c_d_e_overall_follows_material_applicable_blocks_only():
    ok = [blk("A", ST.COMPLETE), blk("B", ST.COMPLETE)]
    assert overall_completeness(ok) == ST.COMPLETE  # A
    assert overall_completeness([*ok, blk("C", ST.UNAVAILABLE)]) == ST.PARTIAL  # B
    assert overall_completeness([blk("A", ST.UNAVAILABLE), blk("B", ST.UNAVAILABLE)]) == (
        ST.UNAVAILABLE)  # C
    assert overall_completeness([*ok, blk("KPI", ST.UNAVAILABLE, material=False)]) == (
        ST.COMPLETE)  # D: optional never counts
    assert overall_completeness([*ok, blk("ORDERS", ST.NOT_APPLICABLE)]) == ST.COMPLETE  # E
    assert overall_completeness([blk("ORDERS", ST.NOT_APPLICABLE)]) == ST.UNAVAILABLE


def test_q_r_w_forged_or_unreasoned_completeness_is_refused():
    with pytest.raises(ValidationError, match="ledger"):  # Q / W
        ResearchCompleteness(business_model="BANK", overall=ST.COMPLETE,
                             blocks=(blk("A", ST.COMPLETE), blk("B", ST.UNAVAILABLE)))
    with pytest.raises(ValidationError, match="reason codes"):  # R
        CompletenessBlock(block="B", title="B", material=True, status=ST.PARTIAL)
    with pytest.raises(ValidationError, match="no incompleteness reason"):
        CompletenessBlock(block="B", title="B", material=True, status=ST.COMPLETE,
                          reasons=("X",))
    with pytest.raises(ValidationError, match="one ledger entry"):
        ResearchCompleteness(business_model="BANK", overall=ST.COMPLETE,
                             blocks=(blk("A", ST.COMPLETE), blk("A", ST.COMPLETE)))


# --- F, G, H: valuation --------------------------------------------------------------------------


def test_f_g_h_lender_valuation_completeness():
    data, f = lender()  # an NBFC lender: P/B and P/E apply, industrial methods do not
    std, fs = lender(lender_rows(basis=B.STANDALONE))  # standalone: PAT is the owners'

    def v(d, fc, *mult, **kw):
        return build_valuation(fc, request=ValuationRequest(
            multiples=mult, assumptions=(shares(),), **kw), box4=d, generated_at=NOW)

    # F: every applicable method valued; DCF / EV methods UNSUPPORTED_SECTOR are N/A
    full = valuation_block(v(std, fs, multiple(VM.PB), multiple(VM.PE, value="10"),
                             methods=tuple(dict.fromkeys((*DEFAULT_METHODS, VM.PB, VM.PE)))))
    assert full.status == ST.COMPLETE and "not applicable: DCF" in (full.detail or "")
    # G: P/B without a multiple -> MULTIPLE_MISSING: never COMPLETE, nothing invented
    none = valuation_block(v(data, f))
    assert (none.status, none.reasons) == (ST.UNAVAILABLE, ("MULTIPLE_MISSING",))
    # H: consolidated P/E without owners' PAT -> ATTRIBUTION_UNKNOWN
    some = valuation_block(v(data, f, multiple(VM.PB), multiple(VM.PE, value="10")))
    assert (some.status, some.reasons) == (ST.PARTIAL, ("ATTRIBUTION_UNKNOWN",))


# --- I, J: peers ---------------------------------------------------------------------------------


def test_i_j_peer_block_material_vs_optional_benchmarks():
    t = layers("screener:9100", NBFC, peer_lender_rows(), Industry.NBFC, peers=table("9101"))
    data = build(t, [layers("screener:9101", NBFC, peer_lender_rows(), Industry.NBFC)])
    ok = peer_block(t.box4, data, "CONSOLIDATED")
    assert ok.status == ST.COMPLETE  # core ROE / PAT growth available
    # J: an optional benchmark (tax rate) with no eligible peer carries no penalty
    gap = data.model_copy(update={"financial_benchmarks": tuple(
        b.model_copy(update={"availability": "UNAVAILABLE"}) if b.metric_code == "TAX_RATE"
        else b for b in data.financial_benchmarks)})
    assert any(b.metric_code == "TAX_RATE" for b in data.financial_benchmarks)
    assert peer_block(t.box4, gap, "CONSOLIDATED").status == ST.COMPLETE
    core = data.model_copy(update={"financial_benchmarks": tuple(
        b.model_copy(update={"availability": "UNAVAILABLE"}) if b.metric_code == "ROE"
        else b for b in data.financial_benchmarks)})
    assert peer_block(t.box4, core, "CONSOLIDATED").status == ST.PARTIAL  # material gap
    lone = layers("screener:9200", FINANCE, peer_lender_rows(), Industry.UNKNOWN,
                  BusinessClass.FINANCIAL, peers=table("9201"))
    zero = build(lone, [layers("screener:9201", FINANCE, peer_lender_rows(), Industry.BANK)])
    block = peer_block(lone.box4, zero, "CONSOLIDATED")
    assert (block.status, block.reasons) == (ST.UNAVAILABLE, ("NO_ELIGIBLE_PEERS",))  # I
    assert peer_block(lone.box4, None, "CONSOLIDATED").reasons == ("LAYER_NOT_SUPPLIED",)


# --- K, L: forecast ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hudco():
    return run_layers(HUDCO)


def test_k_l_forecast_core_lines_per_model(hudco):
    k = forecast_block(hudco["b10"])  # Screener-only HUDCO: equity / borrowings chain missing
    assert (k.status, k.reasons) == (ST.PARTIAL, ("COMMON_EQUITY_NOT_FORECAST",
                                                  "TOTAL_BORROWINGS_NOT_FORECAST"))
    _data, f = lender()  # L: a lender has no REVENUE / EBITDA line - never required
    full = forecast_block(f)
    assert full.status == ST.COMPLETE and "NON_BANK_FINANCIAL: 4 of 4" in (full.detail or "")
    assert not {line.value for line in {v.line for v in f.values}} & {"REVENUE", "EBITDA"}


# --- M, N, O: risks and governance ---------------------------------------------------------------


def _signal(code, status, category="LEVERAGE", severity=None):
    """A typed Box 13 rule outcome (only the fields the ledger reads)."""
    return RiskSignal.model_construct(code=code, status=SignalStatus(status),
                                      signal_class=SignalClass.COMPANY_RISK,
                                      category=RiskCategory(category), severity=severity)


class _Box13:
    def __init__(self, *signals):
        self.signals = signals


def test_m_n_evaluated_no_risk_is_not_unevaluable():
    clean = risk_block(_Box13(_signal("A", "EVALUATED", severity="GREEN"),
                              _signal("B", "NOT_APPLICABLE")))
    assert clean.status == ST.COMPLETE  # M: evaluated, nothing found: a real conclusion
    unknown = risk_block(_Box13(_signal("A", "EVALUATED", severity="GREEN"),
                                _signal("B", "UNAVAILABLE"), _signal("C", "INSUFFICIENT_SAMPLE")))
    assert (unknown.status, unknown.reasons) == (
        ST.PARTIAL, ("RULE_INSUFFICIENT_SAMPLE", "RULE_UNAVAILABLE"))  # N
    assert "1 of 3 applicable rules evaluated" in (unknown.detail or "")
    assert risk_block(_Box13(_signal("B", "UNAVAILABLE"))).status == ST.UNAVAILABLE


def test_o_governance_without_evidence_is_honestly_unavailable(hudco):
    g = governance_block(hudco["b13"])
    assert (g.status, g.reasons) == (ST.UNAVAILABLE, ("MISSING_SOURCE_EVIDENCE",))
    assert g.detail == "0 of 7 governance / management / auditor rules evaluated"
    report = hudco["report"]
    sec = report.section(SectionId.MANAGEMENT_GOVERNANCE)
    assert sec.status != ST.COMPLETE
    # nothing invented: the section holds only upstream claims (one fact, no new content)
    claims = {c.claim_id: c for c in report.claims}
    assert [claims[i].topic for i in sec.claim_ids] == ["EXECUTIVE_PAY_VS_PROFIT"]


# --- S, T, U: real fixtures ----------------------------------------------------------------------


def ledger(report):
    rc = report.research_completeness
    return rc.overall, {b.block: (b.status, b.reasons) for b in rc.blocks if b.material}


def test_s_hudco_exact_regression(hudco):
    report = hudco["report"]
    rc = report.research_completeness
    assert (rc.business_model, rc.subtype) == ("NON_BANK_FINANCIAL", None)
    overall, blocks = ledger(report)
    assert overall == ST.PARTIAL
    assert blocks == {
        "HISTORICAL_FINANCIALS": (ST.COMPLETE, ()),
        "FINANCIAL_ANALYTICS": (ST.COMPLETE, ()),  # general-financial: PAT growth, ROE, ROA
        "BUSINESS_ANALYSIS": (ST.PARTIAL, ("SEGMENT_DATA_MISSING",)),
        "PEER_ANALYSIS": (ST.UNAVAILABLE, ("NO_ELIGIBLE_PEERS",)),
        "FORECAST": (ST.PARTIAL, ("COMMON_EQUITY_NOT_FORECAST", "TOTAL_BORROWINGS_NOT_FORECAST")),
        "VALUATION": (ST.UNAVAILABLE, ("MULTIPLE_MISSING",)),
        "RISK_ANALYSIS": (ST.PARTIAL, ("RULE_UNAVAILABLE",)),
        "GOVERNANCE": (ST.UNAVAILABLE, ("MISSING_SOURCE_EVIDENCE",)),
        "SOURCE_INTEGRITY": (ST.COMPLETE, ()),
    }
    sections = {s.section_id: s for s in report.sections}
    # BEFORE: INDUSTRY_COMPETITION and RISKS were COMPLETE ('every required input family')
    assert sections[SectionId.INDUSTRY_COMPETITION].status == ST.PARTIAL
    assert "NO_ELIGIBLE_PEERS" in sections[SectionId.INDUSTRY_COMPETITION].status_reason
    assert sections[SectionId.RISKS].status == ST.PARTIAL
    assert sections[SectionId.EXECUTIVE_SUMMARY].status != ST.COMPLETE
    optional = {b.block: b.status for b in rc.blocks if not b.material}
    assert optional["ORDER_BOOK"] == ST.NOT_APPLICABLE  # a lender has no order book


def test_t_infy_industrial_requirements_only():
    report = run_layers([(SF, INFY)])["report"]
    rc = report.research_completeness
    assert rc.business_model == "NON_FINANCIAL"
    fa = next(b for b in rc.blocks if b.block == "FINANCIAL_ANALYTICS")
    assert fa.status == ST.COMPLETE and "7 of 7" in (fa.detail or "")  # no lender metric
    fc = next(b for b in rc.blocks if b.block == "FORECAST")
    assert fc.status == ST.COMPLETE  # REVENUE / EBITDA / PAT; CAPEX / FCF optional
    val = next(b for b in rc.blocks if b.block == "VALUATION")
    assert val.status == ST.UNAVAILABLE and "MULTIPLE_MISSING" in val.reasons
    assert "PB" not in (val.detail or "")  # P/B (lender) never required of INFY
    assert rc.overall == ST.PARTIAL


def test_u_yesbank_bank_requirements_only():
    report = run_layers([(SF, YESBANK)])["report"]
    rc = report.research_completeness
    assert rc.business_model == "BANK"
    by = {b.block: b for b in rc.blocks}
    assert by["FINANCIAL_ANALYTICS"].status == ST.PARTIAL  # asset quality not in the data
    assert "GROSS_NPA_RATIO" in (by["FINANCIAL_ANALYTICS"].detail or "")
    assert "BORROWINGS_TO_EQUITY" not in (by["FINANCIAL_ANALYTICS"].detail or "")
    assert by["FORECAST"].reasons == ("PAT_NOT_FORECAST",)  # was Box 10 COMPLETE (revenue)
    val = by["VALUATION"]
    assert "not applicable: DCF_FCFF_EXIT_MULTIPLE, DCF_FCFF_GORDON, EV_EBIT, EV_EBITDA" in (
        val.detail or "") and val.reasons == ("MULTIPLE_MISSING",)
    assert rc.overall == ST.PARTIAL


# --- P, V, W, X: one conclusion, execution apart, forgery, presentation --------------------------


def _panel(report, status="COMPLETE"):
    return build_panel(report, ReportManifest.model_construct(
        artifact_id=report.report_id, screener_page="HUDCO", status=status, files=(),
        failures=()))


def test_p_v_one_conclusion_and_execution_is_separate(hudco):
    report = hudco["report"]
    panel = _panel(report)
    assert (panel.status, panel.research_status) == ("COMPLETE", "PARTIAL")  # V
    assert panel.research_status == report.research_completeness.overall.value  # P
    assert report.section(SectionId.EXECUTIVE_SUMMARY).status != ST.COMPLETE
    # the browser Summary makes no research-completeness claim of its own (its coverage field
    # is page-extraction coverage); the one conclusion is Box 14's
    assert not {"research_status", "research_completeness"} & set(SummaryResult.model_fields)


def test_w_tampered_report_completeness_is_rejected(hudco):
    report = hudco["report"]
    rc = report.research_completeness
    forged_blocks = tuple(b.model_copy(update={"status": ST.COMPLETE, "reasons": ()})
                          if b.block == "PEER_ANALYSIS" else b for b in rc.blocks)
    data = report.model_dump()
    data["research_completeness"]["blocks"] = [b.model_dump() for b in (
        b.model_copy(update={"section_ids": (SectionId.INDUSTRY_COMPETITION,)})
        if b.block == "PEER_ANALYSIS" else b for b in forged_blocks)]
    with pytest.raises(ValidationError, match="COMPLETE over an incomplete section"):
        FinalResearchReportDataset.model_validate(data)
    summary = report.model_dump()  # research PARTIAL, executive summary forged COMPLETE
    for section in summary["sections"]:
        if section["section_id"] == SectionId.EXECUTIVE_SUMMARY.value:
            section["status"] = "COMPLETE"
    with pytest.raises(ValidationError, match="executive summary"):
        FinalResearchReportDataset.model_validate(summary)
    overall = report.model_dump()  # a forged overall over the honest ledger
    overall["research_completeness"]["overall"] = "COMPLETE"
    with pytest.raises(ValidationError, match="ledger"):
        FinalResearchReportDataset.model_validate(overall)
    honest = report.model_dump()
    assert FinalResearchReportDataset.model_validate(honest).research_completeness == rc


def test_x_bounded_completeness_summary_renders(hudco):
    report = hudco["report"]
    lines = dict(summary_lines(report.research_completeness))
    assert list(lines) == ["Overall", "Complete", "Partial", "Unavailable", "Not applicable",
                           "Optional (not counted)"]
    assert lines["Overall"].startswith("PARTIAL (business model NON_BANK_FINANCIAL;")
    assert "Peer analysis (Box 9 core benchmarks) (NO_ELIGIBLE_PEERS)" in lines["Unavailable"]
    assert "Order book: NOT_APPLICABLE" in lines["Optional (not counted)"]
    assert len(lines["Partial"]) < 1000  # bounded: requirement names, not metric dumps
    md = render_markdown(build_presentation(report))
    assert "### Research completeness" in md and "NO\\_ELIGIBLE\\_PEERS" in md
    panel = _panel(report)
    assert panel.research_completeness[0][0] == "Overall"
    assert len(panel.research_completeness) == 6


def test_execution_success_with_unavailable_research_is_valid(hudco):
    report = hudco["report"]
    data = report.model_dump()
    blocks = [{**b, "status": "UNAVAILABLE", "reasons": b["reasons"] or ["INPUT_MISSING"],
               "section_ids": []} for b in data["research_completeness"]["blocks"]]
    data["research_completeness"].update(blocks=blocks, overall="UNAVAILABLE")
    ok = FinalResearchReportDataset.model_validate(data)  # composes; no exception
    assert ok.research_completeness.overall == ST.UNAVAILABLE
    assert _panel(ok).research_status == "UNAVAILABLE"
