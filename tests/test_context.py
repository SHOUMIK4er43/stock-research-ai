import hashlib
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.adapters.mospi.source import MospiSource
from stock_research_ai.adapters.publications.source import PublicationSource
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.bootstrap import (
    context_engine,
    ingest_macro,
    ingest_publication,
    initialize_database,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.macro import MacroRequest, MacroSnapshot
from stock_research_ai.domain.publications import PublicationRequest, PublicationSnapshot
from stock_research_ai.domain.subjects import EvidenceSubject

FIX = Path(__file__).parent / "fixtures/context"
TRUTH = json.loads((FIX / "expected_synthetic.json").read_text())


@pytest.fixture
async def settings(tmp_path):
    value = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "artifacts")
    await initialize_database(value)
    return value


def publication(**updates):
    return PublicationRequest.model_validate(
        {
            "url": "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=sample",
            "source_url": "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=sample",
            "source_key": "rbi:sample",
            "format": "RBI_HTML",
            "replay_file": FIX / "rbi_synthetic.html",
            "evidence_kind": "SYNTHETIC",
            "subjects": [
                {
                    "kind": "SECTOR",
                    "identifier": "banking",
                    "label": "Banking",
                    "basis": "CALLER_DECLARED",
                    "declaration": "Test caller links banking publication",
                }
            ],
            **updates,
        }
    )


def macro(**updates):
    return MacroRequest.model_validate(
        {
            "periods": ["2025-01", "2025-02"],
            "base_year": "2012",
            "metadata_file": FIX / "cpi_metadata_synthetic.json",
            "data_file": FIX / "cpi_synthetic.json",
            "evidence_kind": "SYNTHETIC",
            **updates,
        }
    )


async def test_publication_golden_search_export(settings, tmp_path):
    result = await ingest_publication(settings, publication(), tmp_path / "export")
    truth = TRUTH["publication"]
    assert result.identity.publisher == truth["publisher"]
    assert result.title == truth["title"] and result.publication_date.isoformat() == truth["date"]
    assert result.reference_period == truth["period"]
    assert result.passages[0].text == truth["passage"]
    assert result.coverage.overall == "COMPLETE"
    assert result.subjects[0].basis == "CALLER_DECLARED"
    assert result.attachments[0].original_href == "../sample.pdf"
    assert result.attachments[0].resolved_url == "https://www.rbi.org.in/sample.pdf"
    assert result.coverage.attachments == "LINKED_NOT_ACQUIRED"
    assert (
        await FileRawArtifactStore(settings.artifact_path).read(result.artifact)
        == (FIX / "rbi_synthetic.html").read_bytes()
    )
    restored = PublicationSnapshot.model_validate_json(
        (tmp_path / "export/normalized/snapshot.json").read_text()
    )
    assert restored == result
    async with context_engine(settings) as engine:
        assert await engine.publication_history(result.identity.id) == (result,)
        hits = await engine.search_publications("credit observations")
        assert len(hits) == 1 and hits[0].retrieval_id == result.retrieval.id
        assert hits[0].provenance.raw_artifact_id == result.artifact.id
        with pytest.raises(ValueError):
            await engine.search_publications("")


async def test_issuer_pdf_subject_without_fake_company(settings, tmp_path, make_document_pdf):
    p = tmp_path / "issuer.pdf"
    p.write_bytes(
        make_document_pdf(
            ["July 19, 2025\nYES BANK ANNOUNCES FINANCIAL RESULTS\nSynthetic company evidence."]
        )
    )
    req = publication(
        url="https://www.yesbank.in/example.pdf",
        source_url="https://www.yesbank.in/example.pdf",
        source_key="issuer:sample",
        format="ISSUER_PDF",
        replay_file=p,
        subjects=[
            {
                "kind": "COMPANY",
                "identifier": "YESBANK",
                "label": "YES BANK",
                "basis": "CALLER_DECLARED",
                "declaration": "Explicit proof company",
            }
        ],
    )
    result = await ingest_publication(settings, req, tmp_path / "export")
    assert result.title == "YES BANK ANNOUNCES FINANCIAL RESULTS"
    assert result.publication_date.isoformat() == "2025-07-19"
    assert result.identity.publisher == "YES BANK"
    assert result.passages[0].provenance.source_location.locator == "pdf:page[1]"
    assert result.coverage.overall == "COMPLETE"


