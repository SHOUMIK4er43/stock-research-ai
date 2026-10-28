"""Reporting-basis integrity: ONE analytical basis per research output.

Summary, Detailed Research, Negatives (the panel's risk register), peer benchmarks, forecasts
and valuation all use financial_analytics.select_analytical_basis (CONSOLIDATED when Box 4 holds
consolidated values, else STANDALONE). The other basis is only ever the labelled control layer;
a figure missing on the analytical basis is UNAVAILABLE / BASIS_MISMATCH, never filled from the
other basis. HUDCO (2026-10-18) is the exact regression: Summary was standalone, Detailed
consolidated.

  A deterministic basis            B/C/O/P Summary = Detailed = Negatives basis (HUDCO, offline)
  D/E peer anchor (no fallback)    F consolidated value under a standalone analysis is refused
  G/H mixed vs same-basis inputs   I/J/K/L quarterly / annual / point-in-time / growth
  M risk signals                   N forecast inputs
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from test_financial_analytics import (
    B,
    M,
    V,
    company,
    fy,
    make,
    quarter,
)
from test_financial_cell_integrity import (
    KEY,
    filing_observations,
    reconcile,
    secondary_observations,
)
from test_industry_intelligence import IT, PEER_ROWS, layers, screener
from test_screener_dataset import page

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.rendering.acquire import StageRequest, capture_stage, page_urls
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.screener.source import ScreenerSource, analytical_page_urls
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import due_diligence as dd
from stock_research_ai.application.browser_jobs import BrowserJobManager
from stock_research_ai.application.due_diligence import build_due_diligence
from stock_research_ai.application.engine import ExtractionServices, ResearchEngine
from stock_research_ai.application.final_report_composer import build_final_report
from stock_research_ai.application.financial_analytics import (
    build_financial_analytics,
    select_analytical_basis,
)
from stock_research_ai.application.forecasting import build_forecast
from stock_research_ai.application.industry_intelligence import build_industry_intelligence
from stock_research_ai.application.red_flags import DueDiligenceInputError
from stock_research_ai.application.report_claims import ReportCompositionError
from stock_research_ai.application.report_orchestrator import compose_from_stage
from stock_research_ai.application.report_panel import build_panel
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalystExecutionSpec
from stock_research_ai.domain.due_diligence import DueDiligenceRequest
from stock_research_ai.domain.final_research_report import FinalReportRequest
from stock_research_ai.domain.financial_canonical import PeriodType, ValueKind
from stock_research_ai.domain.forecasting import ForecastRequest
from stock_research_ai.domain.report_rendering import ReportManifest
from stock_research_ai.domain.summary import SummaryModelOutput, SummaryUsage
from stock_research_ai.interfaces import http as http_interface

NOW = datetime(2026, 10, 18, 5, 37, 30, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "reporting_basis"
HUDCO_STANDALONE = ("hudco_standalone.html", "https://www.screener.in/company/HUDCO/")
HUDCO_CONSOLIDATED = ("hudco_consolidated.html",
                      "https://www.screener.in/company/HUDCO/consolidated/")
#: comparable claims outside these sections are company-level analysis (analytical basis only)
CONTROL_SECTIONS = {"APPENDICES", "DATA_QUALITY_RECONCILIATION"}
ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def both_bases(**kw) -> list[V]:
    """Consolidated grows 100 a year; the standalone control is flat 500 and otherwise whole."""
    standalone = [
        V(r.metric, r.period, 500, basis=B.STANDALONE)
        if r.metric == M.REVENUE_FROM_OPERATIONS else r
        for r in company(basis=B.STANDALONE)
    ]
    return company(**kw) + standalone


def box5_of(rows: list[V]):
    data = make(rows)
    return data, build_financial_analytics(data, generated_at=NOW)


def input_bases(data, analytics, m) -> set:
    values = {v.canonical_value_id: v.reporting_basis for v in data.canonical_values}
    metrics = {x.metric_id: x.reporting_basis for x in analytics.all_metrics()}
    return {values[i] for i in m.input_value_ids} | {metrics[i] for i in m.input_metric_ids}


def calculated(analytics, code, basis, end=date(2026, 3, 31), ptype=PeriodType.FY):
    return [m for m in analytics.all_metrics() if m.code == code and m.reporting_basis == basis
            and m.period.period_end == end and m.period.period_type == ptype
            and m.status.value == "CALCULATED"]


# --- A: one deterministic basis -----------------------------------------------------------------


def test_a_both_bases_select_consolidated_deterministically():
    data = make(both_bases())
    shuffled = data.model_copy(update={"canonical_values": data.canonical_values[::-1]})
    assert select_analytical_basis(data) == select_analytical_basis(shuffled) == (
        B.CONSOLIDATED, "CONSOLIDATED: the primary analytical basis")
    assert select_analytical_basis(make(company(basis=B.STANDALONE)))[0] == B.STANDALONE
    assert select_analytical_basis(make([])) is None


def test_a_every_box_reports_the_same_selected_basis():
    data, box5 = box5_of(both_bases())
    box10 = build_forecast(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box10=box10)
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box4=data,
        box5=box5, box13=box13, box10=box10)
    assert box10.base.reporting_basis == B.CONSOLIDATED
    assert box13.analytical_basis.value == "CONSOLIDATED" == report.metadata.analytical_basis
    assert box13.control_basis.value == "STANDALONE" == report.metadata.control_basis


# --- D / E: peer benchmarks anchor on the analytical basis only ----------------------------------


def peer_case(target_rows: list[V]):
    data, box5 = box5_of(target_rows)
    data = data.model_copy(update={"company_key": "screener:1489"})
    box5 = build_financial_analytics(data, generated_at=NOW)
    from stock_research_ai.application.peer_selection import CompanyLayers
    target = CompanyLayers("screener:1489", screener("1489", "Widget Co", "WIDGET", IT,
                                                     PEER_ROWS), data, box5)
    peers = [layers("screener:2001", "Rival Systems", "RIVAL", IT),
             layers("screener:2008", "Solo Systems", "SOLO", IT, company(basis=B.STANDALONE))]
    return data, box5, build_industry_intelligence(target=target, peers=peers, generated_at=NOW)


def test_d_peer_benchmarks_use_the_detailed_basis_and_refuse_other_basis_peers():
    _data, box5, box9 = peer_case(both_bases())
    assert box9.financial_benchmarks
    assert {b.reporting_basis for b in box9.financial_benchmarks} == {B.CONSOLIDATED}
    roe = next(b for b in box9.financial_benchmarks if b.metric_code == "ROE")
    assert roe.target.value == calculated(box5, "ROE", B.CONSOLIDATED)[0].value
    solo = next(o for o in roe.peers if o.company_key == "screener:2008")
    assert solo.status.value == "BASIS_MISMATCH"  # a standalone-only peer is never pooled


def test_e_consolidated_selected_standalone_only_metric_is_not_substituted():
    # consolidated equity is missing, so consolidated ROE cannot be calculated; the
    # standalone ROE exists but must not become the target's ROE benchmark.
    rows = [r for r in both_bases() if not (r.basis == B.CONSOLIDATED and r.metric == M.RESERVES)]
    _data, box5, box9 = peer_case(rows)
    assert not calculated(box5, "ROE", B.CONSOLIDATED)
    assert calculated(box5, "ROE", B.STANDALONE)
    assert not any(b.metric_code == "ROE" for b in box9.financial_benchmarks)
    assert {b.reporting_basis for b in box9.financial_benchmarks} == {B.CONSOLIDATED}
    warning = next(w for w in box9.warnings if w.code == "TARGET_BASIS_UNAVAILABLE"
                   and (w.detail or "").startswith("ROE:"))
    assert "no calculated CONSOLIDATED value; STANDALONE not substituted" in (warning.detail or "")


# --- F: a consolidated value under a standalone analysis is refused ------------------------------


def test_f_standalone_selected_consolidated_value_never_enters():
    rows = company(basis=B.STANDALONE) + [
        V(M.EPS, fy(2026), "9.90", basis=B.CONSOLIDATED, kind=ValueKind.PER_SHARE)]
    data, box5 = box5_of(rows)
    assert select_analytical_basis(data)[0] == B.STANDALONE  # no consolidated MONEY values
    assert {f.reporting_basis for f in box5.frames} == {B.STANDALONE}
    assert all(m.reporting_basis == B.STANDALONE for m in box5.all_metrics())
    default = build_forecast(data, generated_at=NOW)
    assert default.base.reporting_basis == B.STANDALONE
    forced = build_forecast(data, request=ForecastRequest(reporting_basis=B.CONSOLIDATED),
                            generated_at=NOW)
    with pytest.raises(DueDiligenceInputError) as refused:
        build_due_diligence(data, box5, request=DueDiligenceRequest(
            as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box10=forced)
    assert refused.value.code == "BASIS_MISMATCH"


# --- G / H: derived metrics -----------------------------------------------------------------------


def test_g_mixed_basis_inputs_never_produce_a_derived_metric():
    pat_only = [r for r in company() if r.metric not in (M.RESERVES, M.EQUITY_SHARE_CAPITAL)]
    equity_only = [r for r in company(basis=B.STANDALONE)
                   if r.metric in (M.RESERVES, M.EQUITY_SHARE_CAPITAL)]
    data, box5 = box5_of(pat_only + equity_only)
    for basis in (B.CONSOLIDATED, B.STANDALONE):
        assert not calculated(box5, "ROE", basis)  # consolidated PAT x standalone equity: none
    assert not calculated(box5, "SHAREHOLDERS_EQUITY", B.CONSOLIDATED,
                          ptype=PeriodType.POINT_IN_TIME)
    for m in box5.all_metrics():
        if m.status.value == "CALCULATED":
            assert input_bases(data, box5, m) <= {m.reporting_basis}, m.code


def test_h_same_basis_inputs_still_calculate_on_each_basis():
    data, box5 = box5_of(both_bases())
    cons, alone = calculated(box5, "ROE", B.CONSOLIDATED), calculated(box5, "ROE", B.STANDALONE)
    assert cons and alone
    assert input_bases(data, box5, cons[0]) == {B.CONSOLIDATED}
    assert input_bases(data, box5, alone[0]) == {B.STANDALONE}


# --- I / J / K / L: quarters, years, balances, growth ---------------------------------------------


def test_i_j_k_every_calculation_stays_on_one_basis_for_every_period_type():
    quarters = [V(M.REVENUE_FROM_OPERATIONS, quarter(date(2025, 12, 31)), 400),
                V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 3, 31)), 440),
                V(M.REVENUE_FROM_OPERATIONS, quarter(date(2025, 3, 31)), 380),
                V(M.REVENUE_FROM_OPERATIONS, quarter(date(2026, 3, 31)), 90, basis=B.STANDALONE),
                V(M.REVENUE_FROM_OPERATIONS, quarter(date(2025, 12, 31)), 70,
                  basis=B.STANDALONE)]
    data, box5 = box5_of(both_bases() + quarters)
    seen = set()
    for m in box5.all_metrics():
        if m.status.value == "CALCULATED" and (m.input_value_ids or m.input_metric_ids):
            assert input_bases(data, box5, m) == {m.reporting_basis}, (m.code, m.period)
            seen.add(m.period.period_type)
    assert {PeriodType.FY, PeriodType.QUARTER, PeriodType.POINT_IN_TIME} <= seen
    qoq = calculated(box5, "REVENUE_GROWTH_QOQ", B.CONSOLIDATED, ptype=PeriodType.QUARTER)
    assert qoq and qoq[0].value == D("10.000000")  # 440 / 400, never 90 or 70


def test_l_growth_never_crosses_bases():
    data, box5 = box5_of(both_bases())
    assert calculated(box5, "REVENUE_GROWTH_YOY", B.CONSOLIDATED)[0].value == D("7.142857")
    assert calculated(box5, "REVENUE_GROWTH_YOY", B.STANDALONE)[0].value == D("0.000000")
    gap = [r for r in both_bases() if not (r.basis == B.CONSOLIDATED and r.metric ==
                                           M.REVENUE_FROM_OPERATIONS and r.period == fy(2025))]
    _data, broken = box5_of(gap)
    # FY2025 consolidated revenue is missing: the standalone FY2025 (500) is not a prior year
    assert not calculated(broken, "REVENUE_GROWTH_YOY", B.CONSOLIDATED)


# --- M: risk signals ------------------------------------------------------------------------------


def test_m_risk_signals_never_consume_the_other_basis():
    rows = [r for r in both_bases() if not (r.basis == B.CONSOLIDATED and r.metric == M.RESERVES)]
    data, box5 = box5_of(rows)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW)
    bases = {v.canonical_value_id: v.reporting_basis for v in data.canonical_values}
    bases |= {m.metric_id: m.reporting_basis for m in box5.all_metrics()}
    for s in box13.signals:
        assert s.basis.value in ("CONSOLIDATED", "NOT_APPLICABLE"), s.code
        used = {bases[r] for r in s.record_ids if r in bases}
        if s.basis.value == "CONSOLIDATED":
            assert used <= {B.CONSOLIDATED}, s.code


# --- N: forecasts ---------------------------------------------------------------------------------


def test_n_forecast_never_switches_to_the_other_basis():
    no_revenue = [r for r in both_bases() if not (r.basis == B.CONSOLIDATED and r.metric ==
                                                  M.REVENUE_FROM_OPERATIONS)]
    data = make(no_revenue)
    f = build_forecast(data, generated_at=NOW)
    # BEFORE: "first basis with annual revenue" silently produced a STANDALONE forecast
    assert f.base.reporting_basis == B.CONSOLIDATED == select_analytical_basis(data)[0]
    bases = {v.canonical_value_id: v.reporting_basis for v in data.canonical_values}
    used = {bases[i.canonical_value_id] for v in f.actuals
            for i in ((v.actual,) if v.actual else v.actual_components)}
    assert used <= {B.CONSOLIDATED}
    revenue = [v for v in f.actuals if v.line.value == "REVENUE"]
    assert revenue and all(v.status == "UNAVAILABLE" for v in revenue)


# --- O: final report composition ------------------------------------------------------------------


def test_o_report_refuses_a_layer_on_another_basis(monkeypatch):
    data, box5 = box5_of(both_bases())
    forced = build_forecast(data, request=ForecastRequest(reporting_basis=B.STANDALONE),
                            generated_at=NOW)
    request = DueDiligenceRequest(as_of=NOW.date(), company_key=data.company_key)
    with pytest.raises(DueDiligenceInputError) as refused:
        build_due_diligence(data, box5, request=request, generated_at=NOW, box10=forced)
    assert refused.value.code == "BASIS_MISMATCH"
    # defence in depth: even a Box 13 that let it through cannot get it into Box 14
    monkeypatch.setattr(dd, "_check_basis", lambda *a: None)
    box13 = build_due_diligence(data, box5, request=request, generated_at=NOW, box10=forced)
    with pytest.raises(ReportCompositionError) as composed:
        build_final_report(request=FinalReportRequest(as_of=NOW.date(),
                                                      company_key=data.company_key),
                           generated_at=NOW, box4=data, box5=box5, box13=box13, box10=forced)
    assert composed.value.code == "BASIS_MISMATCH"


def comparable_claims(report):
    return [c for c in report.claims if c.value is not None
            and c.claim_type.value in ("FACT", "CALCULATION", "SCENARIO")
            and not set(s.value for s in c.section_ids) <= CONTROL_SECTIONS]


def test_o_report_surfaces_share_one_basis():
    data, box5 = box5_of(both_bases())
    box10 = build_forecast(data, generated_at=NOW)
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box10=box10)
    report = build_final_report(request=FinalReportRequest(
        as_of=NOW.date(), company_key=data.company_key), generated_at=NOW, box4=data,
        box5=box5, box13=box13, box10=box10)
    assert comparable_claims(report)
    assert {c.basis.value for c in comparable_claims(report)} <= {"CONSOLIDATED",
                                                                 "NOT_APPLICABLE"}
    claims = {c.claim_id: c for c in report.claims}
    for entry in report.risks.risk_register:  # the Negatives panel's source
        assert claims[entry.claim_id].basis.value in ("CONSOLIDATED", "NOT_APPLICABLE")
    # the standalone control is still present and labelled (never silently dropped)
    assert any(c.basis.value == "STANDALONE" for c in report.claims)


# --- B / C / P: HUDCO, exact, offline -------------------------------------------------------------


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, system: str, user: str) -> SummaryModelOutput:
        self.calls += 1
        return SummaryModelOutput(
            summary_text=json.dumps({"claim_ids": []}), requested_model="m",
            reported_model="m", finish_reason="stop",
            usage=SummaryUsage(prompt_tokens=1, completion_tokens=1))


async def _summary_app(tmp_path, pages: dict[str, str]):
    settings = Settings(_env_file=None, database_path=tmp_path / "db.sqlite3",  # type: ignore[call-arg]
                        artifact_path=tmp_path / "artifacts", analyst_provider=None)
    await initialize_database(settings)
    database = SQLiteDatabase(settings.database_path)
    requests: list[str] = []

    def screener_pages(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        name = pages.get(request.url.path)
        if name is None:
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, content=(FIXTURES / name).read_bytes(),
                              headers={"content-type": "text/html; charset=utf-8"})

    store = FileRawArtifactStore(tmp_path / "artifacts")
    engine = ResearchEngine(database.repository, extraction=ExtractionServices(
        source=ScreenerSource(transport=httpx.MockTransport(screener_pages)), artifacts=store,
        normalizer=ScreenerNormalizer(), repository=database.extractions))
    jobs = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), lambda: (
        AnalystExecutionSpec(template_id="full-research-v1", template_version=1)))
    pairing = PairingStore(tmp_path / "auth.json")
    token = pairing.complete_pairing(ORIGIN, pairing.issue_pairing_code())
    model = _Model()
    app = http_interface.create_app(engine=engine, settings=settings, job_manager=jobs,
                                    pairing_store=pairing, summary_model_factory=lambda: model)
    return app, token, requests, database


async def _post_summary(app, token, url):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            return await client.post("/v1/summaries", json={
                "portal": "SCREENER", "source_url": url},
                headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"})


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", ORIGIN)


def test_b_summary_and_detailed_share_one_page_order():
    for url in (HUDCO_STANDALONE[1], HUDCO_CONSOLIDATED[1]):
        assert page_urls(url) == analytical_page_urls(url) == (
            HUDCO_CONSOLIDATED[1], HUDCO_STANDALONE[1])


async def _hudco_detailed(tmp_path, url):
    (tmp_path / "c").mkdir(parents=True)
    (tmp_path / "s").mkdir()
    pages = {HUDCO_CONSOLIDATED[1]: await page(tmp_path / "c", FIXTURES, HUDCO_CONSOLIDATED),
             HUDCO_STANDALONE[1]: await page(tmp_path / "s", FIXTURES, HUDCO_STANDALONE)}

    async def fetch_page(u: str):
        return pages[u]

    async def no_evidence(_):
        return None, None

    stage = await capture_stage(StageRequest(source_url=url, as_of=NOW.date()), now=NOW,
                                fetch_page=fetch_page, fetch_evidence=no_evidence,
                                fetch_exchange=None)
    report, _charts = compose_from_stage(stage, [])
    return report


def _rows(summary_input) -> dict[tuple[str, str], tuple[str, str]]:
    return {(r.metric_id, v.period): (str(v.value), r.reporting_scope.value)
            for b in ("annual", "balance_sheet") for r in getattr(summary_input, b).rows
            for v in r.values}


def _claim(report, metric, value_end=date(2026, 3, 31)):
    # every period ending on that date: FY2026, Q4 FY2026 and the balance sheet at 31-Mar-2026
    return [c for c in report.claims if c.metric == metric and c.period_end == value_end
            and "HISTORICAL_FINANCIALS" in {s.value for s in c.section_ids}]


async def test_p_hudco_summary_detailed_and_negatives_share_the_consolidated_basis(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "before").mkdir()
    app, token, requests, database = await _summary_app(tmp_path / "api", {
        "/company/HUDCO/consolidated/": HUDCO_CONSOLIDATED[0],
        "/company/HUDCO/": HUDCO_STANDALONE[0]})
    try:
        # the user's live request: Summary from the STANDALONE company page
        response = await _post_summary(app, token, HUDCO_STANDALONE[1])
    finally:
        await database.close()
    assert response.status_code == 200
    body = response.json()
    # the Detailed primary page first; the standalone view is fetched only to compare
    # freshness (a consolidated view that is not current is never the basis)
    assert requests == ["/company/HUDCO/consolidated/", "/company/HUDCO/"]
    assert body["company"]["reporting_scope"] == "consolidated"
    assert body["source_url"] == HUDCO_CONSOLIDATED[1]

    # BEFORE: the verbatim standalone page (bb3daa23) - kept to show what changed
    before = project_summary_input(
        (await page(tmp_path / "before", FIXTURES, HUDCO_STANDALONE))[0])
    assert before.company.reporting_scope.value == "standalone"
    assert _rows(before)[("revenue", "Mar 2026")] == ("13294", "standalone")

    (tmp_path / "now").mkdir()
    after_page = (await page(tmp_path / "now", FIXTURES, HUDCO_CONSOLIDATED))[0]
    after = _rows(project_summary_input(after_page))
    report = await _hudco_detailed(tmp_path / "detailed", HUDCO_STANDALONE[1])
    assert report.metadata.analytical_basis.value == "CONSOLIDATED"
    for metric, summary_key, value in (("REVENUE", "revenue", "13150"),
                                       ("PAT", "net_profit", "4034"),
                                       ("TOTAL_ASSETS", "total_assets", "166838")):
        assert after[(summary_key, "Mar 2026")] == (value, "consolidated")
        claims = _claim(report, metric)
        # (the Mar 2026 quarter shares the label: every such claim is consolidated)
        assert {c.basis.value for c in claims} == {"CONSOLIDATED"}, metric
        assert value in {str(c.value) for c in claims}, metric
    assert {c.basis.value for c in comparable_claims(report)} <= {"CONSOLIDATED",
                                                                 "NOT_APPLICABLE"}
    # the separate revenue-definition issue is NOT hidden: Screener's standalone 13294
    # remains in the labelled standalone control layer
    assert any(c.metric == "REVENUE" and c.value == D(13294) and c.basis.value == "STANDALONE"
               and {s.value for s in c.section_ids} <= CONTROL_SECTIONS for c in report.claims)
    # Negatives: the panel's risk lists come from the same single-basis risk register
    claims = {c.claim_id: c for c in report.claims}
    assert all(claims[e.claim_id].basis.value in ("CONSOLIDATED", "NOT_APPLICABLE")
               for e in report.risks.risk_register)
    manifest = ReportManifest.model_construct(
        artifact_id=report.report_id, screener_page="HUDCO", status="COMPLETE", files=(),
        failures=())
    panel = build_panel(report, manifest)
    assert (panel.analytical_basis, panel.control_basis) == ("CONSOLIDATED", "STANDALONE")


async def test_p_hudco_detailed_is_the_same_from_either_page(tmp_path):
    a = await _hudco_detailed(tmp_path / "a", HUDCO_STANDALONE[1])
    b = await _hudco_detailed(tmp_path / "b", HUDCO_CONSOLIDATED[1])
    assert a.metadata.analytical_basis == b.metadata.analytical_basis
    assert {(c.metric, c.period, str(c.value), c.basis) for c in comparable_claims(a)} == {
        (c.metric, c.period, str(c.value), c.basis) for c in comparable_claims(b)}


def test_p_hudco_exact_filing_dataset_keeps_one_basis_downstream():
    standalone = [  # the retained standalone Summary observations (extraction bb3daa23)
        V(M.REVENUE, fy(2026), 13294, basis=B.STANDALONE), V(M.REVENUE, fy(2025), 10345,
                                                             basis=B.STANDALONE),
        V(M.PAT, fy(2026), 4034, basis=B.STANDALONE), V(M.PAT, fy(2025), 2709,
                                                        basis=B.STANDALONE)]
    sa = [o.model_copy(update={  # own IDs: make() IDs are index-derived, like the secondaries'
        "company_key": KEY, "observation_id": uuid5(NAMESPACE_URL, f"bb3daa23|{o.observation_id}"),
    }) for o in make(standalone).observations]
    data = reconcile([*filing_observations(), *secondary_observations(), *sa])
    box5 = build_financial_analytics(data, generated_at=NOW)
    assert select_analytical_basis(data)[0] == B.CONSOLIDATED
    roe = calculated(box5, "ROE", B.CONSOLIDATED)
    assert roe and roe[0].value == D("20.198379")  # the corrected consolidated ROE
    box10 = build_forecast(data, generated_at=NOW)
    assert box10.base.reporting_basis == B.CONSOLIDATED
    box13 = build_due_diligence(data, box5, request=DueDiligenceRequest(
        as_of=NOW.date(), company_key=KEY), generated_at=NOW, box10=box10)
    report = build_final_report(request=FinalReportRequest(as_of=NOW.date(), company_key=KEY),
                                generated_at=NOW, box4=data, box5=box5, box13=box13,
                                box10=box10)
    assert {c.basis.value for c in comparable_claims(report)} <= {"CONSOLIDATED",
                                                                 "NOT_APPLICABLE"}
    assert not any(c.value == D(13294) for c in comparable_claims(report))
    calcs = [c for c in comparable_claims(report) if c.metric == "ROE"]
    assert calcs and all(c.value == D("20.198379") for c in calcs if c.period == "FY2026")
    assert all(c.calculation_ids for c in calcs)  # provenance still names the calculation


# --- Q / R / S: ONE basis rule at the page level too (carries_statements) ---------------------
#
# Summary chooses a page; Detailed Research chooses select_analytical_basis over Box 4. Both now
# use one criterion: a page is the analytical-basis page only when it carries statement values
# on its own basis. A consolidated view naming the company but holding no consolidated
# statements (only security-level snapshot metrics) previously made the Summary 'consolidated'
# with no statement rows while Detailed Research was STANDALONE.

SCREENER_FIXTURES = Path(__file__).parent / "fixtures" / "screener"
INFY_PAGE = (str(SCREENER_FIXTURES / "infy_standalone.html"), "https://www.screener.in/company/INFY/")
YESBANK_PAGE = (str(SCREENER_FIXTURES / "yesbank_consolidated.html"),
                "https://www.screener.in/company/YESBANK/consolidated/")


def _no_statements(tmp_path: Path) -> str:
    """HUDCO's consolidated view with every statement section removed (company still named)."""
    import re
    html = (FIXTURES / HUDCO_CONSOLIDATED[0]).read_text()
    for section in ("quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios"):
        html = re.sub(rf'<section id="{section}".*?</section>', "", html, flags=re.S)
    path = tmp_path / "hudco_consolidated_no_statements.html"
    path.write_text(html)
    return str(path)


