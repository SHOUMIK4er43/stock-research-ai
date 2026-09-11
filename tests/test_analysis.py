"""Deterministic validator proofs over real Part 1-5A evidence. No network, no live model."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.application import analysis as analyst_engine
from stock_research_ai.application.analysis_validation import (
    extract_numeric_tokens,
    validate_candidate,
    validate_response,
)
from stock_research_ai.bootstrap import (
    calculate_financials,
    evaluate_valuation,
    extract_snapshot,
    ingest_document,
    initialize_database,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import (
    AnalysisRequest,
    AnalystClaimCandidate,
    EvidenceReference,
    RawAnalystResponse,
)
from stock_research_ai.domain.calculations import CalculationRequest
from stock_research_ai.domain.documents import DocumentRequest, DocumentType
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.valuation import ValuationRequest

FIX = Path(__file__).parent / "fixtures"


def calc_request(source):
    return CalculationRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            extraction_id=source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit IT proof classification",
            ),
            alignment=dict(
                fiscal_year_end_month=3,
                declaration="Caller declares March fiscal year end.",
            ),
            calculations=[
                dict(
                    metric_id="infy_sales_growth",
                    formula_id="SOURCE_METRIC_GROWTH",
                    source_metric="sales",
                    current_period="2025-03",
                    comparison="ANNUAL_YOY",
                )
            ],
        )
    )


def pe_request(source, method="PRICE_TO_EARNINGS"):
    rows = [o for o in source.top_metrics if o.metric_id == "current_price"]
    eps_rows = [
        o
        for o in source.profit_loss
        if o.metric_id == "eps" and o.period.fiscal_label == "Mar 2026"
    ]
    return ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="IT_SERVICES_PROFILE",
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            selections=[
                dict(
                    method=method,
                    price=dict(
                        kind="FINANCIAL",
                        snapshot_id=str(source.metadata.id),
                        observation_id=str(rows[-1].id),
                    ),
                    denominator=dict(
                        kind="FINANCIAL",
                        snapshot_id=str(source.metadata.id),
                        observation_id=str(eps_rows[-1].id),
                    ),
                )
            ],
        )
    )


@pytest.fixture
async def world(tmp_path, make_document_pdf):
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/INFY/",
            replay_path=FIX / "screener/infy_standalone.html",
        ),
        tmp_path / "extraction",
    )
    calc_snapshot = await calculate_financials(settings, calc_request(source), tmp_path / "calc")
    valuation_snapshot = await evaluate_valuation(settings, pe_request(source), tmp_path / "val")
    pdf_path = tmp_path / "management.pdf"
    pdf_path.write_bytes(
        make_document_pdf(texts=("Management remarks\nRevenue grew due to strong client demand.",))
    )
    document = await ingest_document(
        settings,
        DocumentRequest(
            company_id=source.company.company_id,
            url="https://example.invalid/infy-investor-note.pdf",
            title="Investor Note",
            document_type=DocumentType.INVESTOR_PRESENTATION,
            replay_path=pdf_path,
        ),
        tmp_path / "doc",
    )
    from stock_research_ai.adapters.sqlite import SQLiteDatabase

    database = SQLiteDatabase(settings.database_path)
    try:
        request = AnalysisRequest.model_validate(
            dict(
                company_id=source.company.company_id,
                classification=dict(
                    classification="NON_FINANCIAL",
                    basis="CALLER_DECLARED",
                    declaration="Explicit proof profile",
                ),
                profile_id="IT_SERVICES_PROFILE",
                financial_extraction_id=source.metadata.id,
                calculation_snapshot_ids=(calc_snapshot.id,),
                valuation_snapshot_ids=(valuation_snapshot.id,),
                document_chunk_refs=((document.version_id, document.chunks[0].id),),
                sections=(
                    "HISTORICAL_FUNDAMENTALS",
                    "VALUATION",
                    "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
                    "BUSINESS_MODEL",
                    "SECTOR_KPIS",
                ),
            )
        )
        company = await database.repository.get(source.company.company_id)
        evidence = await analyst_engine.resolve_evidence(
            request,
            company,
            extractions=database.extractions,
            market=database.market,
            calculations=database.calculations,
            patterns=database.patterns,
            market_calculations=database.market_calculations,
            valuations=database.valuations,
            documents=database.documents,
            context=database.context,
        )
        bundle = analyst_engine.assemble_bundle(request, evidence)
        yield source, calc_snapshot, valuation_snapshot, document, bundle, evidence
    finally:
        await database.close()


def calc_ref(evidence, bundle):
    return next(
        r
        for r in bundle.sources
        if r.kind == "FINANCIAL_CALCULATION" and r.item_key == "infy_sales_growth"
    )


def valuation_ref(bundle):
    return next(r for r in bundle.sources if r.kind == "VALUATION")


def chunk_ref(bundle):
    return next(r for r in bundle.sources if r.kind == "DOCUMENT_CHUNK")


def citation_dict(ref):
    return dict(
        kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
    )


def valuation_result(evidence, ref):
    v = evidence.valuations[ref.owning_snapshot_id]
    return next(r for r in v.results if str(r.id) == ref.item_key)


async def test_valid_evidence_backed_claim_accepted(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"The observed price-to-earnings multiple is {value.display_value}x.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None
    assert claim.epistemic_class == "INTERPRETATION"
    assert claim.source_epistemic_classes == ("CALCULATION",)


@pytest.mark.parametrize("field", ["epistemic_class", "source_epistemic_classes", "confidence"])
def test_model_cannot_supply_trusted_epistemic_or_support_fields(field):
    candidate = {
        "section": "VALUATION",
        "statement": "The multiple is observed.",
        "materiality": "PRIMARY",
        "citations": [
            {"kind": "VALUATION", "owning_snapshot_id": str(uuid4()), "item_key": None}
        ],
        field: "FACT",
    }
    with pytest.raises(ValidationError):
        AnalystClaimCandidate.model_validate(candidate)


async def test_nonexistent_evidence_citation_rejected(world):
    *_, bundle, evidence = world
    candidate = dict(
        section="VALUATION",
        statement="Some claim.",
        materiality="PRIMARY",
        citations=[dict(kind="VALUATION", owning_snapshot_id=str(uuid4()), item_key=None)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection is not None
    assert rejection.issues[0].code == "CITATION_NOT_IN_BUNDLE"


async def test_wrong_item_key_rejected(world):
    *_, bundle, evidence = world
    ref = calc_ref(evidence, bundle)
    candidate = dict(
        section="HISTORICAL_FUNDAMENTALS",
        statement="Some claim.",
        materiality="PRIMARY",
        citations=[
            dict(
                kind=ref.kind,
                owning_snapshot_id=str(ref.owning_snapshot_id),
                item_key="nonexistent_metric",
            )
        ],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "CITATION_NOT_IN_BUNDLE"


async def test_wrong_owning_snapshot_rejected(world):
    *_, bundle, evidence = world
    ref = calc_ref(evidence, bundle)
    candidate = dict(
        section="HISTORICAL_FUNDAMENTALS",
        statement="Some claim.",
        materiality="PRIMARY",
        citations=[dict(kind=ref.kind, owning_snapshot_id=str(uuid4()), item_key=ref.item_key)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "CITATION_NOT_IN_BUNDLE"


async def test_evidence_outside_bundle_rejected_even_when_real(world):
    """A citation pointing at a real snapshot never pinned into THIS bundle must still
    refuse - proves the check is bundle membership, not global existence."""
    *_, bundle, evidence = world
    candidate = dict(
        section="VALUATION",
        statement="Some claim.",
        materiality="PRIMARY",
        citations=[
            dict(kind="MARKET_CALCULATION", owning_snapshot_id=str(uuid4()), item_key="RETURN")
        ],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "CITATION_NOT_IN_BUNDLE"


async def test_unsupported_numeric_claim_rejected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    candidate = dict(
        section="VALUATION",
        statement="The multiple is 999999.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "UNSUPPORTED_NUMERIC_CLAIM"


async def test_right_number_wrong_sign_rejected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"The multiple is -{value.display_value}.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "UNSUPPORTED_NUMERIC_CLAIM"


async def test_right_number_wrong_unit_rejected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"The multiple is {value.display_value}%.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "UNSUPPORTED_NUMERIC_CLAIM"


async def test_hidden_arithmetic_rejected(world):
    """A number that is the SUM of two cited values (never itself a canonical value) must
    be rejected - the validator never performs arithmetic on the caller's behalf."""
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    fabricated = value.display_value + value.display_value
    candidate = dict(
        section="VALUATION",
        statement=f"Twice the multiple is {fabricated}.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "UNSUPPORTED_NUMERIC_CLAIM"


async def test_uncited_numerical_value_rejected(world):
    """A real canonical value that exists in the bundle but was never CITED by this
    candidate must still be rejected - citation, not bundle membership, is what counts."""
    *_, bundle, evidence = world
    val_ref = valuation_ref(bundle)
    calculation_ref = calc_ref(evidence, bundle)
    value = next(v for v in valuation_result(evidence, val_ref).values if v.name == "multiple")
    candidate = dict(
        section="HISTORICAL_FUNDAMENTALS",
        statement=f"Sales growth is somehow {value.display_value}.",
        materiality="PRIMARY",
        citations=[citation_dict(calculation_ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None and rejection.issues[0].code == "UNSUPPORTED_NUMERIC_CLAIM"


@pytest.mark.parametrize(
    "phrase",
    [
        "we recommend investors buy the stock",
        "sell the stock now",
        "hold the position",
        "target price of 500",
        "initiate coverage with",
    ],
)
async def test_forbidden_recommendation_language_rejected(world, phrase):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    candidate = dict(
        section="VALUATION", statement=phrase, materiality="PRIMARY", citations=[citation_dict(ref)]
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert "FORBIDDEN_RECOMMENDATION_LANGUAGE" in [i.code for i in rejection.issues]


async def test_forbidden_certainty_language_rejected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    candidate = dict(
        section="VALUATION",
        statement="This is guaranteed to hold.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert "FORBIDDEN_CERTAINTY_LANGUAGE" in [i.code for i in rejection.issues]


async def test_bank_industrial_language_rejected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    candidate = dict(
        section="VALUATION",
        statement="Net debt remains manageable for this business.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "BANK_PROFILE")
    assert claim is None and rejection.issues[0].code == "SECTOR_LANGUAGE_VIOLATION"


async def test_same_statement_allowed_for_it_profile(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    candidate = dict(
        section="VALUATION",
        statement="Net debt is not part of this observed multiple.",
        materiality="CONTEXTUAL",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None


async def test_exact_management_quote_accepted(world):
    *_, document, bundle, evidence = world
    ref = chunk_ref(bundle)
    text = document.chunks[0].text
    quote = text[:20].strip()
    assert quote
    candidate = dict(
        section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        statement="Management noted strong demand.",
        materiality="SUPPORTING",
        citations=[citation_dict(ref)],
        management_quote=dict(chunk=citation_dict(ref), text=quote),
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None
    assert claim.management_statement is not None
    assert claim.management_statement.text == quote


async def test_altered_management_quote_rejected(world):
    *_, bundle, evidence = world
    ref = chunk_ref(bundle)
    candidate = dict(
        section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        statement="Management noted strong demand.",
        materiality="SUPPORTING",
        citations=[citation_dict(ref)],
        management_quote=dict(
            chunk=citation_dict(ref), text="This exact sentence was never actually written."
        ),
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert rejection.issues[0].code == "MANAGEMENT_QUOTE_NOT_EXACT_SUBSTRING"


async def test_paraphrase_is_ordinary_interpretation_not_a_management_statement(world):
    *_, bundle, evidence = world
    ref = chunk_ref(bundle)
    candidate = dict(
        section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        statement="Management commentary attributes revenue growth to client demand.",
        materiality="SUPPORTING",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None
    assert claim.management_statement is None
    assert claim.epistemic_class == "INTERPRETATION"


async def test_temporal_limitation_propagated(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"The observed multiple is {value.display_value}x.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, _ = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim.time_scope.point_in_time_unverified is True
    assert "POINT_IN_TIME_AVAILABILITY_UNVERIFIED" in claim.limitations


async def test_historical_known_wording_rejected_when_unverified(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"This multiple of {value.display_value}x was already known to the market.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert rejection.issues[0].code == "UNVERIFIED_HISTORICAL_KNOWLEDGE_CLAIM"


async def test_duplicate_claim_detected(world):
    *_, bundle, evidence = world
    ref = valuation_ref(bundle)
    value = next(v for v in valuation_result(evidence, ref).values if v.name == "multiple")
    candidate = dict(
        section="VALUATION",
        statement=f"The observed multiple is {value.display_value}x.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    raw = RawAnalystResponse(
        structured_payload={"claims": [candidate, dict(candidate)]},
        structured_payload_sha256="0" * 64,
    )
    accepted, rejected = validate_response(raw, bundle, evidence, "IT_SERVICES_PROFILE")
    assert len(accepted) == 1
    assert any(i.code == "DUPLICATE_CLAIM" for r in rejected for i in r.issues)


async def test_malformed_response_rejected(world):
    *_, bundle, evidence = world
    raw = RawAnalystResponse(
        structured_payload={"claims": "not-a-list"}, structured_payload_sha256="0" * 64
    )
    accepted, rejected = validate_response(raw, bundle, evidence, "IT_SERVICES_PROFILE")
    assert accepted == () and rejected[0].issues[0].code == "MALFORMED_RESPONSE"


async def test_missing_required_field_rejected(world):
    *_, bundle, evidence = world
    raw = RawAnalystResponse(
        structured_payload={"claims": [{"section": "VALUATION"}]},
        structured_payload_sha256="0" * 64,
    )
    accepted, rejected = validate_response(raw, bundle, evidence, "IT_SERVICES_PROFILE")
    assert accepted == () and rejected[0].issues[0].code == "MALFORMED_RESPONSE"


def test_business_model_coverage_never_available_from_registry_facts_alone():
    company_ref = EvidenceReference(
        kind="COMPANY", owning_snapshot_id=uuid4(), snapshot_payload_sha256="0" * 64
    )
    coverage = analyst_engine.compute_section_coverage(
        ("BUSINESS_MODEL",),
        (company_ref,),
        analyst_engine.ResolvedEvidence(company=None, financial=None),
    )
    assert coverage[0].status == "INSUFFICIENT_INPUT"


def test_section_coverage_unavailable_with_nothing_pinned():
    coverage = analyst_engine.compute_section_coverage(
        ("INDUSTRY_AND_MACRO",), (), analyst_engine.ResolvedEvidence(company=None, financial=None)
    )
    assert coverage[0].status == "UNAVAILABLE"


def test_heterogeneous_nonavailable_coverage_policy():
    refs = [
        EvidenceReference(
            kind="SECTOR_KPI",
            owning_snapshot_id=uuid4(),
            item_key="a",
            snapshot_payload_sha256="0" * 64,
        ),
        EvidenceReference(
            kind="SECTOR_KPI",
            owning_snapshot_id=uuid4(),
            item_key="b",
            snapshot_payload_sha256="0" * 64,
        ),
    ]
    result = analyst_engine._aggregate(
        "SECTOR_KPIS", ["INSUFFICIENT_INPUT", "UNVERIFIED_METRIC_SEMANTICS"], refs
    )
    assert result.status == "UNAVAILABLE"
    assert result.reason == "HETEROGENEOUS_NON_AVAILABLE_INPUTS"
    assert result.upstream_statuses == ("INSUFFICIENT_INPUT", "UNVERIFIED_METRIC_SEMANTICS")


def test_single_nonavailable_status_propagates_verbatim():
    refs = [
        EvidenceReference(
            kind="SECTOR_KPI",
            owning_snapshot_id=uuid4(),
            item_key="a",
            snapshot_payload_sha256="0" * 64,
        )
    ]
    result = analyst_engine._aggregate("SECTOR_KPIS", ["NOT_APPLICABLE"], refs)
    assert result.status == "NOT_APPLICABLE" and result.upstream_statuses == ("NOT_APPLICABLE",)


def test_partial_coverage_when_mixed_available_and_not():
    refs = [
        EvidenceReference(
            kind="SECTOR_KPI",
            owning_snapshot_id=uuid4(),
            item_key="a",
            snapshot_payload_sha256="0" * 64,
        )
    ]
    result = analyst_engine._aggregate("SECTOR_KPIS", ["AVAILABLE", "NOT_APPLICABLE"], refs)
    assert result.status == "PARTIAL"


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("The multiple is 20.5x.", [("20.5", "MULTIPLE_X")]),
        ("Margin improved by 150bps.", [("150", "BASIS_POINTS")]),
        ("Growth was 12.3%.", [("12.3", "PERCENT")]),
        ("Price is Rs 1087.", [("1087", "CURRENCY")]),
        ("A plain number 42 appears.", [("42", "NUMBER")]),
    ],
)
def test_numeric_token_extraction_classifies_unit_family(statement, expected):
    tokens = extract_numeric_tokens(statement)
    assert [(str(t.value), t.family) for t in tokens] == expected
