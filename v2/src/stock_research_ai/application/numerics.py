"""Shared deterministic Decimal root growth; caller owns working precision."""

from decimal import Decimal


def root_growth(ratio: Decimal, periods: Decimal) -> Decimal:
    if ratio <= 0 or periods <= 0:
        raise ValueError("Positive ratio and period count required")
    # Exact square root is useful for perfect-square proofs; Decimal sqrt is deterministic.
    root = ratio.sqrt() if periods == 2 else (ratio.ln() / periods).exp()
    return root - 1
