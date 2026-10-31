"""Content-addressed exact bytes, atomically published and verified on read."""

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from stock_research_ai.domain.provenance import AcquiredPayload, RawArtifact
from stock_research_ai.private_files import ensure_private_dir


class FileRawArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def put(self, payload: AcquiredPayload) -> RawArtifact:
        if payload.content is None:
            raise ValueError("No acquired bytes to preserve")
        digest = hashlib.sha256(payload.content).hexdigest()
        relative = Path("sha256") / digest[:2] / f"{digest}.{payload.file_extension}"
        destination = self.root / relative
        ensure_private_dir(destination.parent)
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload.content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(name, destination)
            except FileExistsError:
                if destination.read_bytes() != payload.content:
                    raise ValueError(
                        "Existing raw artifact failed integrity verification"
                    ) from None
        finally:
            Path(name).unlink(missing_ok=True)
        retrieval = payload.retrieval
        artifact = RawArtifact.model_validate(
            {
                "id": uuid4(),
                "source_url": retrieval.final_url,
                "source_type": payload.source_type,
                "retrieved_at": retrieval.retrieved_at,
                "content_reference": relative.as_posix(),
                "content_sha256": digest,
                "collector_version": retrieval.collector_version,
                "retrieval_id": retrieval.id,
                "size_bytes": len(payload.content),
                "content_type": retrieval.content_type,
                "encoding": retrieval.encoding,
                "complete": retrieval.complete,
                "representation": "replay_bytes"
                if retrieval.mode == "replay"
                else "decoded_http_body",
            }
        )
        await self.read(artifact)
        return artifact

    async def read(self, artifact: RawArtifact) -> bytes:
        path = (self.root / artifact.content_reference).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact path escapes storage root")
        content = path.read_bytes()
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.content_sha256
        ):
            raise ValueError("Raw artifact integrity verification failed")
        return content
