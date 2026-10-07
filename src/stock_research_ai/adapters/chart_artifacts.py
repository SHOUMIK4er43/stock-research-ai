"""Write a FinancialChartBundle's SVG artifacts to disk, content-addressed (once per hash)."""

import hashlib
import os
import tempfile
from pathlib import Path

from stock_research_ai.domain.financial_charts import FinancialChartBundle


def write_chart_artifacts(bundle: FinancialChartBundle, root: Path) -> list[Path]:
    """Each artifact lands at <root>/<content_reference>; existing identical files are kept,
    a different file at the same address is an integrity error."""
    written = []
    for artifact in bundle.artifacts:
        data = artifact.svg.encode("utf-8")
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError(f"artifact {artifact.name} does not match its hash")
        destination = root / artifact.content_reference
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError(f"{destination} exists with different content")
            written.append(destination)
            continue
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
        written.append(destination)
    return written