@pytest.mark.parametrize(
    "case",
    ["missing_body", "missing_date", "malformed", "attachment_only", "subjects", "unavailable"],
)
async def test_publication_failures(case, settings, tmp_path):
    text = (FIX / "rbi_synthetic.html").read_text()
    if case in {"missing_body", "attachment_only"}:
        import re

        text = re.sub(r"<p>.*?</p>", "", text)
    if case == "missing_date":
        text = text.replace("Date : Jul 31, 2024", "Unknown")
    if case == "malformed":
        text = text.replace("</table>", "")
    path = tmp_path / "source.html"
    path.write_text(text)
    req = publication(replay_file=path)
    if case == "subjects":
        req = publication(replay_file=path, subjects=[*req.subjects, *req.subjects])
    if case == "unavailable":
        req = publication(replay_file=tmp_path / "absent")
    result = await ingest_publication(settings, req, tmp_path / "export")
    assert result.coverage.overall != "COMPLETE"
    assert result.coverage.issues


async def test_publication_versions(settings, tmp_path):
    async with context_engine(settings) as engine:
        a = await engine.ingest_publication(publication())
        b = await engine.ingest_publication(publication())
        p = tmp_path / "changed.html"
        p.write_bytes(
            (FIX / "rbi_synthetic.html").read_bytes().replace(b"Sample credit", b"Revised credit")
        )
        c = await engine.ingest_publication(publication(replay_file=p))
        assert a.id != b.id != c.id
        assert a.artifact.content_sha256 == b.artifact.content_sha256 != c.artifact.content_sha256
        assert a.retrieval.id != b.retrieval.id
        assert await engine.publication_history(a.identity.id) == (a, b, c)
    db = SQLiteDatabase(settings.database_path)
    try:
        with pytest.raises(IntegrityError):
            await db.context.save_publication(a)
    finally:
        await db.close()


async def test_macro_synthetic_golden(settings, tmp_path):
    result = await ingest_macro(settings, macro(), tmp_path / "export")
    for field in ("base_year", "geography", "population", "frequency", "unit"):
        assert getattr(result.series, field) == TRUTH["macro"][field]
    assert [o.value for o in result.observations] == [Decimal("100.25"), Decimal("101.50")]
    assert result.series.seasonal_adjustment == "UNKNOWN"
    assert result.coverage.overall == "PARTIAL"
    assert result.coverage.missing_periods == ()
    assert "revision_status" in result.coverage.unknown_metadata
    assert result.observations[0].source_status == "F"
    for obs in result.observations:
        assert obs.retrieved_at == result.retrievals[1].retrieved_at
        assert obs.published_at is None and obs.provider_vintage_at is None
        assert obs.provenance.raw_artifact_id == result.artifacts[1].id
    assert (
        result.artifacts[1].content_sha256
        == hashlib.sha256((FIX / "cpi_synthetic.json").read_bytes()).hexdigest()
    )
    assert (
        MacroSnapshot.model_validate_json(
            (tmp_path / "export/normalized/snapshot.json").read_text()
        )
        == result
    )


@pytest.mark.parametrize(
    "case",
    [
        "malformed",
        "duplicate",
        "missing",
        "unknown_unit",
        "unavailable",
        "wrong_base",
        "wrong_mapping",
        "pagination",
        "invalid_year",
        "invalid_envelope",
    ],
)
async def test_macro_failures(case, settings, tmp_path):
    body = json.loads((FIX / "cpi_synthetic.json").read_text())
    data = body["result"]["structuredContent"]
    if case == "malformed":
        data["data"][0]["index"] = "not a number"
    if case == "duplicate":
        data["data"].append(data["data"][0])
    if case == "missing":
        data["data"].pop()
    if case == "unknown_unit":
        for row in data["data"]:
            row["unit"] = "mystery"
    if case == "wrong_base":
        for row in data["data"]:
            row["baseyear"] = "2024"
    if case == "wrong_mapping":
        data["data"][0]["state"] = "Wrong geography"
    if case == "pagination":
        data["meta_data"]["totalPages"] = 2
    if case == "invalid_year":
        data["data"][0]["year"] = 2025.5
    if case == "invalid_envelope":
        body["result"] = []
    path = tmp_path / "data.json"
    path.write_text(json.dumps(body))
    if case == "unavailable":
        path = tmp_path / "absent"
    result = await ingest_macro(settings, macro(data_file=path), tmp_path / "export")
    assert result.coverage.overall != "COMPLETE"
    if case == "duplicate":
        assert result.coverage.duplicate_periods == ("2025-01",)
    if case == "missing":
        assert result.coverage.missing_periods == ("2025-02",)
    if case == "unknown_unit":
        assert "unit" in result.coverage.unknown_metadata


