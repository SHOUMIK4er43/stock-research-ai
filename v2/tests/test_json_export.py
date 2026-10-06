import hashlib
import json

import pytest

from stock_research_ai.adapters.json_export import export_result
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore


async def test_export_is_deterministic_and_does_not_overwrite(yesbank_result, tmp_path):
    store = FileRawArtifactStore(tmp_path / "artifacts")
    destination = tmp_path / "copy"
    await export_result(yesbank_result, destination, store)
    original = tmp_path / "export"
    for path in original.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (destination / path.relative_to(original)).read_bytes()
    with pytest.raises(ValueError, match="empty directory"):
        await export_result(yesbank_result, destination, store)
    value = json.loads((destination / "normalized/quarterly.json").read_text())["data"][0]["value"]
    assert isinstance(value, str)


def test_fixture_manifest_integrity(fixture_dir):
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    for fixture in manifest["fixtures"]:
        content = (fixture_dir / fixture["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == fixture["sha256"]
        assert b"csrfmiddlewaretoken" not in content
        assert b"<script" not in content
