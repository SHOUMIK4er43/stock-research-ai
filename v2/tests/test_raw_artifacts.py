from datetime import UTC, datetime
from uuid import uuid4

import pytest

from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.domain.provenance import AcquiredPayload, SourceRetrieval


def payload(content):
    return AcquiredPayload(
        content=content,
        retrieval=SourceRetrieval(
            id=uuid4(),
            provider="test",
            requested_url="https://example.com/",
            final_url="https://example.com/",
            retrieved_at=datetime.now(UTC),
            status="success",
        ),
    )


async def test_content_addressing_and_independent_retrievals(tmp_path):
    store = FileRawArtifactStore(tmp_path)
    raw = b"\xff\x00 exact bytes\r\n"
    a, b = await store.put(payload(raw)), await store.put(payload(raw))
    assert a.content_reference == b.content_reference
    assert a.id != b.id and a.retrieval_id != b.retrieval_id
    assert await store.read(a) == raw == await store.read(b)
    assert len(list(tmp_path.rglob("*.html"))) == 1


async def test_corruption_is_detected_on_read_and_put(tmp_path):
    store = FileRawArtifactStore(tmp_path)
    acquired = payload(b"evidence")
    artifact = await store.put(acquired)
    (tmp_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        await store.read(artifact)
    with pytest.raises(ValueError, match="integrity"):
        await store.put(acquired)
    assert not list(tmp_path.rglob(".pending-*"))


async def test_store_rejects_missing_bytes_and_path_traversal(tmp_path):
    store = FileRawArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="No acquired"):
        await store.put(payload(None))
    artifact = await store.put(payload(b"evidence"))
    with pytest.raises(ValueError, match="escapes"):
        await store.read(artifact.model_copy(update={"content_reference": "../outside.html"}))
