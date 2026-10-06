"""MCP tools exposing the same research engine as the HTTP adapter."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import ValidationError

from stock_research_ai.application.engine import Capabilities, ResearchEngine
from stock_research_ai.bootstrap import research_engine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import (
    Company,
    CompanyRegistration,
    ExchangeListing,
    ProviderIdentifier,
)


def create_server(
    engine: ResearchEngine | None = None,
    settings: Settings | None = None,
) -> MCPServer[ResearchEngine]:
    """Create a stdio-ready server; resource ownership belongs to its lifespan."""

    @asynccontextmanager
    async def lifespan(_: MCPServer[ResearchEngine]) -> AsyncIterator[ResearchEngine]:
        if engine is not None:
            yield engine
            return
        runtime_settings = settings if settings is not None else Settings()
        async with research_engine(runtime_settings) as owned_engine:
            yield owned_engine

    server: MCPServer[ResearchEngine] = MCPServer("Stock Research AI", lifespan=lifespan)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only, structured_output=True)
    async def health_check() -> dict[str, str]:
        """Check whether the local Stock Research AI server is responding."""
        return {"status": "ok"}

    @server.tool(annotations=read_only, structured_output=True)
    async def get_capabilities(ctx: Context[ResearchEngine]) -> Capabilities:
        """List supported foundation operations and whether analysis is available."""
        return await ctx.request_context.lifespan_context.get_capabilities()

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    async def register_company(
        name: str,
        ctx: Context[ResearchEngine],
        listings: list[ExchangeListing] | None = None,
        provider_identifiers: list[ProviderIdentifier] | None = None,
    ) -> Company:
        """Register a company and its supplied exchange and provider identifiers."""
        try:
            registration = CompanyRegistration(
                name=name,
                listings=tuple(listings or ()),
                provider_identifiers=tuple(provider_identifiers or ()),
            )
        except ValidationError as exc:
            raise ToolError(f"Invalid company registration: {exc}") from exc
        return await ctx.request_context.lifespan_context.register_company(registration)

    @server.tool(annotations=read_only, structured_output=True)
    async def get_company(company_id: UUID, ctx: Context[ResearchEngine]) -> Company:
        """Read a registered company using its internal company UUID."""
        company = await ctx.request_context.lifespan_context.get_company(company_id)
        if company is None:
            raise ToolError("Company not found")
        return company

    return server