async def _detailed(tmp_path: Path, url: str, pages: dict[str, str]):
    """Detailed Research through capture_stage; `pages`: URL -> fixture file (absent = blocked)."""
    acquired = {}
    for i, (u, name) in enumerate(pages.items()):
        (tmp_path / str(i)).mkdir(parents=True)
        acquired[u] = await page(tmp_path / str(i), FIXTURES, (name, u))

    async def fetch_page(u: str):
        if u not in acquired:
            raise RuntimeError("blocked")
        return acquired[u]

    async def no_evidence(_):
        return None, None

    stage = await capture_stage(StageRequest(source_url=url, as_of=NOW.date()), now=NOW,
                                fetch_page=fetch_page, fetch_evidence=no_evidence,
                                fetch_exchange=None)
    report, _charts = compose_from_stage(stage, [])
    panel = build_panel(report, ReportManifest.model_construct(
        artifact_id=report.report_id, screener_page="X", status="COMPLETE", files=(),
        failures=()))
    return report, panel


async def _summary_scope(tmp_path: Path, url: str, pages: dict[str, str]):
    (tmp_path / "api").mkdir(parents=True)
    paths = {"/" + u.split("://www.screener.in/", 1)[1]: name for u, name in pages.items()}
    app, token, requests, database = await _summary_app(tmp_path / "api", paths)
    try:
        response = await _post_summary(app, token, url)
    finally:
        await database.close()
    assert response.status_code == 200, response.text
    body = response.json()
    return body["company"]["reporting_scope"], body["source_url"], requests


