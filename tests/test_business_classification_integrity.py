"""Business classification integrity: ONE classification per research run.

Box 4 decides it once (shared classifier `financial_business.classify`): Industry (the subtype
GENERAL / BANK / NBFC / INSURANCE / UNKNOWN) plus its coarse BusinessClass (FINANCIAL /
NON_FINANCIAL / UNKNOWN). Every box reads that decision: Box 5's sector, Box 7/8/9's sector
family and Box 13's gate are refinements of it, never second guesses; Box 14 refuses a layer
that contradicts it. UNKNOWN never acts as GENERAL.

HUDCO (Screener 'Financial Services / Finance / Financial Institution', lender P&L) is
financial with an unresolved subtype: Industry UNKNOWN, class FINANCIAL - everywhere.
BEFORE: Box 4 UNKNOWN, Box 7/9 FINANCIAL re-derived from breadcrumbs, Box 13 FINANCIAL
re-labelled from the restricted set, report 'UNKNOWN / FINANCIAL' + business model 'GENERAL'.
"""

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_financial_analytics import M, company, make
from test_screener_dataset import page
from test_summary_input import INFY, YESBANK

from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
from stock_research_ai.application import due_diligence as dd
from stock_research_ai.application.business_intelligence import build_business_intelligence
from stock_research_ai.application.business_registry import sector_family
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import build_financial_analytics
from stock_research_ai.application.financial_reconciliation import (
    build_canonical_dataset,
    detect_classification,
)
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.fundamental_valuation import build_valuation
from stock_research_ai.application.industry_intelligence import build_industry_intelligence
from stock_research_ai.application.management_guidance import _applicable
from stock_research_ai.application.peer_registry import BENCHMARK_METRICS, applies
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.application.report_claims import ReportCompositionError
from stock_research_ai.application.report_panel import build_panel
from stock_research_ai.application.report_presentation import build_presentation
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.domain.due_diligence import DueDiligenceRequest, SectorGate
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_analytics import Sector
from stock_research_ai.domain.financial_business import classify, combine
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    CanonicalFinancialDataset,
    Industry,
)
from stock_research_ai.domain.report_rendering import ReportManifest

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
FC, NF, UC = BusinessClass.FINANCIAL, BusinessClass.NON_FINANCIAL, BusinessClass.UNKNOWN
BASIS = Path(__file__).parent / "fixtures" / "reporting_basis"
SCREENER = Path(__file__).parent / "fixtures" / "screener"
HUDCO = [(BASIS, ("hudco_consolidated.html",
                  "https://www.screener.in/company/HUDCO/consolidated/")),
         (BASIS, ("hudco_standalone.html", "https://www.screener.in/company/HUDCO/"))]
HUDCO_NAMES = ["Financial Services", "Financial Services", "Finance", "Financial Institution"]
LENDER_ROWS = ["Revenue", "Interest", "Expenses", "Financing Profit", "Net Profit"]


def decided(names=(), description="", labels=()):
    d = classify(list(names), description=description, labels=list(labels))
    return d.industry, d.business_class


# --- A-G: the shared classifier and its contract ---------------------------------------------


def test_a_financial_non_bank_is_never_general():
    assert decided(["Finance", "Non Banking Financial Company"], labels=LENDER_ROWS) == (
        Industry.NBFC, FC)
    assert decided(["Housing Finance"], labels=LENDER_ROWS) == (Industry.NBFC, FC)
    assert decided(HUDCO_NAMES, labels=LENDER_ROWS) == (Industry.UNKNOWN, FC)


def test_b_c_bank_and_industrial_keep_their_class():
    assert decided(["Banks", "Private Sector Bank"], labels=["Deposits", "Advances"]) == (
        Industry.BANK, FC)
    assert decided(["Information Technology", "IT - Software"]) == (Industry.GENERAL, NF)
    assert decided(["Manufacturing"], labels=["Sales", "Operating Profit"]) == (
        Industry.GENERAL, NF)


def test_d_no_evidence_stays_unknown_never_general():
    assert decided() == (Industry.UNKNOWN, UC)
    assert decided(["Diversified"]) == (Industry.UNKNOWN, UC)


def test_e_conflicting_evidence_is_deterministic():
    assert decided(["Banks", "Insurance"]) == (Industry.UNKNOWN, FC)  # financial, which one?
    nbfc, bank = classify(["Housing Finance"]), classify(["Banks"])
    general = classify(["IT - Software"])
    for pair in ([nbfc, bank], [bank, nbfc]):  # order never matters
        assert (combine(pair).industry, combine(pair).business_class) == (Industry.UNKNOWN, FC)
    for pair in ([nbfc, general], [general, nbfc]):
        assert (combine(pair).industry, combine(pair).business_class) == (Industry.UNKNOWN, UC)
    assert combine([general, general]) == general


