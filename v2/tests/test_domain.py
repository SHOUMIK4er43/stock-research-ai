from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.domain.company import (
    Company,
    CompanyRegistration,
    ExchangeListing,
    ProviderIdentifier,
)
from stock_research_ai.domain.financial import (
    DerivedMetric,
    FinancialObservation,
    FinancialPeriod,
    FinancialUnit,
    PeriodKind,
    ReportingScope,
    UnitKind,
    ValueStatus,
)
from stock_research_ai.domain.provenance import (
    CalculationProvenance,
    ObservationProvenance,
    RawArtifact,
    SourceLocation,
)


@pytest.fixture
def observation_payload() -> dict[str, object]:
    return {
        "id": uuid4(),
        "company_id": uuid4(),
        "metric": "revenue",
        "value": "12345678901234567890.1200",
        "value_status": "available",
        "unit": {"kind": "money", "currency": "INR"},
        "period": {
            "kind": "duration",
            "fiscal_label": "FY2025-26",
            "start_date": "2025-04-01",
            "end_date": "2026-03-31",
        },
        "reporting_scope": "consolidated",
        "provenance": {
            "raw_artifact_id": uuid4(),
            "source_location": {"section": "Income statement", "locator": "table=1,row=2,col=3"},
            "normalizer_version": "1",
        },
        "original_value": "12345678901234567890.1200",
        "original_unit": {"kind": "money", "currency": "INR"},
    }


def test_company_separates_identity_listings_and_provider_identifiers() -> None:
    company = Company(
        id=uuid4(),
        name=" Example Ltd ",
        listings=(ExchangeListing(symbol=" EXAMPLE "),),
        provider_identifiers=(ProviderIdentifier(provider="example", identifier="123"),),
    )
    assert company.name == "Example Ltd"
    assert company.listings[0].symbol == "EXAMPLE"
    assert company.listings[0].exchange is None
    assert company.provider_identifiers[0].identifier == "123"
    assert "reporting_scope" not in Company.model_fields
    assert Company.model_validate_json(company.model_dump_json()) == company


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "  "},
        {"name": "Example", "listings": [{"symbol": " "}]},
        {"name": "Example", "listings": [{"symbol": "EXAMPLE", "exchange": " "}]},
        {"name": "Example", "provider_identifiers": [{"provider": " ", "identifier": "x"}]},
        {"name": "Example", "provider_identifiers": [{"provider": "test", "identifier": " "}]},
        {"name": "Example", "reporting_scope": "consolidated"},
    ],
)
def test_invalid_company_registration_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CompanyRegistration.model_validate(payload)


def test_company_and_nested_identity_are_immutable() -> None:
    company = Company(name="Example", id=uuid4(), listings=(ExchangeListing(symbol="EXAMPLE"),))
    with pytest.raises(ValidationError, match="frozen"):
        company.name = "Changed"
    with pytest.raises(ValidationError, match="frozen"):
        company.listings[0].symbol = "CHANGED"


def test_financial_decimals_round_trip_without_float_loss(
    observation_payload: dict[str, object],
) -> None:
    observation = FinancialObservation.model_validate(observation_payload)
    restored = FinancialObservation.model_validate_json(observation.model_dump_json())
    assert restored == observation
    assert restored.value == Decimal("12345678901234567890.1200")
    assert restored.value.as_tuple().exponent == -4
    assert restored.reporting_scope == ReportingScope.CONSOLIDATED
    assert restored.layer == "NORMALIZED"
    assert restored.period.start_date == date(2025, 4, 1)


@pytest.mark.parametrize("value", [1.2, True, Decimal("NaN"), Decimal("Infinity"), "-Infinity"])
def test_inexact_or_nonfinite_financial_values_are_rejected(
    observation_payload: dict[str, object], value: object
) -> None:
    with pytest.raises(ValidationError):
        FinancialObservation.model_validate({**observation_payload, "value": value})


def test_missing_is_distinct_from_zero(observation_payload: dict[str, object]) -> None:
    zero = FinancialObservation.model_validate({**observation_payload, "value": "0"})
    missing = FinancialObservation.model_validate(
        {**observation_payload, "value": None, "value_status": "missing", "original_value": "--"}
    )
    assert zero.value == Decimal("0")
    assert zero.value_status == ValueStatus.AVAILABLE
    assert missing.value is None
    assert missing.value_status == ValueStatus.MISSING


@pytest.mark.parametrize("field", ["original_value", "original_unit"])
def test_available_observations_require_original_representation(
    observation_payload: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError, match="original value and unit"):
        FinancialObservation.model_validate({**observation_payload, field: None})


def test_original_value_preserves_source_whitespace(
    observation_payload: dict[str, object],
) -> None:
    original = "  12,345.00\n"
    observation = FinancialObservation.model_validate(
        {**observation_payload, "original_value": original}
    )
    assert observation.original_value == original
    assert (
        FinancialObservation.model_validate_json(observation.model_dump_json()).original_value
        == original
    )


