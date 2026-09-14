"""Stock Research AI: a local-first equity research foundation."""

from importlib import metadata as _metadata

try:
    #: Part 6C-A: the single authoritative version source is pyproject.toml's
    #: [project].version - read at runtime rather than hand-duplicated here, so a
    #: version bump can never leave this string stale relative to the package itself.
    __version__ = _metadata.version("stock-research-ai")
except _metadata.PackageNotFoundError:
    # Only reachable if this package is imported without being installed at all
    # (e.g. a bare source checkout with no `uv sync`/`pip install`) - not a state any
    # normal `uv run`/installed use of this project is ever in.
    __version__ = "0.0.0+unknown"


def main() -> None:
    """Preserve the existing console entry point without startup side effects."""
    from stock_research_ai.__main__ import main as run

    run()