def test_f_provider_classification_maps_explicitly_and_one_pair_only():
    box4 = make(company(), Industry.GENERAL)
    assert box4.business_class == NF and box4.classification == NF
    with pytest.raises(ValidationError, match="one classification only"):
        CanonicalFinancialDataset.model_validate(
            box4.model_dump() | {"business_class": "FINANCIAL"})
    legacy = box4.model_dump()
    legacy.pop("business_class")  # a dataset written before the class was recorded
    assert CanonicalFinancialDataset.model_validate(legacy).business_class == NF
    # a stale class never outlives an industry change: the accessor follows the industry
    assert box4.model_copy(update={"industry": Industry.BANK}).classification == FC


def test_g_lender_statement_structure_supports_financial_without_issuer_names():
    assert decided(labels=LENDER_ROWS) == (Industry.UNKNOWN, FC)  # not BANK, not GENERAL


# --- N / O / P: applicability follows the ONE classification -----------------------------------


def _box5(industry, business_class=None):
    data = make(company(), industry)
    if business_class is not None:
        data = data.model_copy(update={"business_class": business_class})
    return data, build_financial_analytics(data, generated_at=NOW)


def _status(box5, code):
    return {m.status.value for m in box5.all_metrics() if m.code == code}


@pytest.mark.parametrize("industry,cls,sector", [
    (Industry.UNKNOWN, FC, Sector.FINANCIAL), (Industry.NBFC, None, Sector.FINANCIAL),
    (Industry.UNKNOWN, None, Sector.UNKNOWN), (Industry.GENERAL, None, Sector.GENERAL)])
def test_n_o_p_box5_formulas_follow_the_classification(industry, cls, sector):
    data, box5 = _box5(industry, cls)
    assert box5.sector == sector
    if sector == Sector.GENERAL:
        assert "CALCULATED" in _status(box5, "EBITDA_MARGIN")
        return
    assert _status(box5, "EBITDA_MARGIN") == {"UNSUPPORTED_SECTOR"}  # N: no industrial
    assert "CALCULATED" not in _status(box5, "FINANCING_PROFIT_MARGIN")  # O: no bank-only
    assert "CALCULATED" in _status(box5, "ROE")  # business-neutral metrics remain


def test_p_unknown_family_gets_no_sector_specific_rule():
    assert sector_family(Industry.UNKNOWN, ["IT - Software"]) == (
        sector_family(Industry.UNKNOWN, ["IT - Software"])[0],
        "Box 4 business classification unresolved")
    assert sector_family(Industry.UNKNOWN, ["IT - Software"])[0].value == "UNKNOWN"
    for metric in BENCHMARK_METRICS:
        assert applies(metric, "UNKNOWN") == (metric.families == "ALL")
    assert _applicable("UNIVERSAL", "UNKNOWN")
    assert not _applicable("NON_FINANCIAL", "UNKNOWN") and not _applicable("FINANCIAL", "UNKNOWN")


# --- H-M / Q / R: every layer of one run states the same classification -------------------------


def run_layers(pages):
    async def acquire():
        out = []
        for fixtures, case in pages:
            with tempfile.TemporaryDirectory() as t:
                out.append(await page(Path(t), fixtures, case))
        return out
    acquired = asyncio.run(acquire())
    screeners = tuple(build_screener_dataset(e, b, None) for e, b in acquired)
    b4 = build_canonical_dataset(company_key=screeners[0].company_key,
                                 company_name=screeners[0].profile.name, screeners=screeners,
                                 bundle=None, catalogue=None, generated_at=NOW)
    b5 = build_financial_analytics(b4, generated_at=NOW)
    b7 = build_business_intelligence(box4=b4, bundle=None, catalogue=None,
                                     screener=screeners[0], generated_at=NOW)
    b9 = build_industry_intelligence(target=CompanyLayers(b4.company_key, screener=screeners[0],
                                                          box4=b4, box5=b5, box7=b7),
                                     peers=[], generated_at=NOW)
    b10 = build_forecast(b4, generated_at=NOW)
    b11 = build_valuation(b10, box4=b4, generated_at=NOW)
    b13 = build_due_diligence(b4, b5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=b4.company_key), generated_at=NOW, box7=b7, box9=b9,
        box10=b10, box11=b11)
    report = build_final_report(request=FinalReportRequest(as_of=NOW.date(),
                                                           company_key=b4.company_key),
                                generated_at=NOW, box4=b4, box5=b5, box13=b13, box7=b7,
                                box9=b9, box10=b10, box11=b11)
    summary = project_summary_input(acquired[0][0])
    return dict(b4=b4, b5=b5, b7=b7, b9=b9, b10=b10, b11=b11, b13=b13, report=report,
                summary=summary)


