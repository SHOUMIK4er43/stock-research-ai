"""Canonical serialization for the Detailed Research Screener dataset."""

import json

from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset


def canonical_dataset_json(dataset: DetailedScreenerDataset) -> str:
    """One canonical encoding: sorted keys, compact, exact decimal strings, UTF-8 safe."""
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
