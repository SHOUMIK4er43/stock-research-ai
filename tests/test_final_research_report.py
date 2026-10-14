"""Box 14 final research report composer: one structured, source-auditable dossier.

Every fixture is a real upstream dataset built offline by the real Box 3-13 engines (synthetic
PDFs through Box 1-3, a synthetic Box 4, real Box 5 / 6 / 7 / 8 / 10 / 11 / 12 / 13 builds).
Adversarial cases forge a composed report and show the dataset contract refuses it.
"""

import ast
import asyncio
import socket
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import test_fundamental_valuation as tv
import test_market_intelligence as tm
from pydantic import ValidationError
from test_business_intelligence import COMPANY, GEOGRAPHY, T, ar, filings, segment_table
from test_financial_analytics import B, M, V, company, fy, make, quarter
from test_management_intelligence import BSE, NOW, Q4FY25, Q4FY26

from stock_research_ai.application.business_intelligence import build_business_intelligence
from stock_research_ai.application.due_diligence import (
    build_due_diligence,
    canonical_due_diligence_json,
    canonical_market_json,
)
from stock_research_ai.application.financial_analytics import (
    build_financial_analytics,
    canonical_analytics_json,
)
from stock_research_ai.application.financial_charts import build_chart_bundle
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.forecasting import build_forecast, canonical_forecast_json
from stock_research_ai.application.fundamental_valuation import build_valuation
from stock_research_ai.application.management_intelligence import build_management_intelligence
from stock_research_ai.application.report_claims import Book, ReportCompositionError, url_key
from stock_research_ai.domain import due_diligence as dd
from stock_research_ai.domain import final_research_report as fr
from stock_research_ai.domain.final_research_report import (
    ClaimBasis,
    ClaimType,
    FinalReportRequest,
    FinalResearchReportDataset,
    Layer,
    OrderBucketKind,
    ReportLimits,
    SectionId,
    SectionStatus,
    SourceType,
)
from stock_research_ai.domain.financial_canonical import Industry, PeriodType

S = SectionId
CT = ClaimType
AS_OF = date(2026, 10, 10)
FY26 = date(2026, 3, 31)
ROOT = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
NEW_MODULES = ("domain/final_research_report.py", "application/report_claims.py",
               "application/report_sections.py", "application/final_report_composer.py")
SRC = dd.DiligenceSource(authority=dd.SourceAuthority.EXCHANGE_FILING,
                         reference="Annual report FY2026, notes", url="https://www.bseindia.com/"
                         "xml-data/corpfiling/AttachHis/widget-ar-2026.pdf")
EPC = {"business_model": "ORDER_BOOK_DRIVEN", "business_model_source": SRC}
QUARTERS = [date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31),
            date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31),
            date(2026, 6, 30), date(2026, 9, 30)]
AR_PAGES = ar(["Widget Co Ltd Annual Report 2025-26",
               "Widget Co is a specialty chemicals company that manufactures performance "
               "chemicals for pharma and agri customers."], segment_table(), GEOGRAPHY)


# --- the upstream stack ---------------------------------------------------------------------------


@dataclass
class Stack:
    box4: Any
    box5: Any
    bundle: Any
    box6: Any
    box7: Any
    box8: Any
    box10: Any
    box11: Any
    box12: Any


def financial_rows() -> list[V]:
    rows = company() + company(basis=B.STANDALONE) + [V(M.CAPEX, fy(y), 60)
                                                      for y in range(2021, 2027)]
    for i, end in enumerate(QUARTERS):
        rows += [V(M.REVENUE_FROM_OPERATIONS, quarter(end), Decimal(400 + 10 * i)),
                 V(M.OPERATING_PROFIT, quarter(end), Decimal(80 + i)),
                 V(M.PAT, quarter(end), Decimal(50 + i))]
    return rows


def holdings() -> tuple[dd.HoldingPoint, ...]:
    ends = [date(2023, 6, 30) + timedelta(days=0)]
    ends = [date(y, m, d) for y in (2023, 2024, 2025, 2026)
            for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
            if date(2023, 6, 30) <= date(y, m, d) <= date(2026, 6, 30)]
    return tuple(dd.HoldingPoint(period_end=e, category=dd.HolderCategory.PROMOTER,
                                 pct=Decimal("72.84"), source=SRC) for e in ends)


def request13(**kw: Any) -> dd.DueDiligenceRequest:
    base: dict[str, Any] = {
        "as_of": AS_OF, "company_key": COMPANY.company_key, "nse_symbol": tm.SYMBOL, **EPC,
        "contingent_liabilities": (
            dd.ContingentItem(as_of=FY26, basis="STANDALONE", category="LEGAL_CLAIM",
                              description="arbitration claims not acknowledged as debts",
                              amount_crore=Decimal("1500"), source=SRC),
            dd.ContingentItem(as_of=FY26, basis="STANDALONE", category="TAX",
                              description="income-tax demands under appeal",
                              amount_crore=Decimal("40"), source=SRC)),
        "ratings": (dd.RatingInput(action_date=date(2026, 1, 5), agency="CARE",
                                   instrument="Long-term bank facilities",
                                   rating="CARE AA; Stable", outlook="STABLE",
                                   action="REAFFIRMED",
                                   sensitivities="Sustained decline in operating margins",
                                   source=SRC),),
        "holdings": holdings(),
    }
    base.update(kw)
    return dd.DueDiligenceRequest(**base)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Stack:
    box4 = make(financial_rows()).model_copy(update={"company_key": COMPANY.company_key})
    box5 = build_financial_analytics(box4, generated_at=NOW)
    bundle, catalogue = asyncio.run(filings(tmp_path_factory.mktemp("box14"), {
        "/ar2026.pdf": AR_PAGES, "/q4fy25.pdf": (T.CONCALL_TRANSCRIPT, Q4FY25, BSE),
        "/q4fy26.pdf": (T.CONCALL_TRANSCRIPT, Q4FY26, BSE)}))
    box7 = build_business_intelligence(box4=box4, bundle=bundle, catalogue=catalogue,
                                       screener=None, generated_at=NOW)
    box8 = build_management_intelligence(bundle=bundle, catalogue=catalogue, box4=box4,
                                         box5=box5, box7=box7, generated_at=NOW)
    box10 = build_forecast(box4, management=box8, generated_at=NOW)
    box11 = build_valuation(box10, request=tv.request(
        methods=tuple(tv.VM), extra=[tv.a(tv.K.MINORITY_INTEREST, 0)]), box4=box4,
        generated_at=NOW)
    box12 = tm.build(tm.order_payloads(), document_texts=tm.docs())
    box6 = build_chart_bundle(box4, box5, generated_at=NOW)
    return Stack(box4, box5, bundle, box6, box7, box8, box10, box11, box12)


LAYERS = ("bundle", "box7", "box8", "box10", "box11", "box12")


def box13(st: Stack, *, skip: tuple[str, ...] = (), **kw: Any) -> dd.DueDiligenceDataset:
    upstream = {k: getattr(st, k) for k in LAYERS if k not in skip}
    upstream.update({k: v for k, v in kw.items() if k in LAYERS})
    req = request13(**{k: v for k, v in kw.items() if k not in LAYERS})
    return build_due_diligence(st.box4, st.box5, request=req, generated_at=NOW, **upstream)


def compose(st: Stack, d13: dd.DueDiligenceDataset | None = None, *,
            skip: tuple[str, ...] = (), generated_at: Any = NOW, **kw: Any
            ) -> FinalResearchReportDataset:
    upstream: dict[str, Any] = {k: getattr(st, k) for k in (*LAYERS, "box6") if k not in skip}
    upstream.update({k: kw.pop(k) for k in list(kw) if k in (*LAYERS, "box6", "box9")})
    req = FinalReportRequest(as_of=kw.pop("as_of", AS_OF), company_key=kw.pop(
        "company_key", COMPANY.company_key), nse_symbol=kw.pop("nse_symbol", tm.SYMBOL))
    return fr_build(request=req, generated_at=generated_at, box4=kw.pop("box4", st.box4),
                    box5=kw.pop("box5", st.box5),
                    box13=d13 if d13 is not None else box13(st, skip=skip), **upstream, **kw)


def fr_build(**kw: Any) -> FinalResearchReportDataset:
    from stock_research_ai.application.final_report_composer import build_final_report
    return build_final_report(**kw)


@pytest.fixture(scope="module")
def d13(stack: Stack) -> dd.DueDiligenceDataset:
    return box13(stack)


@pytest.fixture(scope="module")
def report(stack: Stack, d13: dd.DueDiligenceDataset) -> FinalResearchReportDataset:
    return compose(stack, d13)


def claims(r: FinalResearchReportDataset) -> dict[UUID, fr.ReportClaim]:
    return {c.claim_id: c for c in r.claims}


def forge(r: FinalResearchReportDataset, mutate: Any) -> None:
    data = r.model_dump()
    mutate(data)
    with pytest.raises(ValidationError):
        FinalResearchReportDataset.model_validate(data)


def index_of(data: dict[str, Any], kind: str, pred: Any) -> int:
    return next(i for i, x in enumerate(data[kind]) if pred(x))


# --- A-C. the dataset, its sections and their statuses --------------------------------------------


def test_one_dataset_with_every_canonical_section_in_order_and_a_status(report):
    assert isinstance(report, FinalResearchReportDataset)
    assert [s.section_id for s in report.sections] == list(fr.SECTION_ORDER)
    assert len(report.sections) == 23 and [s.order for s in report.sections] == list(range(23))
    assert report.sections[0].section_id == S.DUE_DILIGENCE_STATUS
    assert report.sections[1].section_id == S.EXECUTIVE_SUMMARY
    for s in report.sections:
        assert s.status in SectionStatus and s.status_reason
        c = s.completeness
        assert set(c.available) | set(c.missing) | set(c.not_applicable) == set(c.required)
    assert report.recommendation == "NONE" and report.executive_summary.recommendation == "NONE"
    # no Box 15 work: no rendered page, chart or markdown report in the dataset
    text = report.model_dump_json()
    assert "<svg" not in text and "<html" not in text.lower()


def test_identity_bases_and_input_references(report, stack, d13):
    m = report.metadata
    assert m.company_key == COMPANY.company_key and m.nse_symbol == tm.SYMBOL
    assert m.analytical_basis == ClaimBasis.CONSOLIDATED
    assert m.control_basis == ClaimBasis.STANDALONE
    assert m.as_of == AS_OF and m.generated_at == NOW
    refs = {r.layer: r for r in m.inputs}
    assert set(refs) == {Layer.BOX3, Layer.BOX4, Layer.BOX5, Layer.BOX6, Layer.BOX7,
                         Layer.BOX8, Layer.BOX10, Layer.BOX11, Layer.BOX12, Layer.BOX13}
    import hashlib

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    assert refs[Layer.BOX4].sha256 == sha(canonical_dataset_json(stack.box4))
    assert refs[Layer.BOX5].sha256 == sha(canonical_analytics_json(stack.box5))
    assert refs[Layer.BOX10].sha256 == sha(canonical_forecast_json(stack.box10))
    assert refs[Layer.BOX12].sha256 == sha(canonical_market_json(stack.box12))
    assert refs[Layer.BOX13].sha256 == sha(canonical_due_diligence_json(d13))
    assert all(r.assessed_by_box13 for layer, r in refs.items() if layer != Layer.BOX6)


def test_inputs_are_never_mutated(stack, d13):
    before = [x.model_dump_json() for x in (*(getattr(stack, k) for k in (*LAYERS, "box4",
                                                                          "box5", "box6")),
                                            d13)]
    compose(stack, d13)
    after = [x.model_dump_json() for x in (*(getattr(stack, k) for k in (*LAYERS, "box4",
                                                                         "box5", "box6")),
                                           d13)]
    assert before == after


