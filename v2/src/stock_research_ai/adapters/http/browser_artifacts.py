"""Additive read/projection wrapper over the existing, frozen Part 6A export functions -
markdown_export.py and json_export.py are never modified. render_institutional_report/
render_investor_brief are already pure, in-memory functions, reused directly.
RESEARCH_JSON reuses export_report exactly (via a throwaway temp directory) rather than
duplicating its private payload-construction logic, so output is guaranteed
byte-identical to the existing CLI/proof-script artifact by construction, not by a
second implementation that could drift from it.
"""

import tempfile
from pathlib import Path

from stock_research_ai.adapters.http.browser_schemas import ArtifactKind
from stock_research_ai.adapters.json_export import export_report
from stock_research_ai.adapters.markdown_export import (
    render_institutional_report,
    render_investor_brief,
)
from stock_research_ai.domain.report import ResearchReportSnapshot

_MEDIA_TYPES: dict[ArtifactKind, str] = {
    "INSTITUTIONAL_REPORT": "text/markdown; charset=utf-8",
    "INVESTOR_BRIEF": "text/markdown; charset=utf-8",
    "RESEARCH_JSON": "application/json",
}


def media_type_for(artifact_kind: ArtifactKind) -> str:
    return _MEDIA_TYPES[artifact_kind]


def render_artifact(snapshot: ResearchReportSnapshot, artifact_kind: ArtifactKind) -> str:
    """Returns the artifact's exact rendered content as text. Never accepts or produces
    a filesystem path from/for the caller - the HTTP layer serves this returned text
    directly, keyed only by the already-resolved snapshot and a closed artifact_kind."""
    if artifact_kind == "INSTITUTIONAL_REPORT":
        return render_institutional_report(snapshot)
    if artifact_kind == "INVESTOR_BRIEF":
        return render_investor_brief(snapshot)
    if artifact_kind == "RESEARCH_JSON":
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "export"
            export_report(snapshot, output)
            return (output / "COMPANY_RESEARCH.json").read_text()
    raise ValueError(f"Unknown artifact kind: {artifact_kind!r}")
