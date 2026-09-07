"""Stock Research AI: a local-first equity research foundation."""

__version__ = "0.1.0"


def main() -> None:
    """Preserve the existing console entry point without startup side effects."""
    from stock_research_ai.__main__ import main as run

    run()
