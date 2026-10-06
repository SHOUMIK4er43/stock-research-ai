import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

import pytest
from mcp import Client, StdioServerParameters

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company
from stock_research_ai.interfaces import mcp as mcp_interface


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_schemas_and_company_round_trip(
    engine: ResearchEngine, mode: Literal["auto", "legacy"]
) -> None:
    server = mcp_interface.create_server(engine=engine)
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert set(tools) == {
            "health_check",
            "get_capabilities",
            "register_company",
            "get_company",
        }
        for tool in tools.values():
            assert tool.output_schema is not None
            assert tool.output_schema["type"] == "object"
            assert "ctx" not in tool.input_schema.get("properties", {})
        registration_schema = tools["register_company"].input_schema
        assert registration_schema["required"] == ["name"]
        assert set(registration_schema["properties"]) == {
            "name",
            "listings",
            "provider_identifiers",
        }
        assert tools["get_company"].input_schema["properties"]["company_id"]["format"] == "uuid"

        health = await client.call_tool("health_check")
        assert not health.is_error
        assert health.structured_content == {"status": "ok"}
        capabilities = await client.call_tool("get_capabilities")
        assert not capabilities.is_error
        assert capabilities.structured_content == (await engine.get_capabilities()).model_dump(
            mode="json"
        )

        result = await client.call_tool(
            "register_company",
            {
                "name": "Yes Bank",
                "listings": [{"exchange": "NSE", "symbol": "YESBANK"}],
                "provider_identifiers": [{"provider": "screener", "identifier": "YESBANK"}],
            },
        )
        assert not result.is_error
        company = Company.model_validate(result.structured_content)
        assert company.name == "Yes Bank"
        assert company.listings[0].symbol == "YESBANK"
        assert await engine.get_company(company.id) == company

        fetched = await client.call_tool("get_company", {"company_id": str(company.id)})
        assert not fetched.is_error
        assert fetched.structured_content == result.structured_content


async def test_mcp_validation_and_missing_company(engine: ResearchEngine) -> None:
    server = mcp_interface.create_server(engine=engine)
    async with Client(server) as client:
        invalid_id = await client.call_tool("get_company", {"company_id": "not-a-uuid"})
        assert invalid_id.is_error
        missing = await client.call_tool("get_company", {"company_id": str(uuid4())})
        assert missing.is_error
        assert any(
            content.type == "text" and "Company not found" in content.text
            for content in missing.content
        )
        invalid_name = await client.call_tool("register_company", {"name": "   "})
        assert invalid_name.is_error
        missing_name = await client.call_tool("register_company", {})
        assert missing_name.is_error


@pytest.mark.parametrize("injected", [False, True])
async def test_mcp_resource_ownership(
    engine: ResearchEngine, monkeypatch: pytest.MonkeyPatch, injected: bool
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def tracked_engine(_: Settings) -> AsyncIterator[ResearchEngine]:
        events.append("opened")
        try:
            yield engine
        finally:
            events.append("closed")

    monkeypatch.setattr(mcp_interface, "research_engine", tracked_engine)
    server = mcp_interface.create_server(engine=engine if injected else None)
    assert events == []
    async with Client(server) as client:
        assert events == ([] if injected else ["opened"])
        assert not (await client.call_tool("get_capabilities")).is_error
    assert events == ([] if injected else ["opened", "closed"])


async def test_mcp_stdio_process_persists_company_to_sqlite(database: SQLiteDatabase) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "stock_research_ai", "--database", str(database.path), "serve-mcp"],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with asyncio.timeout(15), Client(server) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == {
            "health_check",
            "get_capabilities",
            "register_company",
            "get_company",
        }
        health = await client.call_tool("health_check", {})
        assert not health.is_error
        assert health.structured_content == {"status": "ok"}
        capabilities = await client.call_tool("get_capabilities", {})
        assert not capabilities.is_error
        assert capabilities.structured_content["analysis_available"] is False
        registered = await client.call_tool("register_company", {"name": "Stdio Company"})
        assert not registered.is_error
        company = Company.model_validate(registered.structured_content)
        fetched = await client.call_tool("get_company", {"company_id": str(company.id)})
        assert not fetched.is_error
        assert fetched.structured_content == registered.structured_content
    assert await database.repository.get(company.id) == company