def test_deterministic_output_independent_of_generated_at(stack, d13, report):
    from stock_research_ai.application.final_report_composer import canonical_report_json
    later = compose(stack, d13, generated_at=NOW.replace(year=2031))
    assert canonical_report_json(later) == canonical_report_json(report)
    assert later.report_id == report.report_id
    assert [c.claim_id for c in later.claims] == [c.claim_id for c in report.claims]


async def test_async_wrapper_runs_off_the_loop(stack, d13, report):
    from stock_research_ai.application.final_report_composer import build_final_report_async
    req = FinalReportRequest(as_of=AS_OF, company_key=COMPANY.company_key,
                             nse_symbol=tm.SYMBOL)
    out = await build_final_report_async(
        request=req, generated_at=NOW, box4=stack.box4, box5=stack.box5, box13=d13,
        bundle=stack.bundle, box6=stack.box6, box7=stack.box7, box8=stack.box8,
        box10=stack.box10, box11=stack.box11, box12=stack.box12)
    assert out.report_id == report.report_id


# --- D-F. claims: provenance, calculation references, kinds kept apart ----------------------------


def test_every_numeric_claim_is_traceable_to_a_document_or_record(report):
    sources = {s.source_id: s for s in report.sources}
    for c in report.claims:
        if c.claim_type != CT.ANALYST_INFERENCE:
            assert c.upstream, c.topic
        if c.value is None:
            continue
        assert c.source_ids, c.topic
        kinds = {sources[s].source_type for s in c.source_ids}
        if c.claim_type in (CT.FACT, CT.CALCULATION):
            assert kinds - {SourceType.UPSTREAM_DATASET}, c.topic
        if c.claim_type == CT.SCENARIO:
            assert {u.layer for u in c.upstream} & {Layer.BOX10, Layer.BOX11}, c.topic


def test_calculation_claims_cite_their_upstream_calculation_and_formula(report, stack):
    metric_ids = {m.metric_id for name in ("growth", "returns", "profitability", "leverage",
                                           "cash_flow_quality", "accruals", "building_blocks")
                  for m in getattr(stack.box5, name)}
    calcs = [c for c in report.claims if c.claim_type == CT.CALCULATION]
    assert calcs and all(c.calculation_ids and c.formula_ids for c in calcs)
    growth = next(c for c in calcs if c.topic == "REVENUE_GROWTH_YOY")
    assert set(growth.calculation_ids) <= metric_ids
    assert any(u.layer == Layer.BOX5 and u.kind == "METRIC" for u in growth.upstream)


def test_numbers_are_copied_never_recomputed(report, stack):
    by_id = {v.canonical_value_id: v for v in stack.box4.canonical_values}
    facts = [c for c in report.claims if c.claim_type == CT.FACT and any(
        u.kind == "CANONICAL_VALUE" for u in c.upstream) and c.topic != "ORDER_CHAIN"]
    assert facts
    for c in facts:
        (u,) = [u for u in c.upstream if u.kind == "CANONICAL_VALUE"]
        assert c.value == by_id[u.record_id].value
    t = report.table("ANNUAL_FINANCIALS")
    all_claims = claims(report)
    for row in t.rows:
        for cell in row.cells:
            if cell.value is not None:
                assert all_claims[cell.claim_id].value == cell.value


def test_claim_kinds_stay_separate(report):
    for c in report.claims:
        if c.claim_type == CT.MANAGEMENT_STATEMENT:
            assert any(u.layer == Layer.BOX8 for u in c.upstream)
        if c.claim_type == CT.SCENARIO:
            assert c.scenario is not None
        else:
            assert c.scenario is None
        if c.claim_type == CT.DATA_LIMITATION:
            assert c.limitations and c.polarity == fr.Polarity.NEUTRAL
    kinds = {c.claim_type for c in report.claims}
    assert {CT.FACT, CT.CALCULATION, CT.MANAGEMENT_STATEMENT, CT.SCENARIO,
            CT.DATA_LIMITATION} <= kinds


def test_claim_contract_rejects_untraceable_or_mislabelled_claims():
    up = fr.UpstreamRecord(layer=Layer.BOX4, kind="CANONICAL_VALUE", record_id=uuid4())
    base: dict[str, Any] = {"claim_id": uuid4(), "claim_type": CT.FACT, "topic": "PAT",
                            "statement": "PAT reported 10.", "basis": ClaimBasis.CONSOLIDATED,
                            "upstream": (up,)}
    fr.ReportClaim(**base)
    with pytest.raises(ValidationError, match="provenance"):
        fr.ReportClaim(**base | {"value": Decimal(10)})  # numeric without a source
    with pytest.raises(ValidationError, match="upstream records"):
        fr.ReportClaim(**base | {"upstream": ()})
    with pytest.raises(ValidationError, match="calculation"):
        fr.ReportClaim(**base | {"claim_type": CT.CALCULATION})  # no calc / formula reference
    with pytest.raises(ValidationError, match="scenario"):
        fr.ReportClaim(**base | {"claim_type": CT.SCENARIO})  # scenario without a scenario
    with pytest.raises(ValidationError, match="scenario"):
        fr.ReportClaim(**base | {"scenario": fr.Scenario.BASE})  # a forecast labelled a fact
    with pytest.raises(ValidationError, match="Box 8"):
        fr.ReportClaim(**base | {"claim_type": CT.MANAGEMENT_STATEMENT})
    with pytest.raises(ValidationError, match="Box 10"):
        fr.ReportClaim(**base | {"claim_type": CT.SCENARIO, "scenario": fr.Scenario.BASE})
    with pytest.raises(ValidationError, match="inference"):
        fr.ReportClaim(**base | {"claim_type": CT.ANALYST_INFERENCE, "upstream": ()})
    with pytest.raises(ValidationError, match="inference"):
        fr.ReportClaim(**base | {"claim_type": CT.ANALYST_INFERENCE,
                                 "derived_from": (uuid4(),), "value": Decimal(1),
                                 "source_ids": (uuid4(),)})
    with pytest.raises(ValidationError, match="limitation"):
        fr.ReportClaim(**base | {"claim_type": CT.DATA_LIMITATION})
    with pytest.raises(ValidationError, match="never a statement"):
        fr.ReportClaim(**base | {"claim_type": CT.DATA_LIMITATION, "limitations": ("X",),
                                 "polarity": fr.Polarity.NEGATIVE})


# --- G-I. basis -----------------------------------------------------------------------------------


def test_consolidated_core_and_standalone_control_layer(report):
    all_claims = claims(report)
    annual = report.table("ANNUAL_FINANCIALS")
    assert annual.basis == ClaimBasis.CONSOLIDATED and not annual.appendix
    assert {all_claims[i].basis for i in annual.claim_ids} <= {ClaimBasis.CONSOLIDATED,
                                                               ClaimBasis.NOT_APPLICABLE}
    control = report.table("STANDALONE_FINANCIALS")
    assert control.appendix and control.basis == ClaimBasis.STANDALONE
    assert {all_claims[i].basis for i in control.claim_ids} == {ClaimBasis.STANDALONE}
    assert all(c.basis in ClaimBasis for c in report.claims)


def test_a_table_never_silently_mixes_bases(report):
    standalone = next(c for c in report.claims if c.basis == ClaimBasis.STANDALONE
                      and c.value is not None and c.topic == "PAT")

    def mix(data: dict[str, Any]) -> None:
        t = index_of(data, "tables", lambda x: x["code"] == "ANNUAL_FINANCIALS")
        cell = next(c for row in data["tables"][t]["rows"] for c in row["cells"]
                    if c["value"] is not None)
        cell["claim_id"], cell["value"] = standalone.claim_id, standalone.value
    forge(report, mix)


def test_reconciliation_uses_box13_statuses_and_keeps_bases_apart(report, d13):
    assert len(report.reconciliation) == len(d13.standalone_consolidated_reconciliation)
    statuses = {c.comparison_id: c.status.value for c in d13.standalone_consolidated_reconciliation}
    all_claims = claims(report)
    for r in report.reconciliation:
        assert r.status == statuses[r.comparison_id]
        if r.standalone_claim_id:
            assert all_claims[r.standalone_claim_id].basis == ClaimBasis.STANDALONE
        if r.consolidated_claim_id:
            assert all_claims[r.consolidated_claim_id].basis == ClaimBasis.CONSOLIDATED
        if r.status in ("UNAVAILABLE", "NOT_COMPARABLE"):
            assert r.classification == "DATA_LIMITATION"
            assert all_claims[r.claim_id].claim_type == CT.DATA_LIMITATION
    table = report.table("STANDALONE_CONSOLIDATED")
    assert {c.key: c.basis for c in table.columns}["STANDALONE"] == ClaimBasis.STANDALONE


# --- J. executive summary built last --------------------------------------------------------------


def test_executive_summary_only_cites_completed_section_claims(report):
    body = {i for s in report.sections if s.section_id != S.EXECUTIVE_SUMMARY
            for i in (*s.claim_ids, *s.limitation_claim_ids)}
    summary = report.executive_summary
    listed = {i for p in summary.points for i in p.claim_ids}
    assert listed and listed <= body
    assert summary.built_after == tuple(s for s in SectionId if s != S.EXECUTIVE_SUMMARY)
    assert len(summary.primary_claim_ids) <= fr.MAX_SUMMARY_PRIMARY
    kinds = {p.kind for p in summary.points}
    assert kinds == set(fr.SummaryKind)
    for p in summary.points:
        assert (p.status == "AVAILABLE") == bool(p.claim_ids or p.monitoring_item_ids)


def test_summary_balance_keeps_both_risks_and_positive_evidence(report):
    all_claims = claims(report)
    listed = [all_claims[i] for p in report.executive_summary.points for i in p.claim_ids]
    assert report.risks.red and report.risks.positive_controls
    assert any(c.polarity == fr.Polarity.NEGATIVE for c in listed)
    assert any(c.polarity == fr.Polarity.POSITIVE for c in listed)
    red_claims = {e.claim_id for e in report.risks.risk_register if e.risk_id in report.risks.red}
    assert red_claims <= set(report.executive_summary.risk_claim_ids)

    def erase_positive(data: dict[str, Any]) -> None:
        for p in data["executive_summary"]["points"]:
            if p["kind"] == "STRENGTHS":
                p["claim_ids"], p["status"], p["reason"] = (), "UNAVAILABLE", "x"
        data["executive_summary"]["primary_claim_ids"] = tuple(
            i for p in data["executive_summary"]["points"] if p["kind"] in fr.PRIMARY_KINDS
            for i in p["claim_ids"])
    forge(report, erase_positive)


def test_summary_claim_limit_is_enforced(report):
    with pytest.raises(ValidationError):
        fr.ExecutiveSummary(built_after=(), points=(), primary_claim_ids=tuple(
            uuid4() for _ in range(16)), risk_claim_ids=(), limitation_claim_ids=())


def test_summary_may_not_invent_a_claim(report):
    stray = next(c for c in report.claims if c.claim_id not in {
        i for s in report.sections for i in (*s.claim_ids, *s.limitation_claim_ids)})

    def add(data: dict[str, Any]) -> None:
        point = data["executive_summary"]["points"][0]
        point["claim_ids"] = (*point["claim_ids"], stray.claim_id)
    forge(report, add)


# --- K. source register ---------------------------------------------------------------------------