@pytest.fixture(scope="module")
def hudco():
    return run_layers(HUDCO)


def _panel(report):
    return build_panel(report, ReportManifest.model_construct(
        artifact_id=report.report_id, screener_page="X", status="COMPLETE", files=(),
        failures=()))


def test_h_i_j_k_l_m_hudco_has_one_classification_in_every_layer(hudco):
    h = hudco
    assert (h["b4"].industry, h["b4"].classification) == (Industry.UNKNOWN, FC)
    assert "financial evidence, subtype unresolved" in (h["b4"].classification_reason or "")
    assert h["summary"].business_classification == Industry.UNKNOWN  # I
    assert h["b5"].sector == Sector.FINANCIAL
    assert h["b7"].sector.value == "FINANCIAL"
    assert h["b7"].business_structure.sector_reason.startswith("Box 4")  # derived, not guessed
    assert h["b7"].business_structure.industry == Industry.UNKNOWN
    assert h["b9"].sector_family == "FINANCIAL"  # J
    assert h["b10"].industry == h["b11"].industry == Industry.UNKNOWN  # K, L
    assert (h["b13"].industry, h["b13"].sector_gate) == (Industry.UNKNOWN, SectorGate.FINANCIAL)
    m = h["report"].metadata
    assert (m.industry, m.business_class) == (Industry.UNKNOWN, "FINANCIAL")
    panel = _panel(h["report"])
    assert (panel.business_class, panel.industry) == ("FINANCIAL", "UNKNOWN")
    facts = dict(build_presentation(h["report"]).header_facts)
    assert facts["Business classification"] == "FINANCIAL (subtype unresolved)"
    assert facts["Order-book model"] == "not order-book driven"  # not a business class


def test_m_report_refuses_a_layer_with_another_classification(hudco, monkeypatch):
    h = hudco
    other = h["b10"].model_copy(update={"industry": Industry.GENERAL})
    monkeypatch.setattr(dd, "_check_lineage", lambda *a, **k: None)
    b13 = build_due_diligence(h["b4"], h["b5"], request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=h["b4"].company_key), generated_at=NOW, box10=other)
    with pytest.raises(ReportCompositionError) as refused:
        build_final_report(request=FinalReportRequest(as_of=NOW.date(),
                                                      company_key=h["b4"].company_key),
                           generated_at=NOW, box4=h["b4"], box5=h["b5"], box13=b13,
                           box10=other)
    assert refused.value.code == "CLASSIFICATION_MISMATCH"


def test_m_unknown_company_gate_is_unknown_and_industrial_rules_withheld():
    data, box5 = _box5(Industry.UNKNOWN)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW)
    assert box13.sector_gate == SectorGate.UNKNOWN  # BEFORE: labelled FINANCIAL
    gated = box13.sector_specific_risks.gated_rules
    assert box13.sector_specific_risks.sector_gate == SectorGate.UNKNOWN
    assert gated  # industrial rules are recorded as withheld, not run
    for s in box13.signals:
        if s.code in gated:
            assert s.status.value != "EVALUATED" and s.severity is None


@pytest.mark.parametrize("pages,industry,cls,family,gate", [
    ([(SCREENER, INFY)], Industry.GENERAL, NF, "IT_SERVICES", SectorGate.NON_FINANCIAL),  # Q
    ([(SCREENER, YESBANK)], Industry.BANK, FC, "FINANCIAL", SectorGate.FINANCIAL),  # R
])
def test_q_r_non_financial_and_bank_fixtures_unchanged(pages, industry, cls, family, gate):
    r = run_layers(pages)
    assert (r["b4"].industry, r["b4"].classification) == (industry, cls)
    assert r["summary"].business_classification == industry
    assert r["b7"].sector.value == family and r["b9"].sector_family == family
    assert r["b10"].industry == r["b11"].industry == r["b13"].industry == industry
    assert r["b13"].sector_gate == gate
    assert r["report"].metadata.business_class == cls.value


def test_box4_classification_is_one_decision_over_all_pages(hudco):
    async def both():
        out = []
        for fixtures, case in HUDCO:
            with tempfile.TemporaryDirectory() as t:
                e, b = await page(Path(t), fixtures, case)
                out.append(build_screener_dataset(e, b, None))
        return out
    d = detect_classification(asyncio.run(both()))
    assert (d.industry, d.business_class) == (hudco["b4"].industry, hudco["b4"].classification)
    assert M  # the taxonomy itself is unchanged