async def test_q_carries_statements_is_the_page_level_basis_rule(tmp_path):
    from stock_research_ai.adapters.screener.source import carries_statements
    (tmp_path / "c").mkdir()
    (tmp_path / "s").mkdir()
    (tmp_path / "e").mkdir()
    assert carries_statements((await page(tmp_path / "c", FIXTURES, HUDCO_CONSOLIDATED))[0])
    assert carries_statements((await page(tmp_path / "s", FIXTURES, HUDCO_STANDALONE))[0])
    empty = (await page(tmp_path / "e", FIXTURES, (_no_statements(tmp_path),
                                                    HUDCO_CONSOLIDATED[1])))[0]
    assert empty.company is not None and empty.top_metrics  # snapshot metrics only
    assert not carries_statements(empty)
    assert not carries_statements(empty.model_copy(update={"company": None}))


@pytest.mark.parametrize("entry", [HUDCO_STANDALONE[1], HUDCO_CONSOLIDATED[1]])
async def test_r_consolidated_view_without_statements_never_splits_summary_and_detailed(
        tmp_path, entry):
    pages = {HUDCO_CONSOLIDATED[1]: _no_statements(tmp_path),
             HUDCO_STANDALONE[1]: HUDCO_STANDALONE[0]}
    scope, source, requests = await _summary_scope(tmp_path / "summary", entry, pages)
    report, panel = await _detailed(tmp_path / "detailed", entry, pages)
    # BEFORE: Summary 'consolidated' (no statement rows) vs Detailed STANDALONE
    assert (scope, source) == ("standalone", HUDCO_STANDALONE[1])
    assert requests == ["/company/HUDCO/consolidated/", "/company/HUDCO/"]
    assert report.metadata.analytical_basis.value == "STANDALONE"
    assert report.metadata.control_basis is None  # no consolidated statements to control
    assert (panel.analytical_basis, panel.control_basis) == ("STANDALONE", None)
    assert {c.basis.value for c in comparable_claims(report)} <= {"STANDALONE",
                                                                 "NOT_APPLICABLE"}
    claims = {c.claim_id: c for c in report.claims}
    assert all(claims[e.claim_id].basis.value in ("STANDALONE", "NOT_APPLICABLE")
               for e in report.risks.risk_register)  # Negatives


