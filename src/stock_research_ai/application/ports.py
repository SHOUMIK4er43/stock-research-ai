"""Application-owned persistence interface; no storage implementation details."""

from typing import Protocol
from uuid import UUID

from stock_research_ai.domain.company import Company


class CompanyRepository(Protocol):
    async def save(self, company: Company) -> None:
        """Persist a company."""
        ...

    async def get(self, company_id: UUID) -> Company | None:
        """Return a company, or None when the ID is unknown."""
        ...