def test_source_register_is_deduplicated(report, stack):
    keys = [s.key for s in report.sources]
    assert len(keys) == len(set(keys))
    assert len({s.source_id for s in report.sources}) == len(report.sources)
    filings = [s for s in report.sources if s.source_type == SourceType.FILING]
    assert filings and all(s.url is None or "#" not in s.url for s in report.sources)
    # every Box 13 citation of the same annual report is one source, however often cited
    ar_sources = [s for s in report.sources if s.url and "widget-ar-2026" in s.url]
    assert len(ar_sources) == 1 and ar_sources[0].claim_count > 1
    for s in report.sources:
        assert s.layers and s.authority and s.title
    used = {i for c in report.claims for i in c.source_ids}
    assert used <= {s.source_id for s in report.sources}


def test_url_keys_drop_locators_but_keep_documents():
    a = url_key("https://www.screener.in/company/RVNL/consolidated/#balance-sheet/tr[1]")
    b = url_key("https://WWW.screener.in/company/RVNL/consolidated/#profit-loss/tr[9]")
    assert a == b == "https://www.screener.in/company/RVNL/consolidated/"
    assert url_key("https://x.in/a.pdf?id=1") != url_key("https://x.in/a.pdf?id=2")
    assert url_key("not a url") is None and url_key("ftp://x/y") is None


def test_book_deduplicates_claims_and_sources():
    book = Book("k", AS_OF, ReportLimits())
    up = fr.UpstreamRecord(layer=Layer.BOX4, kind="CANONICAL_VALUE", record_id=uuid4())
    s1 = book.source(Layer.BOX4, SourceType.FILING, "X", url="https://a.in/r.pdf#p1")
    s2 = book.source(Layer.BOX13, SourceType.FILING, "Y", url="https://a.in/r.pdf#p9")
    assert s1 == s2 and book.source_count == 1
    kw: dict[str, Any] = {"basis": ClaimBasis.CONSOLIDATED, "upstream": (up,),
                          "sources": (s1,), "value": Decimal(5)}
    c1 = book.claim(S.HISTORICAL_FINANCIALS, CT.FACT, "PAT", "PAT 5.", **kw)
    c2 = book.claim(S.RISKS, CT.FACT, "PAT", "PAT 5.", **kw)
    assert c1 == c2
    claims_, sources_, _ = book.finish({})
    assert len(claims_) == 1 and claims_[0].section_ids == (S.HISTORICAL_FINANCIALS, S.RISKS)
    assert sources_[0].authority == ("BOX13:Y", "BOX4:X")


def test_one_fact_is_one_claim_across_sections(report):
    referenced: dict[UUID, set[SectionId]] = {}
    for s in report.sections:
        for i in s.claim_ids:
            referenced.setdefault(i, set()).add(s.section_id)
    assert any(len(v) > 1 for v in referenced.values())  # shared, not copied
    content = [(c.claim_type, c.topic, c.metric, c.period, c.basis, c.scenario, c.value,
                c.statement, c.source_text, tuple(sorted(str(u.record_id) for u in c.upstream)))
               for c in report.claims]
    assert len(content) == len(set(content))
    pat = [c for c in report.claims if c.topic == "PAT" and c.period == "Mar 2026"
           and c.basis == ClaimBasis.CONSOLIDATED and c.claim_type == CT.FACT]
    assert len(pat) == 1  # the annual figure, cited by the table and the risk inputs alike

    def duplicate(data: dict[str, Any]) -> None:
        data["claims"] = [*data["claims"], data["claims"][0]]
    forge(report, duplicate)


def test_different_readings_of_one_record_are_never_merged():
    book = Book("k", AS_OF, ReportLimits())
    up = fr.UpstreamRecord(layer=Layer.BOX7, kind="DATASET", record_id=uuid4())
    src = book.source(Layer.BOX7, SourceType.PROVIDER_RECORD, "C", url="https://a.in/c")
    kw: dict[str, Any] = {"basis": ClaimBasis.NOT_APPLICABLE, "upstream": (up,),
                          "sources": (src,)}
    a = book.claim(None, CT.FACT, "CLASSIFICATION", "Classification (Sector).",
                   source_text="Industrials", **kw)
    b = book.claim(None, CT.FACT, "CLASSIFICATION", "Classification (Industry).",
                   source_text="Railways", **kw)
    assert a != b and book.get(a).source_text == "Industrials"


def test_unknown_source_and_risk_ids_are_refused(report):
    def fake_source(data: dict[str, Any]) -> None:
        i = index_of(data, "claims", lambda c: c["source_ids"])
        data["claims"][i]["source_ids"] = (uuid4(),)
    forge(report, fake_source)

    def fake_risk(data: dict[str, Any]) -> None:
        data["claims"][0]["risk_ids"] = (uuid4(),)
    forge(report, fake_risk)


def test_company_fact_never_rests_on_analyst_input_alone(report):
    def analyst(data: dict[str, Any]) -> None:
        i = index_of(data, "claims", lambda c: c["claim_type"] == "FACT" and c["source_ids"])
        for sid in data["claims"][i]["source_ids"]:
            j = index_of(data, "sources", lambda s, sid=sid: s["source_id"] == sid)
            data["sources"][j]["source_type"] = "ANALYST_INPUT"
    forge(report, analyst)


# --- L-M. disagreements, risk vs data limitation --------------------------------------------------


@pytest.fixture(scope="module")
def conflicted(stack):
    other = SRC.model_copy(update={"url": "https://www.nseindia.com/pledge-2026.pdf"})
    d = box13(stack, pledges=(
        dd.PledgePoint(period_end=date(2026, 6, 30), pledged_pct_of_promoter_holding=3,
                       source=SRC),
        dd.PledgePoint(period_end=date(2026, 6, 30), pledged_pct_of_promoter_holding=25,
                       source=other)))
    return d, compose(stack, d)


def test_input_conflict_stays_a_data_limitation_never_a_company_risk(conflicted):
    d, r = conflicted
    pledge = d.by_code("PROMOTER_PLEDGE")[0]
    assert pledge.status.value == "UNAVAILABLE" and pledge.reason == "INPUT_CONFLICT"
    entry = next(e for e in r.risks.risk_register if e.code == "PROMOTER_PLEDGE")
    claim = claims(r)[entry.claim_id]
    assert claim.claim_type == CT.DATA_LIMITATION
    assert "SOURCE_CONFLICT_UNRESOLVED" in claim.limitations
    assert entry.risk_id not in (*r.risks.red, *r.risks.yellow)
    assert any(x.claim_id == entry.claim_id for x in r.risks.data_limitations)
    assert r.due_diligence_status.open_source_conflicts == d.source_disagreements.open_count > 0


def test_source_disagreements_keep_every_value(conflicted):
    d, r = conflicted
    assert len(r.source_disagreements) == len(d.source_disagreements.entries)
    view = next(x for x in r.source_disagreements if x.status == "OPEN")
    assert view.canonical_selection is None and len(view.values) >= 2
    assert {v.value for v in view.values} >= {Decimal(3), Decimal(25)}
    assert claims(r)[view.claim_id].statement.startswith("Source disagreement remains open")
    assert r.due_diligence_status.conflict_claim_ids


def test_company_risks_and_data_limitations_never_share_a_list(report, d13):
    assert set(report.risks.red) == {i for i in d13.red_flags
                                     if d13.signal(i).severity == dd.Severity.RED}
    assert set(report.risks.yellow) == {i for i in d13.red_flags
                                        if d13.signal(i).severity == dd.Severity.YELLOW}
    assert set(report.risks.positive_controls) == set(d13.positive_controls)
    assert len(report.risks.risk_register) == len(d13.signals)
    all_claims = claims(report)
    assert all(all_claims[x.claim_id].claim_type == CT.DATA_LIMITATION
               for x in report.risks.data_limitations)
    risk_claims = {e.claim_id for e in report.risks.risk_register
                   if e.risk_id in (*report.risks.red, *report.risks.yellow)}
    assert all(all_claims[i].claim_type == CT.CALCULATION for i in risk_claims)

    def promote(data: dict[str, Any]) -> None:
        unavailable = next(e for e in data["risks"]["risk_register"]
                           if e["status"] == "UNAVAILABLE")
        data["risks"]["red"] = (*data["risks"]["red"], unavailable["risk_id"])
    forge(report, promote)


def test_open_diligence_and_data_quality_are_preserved(report, d13):
    assert len(report.risks.open_diligence) == len(d13.open_diligence_items)
    assert {o.item_id for o in report.risks.open_diligence} == {
        i.item_id for i in d13.open_diligence_items}
    dq_ids = {x.entry_id for x in report.risks.data_limitations}
    assert {i.item_id for i in d13.data_quality_summary.items} <= dq_ids
    appendix = next(a for a in report.appendices if a.appendix_id == fr.AppendixId.OPEN_DILIGENCE)
    assert appendix.entries == len(d13.open_diligence_items)


def test_section_zero_is_a_status_not_a_score(report):
    z = report.due_diligence_status
    assert z.scoring == "NONE" and z.families_checked == len(fr.InputFamily)
    assert sum(n for _, n in z.sections_by_status) == z.sections_total == 23
    assert z.valuation_status.startswith("Box 11") and z.forecast_status.startswith("Box 10")
    assert "percent" not in z.model_dump_json().lower()
    missing = {f.family for f in z.input_families if f.status == fr.FamilyStatus.MISSING}
    assert fr.InputFamily.BOX9_INDUSTRY in missing


# --- N. order book / L1 ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def l1_only(stack):
    ledger = tm.build([*tm.base_payloads(), tm.nse_announcements(tm.announcement(
        "03-Sep-2026 16:06:07", "Updates", tm.L1_TEXT, seq="1001", url=tm.L1_URL))],
        document_texts=tm.docs())
    return compose(stack, box13(stack, box12=ledger), box12=ledger), ledger


def test_l1_never_enters_the_confirmed_order_book(l1_only):
    r, ledger = l1_only
    buckets = {b.bucket: b for b in r.order_book.buckets}
    assert set(buckets) == set(OrderBucketKind)
    chain = ledger.order_chains[0]
    assert chain.chain_id in buckets[OrderBucketKind.PENDING_L1].chain_ids
    assert chain.chain_id not in buckets[OrderBucketKind.CONFIRMED_ORDER_BOOK].chain_ids
    all_claims = claims(r)
    for b in r.order_book.buckets:
        for i in b.value_claim_ids:
            l1 = "L1" in all_claims[i].topic.split("_")
            assert l1 == (b.bucket != OrderBucketKind.CONFIRMED_ORDER_BOOK)
    assert r.order_book.combined_total == "NONE"
    assert r.order_book.l1_policy == "L1_NEVER_CONFIRMED_REPORTED_SEPARATELY"
    pending_rows = [row for row in r.table("ORDER_LEDGER").rows]
    assert pending_rows and all(c.value is None for row in pending_rows for c in row.cells
                                if c.column == "CONFIRMED")


def test_confirmed_and_pending_are_separate_labelled_buckets(report):
    labels = {b.bucket: b.label for b in report.order_book.buckets}
    assert "CONFIRMED" in labels[OrderBucketKind.CONFIRMED_ORDER_BOOK]
    assert "PENDING L1" in labels[OrderBucketKind.PENDING_L1]
    assert "UNRESOLVED L1" in labels[OrderBucketKind.UNRESOLVED_L1]
    assert "SOURCE INCOMPLETE" in labels[OrderBucketKind.SOURCE_INCOMPLETE]
    confirmed = next(b for b in report.order_book.buckets
                     if b.bucket == OrderBucketKind.CONFIRMED_ORDER_BOOK)
    assert len(confirmed.chain_ids) == 1