@pytest.mark.parametrize("value,status", [(None, "available"), ("0", "missing")])
def test_value_and_missing_status_must_agree(
    observation_payload: dict[str, object], value: object, status: str
) -> None:
    with pytest.raises(ValidationError):
        FinancialObservation.model_validate(
            {**observation_payload, "value": value, "value_status": status}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "duration", "end_date": "2026-03-31"},
        {"kind": "duration", "start_date": "2026-04-01", "end_date": "2026-03-31"},
        {"kind": "instant", "start_date": "2026-03-31", "end_date": "2026-03-31"},
    ],
)
def test_period_contract_rejects_ambiguous_dates(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FinancialPeriod.model_validate({"fiscal_label": "FY2025-26", **payload})


def test_instant_period_preserves_actual_date_and_fiscal_label() -> None:
    period = FinancialPeriod(
        kind=PeriodKind.INSTANT, fiscal_label="FY2025-26", end_date=date(2026, 3, 31)
    )
    assert period.start_date is None
    assert period.end_date == date(2026, 3, 31)
    assert period.fiscal_label == "FY2025-26"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "money"},
        {"kind": "money", "currency": "inr"},
        {"kind": "ratio", "currency": "INR"},
        {"kind": "number", "scale": "0"},
        {"kind": "number", "scale": "-1"},
        {"kind": "number", "scale": 0.1},
    ],
)
def test_units_require_consistent_currency_and_exact_positive_scale(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        FinancialUnit.model_validate(payload)


def test_money_scale_preserves_reported_unit() -> None:
    unit = FinancialUnit(kind=UnitKind.MONEY, currency="INR", scale=Decimal("10000000"))
    assert unit.scale == Decimal("10000000")
    assert unit.currency == "INR"


def test_records_link_derivation_to_raw_source(observation_payload: dict[str, object]) -> None:
    raw = RawArtifact.model_validate(
        {
            "id": uuid4(),
            "source_url": "https://example.com/report",
            "source_type": "annual_report",
            "retrieved_at": datetime(2026, 9, 1, tzinfo=UTC),
            "content_reference": "artifacts/example-report",
            "content_sha256": "a" * 64,
            "collector_version": "1",
        }
    )
    observation = FinancialObservation.model_validate(
        {
            **observation_payload,
            "provenance": ObservationProvenance(
                raw_artifact_id=raw.id,
                source_location=SourceLocation(locator="page=42,table=2,row=revenue,col=FY2025-26"),
                normalizer_version="1",
            ),
        }
    )
    result = DerivedMetric(
        id=uuid4(),
        company_id=observation.company_id,
        metric="example_result",
        value=Decimal("100"),
        unit=FinancialUnit(kind=UnitKind.RATIO),
        period=observation.period,
        reporting_scope=observation.reporting_scope,
        provenance=CalculationProvenance(
            input_ids=(observation.id,),
            formula="example_formula(input)",
            calculation_version="1",
            calculated_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
    )
    assert result.provenance.input_ids == (observation.id,)
    assert observation.provenance.raw_artifact_id == raw.id
    assert str(raw.source_url) == "https://example.com/report"
    assert raw.layer == "RAW"
    assert result.layer == "DERIVED"
    assert DerivedMetric.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError, match="frozen"):
        result.provenance.calculation_version = "2"


def test_normalized_data_requires_evidence(observation_payload: dict[str, object]) -> None:
    observation_payload.pop("provenance")
    with pytest.raises(ValidationError):
        FinancialObservation.model_validate(observation_payload)
    with pytest.raises(ValidationError):
        SourceLocation(locator=" ")


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_sha256": "not-a-hash"},
        {"retrieved_at": "2026-09-01T00:00:00"},
        {"source_url": "not-a-url"},
    ],
)
def test_raw_artifacts_require_valid_source_and_retrieval_metadata(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RawArtifact.model_validate(
            {
                "id": uuid4(),
                "source_url": "https://example.com/report",
                "source_type": "annual_report",
                "retrieved_at": "2026-09-01T00:00:00Z",
                "content_reference": "artifacts/report",
                "content_sha256": "a" * 64,
                "collector_version": "1",
                **overrides,
            }
        )


def test_calculation_lineage_requires_inputs() -> None:
    with pytest.raises(ValidationError):
        CalculationProvenance(
            input_ids=(),
            formula="example_formula(input)",
            calculation_version="1",
            calculated_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_snapshot_rejects_broken_provenance_and_company_identity(yesbank_result):
    from stock_research_ai.domain.extraction import CompanyExtractionResult

    payload = yesbank_result.model_dump(mode="json")
    payload["quarterly"][0]["provenance"]["raw_artifact_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="snapshot evidence"):
        CompanyExtractionResult.model_validate(payload)
    payload = yesbank_result.model_dump(mode="json")
    payload["quarterly"][0]["company_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="snapshot company"):
        CompanyExtractionResult.model_validate(payload)
