import ast
import sys
from importlib.util import resolve_name
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.domain.company import Company, CompanyRegistration, ExchangeListing


class MemoryRepository:
    def __init__(self) -> None:
        self.companies: dict[UUID, Company] = {}
        self.saved: list[Company] = []
        self.requested: list[UUID] = []
        self.fail_save = False

    async def save(self, company: Company) -> None:
        if self.fail_save:
            raise RuntimeError("storage unavailable")
        self.companies[company.id] = company
        self.saved.append(company)

    async def get(self, company_id: UUID) -> Company | None:
        self.requested.append(company_id)
        return self.companies.get(company_id)


@pytest.mark.asyncio
async def test_capabilities_truthfully_describe_part_one() -> None:
    repository = MemoryRepository()
    capabilities = await ResearchEngine(repository).get_capabilities()
    assert capabilities.operations == ("get_capabilities", "register_company", "get_company")
    assert capabilities.analysis_available is False
    assert repository.saved == []
    assert repository.requested == []


@pytest.mark.asyncio
async def test_registration_allocates_identity_and_persists_company() -> None:
    repository = MemoryRepository()
    engine = ResearchEngine(repository)
    registration = CompanyRegistration(
        name="Example Ltd", listings=(ExchangeListing(exchange="TEST", symbol="EXAMPLE"),)
    )
    company = await engine.register_company(registration)
    assert company.id.version == 4
    assert company.name == registration.name
    assert company.listings == registration.listings
    assert repository.saved == [company]
    assert await engine.get_company(company.id) == company
    assert repository.requested == [company.id]


@pytest.mark.asyncio
async def test_unknown_company_returns_none() -> None:
    repository = MemoryRepository()
    unknown_id = uuid4()
    assert await ResearchEngine(repository).get_company(unknown_id) is None
    assert repository.requested == [unknown_id]


@pytest.mark.asyncio
async def test_each_registration_gets_a_new_internal_identity() -> None:
    repository = MemoryRepository()
    engine = ResearchEngine(repository)
    registration = CompanyRegistration(name="Example Ltd")
    first = await engine.register_company(registration)
    second = await engine.register_company(registration)
    assert first.id != second.id
    assert len(repository.companies) == 2


@pytest.mark.asyncio
async def test_failed_persistence_does_not_return_a_successful_registration() -> None:
    repository = MemoryRepository()
    repository.fail_save = True
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await ResearchEngine(repository).register_company(CompanyRegistration(name="Example Ltd"))
    assert repository.companies == {}


def test_core_imports_only_domain_application_pydantic_and_standard_library() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "stock_research_ai"
    for layer in ("domain", "application"):
        for source in sorted((package_root / layer).glob("*.py")):
            module_package = f"stock_research_ai.{layer}"
            for node in ast.walk(ast.parse(source.read_text())):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = resolve_name("." * node.level + module, module_package)
                    modules = [module]
                for module in modules:
                    root = module.split(".")[0]
                    if root in sys.stdlib_module_names or root == "pydantic":
                        continue
                    assert any(
                        module == allowed or module.startswith(allowed + ".")
                        for allowed in (
                            "stock_research_ai.domain",
                            "stock_research_ai.application",
                        )
                    ), f"Forbidden core dependency in {source.name}: {module}"


async def test_extraction_requires_complete_configured_services():
    from stock_research_ai.domain.extraction import ExtractionRequest

    with pytest.raises(RuntimeError, match="not configured"):
        await ResearchEngine(MemoryRepository()).extract_company(
            ExtractionRequest(provider="neutral", url="https://example.com/")
        )


async def test_engine_preserves_then_reads_evidence_before_normalizing(yesbank_result):
    from stock_research_ai.application.engine import ExtractionServices
    from stock_research_ai.domain.extraction import ExtractionRequest
    from stock_research_ai.domain.provenance import AcquiredPayload

    events = []
    result = yesbank_result
    artifact = result.raw_artifacts[0]
    retrieval = result.retrievals[0]

    class Source:
        async def acquire(self, request):
            events.append("acquire")
            return AcquiredPayload(retrieval=retrieval, content=b"exact acquisition")

    class Store:
        async def put(self, payload):
            assert payload.content == b"exact acquisition"
            events.append("preserve")
            return artifact

        async def read(self, value):
            assert value == artifact
            events.append("verified read")
            return b"verified evidence"

    class Normalizer:
        def identify(self, content, raw, event, company_id):
            assert content == b"verified evidence" and raw == artifact
            events.append("identify")
            return result.company

        def normalize(self, content, raw, event, company_id):
            assert content == b"verified evidence" and company_id == result.company.company_id
            events.append("normalize")
            return result

    class Repository:
        async def record_evidence(self, event, raw):
            events.append("record evidence")

        async def bind_company(self, identity):
            events.append("bind")
            return identity.company_id

        async def save_extraction(self, snapshot):
            assert snapshot == result
            events.append("persist snapshot")

    engine = ResearchEngine(
        MemoryRepository(), ExtractionServices(Source(), Store(), Normalizer(), Repository())
    )
    assert (
        await engine.extract_company(
            ExtractionRequest(provider="neutral", url="https://example.com/")
        )
        == result
    )
    assert events == [
        "acquire",
        "preserve",
        "record evidence",
        "verified read",
        "identify",
        "bind",
        "normalize",
        "persist snapshot",
    ]