def test_forged_order_buckets_are_refused(l1_only):
    r, ledger = l1_only
    chain = ledger.order_chains[0].chain_id

    def double(data: dict[str, Any]) -> None:
        for b in data["order_book"]["buckets"]:
            if b["bucket"] == "CONFIRMED_ORDER_BOOK":
                b["chain_ids"] = (chain,)
    forge(r, double)
    l1_value = next((c for c in r.claims if "L1" in c.topic.split("_")
                     and c.claim_type != CT.DATA_LIMITATION), None)
    assert l1_value is not None

    def l1_in_confirmed(data: dict[str, Any]) -> None:
        for b in data["order_book"]["buckets"]:
            if b["bucket"] == "CONFIRMED_ORDER_BOOK":
                b["value_claim_ids"] = (l1_value.claim_id,)
    forge(r, l1_in_confirmed)


def test_counted_l1_chain_is_refused_upstream(l1_only):
    _, ledger = l1_only
    chain = ledger.order_chains[0]
    with pytest.raises(ValidationError):
        type(ledger).model_validate(ledger.model_dump() | {"order_chains": (
            chain.model_copy(update={"counted_in_confirmed": True}).model_dump(),)})


# --- O-T. forecasts, scenarios, valuation ---------------------------------------------------------


def test_forecasts_are_labelled_scenarios_never_history(report, stack):
    all_claims = claims(report)
    for scenario in stack.box10.scenarios:
        t = report.table(f"FORECAST_{scenario.value}")
        kinds = {c.key: c.kind for c in t.columns}
        assert fr.ColumnKind.FORECAST in kinds.values()
        for row in t.rows:
            assert row.kind == fr.RowKind.FORECAST
            for cell in row.cells:
                if cell.claim_id is None:
                    continue
                c = all_claims[cell.claim_id]
                if kinds[cell.column] == fr.ColumnKind.FORECAST:
                    assert c.claim_type == CT.SCENARIO and c.scenario.value == scenario.value
                else:
                    assert c.claim_type != CT.SCENARIO
        assumptions = report.table(f"ASSUMPTIONS_{scenario.value}")
        assert all(all_claims[i].claim_type == CT.SCENARIO for i in assumptions.claim_ids)
        assert all(any(u.kind == "ASSUMPTION" for u in all_claims[i].upstream)
                   for i in assumptions.claim_ids)


def test_forecast_mislabelled_as_history_is_refused(report):
    scenario = next(c for c in report.claims if c.claim_type == CT.SCENARIO
                    and c.value is not None and c.basis == ClaimBasis.CONSOLIDATED)

    def relabel(data: dict[str, Any]) -> None:
        t = index_of(data, "tables", lambda x: x["code"] == "ANNUAL_FINANCIALS")
        cell = next(c for row in data["tables"][t]["rows"] for c in row["cells"]
                    if c["value"] is not None)
        cell["claim_id"], cell["value"] = scenario.claim_id, scenario.value
    forge(report, relabel)


def test_bull_base_bear_come_from_box10_with_no_probability(report, stack):
    cases = {c.case: c for c in report.investment_case.cases}
    assert set(cases) == {"BULL_CASE", "BASE_CASE", "BEAR_CASE"}
    all_claims = claims(report)
    for c in cases.values():
        assert c.probability == "NOT_PROVIDED"
        for i in (*c.driver_claim_ids, *c.forecast_claim_ids, *c.valuation_claim_ids):
            assert all_claims[i].claim_type == CT.SCENARIO
            assert all_claims[i].scenario == c.scenario
        assert c.conditions_required and c.invalidation_conditions
    assert report.investment_case.expected_value == "NONE"
    assert report.investment_case.recommendation == "NONE"
    ids = {v.value_id for v in stack.box10.values} | {a.assumption_id
                                                       for a in stack.box10.assumptions}
    for c in cases.values():
        for i in (*c.driver_claim_ids, *c.forecast_claim_ids):
            assert {u.record_id for u in all_claims[i].upstream} & ids


def test_valuation_methods_stay_separate_with_status_and_limitations(report, stack):
    v = report.valuation
    assert v.blended_value == "NONE" and v.target_price == "NONE"
    assert len(v.methods) == len(stack.box11.results)
    all_claims = claims(report)
    for m in v.methods:
        assert m.status in ("COMPLETE", "PARTIAL", "UNAVAILABLE")
        if m.status != "COMPLETE":
            assert m.limitation_claim_ids and m.reason
        for i in (*m.result_claim_ids, *m.assumption_claim_ids):
            assert all_claims[i].claim_type == CT.SCENARIO and all_claims[i].scenario == (
                m.scenario)
    gordon = [m for m in v.methods if m.method == "DCF_FCFF_GORDON"]
    assert {m.scenario for m in gordon} == {fr.Scenario.BASE, fr.Scenario.BULL,
                                           fr.Scenario.BEAR}
    per_share = [all_claims[i] for m in gordon for i in m.result_claim_ids
                 if all_claims[i].topic == "VALUATION_FAIR_VALUE_PER_SHARE"]
    assert len(per_share) == 3
    box11 = {r.scenario.value: r.fair_value_per_share for r in stack.box11.results
             if r.method.value == "DCF_FCFF_GORDON"}
    assert {c.scenario.value: c.value for c in per_share} == box11
    point = next(p for p in report.executive_summary.points
                 if p.kind == fr.SummaryKind.VALUATION_RANGE)
    assert point.status == "AVAILABLE" and {all_claims[i].scenario for i in point.claim_ids}
    assert all(all_claims[i].topic == "VALUATION_FAIR_VALUE_PER_SHARE"
               for i in point.claim_ids)


def test_missing_valuation_and_forecast_are_stated(stack):
    no_value = compose(stack, box13(stack, skip=("box11",)), skip=("box11",))
    assert no_value.section(S.VALUATION).status == SectionStatus.UNAVAILABLE
    assert any(c.topic == "LAYER_BOX11" for c in no_value.claims)
    assert not no_value.due_diligence_status.valuation_inputs_complete
    point = next(p for p in no_value.executive_summary.points
                 if p.kind == fr.SummaryKind.VALUATION_RANGE)
    assert point.status == "UNAVAILABLE" and point.reason
    none = compose(stack, box13(stack, skip=("box10", "box11")), skip=("box10", "box11"))
    assert none.section(S.FORECASTS).status == SectionStatus.UNAVAILABLE
    assert none.section(S.SCENARIO_ANALYSIS).status == SectionStatus.UNAVAILABLE
    assert none.investment_case.cases == ()


# --- P / management -------------------------------------------------------------------------------


def test_guidance_is_a_management_statement_and_outcomes_link_separately(report, stack):
    all_claims = claims(report)
    assert len(report.guidance_tracker) == len(stack.box8.guidance)
    outcomes = {o.guidance_id: o.result.value for o in stack.box8.outcomes}
    for g in report.guidance_tracker:
        assert all_claims[g.statement_claim_id].claim_type == CT.MANAGEMENT_STATEMENT
        assert g.result == outcomes[g.guidance_id]
        if g.actual_claim_id is not None:
            assert all_claims[g.actual_claim_id].claim_type in (CT.FACT, CT.CALCULATION)
    open_items = [g for g in report.guidance_tracker if g.result == "NOT_YET_DUE"]
    assert open_items and all(g.actual_claim_id is None for g in open_items)
    statements = [c for c in report.claims if c.claim_type == CT.MANAGEMENT_STATEMENT]
    assert statements and all(c.source_text for c in statements)

    def as_fact(data: dict[str, Any]) -> None:
        target = report.guidance_tracker[0].statement_claim_id
        i = index_of(data, "claims", lambda c: c["claim_id"] == target)
        data["claims"][i]["claim_type"] = "FACT"
    forge(report, as_fact)


def test_missing_management_data_is_explicit(stack):
    r = compose(stack, box13(stack, skip=("box8",)), skip=("box8",))
    sec = r.section(S.CONCALL_GUIDANCE)
    assert sec.status == SectionStatus.UNAVAILABLE and not r.guidance_tracker
    assert any(claims(r)[i].topic == "CONCALLS" for i in sec.limitation_claim_ids)


# --- historical / quarterly / cash flow / forensics -----------------------------------------------


def test_historical_annual_table_has_five_years_from_box4_and_box5(report, stack):
    t = report.table("ANNUAL_FINANCIALS")
    assert len(t.columns) == 5 and all(c.kind == fr.ColumnKind.HISTORICAL for c in t.columns)
    keys = [row.key for row in t.rows]
    for key in ("REVENUE", "PAT", "CFO", "GROSS_DEBT", "ROE", "ROCE", "EBITDA"):
        assert key in keys
    revenue = next(row for row in t.rows if row.key == "REVENUE")
    fy26 = next(v for v in stack.box4.canonical_values if v.metric == M.REVENUE_FROM_OPERATIONS
                and v.period.period_end == FY26 and v.reporting_basis == B.CONSOLIDATED)
    assert revenue.cells[-1].value == fy26.value
    full = report.table("ANNUAL_FINANCIALS_FULL")
    assert full.appendix and len(full.columns) == 6


def test_quarterly_table_is_not_annualised_and_growth_is_upstream(report, stack):
    t = report.table("QUARTERLY_FINANCIALS")
    assert len(t.columns) == 8 and t.columns[-1].period_end == QUARTERS[-1]
    full = report.table("QUARTERLY_FINANCIALS_FULL")
    assert len(full.columns) == len(QUARTERS)
    all_claims = claims(report)
    yoy = next(row for row in t.rows if row.key == "REVENUE_GROWTH_YOY")
    metric_ids = {m.metric_id for m in stack.box5.growth}
    for cell in yoy.cells:
        if cell.claim_id is not None:
            assert set(all_claims[cell.claim_id].calculation_ids) <= metric_ids


def test_cash_flow_forensics_and_contingencies(report, d13):
    assert report.table("CASH_FLOW_BALANCE_SHEET").basis == ClaimBasis.CONSOLIDATED
    total = next(c for c in report.claims if c.topic == "CONTINGENT_LIABILITIES_TOTAL")
    assert total.value == d13.contingent_liabilities.total_crore == Decimal(1540)
    flag = d13.by_code("CONTINGENT_LIABILITY")[0]
    assert flag.severity == dd.Severity.RED and flag.risk_id in report.risks.red
    assert total.basis == ClaimBasis.STANDALONE and total.derived_from
    rows = report.table("FORENSIC_MODELS").rows
    shown = {r.key for r in rows}
    assert shown == {f"{m.model}/{m.variant}" for m in d13.financial_forensics.models}
    for m in d13.financial_forensics.models:
        row = next(r for r in rows if r.key == f"{m.model}/{m.variant}")
        assert (row.cells[0].value is not None) == (m.status == "CALCULATED")


# --- events, materiality, appendices --------------------------------------------------------------


@pytest.fixture(scope="module")
def big_ledger(stack):
    rows = [tm.announcement((date(2025, 1, 1) + timedelta(days=i)).strftime(
        "%d-%b-%Y 10:00:00"), "Change in Key Managerial Personnel",
        "Change in Key Managerial Personnel - appointment of Company Secretary",
        seq=str(5000 + i)) for i in range(120)]
    rows += [tm.announcement("02-Feb-2026 10:00:00", "General Updates",
                             f"Newspaper publication {i}", seq=str(9000 + i))
             for i in range(60)]
    ledger = tm.build([*tm.base_payloads(), tm.nse_announcements(*rows)])
    return ledger, compose(stack, box13(stack, box12=ledger), box12=ledger,
                           limits=ReportLimits(max_core_events=10, max_appendix_rows=50))