@pytest.mark.parametrize("company,entry,pages,expected,control", [
    # both bases: CONSOLIDATED from either view, standalone as the labelled control layer
    ("HUDCO", HUDCO_STANDALONE[1], {HUDCO_CONSOLIDATED[1]: HUDCO_CONSOLIDATED[0],
                                    HUDCO_STANDALONE[1]: HUDCO_STANDALONE[0]},
     "CONSOLIDATED", "STANDALONE"),
    # G: only a standalone page acquirable -> STANDALONE everywhere, no control layer
    ("INFY", INFY_PAGE[1], {INFY_PAGE[1]: INFY_PAGE[0]}, "STANDALONE", None),
    # H: only the consolidated page acquirable -> CONSOLIDATED everywhere
    ("YESBANK", YESBANK_PAGE[1], {YESBANK_PAGE[1]: YESBANK_PAGE[0]}, "CONSOLIDATED", None),
])
async def test_s_company_matrix_one_basis_in_every_output(tmp_path, company, entry, pages,
                                                           expected, control):
    scope, _source, _requests = await _summary_scope(tmp_path / "summary", entry, pages)
    report, panel = await _detailed(tmp_path / "detailed", entry, pages)
    m = report.metadata
    assert scope.upper() == expected, company  # Summary
    assert m.analytical_basis.value == expected  # Box 13 -> Box 14 / 15
    assert (m.control_basis.value if m.control_basis else None) == control
    assert (panel.analytical_basis, panel.control_basis) == (expected, control)
    claims = {c.claim_id: c for c in report.claims}
    assert {c.basis.value for c in comparable_claims(report)} <= {expected, "NOT_APPLICABLE"}
    assert all(claims[e.claim_id].basis.value in (expected, "NOT_APPLICABLE")
               for e in report.risks.risk_register)  # Negatives


