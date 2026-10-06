"""Part 6B0: deterministic document-chunk selection. Plain substring match only (the same
primitive DocumentRepository.search already uses), applied strictly to the documents
passed in - never a repository-wide search - so run-scope isolation holds by
construction. No management/guidance promotion: a SelectedChunk is only ever
(document_version_id, chunk_id, ...) metadata, never a ManagementStatement.
"""

from stock_research_ai.application.document_topics import (
    DocumentResearchTopic,
    SelectedChunk,
    match_document_topics,
)
from stock_research_ai.bootstrap import ingest_document
from stock_research_ai.domain.documents import DocumentRequest, DocumentType


async def _ingest(
    document_settings, document_company, tmp_path, make_document_pdf, texts, document_type, name
):
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(make_document_pdf(texts=texts))
    request = DocumentRequest(
        company_id=document_company.id,
        url=f"https://example.com/{name}.pdf",
        document_type=document_type,
        replay_path=path,
    )
    return await ingest_document(document_settings, request, tmp_path / f"{name}-out")


async def test_business_model_topic_matches_annual_report(
    document_settings, document_company, tmp_path, make_document_pdf
):
    document = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=("Our business model focuses on retail lending across India.",),
        document_type=DocumentType.ANNUAL_REPORT,
        name="ar",
    )
    selected = match_document_topics((document,))
    match = next((c for c in selected if c.topic_id == "BUSINESS_MODEL_V1"), None)
    assert match is not None
    assert match.document_type == DocumentType.ANNUAL_REPORT
    assert match.document_version_id == document.version_id
    assert match.matched_term == "business model"


async def test_topic_ignores_disallowed_document_type(
    document_settings, document_company, tmp_path, make_document_pdf
):
    document = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=("Key risk factors include credit risk and market risk.",),
        document_type=DocumentType.CREDIT_RATING,  # RISKS_V1 only allows ANNUAL_REPORT
        name="cr",
    )
    selected = match_document_topics((document,))
    assert not any(c.topic_id == "RISKS_V1" for c in selected)


async def test_run_scope_isolation_only_passed_documents_are_considered(
    document_settings, document_company, tmp_path, make_document_pdf
):
    in_scope = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=("Management strategy update for this quarter.",),
        document_type=DocumentType.CONCALL_TRANSCRIPT,
        name="in-scope",
    )
    out_of_scope = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=("Management strategy update from a prior research run.",),
        document_type=DocumentType.CONCALL_TRANSCRIPT,
        name="out-of-scope",
    )
    # Only `in_scope` is passed in - mirrors run_full_research passing exactly the
    # documents THIS run ingested, never a repository-wide search across history.
    selected = match_document_topics((in_scope,))
    versions = {c.document_version_id for c in selected}
    assert in_scope.version_id in versions
    assert out_of_scope.version_id not in versions


async def test_per_document_cap_and_topic_max_chunks_are_enforced(
    document_settings, document_company, tmp_path, make_document_pdf
):
    document = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=(
            "Page one discusses risk factors in detail.",
            "Page two also discusses risk factors again.",
            "Page three: risk factors once more for good measure.",
        ),
        document_type=DocumentType.ANNUAL_REPORT,
        name="multi-page-risk",
    )
    assert len(document.chunks) == 3  # sanity: three pages, no shared headings -> three chunks

    capped_topic = DocumentResearchTopic(
        topic_id="RISKS_CAPPED",
        section="RISKS",
        allowed_document_types=(DocumentType.ANNUAL_REPORT,),
        query_terms=("risk factors",),
        max_chunks=2,
        per_document_cap=1,
    )
    selected = match_document_topics((document,), topics=(capped_topic,))
    assert len(selected) == 1  # per_document_cap=1 wins before topic max_chunks=2 is reached

    loose_topic = capped_topic.model_copy(update={"per_document_cap": 5, "max_chunks": 2})
    selected = match_document_topics((document,), topics=(loose_topic,))
    assert len(selected) == 2  # topic max_chunks=2 now binds instead


async def test_total_chunk_budget_caps_across_topics(
    document_settings, document_company, tmp_path, make_document_pdf
):
    document = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=(
            "This page covers our business model in depth.",
            "This page covers risk factors in depth.",
        ),
        document_type=DocumentType.ANNUAL_REPORT,
        name="mixed",
    )
    topics = (
        DocumentResearchTopic(
            topic_id="A", section="BUSINESS_MODEL",
            allowed_document_types=(DocumentType.ANNUAL_REPORT,),
            query_terms=("business model",), max_chunks=5, per_document_cap=5,
        ),
        DocumentResearchTopic(
            topic_id="B", section="RISKS",
            allowed_document_types=(DocumentType.ANNUAL_REPORT,),
            query_terms=("risk factors",), max_chunks=5, per_document_cap=5,
        ),
    )
    selected = match_document_topics((document,), topics=topics, total_chunk_budget=1)
    assert len(selected) == 1
    assert selected[0].topic_id == "A"  # first topic in registry order wins the single slot


async def test_guidance_style_wording_is_not_selected_by_an_unrelated_topic(
    document_settings, document_company, tmp_path, make_document_pdf
):
    """A chunk mentioning 'expect'/'guidance'/'target' is only ever selected if an actual
    registered query term matches it - selection never treats those words specially."""
    document = await _ingest(
        document_settings,
        document_company,
        tmp_path,
        make_document_pdf,
        texts=("We expect strong growth and guide for a higher target next year.",),
        document_type=DocumentType.CONCALL_TRANSCRIPT,
        name="guidance",
    )
    selected = match_document_topics((document,))
    # MGMT_DOC_INTEL_V1's query terms are "management"/"strategy" - neither appears here.
    assert not any(c.topic_id == "MGMT_DOC_INTEL_V1" for c in selected)


def test_selected_chunk_carries_only_reference_metadata_never_a_management_statement():
    fields = set(SelectedChunk.model_fields)
    assert fields == {
        "document_version_id",
        "chunk_id",
        "document_type",
        "publication_date",
        "fiscal_label",
        "topic_id",
        "matched_term",
        "rank",
    }
