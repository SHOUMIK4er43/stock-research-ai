"""Central application operations, independent of transport and persistence technology."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.ports import CompanyRepository
from stock_research_ai.domain.company import Company, CompanyRegistration


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operations: tuple[
        Literal["get_capabilities"], Literal["register_company"], Literal["get_company"]
    ] = ("get_capabilities", "register_company", "get_company")
    analysis_available: Literal[False] = False


class ResearchEngine:
    def __init__(self, repository: CompanyRepository) -> None:
        self._repository = repository

    async def get_capabilities(self) -> Capabilities:
        return Capabilities()

    async def register_company(self, registration: CompanyRegistration) -> Company:
        company = Company(
            id=uuid4(),
            name=registration.name,
            listings=registration.listings,
            provider_identifiers=registration.provider_identifiers,
        )
        await self._repository.save(company)
        return company

    async def get_company(self, company_id: UUID) -> Company | None:
        return await self._repository.get(company_id)
