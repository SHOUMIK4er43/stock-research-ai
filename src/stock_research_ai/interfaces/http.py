"""HTTP routes exposing the transport-independent research engine."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, status

from stock_research_ai.application.engine import Capabilities, ResearchEngine
from stock_research_ai.bootstrap import research_engine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company, CompanyRegistration


def create_app(
    engine: ResearchEngine | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create an HTTP adapter, owning resources only when no engine is supplied."""
    active_engine = engine

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal active_engine
        if engine is not None:
            yield
            return
        runtime_settings = settings if settings is not None else Settings()
        async with research_engine(runtime_settings) as owned_engine:
            active_engine = owned_engine
            try:
                yield
            finally:
                active_engine = None

    app = FastAPI(
        title="Stock Research AI",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def current_engine() -> ResearchEngine:
        if active_engine is None:
            raise RuntimeError("The application lifespan has not started")
        return active_engine

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def get_capabilities() -> Capabilities:
        return await current_engine().get_capabilities()

    @app.post("/v1/companies", response_model=Company, status_code=status.HTTP_201_CREATED)
    async def register_company(registration: CompanyRegistration) -> Company:
        return await current_engine().register_company(registration)

    @app.get("/v1/companies/{company_id}", response_model=Company)
    async def get_company(company_id: UUID) -> Company:
        company = await current_engine().get_company(company_id)
        if company is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
        return company

    return app