# --- T: a consolidated basis that is not current never becomes the research's latest year --------
#
# TFCI (live, 2026-10-21): Screener's consolidated statements stop at Mar 2022 while standalone
# runs to Mar 2026. BEFORE: CONSOLIDATED was chosen because consolidated values existed, so
# Summary and Detailed both presented FY2022 as the latest year. AFTER: STANDALONE everywhere.

TFCI_CONSOLIDATED = ("tfci_consolidated.html", "https://www.screener.in/company/TFCILTD/consolidated/")
TFCI_STANDALONE = ("tfci_standalone.html", "https://www.screener.in/company/TFCILTD/")
TFCI_PAGES = {TFCI_CONSOLIDATED[1]: TFCI_CONSOLIDATED[0], TFCI_STANDALONE[1]: TFCI_STANDALONE[0]}


async def test_t_stale_consolidated_statements_never_drive_the_box4_basis(tmp_path):
    from stock_research_ai.adapters.screener.research_dataset import build_screener_dataset
    from stock_research_ai.application.financial_analytics import latest_fiscal_year_end
    from stock_research_ai.application.financial_reconciliation import build_canonical_dataset
    (tmp_path / "c").mkdir()
    (tmp_path / "s").mkdir()
    screeners = []
    for n, case in (("c", TFCI_CONSOLIDATED), ("s", TFCI_STANDALONE)):
        screeners.append(build_screener_dataset(*await page(tmp_path / n, FIXTURES, case), None))
    data = build_canonical_dataset(company_key=screeners[0].company_key,
                                   company_name=screeners[0].profile.name,
                                   screeners=tuple(screeners), bundle=None, catalogue=None,
                                   generated_at=NOW)
    assert latest_fiscal_year_end(data, B.CONSOLIDATED) == date(2022, 3, 31)
    assert latest_fiscal_year_end(data, B.STANDALONE) == date(2026, 3, 31)
    basis, reason = select_analytical_basis(data)
    assert basis == B.STANDALONE and "not current" in reason and "2022-03-31" in reason


