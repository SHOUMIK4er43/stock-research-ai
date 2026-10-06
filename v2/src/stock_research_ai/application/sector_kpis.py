"""Sector eligibility and exact semantic gates; arithmetic dispatches to Part 4A."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Literal
from uuid import uuid4

from stock_research_ai.application import calculations as financial
from stock_research_ai.application.hashing import digest
from stock_research_ai.application.ports import SemanticResolver
from stock_research_ai.domain.calculations import (
    CalculationIssue,
    CalculationRequest,
    CalculationSpec,
    SectorDeclaration,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.financial import FinancialObservation
from stock_research_ai.domain.sector_kpis import (
    KPIDefinition,
    SectorKPIEvaluation,
    SectorKPIRequest,
    SectorKPIResult,
    SectorProfile,
)

BANK = "BANK_PROFILE"
IT = "IT_SERVICES_PROFILE"


def definition(
    metric: str,
    profiles: tuple[str, ...],
    semantics: tuple[str, ...],
    formula: str | None,
    source_metric: str = "net_profit",
    generic: bool = False,
) -> KPIDefinition:
    return KPIDefinition.model_validate(
        dict(
            metric_id=metric,
            applicable_profiles=profiles,
            required_semantics=semantics,
            formula=financial.FORMULAS[formula] if formula else None,
            source_metric=source_metric,
            category="GENERIC_SOURCE_METRIC" if generic else "SECTOR_KPI",
            unit="BASIS_POINTS" if metric == "OPERATING_MARGIN_CHANGE_BPS" else "PERCENT",
            period_policy=(
                "DECLARED_FISCAL_PERIOD_KEYS/1; average-assets uses current annual "
                "profit and prior/current fiscal-end assets"
            ),
            limitations=("SOURCE_REPORTED_MEANING_ONLY",) if generic else (),
        )
    )


DEFINITIONS = (
    definition("DEPOSIT_GROWTH", (BANK,), ("DEPOSITS",), "SOURCE_METRIC_GROWTH", "deposits"),
    definition(
        "AVERAGE_ASSETS_RETURN", (BANK,), ("NET_PROFIT", "TOTAL_ASSETS"), "AVERAGE_ASSETS_RETURN"
    ),
    definition(
        "SOURCE_REVENUE_GROWTH",
        (BANK,),
        ("SOURCE_REVENUE",),
        "SOURCE_METRIC_GROWTH",
        "revenue",
        True,
    ),
    definition("SALES_GROWTH", (IT,), ("SALES",), "SOURCE_METRIC_GROWTH", "sales"),
    definition("NET_PROFIT_GROWTH", (IT,), ("NET_PROFIT",), "SOURCE_METRIC_GROWTH"),
    definition("EPS_GROWTH", (IT,), ("EPS",), "SOURCE_METRIC_GROWTH", "eps"),
    definition("OPERATING_MARGIN", (IT,), ("OPERATING_PROFIT", "SALES"), "OPERATING_MARGIN"),
    definition(
        "OPERATING_MARGIN_CHANGE_BPS",
        (IT,),
        ("OPERATING_PROFIT", "SALES"),
        "OPERATING_MARGIN_CHANGE_BPS",
    ),
    *(
        definition(k, (BANK,), (k,), None)
        for k in (
            "NIM",
            "CASA_RATIO",
            "PCR",
            "CREDIT_COST",
            "CRAR",
            "CET1",
            "BANK_ROA",
            "BANK_ROE",
            "ADVANCES_GROWTH",
            "CREDIT_DEPOSIT_RATIO",
            "GNPA",
            "NNPA",
        )
    ),
)


def classification(value: str) -> Literal["BANK", "NON_FINANCIAL"]:
    return "BANK" if value == "BANK" else "NON_FINANCIAL"


PROFILES = tuple(
    SectorProfile(
        profile_id=p,
        version=1,
        classification=classification(c),
        description=description,
        supported_kpis=tuple(
            d.metric_id
            for d in DEFINITIONS
            if p in d.applicable_profiles and d.category == "SECTOR_KPI"
        ),
    )
    for p, c, description in (
        (BANK, "BANK", "Bank evidence eligibility; no inferred banking ratios"),
        (IT, "NON_FINANCIAL", "Explicit IT-services profile; no inferred sector membership"),
    )
)


class GateFailure(Exception):
    def __init__(self, status: str, code: str) -> None:
        self.status, self.code = status, code


def evaluate_kpis(
    source: CompanyExtractionResult,
    request: SectorKPIRequest,
    resolver: SemanticResolver,
    profiles: tuple[SectorProfile, ...] = PROFILES,
    definitions: tuple[KPIDefinition, ...] = DEFINITIONS,
) -> SectorKPIEvaluation:
    if (
        not source.company
        or source.company.company_id != request.company_id
        or source.metadata.id != request.extraction_id
    ):
        raise ValueError("KPI request must pin the exact company/extraction")
    matches = [
        p
        for p in profiles
        if (p.profile_id, p.version) == (request.profile_id, request.profile_version)
    ]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous profile version")
    profile = matches[0]
    if (
        request.classification.provenance
        and request.classification.provenance.raw_artifact_id
        not in {a.id for a in source.raw_artifacts}
    ):
        raise ValueError("Classification provenance must belong to source extraction")
    now, snapshot_id = datetime.now(UTC), uuid4()
    results = []
    for metric in request.kpis:
        found = [d for d in definitions if d.metric_id == metric]
        if len(found) != 1:
            raise ValueError("Unknown or ambiguous KPI definition")
        kpi = found[0]
        mappings = []
        used: list[FinancialObservation] = []
        issues: list[CalculationIssue] = []
        data: dict[str, Any] = dict(
            snapshot_id=snapshot_id,
            extraction_id=request.extraction_id,
            definition=kpi,
            profile=profile,
            sector=request.classification,
            alignment=request.alignment,
            period=request.current_period,
            comparison=request.comparison,
            reporting_scope=request.reporting_scope,
            calculated_at=now,
            status="AVAILABLE",
        )
        try:
            issue = profile_issue(request.classification.classification, profile)
            if issue:
                raise GateFailure("NOT_APPLICABLE", issue)
            if profile.profile_id not in kpi.applicable_profiles or (
                kpi.category == "SECTOR_KPI" and metric not in profile.supported_kpis
            ):
                raise GateFailure("NOT_APPLICABLE", "KPI_NOT_APPLICABLE_TO_PROFILE")
            resolved = resolver.resolve(source, kpi.required_semantics)
            if kpi.formula is None:
                mappings = list(resolved)
                if any(m.status == "UNVERIFIED" for m in resolved):
                    raise GateFailure(
                        "UNVERIFIED_METRIC_SEMANTICS", "CANDIDATE_WITHOUT_APPROVED_SEMANTICS"
                    )
                raise GateFailure(
                    "INSUFFICIENT_INPUT", "REQUIRED_SEMANTICS_OR_FORMULA_CONVENTION_NOT_ESTABLISHED"
                )
            month = int(request.current_period[5:])
            fiscal_end = request.alignment.fiscal_year_end_month
            annual = request.comparison == "ANNUAL_YOY"
            if (annual and month != fiscal_end) or (not annual and (month - fiscal_end) % 3):
                raise GateFailure("INCOMPATIBLE_PERIOD", "PERIOD_OUTSIDE_DECLARED_FISCAL_ALIGNMENT")
            if not annual and metric in {"DEPOSIT_GROWTH", "AVERAGE_ASSETS_RETURN"}:
                raise GateFailure("INCOMPATIBLE_PERIOD", "KPI_REQUIRES_ANNUAL_FISCAL_END_ALIGNMENT")
            section = "profit_loss" if annual else "quarterly"
            prior = financial.previous(
                request.current_period, 3 if request.comparison == "QUARTERLY_SEQUENTIAL" else 12
            )
            roles = []
            if kpi.formula.id == "SOURCE_METRIC_GROWTH":
                sec = "balance_sheet" if metric == "DEPOSIT_GROWTH" else section
                roles = [
                    (kpi.required_semantics[0], sec, key) for key in (request.current_period, prior)
                ]
            elif kpi.formula.id == "AVERAGE_ASSETS_RETURN":
                roles = [
                    ("NET_PROFIT", "profit_loss", request.current_period),
                    ("TOTAL_ASSETS", "balance_sheet", prior),
                    ("TOTAL_ASSETS", "balance_sheet", request.current_period),
                ]
            else:
                roles = [
                    (semantic, section, key)
                    for key in (
                        (request.current_period, prior)
                        if metric == "OPERATING_MARGIN_CHANGE_BPS"
                        else (request.current_period,)
                    )
                    for semantic in kpi.required_semantics
                ]
            observations = {
                o.id: o
                for field in ("profit_loss", "quarterly", "balance_sheet")
                for o in getattr(source, field)
            }
            for semantic, sec, key in roles:
                candidates = [
                    m
                    for m in resolved
                    if m.canonical_semantic == semantic
                    and m.section == sec
                    and financial.period_key(observations[m.observation_id]) == key
                ]
                mappings.extend(candidates)
                if len(candidates) > 1 or any(m.status != "VERIFIED" for m in candidates):
                    raise GateFailure(
                        "UNVERIFIED_METRIC_SEMANTICS", "AMBIGUOUS_OR_UNVERIFIED_SEMANTIC_MAPPING"
                    )
                if not candidates:
                    raise GateFailure("INSUFFICIENT_INPUT", "REQUIRED_SEMANTIC_PERIOD_MISSING")
            if (
                source.company.reporting_scope != request.reporting_scope
                or request.reporting_scope == "unknown"
            ):
                raise GateFailure("INCOMPATIBLE_SCOPE", "EXTRACTION_SCOPE_MISMATCH")
            current_end = next(
                (
                    observations[m.observation_id].period.end_date
                    for m in mappings
                    if financial.period_key(observations[m.observation_id])
                    == request.current_period
                ),
                None,
            )
            start, end = (
                request.classification.effective_start,
                request.classification.effective_end,
            )
            if (start or end) and current_end is None:
                raise GateFailure(
                    "INCOMPATIBLE_PERIOD", "CLASSIFICATION_EFFECTIVE_DATE_CANNOT_BE_VERIFIED"
                )
            if current_end and ((start and current_end < start) or (end and current_end > end)):
                raise GateFailure("NOT_APPLICABLE", "CLASSIFICATION_OUTSIDE_EFFECTIVE_PERIOD")
            spec = CalculationSpec.model_validate(
                dict(
                    metric_id=metric,
                    formula_id=kpi.formula.id,
                    source_metric=kpi.source_metric,
                    current_period=request.current_period,
                    comparison=request.comparison,
                )
            )
            common_request = CalculationRequest(
                company_id=request.company_id,
                extraction_id=request.extraction_id,
                reporting_scope=request.reporting_scope,
                sector=SectorDeclaration.model_validate(
                    request.classification.model_dump(
                        exclude={"version", "effective_start", "effective_end"}
                    )
                ),
                alignment=request.alignment,
                calculations=(spec,),
            )
            if financial.FORMULAS[kpi.formula.id] != kpi.formula:
                raise GateFailure("INVALID_INPUT", "FORMULA_VERSION_OR_DEFINITION_NOT_IMPLEMENTED")
            formula_issues: list[str] = []
            ratio = financial.evaluate(source, common_request, spec, used, formula_issues)
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                value = Decimal(ratio.numerator) / Decimal(ratio.denominator)
                display = value.quantize(Decimal("0.01"))
            data.update(
                value=value,
                display_value=display,
                exact_numerator=ratio.numerator,
                exact_denominator=ratio.denominator,
            )
            issues.extend(
                CalculationIssue(code=code, severity="LIMITATION", message=code)
                for code in (
                    *formula_issues,
                    *kpi.limitations,
                    "PINNED_EXTRACTION_NOT_LATEST_ASSERTION",
                )
            )
        except financial.InputFailure as exc:
            status = "INVALID_INPUT" if exc.status == "DIVISION_BY_ZERO" else exc.status
            if "SCOPE" in exc.issue:
                status = "INCOMPATIBLE_SCOPE"
            elif (
                any(key in exc.issue for key in ("PERIOD", "DURATION", "ALIGNMENT"))
                and status != "INSUFFICIENT_INPUT"
            ):
                status = "INCOMPATIBLE_PERIOD"
            data["status"] = status
            issues.append(CalculationIssue(code=exc.issue, severity="ERROR", message=exc.issue))
        except GateFailure as exc:
            data["status"] = exc.status
            issues.append(CalculationIssue(code=exc.code, severity="ERROR", message=exc.code))
        except ArithmeticError:
            data["status"] = "INVALID_INPUT"
            issues.append(
                CalculationIssue(
                    code="DECIMAL_RANGE_ERROR", severity="ERROR", message="Numeric context exceeded"
                )
            )
        results.append(
            SectorKPIResult.model_validate(
                {
                    **data,
                    "inputs": tuple(used),
                    "mappings": tuple(mappings),
                    "issues": tuple(issues),
                }
            )
        )
    return SectorKPIEvaluation(
        id=snapshot_id,
        request=request,
        profile=profile,
        source_payload_sha256=digest(source),
        calculated_at=now,
        results=tuple(results),
    )


def profile_issue(classification: str, profile: SectorProfile | None) -> str | None:
    """Shared classification eligibility, with no provider or company-name inference."""
    if classification == "UNKNOWN" or profile is None:
        return "UNKNOWN_SECTOR_PROFILE"
    if classification != profile.classification:
        return "PROFILE_CLASSIFICATION_MISMATCH"
    return None