async def test_macro_revision_rebase_and_seasonality(settings, tmp_path):
    async with context_engine(settings) as engine:
        first = await engine.ingest_macro(macro())
        second = await engine.ingest_macro(macro())
        body = json.loads((FIX / "cpi_synthetic.json").read_text())
        rows = body["result"]["structuredContent"]["data"]
        rows[0].update(
            index="100.30",
            status="Revised",
            published_at="2025-03-12T12:00:00+05:30",
            provider_vintage_at="2025-03-13T12:00:00+05:30",
        )
        path = tmp_path / "revision.json"
        path.write_text(json.dumps(body))
        revised = await engine.ingest_macro(macro(data_file=path))
        assert revised.series.id == first.series.id
        assert revised.observations[0].revision_status == "REVISED"
        assert revised.observations[0].value == Decimal("100.30")
        assert await engine.macro_history(first.series.id) == (first, second, revised)
        assert len(await engine.lookup_macro(first.series.id, "2025-01")) == 3
        for row in rows:
            row["baseyear"] = "2024"
        path.write_text(json.dumps(body))
        rebased = await engine.ingest_macro(macro(data_file=path, base_year="2024"))
        assert rebased.series.id != first.series.id
        for row in rows:
            row["seasonal_adjustment"] = "ADJUSTED"
        path.write_text(json.dumps(body))
        adjusted = await engine.ingest_macro(macro(data_file=path, base_year="2024"))
        assert adjusted.series.id != rebased.series.id
        assert first.observations[0].value == Decimal("100.25")


def test_subject_provenance_required():
    with pytest.raises(ValidationError):
        EvidenceSubject(
            kind="SECTOR",
            identifier="banking",
            label="Banking",
            basis="SOURCE_DECLARED",
            declaration="No proof",
        )


async def test_mocked_live_sources(settings):
    def handler(req):
        if req.method == "POST":
            body = json.loads(req.content)
            filename = (
                "cpi_metadata_synthetic.json"
                if body["params"]["name"] == "get_metadata"
                else "cpi_synthetic.json"
            )
            return httpx.Response(200, content=(FIX / filename).read_bytes())
        return httpx.Response(503, content=b"unavailable")

    async def lookup(host):
        return ["93.184.216.34"]

    transport = httpx.MockTransport(handler)
    sources = PublicationSource(DocumentSource(transport=transport, lookup=lookup))
    async with context_engine(settings, sources, MospiSource(transport)) as engine:
        p = await engine.ingest_publication(publication(replay_file=None, evidence_kind="PROVIDER"))
        assert p.coverage.overall == "FAILED"
        m = await engine.ingest_macro(
            macro(metadata_file=None, data_file=None, evidence_kind="PROVIDER")
        )
        assert len(m.observations) == 2
        assert m.retrievals[1].request_method == "POST"
        assert m.retrievals[1].request_parameters["params"]["name"] == "get_data"


def test_context_cli_offline(tmp_path):
    import os
    import subprocess
    import sys

    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        'import socket\ndef blocked(*a,**k): raise AssertionError("Network forbidden")\n'
        "socket.socket.connect=blocked\nsocket.create_connection=blocked\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(guard)}
    db = tmp_path / "cli.sqlite3"
    base = [sys.executable, "-B", "-m", "stock_research_ai", "--database", str(db)]
    subprocess.run([*base, "init-db"], env=env, check=True, capture_output=True)
    for name, request, expected in [("publication", publication(), 0), ("macro", macro(), 3)]:
        file = tmp_path / f"{name}.json"
        file.write_text(request.model_dump_json())
        output = tmp_path / name
        result = subprocess.run(
            [*base, f"ingest-{name}", "--request", str(file), "--output", str(output)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected, result.stderr
        assert (output / "raw/manifest.json").is_file()
        assert (output / "normalized/snapshot.json").is_file()


async def test_source_and_mapping_provenance_not_fabricated(settings):
    async with context_engine(settings) as engine:
        result = await engine.ingest_publication(publication())
    source_subject = EvidenceSubject(
        kind="SECTOR",
        identifier="banking",
        label="Banking",
        basis="SOURCE_DECLARED",
        declaration="Literal supporting text",
        provenance=result.passages[0].provenance,
    )
    data = result.model_dump()
    data["subjects"] = [source_subject]
    assert (
        PublicationSnapshot.model_validate(data).subjects[0].provenance.raw_artifact_id
        == result.artifact.id
    )
    data["subjects"] = [
        source_subject.model_copy(
            update={
                "provenance": source_subject.provenance.model_copy(
                    update={"raw_artifact_id": uuid4()}
                )
            }
        )
    ]
    with pytest.raises(ValidationError):
        PublicationSnapshot.model_validate(data)
