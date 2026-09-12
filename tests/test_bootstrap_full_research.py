"""Part 6B0: bootstrap-level missing-credential behavior. Absent Anthropic configuration
must never crash bootstrap, never silently substitute FakeAnalystModel, and must leave
every unrelated capability/command fully functional - only FULL_RESEARCH_V1's ANALYSIS
stage is affected, and it fails there explicitly and typed.
"""

import pytest

from stock_research_ai.adapters.anthropic.analyst import AnthropicAnalystModel
from stock_research_ai.application.research_plan import FullResearchRequest
from stock_research_ai.bootstrap import (
    full_research,
    initialize_database,
    live_analyst_model,
    research_engine,
)
from stock_research_ai.config import Settings


async def test_live_analyst_model_absent_configuration_returns_none(tmp_path):
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    model, provider_kind = await live_analyst_model(settings)
    assert model is None
    assert provider_kind is None


async def test_live_analyst_model_unsupported_provider_raises_explicitly(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3", analyst_provider="openai", analyst_api_key="k"
    )
    with pytest.raises(RuntimeError, match="Unsupported"):
        await live_analyst_model(settings)


async def test_live_analyst_model_configured_returns_a_real_anthropic_adapter(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        analyst_provider="anthropic",
        analyst_api_key="sk-test",
    )
    model, provider_kind = await live_analyst_model(settings)
    assert isinstance(model, AnthropicAnalystModel)
    assert provider_kind == "LIVE"


async def test_full_research_raises_explicitly_at_analysis_when_unconfigured(tmp_path):
    settings = Settings(database_path=tmp_path / "db.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    with pytest.raises(RuntimeError, match="No LIVE analyst provider"):
        await full_research(
            settings,
            FullResearchRequest(source_url="https://www.screener.in/company/YESBANK/"),
            tmp_path / "out.json",
        )
    # Never partially written, never a silent fake: no output file was produced.
    assert not (tmp_path / "out.json").exists()


async def test_unrelated_capabilities_are_unaffected_by_absent_analyst_configuration(tmp_path):
    """A bare bootstrap engine (used by serve-http/serve-mcp) must not know or care
    whether Anthropic is configured - analysis_available is simply False."""
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    await initialize_database(settings)
    async with research_engine(settings) as engine:
        capabilities = await engine.get_capabilities()
    assert capabilities.analysis_available is False
    assert capabilities.deterministic_challenge_available is False
    assert capabilities.deterministic_report_assembly_available is False
