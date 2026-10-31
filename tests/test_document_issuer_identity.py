"""Issuer binding is fail-closed before Box 3 evidence is exposed or Box 4 parses it."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_financial_reconciliation import (
    COMPANY,
    FY26,
    B,
    M,
    Outcome,
    T,
    build,
    filings,
    one,
    profit_and_loss,
)
from test_screener_dataset import dataset
from test_summary_input import INFY, YESBANK, replay

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.evidence import ScreenerEvidenceSource
from stock_research_ai.domain.document_intelligence import (
    ExtractionStatus,
    IssuerBindingMethod,
    IssuerBindingStatus,
)
from stock_research_ai.domain.evidence import EvidenceCompany, EvidenceDocumentType
from stock_research_ai.domain.financial_canonical import PeriodType, SourceType

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
TCS = EvidenceCompany(
    company_key="screener:TCS",
    name="Tata Consultancy Services Ltd",
    source_company_key="TCS",
    reporting_scope="standalone",
    nse_symbol="TCS",
)
HUDCO = EvidenceCompany(
    company_key="screener:HUDCO",
    name="Housing and Urban Development Corporation Ltd",
    source_company_key="HUDCO",
    reporting_scope="standalone",
    nse_symbol="HUDCO",
)


def source_observations(data, document_id):
    return [o for o in data.observations if o.source.document_id == document_id]


async def test_a_infy_rejects_tcs_filing_before_evidence_or_box4(tmp_path):
    target = COMPANY.model_copy(update={"nse_symbol": "INFY"})
    pages = [[
        "Tata Consultancy Services Limited",
        "NSE Symbol: TCS",
        *profit_and_loss("Consolidated"),
    ]]
    bundle, catalogue = await filings(
        tmp_path, {"/tcs.pdf": (T.ANNUAL_REPORT, pages)}, company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.MISMATCH
    assert extraction.status is ExtractionStatus.FAILED
    assert {w.code for w in extraction.warnings} == {"ISSUER_IDENTITY_MISMATCH"}
    assert extraction.tables == extraction.evidence == extraction.pages == ()

    data = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(data, extraction.document_id) == []
    assert "ISSUER_IDENTITY_MISMATCH" in {w.code for w in data.warnings}


async def test_b_hudco_rejects_another_issuer(tmp_path):
    pages = [[
        "Tata Consultancy Services Limited",
        "NSE Symbol: TCS",
        *profit_and_loss("Consolidated"),
    ]]
    bundle, catalogue = await filings(
        tmp_path, {"/other.pdf": (T.ANNUAL_REPORT, pages)}, company=HUDCO,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.MISMATCH
    assert extraction.status is ExtractionStatus.FAILED
    data = build(
        bundle=bundle, catalogue=catalogue,
        company_key=HUDCO.company_key, company_name=HUDCO.name,
    )
    assert source_observations(data, extraction.document_id) == []


async def test_c_correct_issuer_keeps_the_existing_financial_values(tmp_path):
    bundle, catalogue = await filings(
        tmp_path,
        {"/infosys.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Consolidated")])},
        issuer_name="INFOSYS LIMITED.",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.VERIFIED
    assert extraction.issuer_binding_method is IssuerBindingMethod.NORMALIZED_LEGAL_NAME
    data = build(bundle=bundle, catalogue=catalogue)
    revenue = one(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26, PeriodType.FY)
    assert (revenue.value, revenue.outcome) == (Decimal(178650), Outcome.SINGLE_SOURCE)
    source = next(o for o in source_observations(data, extraction.document_id)
                  if o.metric is M.REVENUE_FROM_OPERATIONS and o.raw_value == "1,78,650")
    assert source.normalized_value == Decimal(178650)


async def test_d_missing_issuer_evidence_is_unverified_and_rejected(tmp_path):
    bundle, catalogue = await filings(
        tmp_path,
        {"/unknown.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Consolidated")])},
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.UNVERIFIED
    assert extraction.status is ExtractionStatus.FAILED
    assert "ISSUER_IDENTITY_UNVERIFIED" in {w.code for w in extraction.warnings}
    data = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(data, extraction.document_id) == []


async def test_d2_company_mention_in_front_page_prose_is_not_issuer_evidence(tmp_path):
    pages = [[
        "This TCS presentation compares its performance with Infosys Limited.",
        *profit_and_loss("Consolidated"),
    ]]
    bundle, catalogue = await filings(
        tmp_path, {"/peer-mention.pdf": (T.INVESTOR_PRESENTATION, pages)},
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.UNVERIFIED
    assert extraction.status is ExtractionStatus.FAILED
    data = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(data, extraction.document_id) == []


async def test_e_peer_document_is_bound_only_in_its_own_company_context(tmp_path):
    bundle, catalogue = await filings(
        tmp_path,
        {"/tcs.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Consolidated")])},
        company=TCS,
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.VERIFIED

    primary = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(primary, extraction.document_id) == []
    peer = build(
        bundle=bundle, catalogue=catalogue,
        company_key=TCS.company_key, company_name=TCS.name,
    )
    assert one(peer, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26, PeriodType.FY).value \
        == Decimal(178650)


async def test_f_case_and_punctuation_variants_bind_exact_normalized_name(tmp_path):
    bundle, _catalogue = await filings(
        tmp_path,
        {"/infosys.pdf": (T.ANNUAL_REPORT, [profit_and_loss("Standalone")])},
        issuer_name="  iNfOsYs, ltd.  ",
    )
    assert bundle.extractions[0].issuer_binding_status is IssuerBindingStatus.VERIFIED
    assert bundle.extractions[0].issuer_binding_method is IssuerBindingMethod.NORMALIZED_LEGAL_NAME


async def test_h_wrong_company_emits_no_extraction_artifacts_or_box4_observations(tmp_path):
    bundle, catalogue = await filings(
        tmp_path,
        {"/tcs.pdf": (T.ANNUAL_REPORT, [[
            "Tata Consultancy Services Limited", "NSE Symbol: TCS",
            *profit_and_loss("Consolidated"),
        ]])},
        company=COMPANY.model_copy(update={"nse_symbol": "INFY"}),
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.statistics.tables == extraction.statistics.evidence_items == 0
    assert not extraction.tables and not extraction.evidence and not extraction.pages
    data = build(bundle=bundle, catalogue=catalogue)
    assert not source_observations(data, extraction.document_id)


async def test_audit_bypass_is_rejected_before_primary_financial_evidence(tmp_path):
    target = COMPANY.model_copy(update={"nse_symbol": "INFY"})
    bundle, catalogue = await filings(
        tmp_path,
        {"/tcs.pdf": (T.ANNUAL_REPORT, [[
            "Tata Consultancy Services Limited",
            "Peer comparison: NSE Symbol: INFY",
            *profit_and_loss("Consolidated"),
        ]])},
        company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.MISMATCH
    assert extraction.issuer_binding_method is None
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.tables == extraction.evidence == extraction.pages == ()
    data = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(data, extraction.document_id) == []
    assert data.observations == ()
    assert data.canonical_values == ()


@pytest.mark.parametrize("lines, expected", [
    (["Tata Consultancy Services Limited", "Peer comparison: NSE Symbol: INFY"],
     IssuerBindingStatus.MISMATCH),
    (["Tata Consultancy Services Limited", "Peer table: Infosys | INFY"],
     IssuerBindingStatus.MISMATCH),
    (["Tata Consultancy Services Limited", "Infosys INFY Infosys INFY"],
     IssuerBindingStatus.MISMATCH),
    (["Tata Consultancy Services Limited", "Peer comparison", "Infosys Limited | NSE: INFY"],
     IssuerBindingStatus.MISMATCH),
    (["Tata Consultancy Services Limited", "Annual Results", "NSE: INFY"],
     IssuerBindingStatus.MISMATCH),
    (["Peer table: Infosys | INFY"], IssuerBindingStatus.UNVERIFIED),
    (["Peer comparison of IT companies", "Infosys Limited | NSE: INFY"],
     IssuerBindingStatus.UNVERIFIED),
    (["ABC Technologies Private Limited", "Parent: Infosys Limited"],
     IssuerBindingStatus.MISMATCH),
    (["Tata Consultancy Services Limited", "NSE: INFY", "NSE: TCS"],
     IssuerBindingStatus.MISMATCH),
])
async def test_foreign_issuer_and_weak_target_mentions_never_bind(tmp_path, lines, expected):
    target = COMPANY.model_copy(update={"nse_symbol": "INFY"})
    bundle, catalogue = await filings(
        tmp_path,
        {"/foreign.pdf": (T.ANNUAL_REPORT, [lines + profit_and_loss("Consolidated")])},
        company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is expected
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.tables == extraction.evidence == extraction.pages == ()
    data = build(bundle=bundle, catalogue=catalogue)
    assert source_observations(data, extraction.document_id) == []
    assert data.observations == data.canonical_values == ()


async def test_matching_legal_name_with_matching_exchange_metadata_is_verified(tmp_path):
    target = COMPANY.model_copy(update={"nse_symbol": "INFY", "name": "Infosys Limited"})
    bundle, catalogue = await filings(
        tmp_path,
        {"/infosys.pdf": (T.ANNUAL_REPORT, [[
            "Infosys Limited", "NSE Symbol: INFY", *profit_and_loss("Consolidated"),
        ]])},
        company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.VERIFIED
    assert extraction.issuer_binding_method is IssuerBindingMethod.NAME_AND_EXCHANGE
    assert extraction.status is ExtractionStatus.SUCCESS
    data = build(bundle=bundle, catalogue=catalogue)
    assert one(data, M.REVENUE_FROM_OPERATIONS, B.CONSOLIDATED, FY26, PeriodType.FY).value \
        == Decimal(178650)


async def test_matching_historical_name_variation_remains_verified(tmp_path):
    bundle, catalogue = await filings(
        tmp_path,
        {"/infosys.pdf": (T.ANNUAL_REPORT, [[
            "Infosys Ltd.", "NSE Symbol: INFY", *profit_and_loss("Standalone"),
        ]])},
        company=COMPANY.model_copy(update={"nse_symbol": "INFY"}),
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.VERIFIED
    data = build(bundle=bundle, catalogue=catalogue)
    assert one(data, M.REVENUE_FROM_OPERATIONS, B.STANDALONE, FY26, PeriodType.FY).value \
        == Decimal(178650)


@pytest.mark.parametrize("conflict", [
    "ISIN: INE999A01099",
    "NSE Symbol: TCS",
    "BSE Scrip Code: 532540",
])
async def test_matching_name_with_conflicting_strong_identifier_is_rejected(tmp_path, conflict):
    target = COMPANY.model_copy(update={
        "name": "Infosys Limited", "nse_symbol": "INFY", "bse_code": "500209",
        "isin": "INE009A01021",
    })
    bundle, catalogue = await filings(
        tmp_path,
        {"/conflicting.pdf": (T.ANNUAL_REPORT, [[
            "Infosys Limited", "NSE Symbol: INFY", conflict,
            *profit_and_loss("Consolidated"),
        ]])},
        company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.MISMATCH
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.tables == extraction.evidence == extraction.pages == ()
    data = build(bundle=bundle, catalogue=catalogue)
    assert data.observations == data.canonical_values == ()


async def test_unresolved_issuer_isin_does_not_get_overridden_by_name_and_symbol(tmp_path):
    target = COMPANY.model_copy(update={"name": "Infosys Limited", "nse_symbol": "INFY"})
    bundle, catalogue = await filings(
        tmp_path,
        {"/unknown-isin.pdf": (T.ANNUAL_REPORT, [[
            "Infosys Limited", "NSE Symbol: INFY", "ISIN: INE999A01099",
            *profit_and_loss("Consolidated"),
        ]])},
        company=target,
        issuer_name="",
    )
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.UNVERIFIED
    assert extraction.status is ExtractionStatus.FAILED
    data = build(bundle=bundle, catalogue=catalogue)
    assert data.observations == data.canonical_values == ()


async def test_real_captured_infy_and_yesbank_sources_do_not_cross_canonical_datasets(
    tmp_path, fixture_dir
):
    """Uses captured Screener pages and the unmodified public YES BANK PDFs in fixtures."""
    infy_page = await dataset(tmp_path / "infy-page", fixture_dir, INFY)
    yesbank_page = await dataset(tmp_path / "yesbank-page", fixture_dir, YESBANK)
    assert infy_page.company_key != yesbank_page.company_key

    infy_capture = await replay(tmp_path / "infy-identity", fixture_dir, INFY)
    infy_store = FileRawArtifactStore(tmp_path / "infy-identity" / "evidence")
    infy_html = await infy_store.read(infy_capture.raw_artifacts[0])
    infy_company = ScreenerEvidenceSource(infy_capture, infy_html).company()
    yesbank_capture = await replay(tmp_path / "yesbank-identity", fixture_dir, YESBANK)
    yesbank_store = FileRawArtifactStore(tmp_path / "yesbank-identity" / "evidence")
    yesbank_html = await yesbank_store.read(yesbank_capture.raw_artifacts[0])
    yesbank_company = ScreenerEvidenceSource(yesbank_capture, yesbank_html).company()
    assert infy_company.nse_symbol == "INFY"
    assert yesbank_company.nse_symbol == "YESBANK"

    docs = Path(fixture_dir).parent / "documents"
    presentation = (docs / "yesbank_presentation.pdf").read_bytes()
    transcript = (docs / "yesbank_transcript.pdf").read_bytes()
    manifest_urls = {
        "/_media/bs/data/announcements/bse/19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf":
            "https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/19072025/"
            "188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf",
        "/_media/bs/data/announcements/bse/22012026/847c7cb1-c4cd-436f-aeae-37a72c9bacb1.pdf":
            "https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/22012026/"
            "847c7cb1-c4cd-436f-aeae-37a72c9bacb1.pdf",
    }
    docs_by_path = {
        "/_media/bs/data/announcements/bse/19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf":
            (EvidenceDocumentType.INVESTOR_PRESENTATION, presentation),
        "/_media/bs/data/announcements/bse/22012026/847c7cb1-c4cd-436f-aeae-37a72c9bacb1.pdf":
            (EvidenceDocumentType.CONCALL_TRANSCRIPT, transcript),
    }

    # The real YES BANK documents are accepted when bound to their captured YES BANK identity.
    valid_bundle, valid_catalogue = await filings(
        tmp_path / "real-yesbank", docs_by_path, company=yesbank_company,
        source_urls=manifest_urls,
    )
    assert all(e.issuer_binding_status is IssuerBindingStatus.VERIFIED
               for e in valid_bundle.extractions)
    assert all(e.issuer_binding_method in {
        IssuerBindingMethod.EXCHANGE_IDENTIFIER, IssuerBindingMethod.NAME_AND_EXCHANGE,
    }
               for e in valid_bundle.extractions)
    yesbank_data = build(
        [yesbank_page], valid_bundle, valid_catalogue,
        company_key=yesbank_page.company_key, company_name=yesbank_page.profile.name,
    )
    assert all(o.company_key == yesbank_page.company_key for o in yesbank_data.observations)

    # Re-cataloguing the same real YES BANK PDFs under Infosys cannot make them Infosys facts.
    wrong_bundle, wrong_catalogue = await filings(
        tmp_path / "real-yesbank-as-infy", docs_by_path, company=infy_company,
        source_urls=manifest_urls,
    )
    assert all(e.issuer_binding_status is IssuerBindingStatus.MISMATCH
               for e in wrong_bundle.extractions)
    infy_data = build([infy_page], wrong_bundle, wrong_catalogue)
    assert all(o.source.document_id not in {e.document_id for e in wrong_bundle.extractions}
               for o in infy_data.observations)
    assert {o.source.source_type for o in infy_data.observations} <= {SourceType.SCREENER}

    # Nor can an INFY Screener dataset be passed into YES BANK's canonical Box 4 context.
    crossed = build(
        [infy_page], company_key=yesbank_page.company_key,
        company_name=yesbank_page.profile.name,
    )
    assert crossed.observations == ()
    assert "COMPANY_IDENTITY_MISMATCH" in {w.code for w in crossed.warnings}


async def test_printed_isin_binds_once_an_exchange_stated_it_for_the_listing(tmp_path):
    """An annual report's cover letter prints NSE symbol AND ISIN. With no ISIN for the
    company it stays unverified (above); with the ISIN the exchange master states for the
    listing it binds, and a different printed ISIN still fails closed."""
    lines = ["Infosys Limited", "NSE Symbol: INFY", "ISIN: INE009A01021",
             *profit_and_loss("Consolidated")]
    target = COMPANY.model_copy(update={"name": "Infosys Limited", "nse_symbol": "INFY",
                                        "isin": "INE009A01021"})
    bundle, catalogue = await filings(tmp_path / "ok", {"/ar.pdf": (T.ANNUAL_REPORT, [lines])},
                                      company=target, issuer_name="")
    extraction = bundle.extractions[0]
    assert extraction.issuer_binding_status is IssuerBindingStatus.VERIFIED
    assert extraction.issuer_binding_method is IssuerBindingMethod.NAME_AND_EXCHANGE
    other = target.model_copy(update={"isin": "INE999A01099"})
    bundle, _ = await filings(tmp_path / "other", {"/ar.pdf": (T.ANNUAL_REPORT, [lines])},
                              company=other, issuer_name="")
    assert bundle.extractions[0].issuer_binding_status is IssuerBindingStatus.MISMATCH


async def test_rating_agency_running_header_is_not_a_second_issuer(tmp_path):
    """'1 CARE Ratings Ltd.' (page number + the agency's running header) is the author of a
    rating rationale, never a conflicting issuer; a rationale about another company still
    fails closed."""
    target = COMPANY.model_copy(update={"name": "Tata Consultancy Services Limited",
                                        "nse_symbol": "TCS"})
    rationale = ["1 CARE Ratings Ltd.", "Press Release", "Tata Consultancy Services Limited",
                 "September 29, 2025", "Long-Term Bank Facilities 175.00 CARE AAA; Stable"]
    bundle, _ = await filings(tmp_path / "ok", {"/r.pdf": (T.CREDIT_RATING_REPORT, [rationale])},
                              company=target, issuer_name="")
    assert bundle.extractions[0].issuer_binding_status is IssuerBindingStatus.VERIFIED
    foreign = ["1 CARE Ratings Ltd.", "Press Release", "Wipro Limited", "September 29, 2025"]
    bundle, _ = await filings(tmp_path / "no", {"/r.pdf": (T.CREDIT_RATING_REPORT, [foreign])},
                              company=target, issuer_name="")
    assert bundle.extractions[0].issuer_binding_status is IssuerBindingStatus.MISMATCH