def test_core_events_are_selected_and_the_full_ledger_goes_to_the_appendix(big_ledger):
    ledger, r = big_ledger
    core = r.table("MATERIAL_EVENTS")
    assert 0 < len(core.rows) <= 10 + len([e for e in r.risks.risk_register])
    ledger_table = r.table("EVENT_LEDGER")
    assert ledger_table.appendix and len(ledger_table.rows) == 50 and ledger_table.truncated
    appendix = next(a for a in r.appendices if a.appendix_id == fr.AppendixId.EVENT_LEDGER)
    assert appendix.truncated and appendix.upstream_entries == len(ledger.events)
    limitation = claims(r)[appendix.limitation_claim_id]
    assert limitation.claim_type == CT.DATA_LIMITATION
    assert "APPENDIX_LIMIT_REACHED" in limitation.limitations
    titles = {c.text for row in core.rows for c in row.cells if c.column == "TITLE"}
    assert not any("Newspaper" in (t or "") for t in titles)  # immaterial events stay out


def test_appendices_cover_the_exhaustive_layer(report):
    ids = {a.appendix_id for a in report.appendices}
    assert ids == set(fr.AppendixId)
    tables = {t.table_id for t in report.tables}
    for a in report.appendices:
        assert set(a.table_ids) <= tables
        if a.status == SectionStatus.COMPLETE and a.appendix_id != fr.AppendixId.SOURCE_REGISTER:
            assert a.entries > 0
    register = next(a for a in report.appendices
                    if a.appendix_id == fr.AppendixId.SOURCE_REGISTER)
    assert register.entries == len(report.sources)
    risk = next(a for a in report.appendices if a.appendix_id == fr.AppendixId.RISK_REGISTER)
    assert risk.entries == len(report.risks.risk_register)


def test_truncated_appendix_must_surface_its_limitation():
    with pytest.raises(ValidationError):
        fr.Appendix(appendix_id=fr.AppendixId.EVENT_LEDGER, title="x",
                    status=SectionStatus.COMPLETE, table_ids=(), claim_ids=(), entries=1,
                    upstream_entries=9, truncated=True)


# --- charts, monitoring, what changes the view ----------------------------------------------------


def test_chart_references_point_at_box6_definitions_only(report, stack):
    assert report.charts
    upstream = {c.chart_id: c for c in stack.box6.charts}
    for ref in report.charts:
        assert ref.upstream_chart_id in upstream
        assert ref.chart_code == upstream[ref.upstream_chart_id].chart_code
        assert ref.chart_id in report.section(ref.section_id).chart_ids
    assert all(ref.basis != ClaimBasis.STANDALONE or ref.section_id == S.APPENDICES
               for ref in report.charts)


def test_what_changes_the_view_uses_upstream_thresholds_only(report, d13):
    assert report.what_changes_the_view
    for v in report.what_changes_the_view:
        for t in v.thresholds:
            signal = next(s for s in d13.signals if s.rule_id == t.rule_id)
            assert t.value in {x.value for x in signal.threshold}
        assert "PRICE" not in v.subject
    directions = {v.direction for v in report.what_changes_the_view}
    assert fr.ViewDirection.STRENGTHENS in directions
    assert fr.ViewDirection.RESOLVES_UNCERTAINTY in directions


def test_monitoring_items_are_structured_and_cite_existing_records(report):
    topics = {m.topic for m in report.monitoring}
    assert {"NEXT_RESULTS", "CFO_CONVERSION", "DEBT", "ORDER_INFLOW", "RATING"} <= topics
    assert len({m.item_id for m in report.monitoring}) == len(report.monitoring)


# --- sector gating, industry ----------------------------------------------------------------------


def test_financial_sector_gates_industrial_sections():
    box4 = make(company(), Industry.BANK)
    box5 = build_financial_analytics(box4, generated_at=NOW)
    d = build_due_diligence(box4, box5, request=dd.DueDiligenceRequest(
        as_of=AS_OF, company_key=box4.company_key), generated_at=NOW)
    r = fr_build(request=FinalReportRequest(as_of=AS_OF, company_key=box4.company_key),
                 generated_at=NOW, box4=box4, box5=box5, box13=d)
    assert r.metadata.sector_gate == "FINANCIAL"
    order = r.section(S.ORDER_BOOK)
    assert order.status == SectionStatus.NOT_APPLICABLE and not r.order_book.applicable
    cash = r.table("CASH_FLOW_BALANCE_SHEET")
    inventory = next((row for row in cash.rows if row.key == "INVENTORY_DAYS"), None)
    if inventory is not None:
        assert all(c.status != fr.CellStatus.AVAILABLE for c in inventory.cells)
        assert any(c.status == fr.CellStatus.NOT_APPLICABLE for c in inventory.cells)
    models = r.table("FORENSIC_MODELS")
    altman = [row for row in models.rows if row.key.startswith("ALTMAN_Z")]
    assert altman and all(c.status == fr.CellStatus.NOT_APPLICABLE
                          for row in altman for c in row.cells)


def test_industry_section_from_box9():
    import test_industry_intelligence as ti
    box9 = ti.build(ti.target(), ti.it_peers())
    box4, box5 = ti.BOX4, ti.target().box5
    d = build_due_diligence(box4, box5, request=dd.DueDiligenceRequest(
        as_of=AS_OF, company_key=box4.company_key), generated_at=NOW, box9=box9)
    r = fr_build(request=FinalReportRequest(as_of=AS_OF, company_key=box4.company_key),
                 generated_at=NOW, box4=box4, box5=box5, box13=d, box9=box9)
    sec = r.section(S.INDUSTRY_COMPETITION)
    assert sec.status == SectionStatus.COMPLETE
    peers = r.table("PEER_BENCHMARKS")
    all_claims = claims(r)
    medians = [all_claims[c.claim_id] for row in peers.rows for c in row.cells
               if c.column == "PEER_MEDIAN" and c.claim_id]
    assert medians and all(m.claim_type == CT.CALCULATION for m in medians)
    assert all(any(u.layer == Layer.BOX9 for u in m.upstream) for m in medians)


def test_missing_industry_is_unavailable_not_invented(report):
    sec = report.section(S.INDUSTRY_COMPETITION)
    assert sec.status == SectionStatus.UNAVAILABLE and not sec.claim_ids
    assert sec.limitation_claim_ids


# --- AC. company / as_of integrity ----------------------------------------------------------------


def test_mismatched_company_fails_closed(stack, d13):
    with pytest.raises(ReportCompositionError, match="COMPANY_MISMATCH"):
        compose(stack, d13, company_key="screener:9999")
    other = stack.box10.model_copy(update={"company_key": "screener:9999"})
    with pytest.raises((ReportCompositionError, ValidationError)):
        compose(stack, d13, box10=other)


def test_a_dataset_box13_did_not_assess_is_refused(stack):
    without = box13(stack, skip=("box8",))
    with pytest.raises(ReportCompositionError, match="UNASSESSED_INPUT"):
        compose(stack, without)  # Box 8 supplied to Box 14 but never assessed by Box 13
    other12 = tm.build(tm.order_payloads())
    with pytest.raises(ReportCompositionError, match="BOX12_MISMATCH"):
        compose(stack, box13(stack), box12=other12)


def test_identity_must_bind(stack, d13):
    with pytest.raises(ReportCompositionError, match="IDENTITY_MISMATCH"):
        compose(stack, d13, nse_symbol="IRCON")


def test_report_as_of_bounds_every_input(stack, d13):
    with pytest.raises(ReportCompositionError, match="INCOMPATIBLE_PERIOD"):
        compose(stack, d13, as_of=date(2026, 9, 1))  # before Box 13 / Box 4 periods
    last = len(stack.box12.events) - 1
    late = stack.box12.model_copy(update={"events": tuple(
        e.model_copy(update={"event_date": date(2027, 1, 1)}) if i == last else e
        for i, e in enumerate(stack.box12.events))})
    with pytest.raises(ReportCompositionError, match="FUTURE_DATED_EVENT"):
        compose(stack, box13(stack, box12=late), box12=late)


def test_non_scenario_claims_after_as_of_are_refused(report):
    def future(data: dict[str, Any]) -> None:
        i = index_of(data, "claims", lambda c: c["claim_type"] == "FACT")
        data["claims"][i]["period_end"] = date(2030, 1, 1)
    forge(report, future)


# --- AD. advice language --------------------------------------------------------------------------


@pytest.mark.parametrize("words", ["We rate this a BUY", "Strong sell", "hold for now",
                                   "Target price of 500", "position size 2%", "buy zone",
                                   "SELL TRIGGER at 100", "Overweight the stock",
                                   "underweight"])
def test_advice_language_is_rejected(words):
    up = fr.UpstreamRecord(layer=Layer.BOX4, kind="CANONICAL_VALUE", record_id=uuid4())
    with pytest.raises(ValidationError, match="advice"):
        fr.ReportClaim(claim_id=uuid4(), claim_type=CT.FACT, topic="X", statement=words,
                       basis=ClaimBasis.CONSOLIDATED, upstream=(up,))


def test_quoted_source_text_and_ordinary_words_are_not_advice():
    up = fr.UpstreamRecord(layer=Layer.BOX8, kind="STATEMENT", record_id=uuid4())
    fr.ReportClaim(claim_id=uuid4(), claim_type=CT.MANAGEMENT_STATEMENT, topic="X",
                   statement="Management statement on capital allocation.",
                   source_text="We will hold our dividend and buy back shares; target price"
                               " discipline", basis=ClaimBasis.NOT_APPLICABLE, upstream=(up,))
    for ok in ("Board approved a buyback", "Promoter shareholding", "PROMOTER_SELLING rule",
               "Holding company structure"):
        assert fr.advice_term(ok) is None


def test_report_written_table_text_is_advice_checked(report):
    def advise(data: dict[str, Any]) -> None:
        t = index_of(data, "tables", lambda x: any(
            c["text_origin"] == "REPORT" for row in x["rows"] for c in row["cells"]))
        for row in data["tables"][t]["rows"]:
            for c in row["cells"]:
                if c["text_origin"] == "REPORT":
                    c["text"] = "BUY"
                    return
    forge(report, advise)


# --- AE. resource bounds --------------------------------------------------------------------------


@pytest.mark.parametrize("limits", [ReportLimits(max_claims=50), ReportLimits(max_tables=3),
                                    ReportLimits(max_sources=3), ReportLimits(max_rows=3)])
def test_resource_bounds_fail_explicitly(stack, d13, limits):
    with pytest.raises(ReportCompositionError, match="RESOURCE_LIMIT_EXCEEDED"):
        compose(stack, d13, limits=limits)


def test_narrative_budget_is_bounded(report):
    def shrink(data: dict[str, Any]) -> None:
        data["metadata"]["limits"]["max_narrative_chars"] = 100
    forge(report, shrink)


# --- Z. offline, layering -------------------------------------------------------------------------