@pytest.mark.parametrize("entry", [TFCI_STANDALONE[1], TFCI_CONSOLIDATED[1]])
async def test_t_tfci_summary_detailed_and_negatives_use_the_current_standalone_basis(
        tmp_path, entry):
    scope, source, requests = await _summary_scope(tmp_path / "summary", entry, TFCI_PAGES)
    assert (scope, source) == ("standalone", TFCI_STANDALONE[1])
    assert requests == ["/company/TFCILTD/consolidated/", "/company/TFCILTD/"]  # compared
    report, panel = await _detailed(tmp_path / "detailed", entry, TFCI_PAGES)
    m = report.metadata
    assert (m.analytical_basis.value, m.control_basis, str(m.latest_fiscal_year_end)) == (
        "STANDALONE", None, "2026-03-31")
    assert (panel.analytical_basis, panel.control_basis) == ("STANDALONE", None)
    assert {c.basis.value for c in comparable_claims(report)} <= {"STANDALONE", "NOT_APPLICABLE"}
    assert not [c for c in report.claims if c.basis.value == "CONSOLIDATED"]  # no stale FY2022
    claims = {c.claim_id: c for c in report.claims}
    assert all(claims[e.claim_id].basis.value in ("STANDALONE", "NOT_APPLICABLE")
               for e in report.risks.risk_register)  # Negatives