def test_composition_makes_no_network_call(stack, d13, monkeypatch):
    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("Box 14 must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert compose(stack, d13).sections


def test_new_modules_import_no_adapter_network_or_ai_code():
    banned = ("httpx", "socket", "urllib.request", "requests", "anthropic",
              "stock_research_ai.adapters", "stock_research_ai.interfaces",
              "stock_research_ai.application.svg_renderer", "stock_research_ai.v2")
    for rel in NEW_MODULES:
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not any(name == b or name.startswith(b + ".") for b in banned), (rel,
                                                                                         name)
        source = (ROOT / rel).read_text()
        assert "datetime.now" not in source and "uuid4" not in source and "random" not in source


def test_no_rating_or_recommendation_fields(report):
    dump = report.model_dump()
    assert dump["recommendation"] == "NONE"
    text = report.model_dump_json().upper()
    for phrase in ("TARGET PRICE", "POSITION SIZE", "BUY ZONE", "SELL TRIGGER"):
        assert phrase not in text.replace("TARGET_PRICE", "")


def test_section_statuses_reflect_available_inputs(report):
    st = {s.section_id: s.status for s in report.sections}
    assert st[S.HISTORICAL_FINANCIALS] == SectionStatus.COMPLETE
    assert st[S.ORDER_BOOK] == SectionStatus.COMPLETE
    assert st[S.INDUSTRY_COMPETITION] == SectionStatus.UNAVAILABLE
    assert st[S.SHAREHOLDING] == SectionStatus.COMPLETE
    assert st[S.CREDIT_LIQUIDITY] == SectionStatus.COMPLETE
    shareholding = report.table("SHAREHOLDING")
    assert {c.key for c in shareholding.columns} >= {"QOQ", "Q4", "Q12"}


def test_period_type_vocabulary_is_upstream(report):
    assert {c.period_end for c in report.table("QUARTERLY_FINANCIALS").columns} <= set(QUARTERS)
    assert PeriodType.QUARTER.value == "QUARTER"


_ = (replace,)


# --- audit remediation M-1: upstream NOT_APPLICABLE is never a data gap ---------------------------

GATED = "UNSUPPORTED_SECTOR_RULE"
INDUSTRIAL_MONITORING = {"CFO_CONVERSION", "DEBTOR_DAYS", "MARGIN", "CAPEX"}
_SECTOR_REPORTS: dict[Industry, tuple[dd.DueDiligenceDataset, FinalResearchReportDataset]] = {}


def sector_report(industry: Industry) -> tuple[dd.DueDiligenceDataset,
                                                FinalResearchReportDataset]:
    if industry not in _SECTOR_REPORTS:
        box4 = make(company(), industry)
        box5 = build_financial_analytics(box4, generated_at=NOW)
        d = build_due_diligence(box4, box5, request=dd.DueDiligenceRequest(
            as_of=AS_OF, company_key=box4.company_key), generated_at=NOW)
        _SECTOR_REPORTS[industry] = d, fr_build(
            request=FinalReportRequest(as_of=AS_OF, company_key=box4.company_key),
            generated_at=NOW, box4=box4, box5=box5, box13=d)
    return _SECTOR_REPORTS[industry]


FINANCIALS = pytest.mark.parametrize("industry", [Industry.BANK, Industry.NBFC,
                                                  Industry.INSURANCE])


def sector_inapplicable_metric_ids(d: dd.DueDiligenceDataset) -> set[UUID]:
    """Box 13 metrics whose gap is a Box 5 metric typed UNSUPPORTED_SECTOR."""
    out = set()
    for f in (*d.earnings_quality.reported, *d.earnings_quality.derived,
              *d.earnings_quality.adjusted, *d.financial_forensics.metrics,
              *d.liquidity_credit_risks.metrics, *d.working_capital_risks.disclosures):
        if f.status == "NOT_APPLICABLE" or (f.reason or "").startswith("UNSUPPORTED_SECTOR:"):
            out.add(f.metric_id)
    return out


@FINANCIALS
def test_financial_sector_inapplicable_items_never_become_data_unavailable(industry):
    d, r = sector_report(industry)
    assert d.sector_gate.value == "FINANCIAL"
    gated = {s.risk_id: s for s in d.signals if s.status.value == GATED}
    inapplicable = sector_inapplicable_metric_ids(d)
    assert gated and inapplicable  # the fixture exercises both upstream paths
    for c in r.claims:
        refs = {u.record_id for u in c.upstream}
        if c.claim_type == CT.DATA_LIMITATION:
            assert not refs & (set(gated) | inapplicable), c.statement
            assert "UNSUPPORTED_SECTOR" not in c.statement
    # the upstream status survives unchanged, typed as not applicable, never as a risk or a gap
    for entry in r.risks.risk_register:
        if entry.risk_id in gated:
            assert entry.status == GATED and entry.severity is None
            claim = claims(r)[entry.claim_id]
            assert claim.claim_type == CT.CALCULATION and claim.value is None
            assert "NOT_APPLICABLE" in claim.limitations
            assert claim.polarity == fr.Polarity.NEUTRAL
    assert not set(gated) & {*r.risks.red, *r.risks.yellow}
    assert not any(e.kind == GATED or set(e.affects) & {s.code for s in gated.values()}
                   for e in r.risks.data_limitations)
    rows = {row.key: row for row in r.table("RISK_REGISTER").rows}
    for rid in gated:
        row = rows[str(rid)]
        assert row.kind != fr.RowKind.DATA_LIMITATION
        assert next(c for c in row.cells if c.column == "VALUE").status == \
            fr.CellStatus.NOT_APPLICABLE


@FINANCIALS
def test_inapplicable_items_are_kept_as_section_metadata(industry):
    d, r = sector_report(industry)
    forensic = r.section(S.ACCOUNTING_FORENSICS)
    for code in ("NOT_APPLICABLE_CFO_TO_PAT", "NOT_APPLICABLE_INVENTORY_DAYS",
                 "NOT_APPLICABLE_HIGH_ACCRUALS", "NOT_APPLICABLE_CONTINGENT_LIABILITY"):
        assert code in forensic.notes
    assert "NOT_APPLICABLE_CUMULATIVE_CFO_TO_PAT_5Y" in r.section(S.CASH_FLOW_BALANCE_SHEET).notes
    assert "NOT_APPLICABLE_CREDIT_DEBT_TO_EQUITY" in r.section(S.CREDIT_LIQUIDITY).notes
    # a gated rule is not a core claim of any section; the full register keeps it
    gated = {s.risk_id for s in d.signals if s.status.value == GATED}
    gated_claims = {e.claim_id for e in r.risks.risk_register if e.risk_id in gated}
    for s in r.sections:
        if s.section_id != S.APPENDICES:
            assert not gated_claims & set(s.claim_ids)
    assert gated_claims <= set(r.table("RISK_REGISTER").claim_ids)
    # industrial forensic models stay NOT_APPLICABLE cells (Altman / Beneish / Piotroski)
    assert all(c.status == fr.CellStatus.NOT_APPLICABLE
               for row in r.table("FORENSIC_MODELS").rows for c in row.cells)


@FINANCIALS
def test_unavailable_but_applicable_metric_stays_a_data_limitation(industry):
    _, r = sector_report(industry)
    missing = [c for c in r.claims if c.claim_type == CT.DATA_LIMITATION
               and "INPUT_MISSING" in c.statement]
    assert missing  # e.g. exceptional items not reported: a real gap, still surfaced
    assert all("DATA_UNAVAILABLE" in c.limitations for c in missing)


@FINANCIALS
def test_no_diligence_or_data_quality_item_from_inapplicability(industry):
    d, r = sector_report(industry)
    gated = {s.risk_id for s in d.signals if s.status.value == GATED}
    assert len(r.risks.open_diligence) == len(d.open_diligence_items)
    assert not any(set(o.related_risk_ids) & gated for o in r.risks.open_diligence)
    dq = [e for e in r.risks.data_limitations if e.layer != "BOX13" or e.entry_id in
          {i.item_id for i in d.data_quality_summary.items}]
    assert len(dq) == len(d.data_quality_summary.items)
    assert set(r.due_diligence_status.open_diligence_item_ids) == {
        i.item_id for i in d.open_diligence_items if i.importance.value in ("HIGH", "MEDIUM")}


@FINANCIALS
def test_executive_summary_states_no_gap_for_an_inapplicable_topic(industry):
    _, r = sector_report(industry)
    points = {p.kind: p for p in r.executive_summary.points}
    order = points[fr.SummaryKind.ORDER_BOOK]
    assert order.status == "NOT_APPLICABLE" and order.reason.startswith("NOT_APPLICABLE")
    assert not order.claim_ids
    assert "ORDER_BOOK" not in r.section(S.EXECUTIVE_SUMMARY).status_reason
    by_id = claims(r)
    for i in (*r.executive_summary.limitation_claim_ids,
              *r.due_diligence_status.unavailable_information_claim_ids):
        assert "UNSUPPORTED_SECTOR" not in by_id[i].statement
        assert GATED not in by_id[i].limitations


@FINANCIALS
def test_industrial_monitoring_items_are_not_applicable(industry):
    _, r = sector_report(industry)
    topics = {m.topic for m in r.monitoring}
    assert not topics & INDUSTRIAL_MONITORING
    assert "NEXT_RESULTS" in topics
    notes = set(r.section(S.MONITORING).notes)
    assert {f"NOT_APPLICABLE_{t}" for t in INDUSTRIAL_MONITORING} <= notes


@FINANCIALS
def test_wholly_inapplicable_order_book_section(industry):
    _, r = sector_report(industry)
    order = r.section(S.ORDER_BOOK)
    assert order.status == SectionStatus.NOT_APPLICABLE
    assert order.status_reason.startswith("NOT_APPLICABLE")
    c = order.completeness
    assert c.missing == () and c.available == () and c.not_applicable == c.required


def test_non_financial_industrial_keeps_every_rule_and_section():
    d, r = sector_report(Industry.GENERAL)
    assert d.sector_gate.value == "NON_FINANCIAL"
    assert not any(s.status.value == GATED for s in d.signals)
    assert not any(n.startswith("NOT_APPLICABLE_") for s in r.sections
                   for n in s.notes if s.section_id != S.ORDER_BOOK)
    assert INDUSTRIAL_MONITORING <= {m.topic for m in r.monitoring}
    models = r.table("FORENSIC_MODELS")
    assert any(c.status == fr.CellStatus.AVAILABLE for row in models.rows for c in row.cells)
    assert r.section(S.ACCOUNTING_FORENSICS).status == SectionStatus.COMPLETE


def test_epc_order_book_stays_applicable(report, d13):
    assert d13.sector_gate.value == "NON_FINANCIAL" and report.order_book.applicable
    order = report.section(S.ORDER_BOOK)
    assert order.status == SectionStatus.COMPLETE and order.completeness.not_applicable == ()
    point = next(p for p in report.executive_summary.points
                 if p.kind == fr.SummaryKind.ORDER_BOOK)
    assert point.status == "AVAILABLE"
    assert not any(n.startswith("NOT_APPLICABLE_") for s in report.sections for n in s.notes)


def _status_of(draft: Any, section: SectionId = S.ACCOUNTING_FORENSICS,
               missing: tuple[fr.InputFamily, ...] = ()) -> Any:
    from types import SimpleNamespace

    from stock_research_ai.application.final_report_composer import REQUIRED, _status
    families = {f: fr.FamilyCheck(family=f, status=fr.FamilyStatus.MISSING if f in missing
                                  else fr.FamilyStatus.AVAILABLE) for f in fr.InputFamily}
    assert set(missing) <= set(REQUIRED[section])
    return _status(SimpleNamespace(drafts={section: draft}), section,  # type: ignore[arg-type]
                   families)


def test_wholly_inapplicable_section_is_not_applicable_not_partial():
    from stock_research_ai.application.report_sections import Draft
    status, reason, completeness = _status_of(Draft(inapplicable=["ALTMAN_Z", "INVENTORY"]))
    assert status == SectionStatus.NOT_APPLICABLE and reason.startswith("NOT_APPLICABLE")
    assert completeness.missing == () and completeness.available == ()
    assert completeness.not_applicable == completeness.required


def test_mixed_section_counts_only_applicable_requirements():
    from stock_research_ai.application.report_sections import Draft
    uid = uuid4()
    mixed = Draft(claims=[uid], inapplicable=["INVENTORY_DAYS"])
    status, _, completeness = _status_of(mixed)
    assert status == SectionStatus.COMPLETE and completeness.missing == ()
    # an inapplicable required family leaves the denominator; a missing applicable one stays
    gated = Draft(claims=[uid], not_applicable={fr.InputFamily.RATINGS})
    status, _, completeness = _status_of(gated, S.CREDIT_LIQUIDITY,
                                         missing=(fr.InputFamily.RATINGS,))
    assert status == SectionStatus.COMPLETE
    assert completeness.not_applicable == (fr.InputFamily.RATINGS,)
    assert completeness.missing == ()
    status, reason, completeness = _status_of(Draft(claims=[uid]), S.CREDIT_LIQUIDITY,
                                              missing=(fr.InputFamily.RATINGS,))
    assert status == SectionStatus.PARTIAL and completeness.missing == (fr.InputFamily.RATINGS,)


def test_inapplicability_needs_the_typed_box5_status_not_reason_text():
    from types import SimpleNamespace

    from stock_research_ai.application.report_sections import Ctx
    from stock_research_ai.domain.financial_analytics import CalculationStatus as CS
    d, _ = sector_report(Industry.BANK)
    f = next(m for m in d.earnings_quality.derived
             if (m.reason or "").startswith("UNSUPPORTED_SECTOR:") and m.record_ids)
    box5 = build_financial_analytics(make(company(), Industry.BANK), generated_at=NOW)
    metrics = {m.metric_id: m for m in (*box5.cash_flow_quality, *box5.working_capital,
                                         *box5.accruals, *box5.profitability,
                                         *box5.building_blocks, *box5.leverage)}
    assert any(metrics[r].status == CS.UNSUPPORTED_SECTOR for r in f.record_ids if r in metrics)
    fake = SimpleNamespace(res=SimpleNamespace(metrics=metrics))
    assert Ctx.inapplicable(fake, f)  # type: ignore[arg-type]
    # the same reason text over a Box 5 record that is merely missing is still a gap
    missing = {r: m.model_copy(update={"status": CS.INPUT_MISSING}) for r, m in metrics.items()}
    assert not Ctx.inapplicable(SimpleNamespace(res=SimpleNamespace(metrics=missing)), f)
    assert not Ctx.inapplicable(fake, f.model_copy(update={"reason": "INPUT_MISSING: x"}))


def test_not_applicable_summary_point_cites_nothing():
    fr.SummaryPoint(kind=fr.SummaryKind.ORDER_BOOK, claim_ids=(), status="NOT_APPLICABLE",
                    reason="NOT_APPLICABLE: bank")
    with pytest.raises(ValidationError):
        fr.SummaryPoint(kind=fr.SummaryKind.ORDER_BOOK, claim_ids=(uuid4(),),
                        status="NOT_APPLICABLE", reason="NOT_APPLICABLE: bank")
    with pytest.raises(ValidationError):
        fr.SummaryPoint(kind=fr.SummaryKind.ORDER_BOOK, claim_ids=(), status="NOT_APPLICABLE")


# --- green-check remediation: mixed applicable / inapplicable sections ----------------------------

APPLICABLE_MONITORING = {"RISK_FLAG", "RATING", "PROMOTER_HOLDING", "DEBT", "NEXT_RESULTS"}
_MONITORED: dict[Industry, tuple[dd.DueDiligenceDataset, FinalResearchReportDataset]] = {}


def monitored_report(industry: Industry) -> tuple[dd.DueDiligenceDataset,
                                                   FinalResearchReportDataset]:
    """A financial company with a RED rating downgrade, ratings and promoter holdings."""
    if industry not in _MONITORED:
        box4 = make(company(), industry)
        box5 = build_financial_analytics(box4, generated_at=NOW)
        ratings = tuple(dd.RatingInput(action_date=day, agency="CARE", instrument="LT",
                                       rating=rating, outlook=outlook, action=action,
                                       source=SRC)
                        for day, rating, outlook, action in (
                            (date(2025, 6, 5), "CARE AA; Stable", "STABLE", "REAFFIRMED"),
                            (date(2026, 1, 5), "CARE A; Negative", "NEGATIVE", "DOWNGRADED")))
        d = build_due_diligence(box4, box5, request=dd.DueDiligenceRequest(
            as_of=AS_OF, company_key=box4.company_key, ratings=ratings, holdings=holdings()),
            generated_at=NOW)
        _MONITORED[industry] = d, fr_build(
            request=FinalReportRequest(as_of=AS_OF, company_key=box4.company_key),
            generated_at=NOW, box4=box4, box5=box5, box13=d)
    return _MONITORED[industry]


@FINANCIALS
def test_mixed_monitoring_section_keeps_its_applicable_items(industry):
    d, r = monitored_report(industry)
    assert d.sector_gate.value == "FINANCIAL"
    assert "CREDIT_DETERIORATION" in {d.signal(i).code for i in d.red_flags}
    topics = {m.topic for m in r.monitoring}
    assert APPLICABLE_MONITORING <= topics and not topics & INDUSTRIAL_MONITORING
    monitoring = r.section(S.MONITORING)
    assert {f"NOT_APPLICABLE_{t}" for t in INDUSTRIAL_MONITORING} <= set(monitoring.notes)
    assert monitoring.status != SectionStatus.NOT_APPLICABLE
    assert not monitoring.status_reason.startswith("NOT_APPLICABLE")
    c = monitoring.completeness
    assert c.missing == () and c.not_applicable == () and c.available == c.required
    # the sector-gated metrics stay not applicable and never become a data limitation
    gated = {s.risk_id for s in d.signals if s.status.value == GATED}
    inapplicable = sector_inapplicable_metric_ids(d)
    assert gated and inapplicable
    for claim in r.claims:
        if claim.claim_type == CT.DATA_LIMITATION:
            assert not {u.record_id for u in claim.upstream} & (gated | inapplicable)
    assert r.section(S.ORDER_BOOK).status == SectionStatus.NOT_APPLICABLE


@FINANCIALS
def test_base_financial_monitoring_section_is_not_wholly_not_applicable(industry):
    _, r = sector_report(industry)
    assert {"DEBT", "NEXT_RESULTS"} <= {m.topic for m in r.monitoring}
    assert r.section(S.MONITORING).status != SectionStatus.NOT_APPLICABLE


@FINANCIALS
def test_summary_monitoring_point_and_section_never_contradict(industry):
    for _, r in (sector_report(industry), monitored_report(industry)):
        point = next(p for p in r.executive_summary.points
                     if p.kind == fr.SummaryKind.MONITORING)
        assert point.status == "AVAILABLE" and point.monitoring_item_ids
        assert set(point.monitoring_item_ids) <= {m.item_id for m in r.monitoring}
        assert r.section(S.MONITORING).status != SectionStatus.NOT_APPLICABLE


def test_a_section_with_items_cannot_be_forged_wholly_not_applicable():
    _, r = monitored_report(Industry.BANK)

    def na(section: SectionId) -> Any:
        def mutate(data: dict[str, Any]) -> None:
            i = index_of(data, "sections", lambda s: s["section_id"] == section)
            data["sections"][i]["status"] = SectionStatus.NOT_APPLICABLE
        return mutate
    forge(r, na(S.MONITORING))
    if r.what_changes_the_view:
        forge(r, na(S.WHAT_CHANGES_THE_VIEW))

    def unknown_item(data: dict[str, Any]) -> None:
        i = index_of(data["executive_summary"], "points",
                     lambda p: p["kind"] == fr.SummaryKind.MONITORING)
        data["executive_summary"]["points"][i]["monitoring_item_ids"] = [uuid4()]
    forge(r, unknown_item)


def test_structured_item_counts_as_section_content():
    from stock_research_ai.application.report_sections import Draft
    inapplicable = ["CFO_CONVERSION", "DEBTOR_DAYS", "MARGIN", "CAPEX"]
    assert not Draft(inapplicable=inapplicable).has_content
    assert Draft(items=[uuid4()]).has_content
    status, reason, completeness = _status_of(Draft(inapplicable=list(inapplicable)),
                                              S.MONITORING)
    assert status == SectionStatus.NOT_APPLICABLE and reason.startswith("NOT_APPLICABLE")
    assert completeness.not_applicable == completeness.required == (fr.InputFamily.BOX13_DILIGENCE,)
    status, _, completeness = _status_of(Draft(items=[uuid4()], inapplicable=list(inapplicable)),
                                         S.MONITORING)
    assert status == SectionStatus.COMPLETE
    assert completeness.available == completeness.required and completeness.missing == ()
    assert completeness.not_applicable == ()


def test_mixed_structured_section_counts_applicable_requirements_normally():
    from stock_research_ai.application.report_sections import Draft
    mixed = Draft(items=[uuid4()], inapplicable=["CFO_CONVERSION"])
    status, _, completeness = _status_of(mixed, S.MONITORING,
                                         missing=(fr.InputFamily.BOX13_DILIGENCE,))
    # a missing applicable requirement is still a gap; inapplicability never hides it
    assert status == SectionStatus.UNAVAILABLE
    assert completeness.missing == (fr.InputFamily.BOX13_DILIGENCE,)
    assert completeness.not_applicable == ()
    # an explicit upstream status still wins over the content rule
    explicit = Draft(items=[uuid4()], inapplicable=["CFO_CONVERSION"],
                     status=SectionStatus.PARTIAL, reason="partial upstream")
    assert _status_of(explicit, S.MONITORING)[0] == SectionStatus.PARTIAL


# --- audit remediation M-2: risk evidence reaches the core event timeline -------------------------

KMP_DESC = "Change in Key Managerial Personnel"


def kmp_rows(n: int) -> list[dict[str, Any]]:
    """n always-material, lower-priority events, newer than every penalty below."""
    return [tm.announcement((date(2026, 1, 1) + timedelta(days=i)).strftime(
        "%d-%b-%Y 10:00:00"), KMP_DESC, f"{KMP_DESC} - appointment of Company Secretary {i}",
        seq=str(5000 + i)) for i in range(n)]


def penalty_row(day: date, seq: int, crore: int | None = None) -> dict[str, Any]:
    amount = f" of Rs. {crore} crore" if crore is not None else ""
    return tm.announcement(day.strftime("%d-%b-%Y 10:00:00"), "Updates",
                           f"Imposition of fine by NSE{amount}", seq=str(seq))


def reference_revenue(crore: int) -> Any:
    from stock_research_ai.domain.market_intelligence import (
        Authority,
        Citation,
        FinancialBasis,
        Provider,
        ReferenceRevenue,
    )
    return ReferenceRevenue(fiscal_year=2026, value_crore=Decimal(crore),
                            basis=FinancialBasis.CONSOLIDATED,
                            citation=Citation(provider=Provider.CALLER,
                                              authority=Authority.EXPLICIT_INPUT,
                                              reference="Box 4 FY26 consolidated revenue"))


def event_report(st: Stack, rows: list[dict[str, Any]], *, cap: int = 10,
                 revenue: int | None = None, **kw13: Any) -> tuple[Any, dd.DueDiligenceDataset,
                                                                   FinalResearchReportDataset]:
    extra = {"reference_revenue": reference_revenue(revenue)} if revenue else {}
    ledger = tm.build([*tm.base_payloads(), tm.nse_announcements(*rows)], **extra)
    d = box13(st, box12=ledger, **kw13)
    return ledger, d, compose(st, d, box12=ledger, limits=ReportLimits(max_core_events=cap))


def core_rows(r: FinalResearchReportDataset) -> dict[str, fr.TableRow]:
    return {row.key: row for row in r.table("MATERIAL_EVENTS").rows}


def signal_of(d: dd.DueDiligenceDataset, code: str) -> Any:
    return next(s for s in d.signals if s.code == code)


def penalty_events(ledger: Any) -> list[Any]:
    return [e for e in ledger.events if e.event_type.value == "EXCHANGE_PENALTY"]


@pytest.fixture(scope="module")
def yellow_linked(stack):
    return event_report(stack, [*kmp_rows(30), penalty_row(date(2024, 6, 3), 7001)])


def test_yellow_risk_evidence_survives_cap_pressure(yellow_linked):
    ledger, d, r = yellow_linked
    (penalty,) = penalty_events(ledger)
    s = signal_of(d, "EXCHANGE_PENALTIES")
    assert s.severity == dd.Severity.YELLOW and penalty.event_id in s.record_ids
    # Box 13 did not grade the event itself: only the aggregate risk cites it
    assert not any(e.severity for e in d.exchange_event_risks.entries)
    core = core_rows(r)
    assert len(core) == 10 and str(penalty.event_id) in core  # 30 newer KMP events lose
    claim = claims(r)[core[str(penalty.event_id)].claim_id]
    assert claim.risk_ids == (s.risk_id,)  # traceable to the risk that cites it
    assert claim.polarity == fr.Polarity.NEUTRAL and claim.claim_type == CT.FACT
    assert "YELLOW" not in claim.statement and "RED" not in claim.statement
    assert sum(1 for k in core if k != str(penalty.event_id)) == 9
    assert "CORE_RISK_EVIDENCE_EVENTS_1_OF_1" in r.section(S.CORPORATE_EVENTS).notes


def test_red_risk_evidence_survives_cap_pressure(stack):
    ledger, d, r = event_report(stack, [*kmp_rows(45), penalty_row(date(2024, 6, 3), 7001,
                                                                   crore=50)], cap=40)
    (penalty,) = penalty_events(ledger)
    s = signal_of(d, "EXCHANGE_PENALTIES")
    assert s.severity == dd.Severity.RED and penalty.event_id in s.record_ids
    core = core_rows(r)
    assert len(core) == 40 and str(penalty.event_id) in core
    assert claims(r)[core[str(penalty.event_id)].claim_id].risk_ids == (s.risk_id,)
    # the full ledger still lists every event, the penalty with the same claim
    ledger_rows = {row.key: row for row in r.table("EVENT_LEDGER").rows}
    assert len(ledger_rows) == len(ledger.events)
    assert ledger_rows[str(penalty.event_id)].claim_id == core[str(penalty.event_id)].claim_id


def test_one_event_cited_by_a_red_and_a_yellow_risk_is_one_row(stack):
    # stated 50 crore: >= 1 % of PAT (aggregate RED) and 2.5 % of revenue (event YELLOW)
    ledger, d, r = event_report(stack, [*kmp_rows(20), penalty_row(date(2024, 6, 3), 7001,
                                                                   crore=50)], revenue=2000)
    (penalty,) = penalty_events(ledger)
    aggregate = signal_of(d, "EXCHANGE_PENALTIES")
    single = signal_of(d, "MATERIAL_REGULATORY_ACTION")
    assert aggregate.severity == dd.Severity.RED and single.severity == dd.Severity.YELLOW
    assert penalty.event_id in aggregate.record_ids and penalty.event_id in single.record_ids
    rows = [row for row in r.table("MATERIAL_EVENTS").rows if row.key == str(penalty.event_id)]
    assert len(rows) == 1
    claim = claims(r)[rows[0].claim_id]
    assert set(claim.risk_ids) == {aggregate.risk_id, single.risk_id}  # both kept
    assert claim.polarity == fr.Polarity.NEUTRAL
    # the summary still lists each risk once; event selection adds no risk statement
    summary = r.executive_summary.risk_claim_ids
    assert len(summary) == len(set(summary))
    assert claim.claim_id not in summary


def test_more_risk_evidence_than_the_cap_is_bounded_and_surfaced(stack):
    days = [date(2024, 1, 1) + timedelta(days=7 * i) for i in range(14)]
    ledger, d, r = event_report(stack, [*kmp_rows(5), *(penalty_row(day, 7000 + i)
                                                        for i, day in enumerate(days))])
    penalties = penalty_events(ledger)
    s = signal_of(d, "EXCHANGE_PENALTIES")
    assert len(penalties) == 14 and all(e.event_id in s.record_ids for e in penalties)
    core = core_rows(r)
    assert len(core) == 10  # the cap holds
    newest = sorted(penalties, key=lambda e: (e.event_date, str(e.event_id)), reverse=True)
    assert set(core) == {str(e.event_id) for e in newest[:10]}
    limits = [claims(r)[i] for i in r.section(S.CORPORATE_EVENTS).limitation_claim_ids]
    limit = next(c for c in limits if c.topic == "CORE_EVENT_SELECTION")
    assert "CORE_EVENT_LIMIT_REACHED" in limit.limitations
    assert {u.record_id for u in limit.upstream} == {e.event_id for e in newest[10:]}
    assert len(r.table("EVENT_LEDGER").rows) == len(ledger.events)


def test_unlinked_events_follow_the_ordinary_rules(yellow_linked, stack):
    _, _, r = yellow_linked
    by_id = claims(r)
    kmp = [row for row in r.table("MATERIAL_EVENTS").rows if row.label == "KMP_CHANGE"]
    assert kmp and all(by_id[row.claim_id].risk_ids == () for row in kmp)
    # the newest KMP events win the remaining slots (type priority, then date)
    dates = sorted((by_id[row.claim_id].period_end for row in kmp), reverse=True)
    assert dates[0] == date(2026, 1, 30)


def test_core_selection_ignores_insertion_order(stack, yellow_linked):
    _, _, first = yellow_linked
    rows = [*kmp_rows(30), penalty_row(date(2024, 6, 3), 7001)]
    _, _, reversed_ = event_report(stack, rows[::-1])
    assert list(core_rows(first)) == list(core_rows(reversed_))


@pytest.mark.parametrize("name,desc,body", [
    ("favourable", "Outcome of arbitration",
     "Arbitral award in favour of the Company for Rs. 500 crore"),
    ("unknown", "Litigation", "Claim of Rs. 500 crore filed against the Company before the "
     "arbitral tribunal"),
    ("award", "Bagging/Receiving of orders/contracts",
     "Letter of Award from NMDC Limited worth Rs. 500 crore"),
])
def test_neutral_or_favourable_events_are_never_described_adversely(stack, name, desc, body):
    row = tm.announcement("01-Sep-2026 10:00:00", desc, body, seq="8101")
    kw: dict[str, Any] = {}
    ledger = tm.build([*tm.base_payloads(), tm.nse_announcements(row)],
                      reference_revenue=reference_revenue(5000))
    if name == "favourable":
        kw["event_assessments"] = (dd.ExchangeEventAssessment(
            event_id=ledger.events[0].event_id, direction="FAVOURABLE", source=SRC),)
    _, d, r = event_report(stack, [row], revenue=5000, **kw)
    (event,) = [e for e in ledger.events]
    assert not any(e.severity for e in d.exchange_event_risks.entries)
    assert not any(event.event_id in s.record_ids for s in d.signals
                   if s.severity in (dd.Severity.RED, dd.Severity.YELLOW))
    core = core_rows(r)
    assert str(event.event_id) in core  # material context, selected by the neutral rules
    claim = claims(r)[core[str(event.event_id)].claim_id]
    assert claim.risk_ids == () and claim.polarity == fr.Polarity.NEUTRAL
    for word in ("adverse", "risk", "penalt", "RED", "YELLOW"):
        assert word not in claim.statement
    assert not {*r.risks.red, *r.risks.yellow}.intersection(
        e.risk_id for e in r.risks.risk_register if e.code in (
            "MATERIAL_LITIGATION", "MATERIAL_REGULATORY_ACTION"))


# --- the ranking itself: pure, total, order-independent -------------------------------------------


def fake_event(n: int, kind: str, day: date, ratio: str | None = None,
               status: str = "ACTIVE") -> Any:
    from types import SimpleNamespace
    m = None if ratio is None else SimpleNamespace(status=SimpleNamespace(value="AVAILABLE"),
                                                   ratio_pct=Decimal(ratio))
    return SimpleNamespace(event_id=UUID(int=n), event_type=SimpleNamespace(value=kind),
                           event_date=day, status=SimpleNamespace(value=status), materiality=m)


def fake_signal(n: int, severity: dd.Severity) -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(risk_id=UUID(int=10_000 + n), severity=severity, code=f"R{n}")


def selection_case() -> tuple[list[Any], dict[UUID, list[Any]], set[UUID]]:
    base = date(2025, 1, 1)
    events = [fake_event(i, "AUDITOR_CHANGE", base + timedelta(days=i)) for i in range(60)]
    events += [fake_event(100, "EXCHANGE_PENALTY", date(2023, 1, 1)),  # RED evidence, oldest
               fake_event(101, "EXCHANGE_PENALTY", date(2023, 2, 1)),  # YELLOW evidence
               fake_event(102, "LITIGATION", date(2023, 3, 1), ratio="9"),  # graded alone
               fake_event(103, "DIVIDEND", date(2026, 1, 1), ratio="0.1"),  # immaterial
               fake_event(104, "OTHER_MATERIAL_EVENT", date(2026, 2, 1))]  # no rule
    red, yellow = fake_signal(1, dd.Severity.RED), fake_signal(2, dd.Severity.YELLOW)
    linked = {UUID(int=100): [yellow, red], UUID(int=101): [yellow]}
    return events, linked, {UUID(int=102)}


def test_ranking_puts_red_then_yellow_evidence_then_graded_then_type_rules():
    from stock_research_ai.application.report_sections import select_core_events
    events, linked, graded = selection_case()
    core, dropped = select_core_events(events, linked, graded, 40)
    assert [e.event_id.int for e in core[:3]] == [100, 101, 102]
    assert len(core) == 40 and dropped == []
    assert {e.event_id.int for e in core[3:]} == set(range(59, 59 - 37, -1))  # newest first
    assert not {103, 104} & {e.event_id.int for e in core}


def test_ranking_is_independent_of_order_duplicates_and_hash_seed():
    import random

    from stock_research_ai.application.report_sections import select_core_events
    events, linked, graded = selection_case()
    expected = [e.event_id for e in select_core_events(events, linked, graded, 40)[0]]
    shuffled = events[:]
    random.Random(20261013).shuffle(shuffled)
    for variant in (events[::-1], shuffled, events + events[:30], events[:30] + events):
        assert [e.event_id for e in select_core_events(variant, linked, graded, 40)[0]] == \
            expected
    reordered = {k: v[::-1] for k, v in reversed(list(linked.items()))}
    assert [e.event_id for e in select_core_events(events, reordered, graded, 40)[0]] == \
        expected


def test_ranking_over_the_cap_keeps_the_most_material_evidence():
    from stock_research_ai.application.report_sections import select_core_events
    red = fake_signal(1, dd.Severity.RED)
    base = date(2024, 1, 1)
    events = [fake_event(i, "EXCHANGE_PENALTY", base + timedelta(days=i),
                         ratio=str(i % 7) if i % 2 else None) for i in range(50)]
    linked = {e.event_id: [red] for e in events}
    core, dropped = select_core_events(events, linked, set(), 40)
    assert len(core) == 40 and len(dropped) == 10
    assert {e.event_id for e in core} | {e.event_id for e in dropped} == \
        {e.event_id for e in events}
    sized = [e for e in core if e.materiality is not None]
    assert len(sized) == 25  # every event with an upstream materiality ranks first
    ratios = [e.materiality.ratio_pct for e in sized]
    assert ratios == sorted(ratios, reverse=True)
    assert all(e.materiality is None for e in dropped)
