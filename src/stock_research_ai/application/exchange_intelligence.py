"""Box 12: decode exchange payloads and resolve one company-market identity.

Pure functions over RawPayload bytes: no network, no clock, no AI. Every payload decodes into
bounded rows or into an explicit issue (blocked, rate limited, unavailable, invalid); a failure
never becomes an empty success and never a zero. Rows that belong to another security (a
market-wide feed, a mismatched ISIN) are dropped with an issue, never silently kept.

Provider identity gating: a feed row is attributed to the company only through a VERIFIED
identifier (one a provider payload stated, see CompanyIdentity) - the row's own NSE symbol,
ISIN or BSE code equal to the verified value, or, for a row that states no identifier at all,
a company-scoped payload whose query key is the verified identifier of that exchange. A row
whose only key is a caller-declared, unverified identifier is never ingested: it is counted
under PROVIDER_IDENTITY_UNVERIFIED instead.
"""

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid5

from stock_research_ai.domain.market_intelligence import (
    ALLOWED_SOURCE_HOSTS,
    IST,
    MAX_PRICE,
    MAX_SOURCE_RECORDS,
    PAYLOAD_AUTHORITY,
    PAYLOAD_PROVIDER,
    Authority,
    CompanyIdentity,
    CorporateActionKind,
    EventSourceRef,
    FetchStatus,
    FilingCategory,
    FilingDocument,
    FinancialBasis,
    IdentityEvidence,
    IdentityField,
    IdentityStatus,
    InsiderTrade,
    Issue,
    MarketIntelligenceRequest,
    PayloadKind,
    Provider,
    RawPayload,
    ShareholdingFiling,
    SourcePayloadRecord,
)

NS = UUID("b5bc90b9-36ef-4b09-aa01-e475960add48")
MAX_FIELD_CHARS = 8_000
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

FAILURE_CODES = {
    FetchStatus.AUTH_REQUIRED: "SOURCE_AUTH_REQUIRED",
    FetchStatus.BLOCKED: "SOURCE_BLOCKED",
    FetchStatus.RATE_LIMITED: "SOURCE_RATE_LIMITED",
    FetchStatus.NOT_FOUND: "SOURCE_UNAVAILABLE",
    FetchStatus.UNSUPPORTED: "SOURCE_UNSUPPORTED",
    FetchStatus.NETWORK_ERROR: "SOURCE_NETWORK_ERROR",
    FetchStatus.TIMEOUT: "SOURCE_TIMEOUT",
    FetchStatus.TOO_LARGE: "SOURCE_TOO_LARGE",
    FetchStatus.HTTP_ERROR: "SOURCE_HTTP_ERROR",
    FetchStatus.INVALID_REQUEST: "SOURCE_INVALID_REQUEST",
}


def uid(*parts: object) -> UUID:
    return uuid5(NS, "|".join(str(p) for p in parts))


def payload_sha256(payload: RawPayload) -> str | None:
    return hashlib.sha256(payload.content).hexdigest() if payload.content is not None else None


def payload_id(payload: RawPayload) -> UUID:
    """Content identity: the retrieval time is deliberately excluded."""
    coverage_attestation = (
        payload.coverage_window_start,
        payload.coverage_window_end,
        payload.coverage_window_complete,
        payload.coverage_window_gaps,
    )
    return uid(
        "payload", payload.kind.value, payload.url, payload.status.value,
        payload_sha256(payload) or "-", payload.error_code or "-",
        *(coverage_attestation if any(coverage_attestation) else ()),
    )


def payload_record(
    payload: RawPayload, decoded: int, *, processing_succeeded: bool = True
) -> SourcePayloadRecord:
    return SourcePayloadRecord(
        payload_id=payload_id(payload),
        kind=payload.kind,
        provider=PAYLOAD_PROVIDER[payload.kind],
        authority=PAYLOAD_AUTHORITY[payload.kind],
        url=payload.url,
        status=payload.status,
        http_status=payload.http_status,
        retrieved_at=payload.retrieved_at,
        content_sha256=payload_sha256(payload),
        size_bytes=len(payload.content or b""),
        error_code=payload.error_code,
        records_decoded=decoded,
        coverage_window_start=payload.coverage_window_start,
        coverage_window_end=payload.coverage_window_end,
        coverage_window_complete=payload.coverage_window_complete,
        coverage_window_gaps=payload.coverage_window_gaps,
        processing_succeeded=processing_succeeded,
    )


def issue(
    code: str,
    message: str,
    *,
    severity: str = "WARNING",
    payload: UUID | None = None,
    locator: str | None = None,
) -> Issue:
    return Issue.model_validate(
        {
            "code": code,
            "severity": severity,
            "message": message[:1000],
            "payload_id": payload,
            "locator": locator[:1000] if locator else None,
        }
    )


# --- decoding --------------------------------------------------------------------------------


@dataclass
class Budget:
    """Total decoded-row budget across all payloads of one build."""

    remaining: int = MAX_SOURCE_RECORDS
    exhausted: bool = False

    def take(self, rows: list[Any]) -> list[Any]:
        if len(rows) > self.remaining:
            self.exhausted = True
            rows = rows[: self.remaining]
        self.remaining -= len(rows)
        return rows


def failure_issue(payload: RawPayload, pid: UUID) -> Issue:
    return issue(
        FAILURE_CODES.get(payload.status, "SOURCE_FAILED"),
        f"{payload.kind.value} not available ({payload.error_code})",
        severity="ERROR",
        payload=pid,
    )


def decode_json(payload: RawPayload, pid: UUID, issues: list[Issue]) -> Any | None:
    if payload.status != FetchStatus.OK or payload.content is None:
        issues.append(failure_issue(payload, pid))
        return None
    try:
        return json.loads(payload.content.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeDecodeError, ValueError, RecursionError):
        issues.append(
            issue("INVALID_SOURCE_DATA", f"{payload.kind.value} is not valid JSON",
                  severity="ERROR", payload=pid)
        )
        return None


def json_rows(
    payload: RawPayload, pid: UUID, issues: list[Issue], budget: Budget, *keys: str
) -> list[tuple[str, dict[str, Any]]]:
    """Rows as (locator, dict). A dict body is unwrapped through `keys` (e.g. 'data', 'Table')."""
    body = decode_json(payload, pid, issues)
    if body is None:
        return []
    path = "$"
    for key in keys:
        if isinstance(body, dict) and key in body:
            body, path = body[key], f"{path}.{key}"
            break
    if not isinstance(body, list):
        issues.append(
            issue("INVALID_SOURCE_DATA", f"{payload.kind.value} has an unrecognised shape",
                  severity="ERROR", payload=pid)
        )
        return []
    rows = [(f"{path}[{i}]", row) for i, row in enumerate(body)]
    bad = sum(1 for _, row in rows if not isinstance(row, dict))
    if bad:
        issues.append(
            issue("INVALID_SOURCE_ROWS", f"{bad} non-object rows ignored", payload=pid)
        )
    taken = budget.take([r for r in rows if isinstance(r[1], dict)])
    if budget.exhausted:
        issues.append(
            issue("SOURCE_RECORD_LIMIT", "Row budget reached; later rows not decoded",
                  severity="ERROR", payload=pid)
        )
    return taken


def csv_rows(
    payload: RawPayload, pid: UUID, issues: list[Issue], budget: Budget
) -> list[tuple[str, dict[str, str]]]:
    if payload.status != FetchStatus.OK or payload.content is None:
        issues.append(failure_issue(payload, pid))
        return []
    try:
        text = payload.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        out: list[tuple[str, dict[str, str]]] = []
        for i, row in enumerate(reader):
            if i >= budget.remaining:
                budget.exhausted = True
                issues.append(
                    issue("SOURCE_RECORD_LIMIT", "Row budget reached", severity="ERROR",
                          payload=pid)
                )
                break
            out.append(
                (
                    f"csv:data-row[{i + 1}]",
                    {
                        (k or "").strip().upper(): (v or "").strip()
                        for k, v in row.items()
                        if isinstance(v, str)
                    },
                )
            )
        budget.remaining -= len(out)
        return out
    except (UnicodeDecodeError, csv.Error, ValueError):
        issues.append(
            issue("INVALID_SOURCE_DATA", f"{payload.kind.value} is not valid CSV",
                  severity="ERROR", payload=pid)
        )
        return []


# --- scalar parsing ---------------------------------------------------------------------------

_DAY_FORMATS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%Y%m%d")
_MOMENT_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
)


def text(value: object, limit: int = MAX_FIELD_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or cleaned in {"-", "NA", "N.A.", "null", "None"}:
        return None
    return cleaned[:limit]


def _valid_year(day: date) -> bool:
    return 1990 <= day.year <= 2100


def parse_day(value: object) -> date | None:
    raw = text(value, 40)
    if raw is None:
        return None
    for fmt in _DAY_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        return parsed if _valid_year(parsed) else None
    moment = parse_moment(raw)
    return moment.date() if moment else None


def parse_moment(value: object) -> datetime | None:
    raw = text(value, 40)
    if raw is None:
        return None
    raw = re.sub(r"\.\d{1,6}$", "", raw)  # BSE fractional seconds
    for fmt in _MOMENT_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=IST) if _valid_year(parsed.date()) else None
    return None


_NUMBER = re.compile(r"^-?\d{1,15}(\.\d{1,8})?$")


def parse_number(value: object, *, maximum: Decimal = MAX_PRICE * 10**6) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, Decimal):
        value = format(value, "f") if value.is_finite() else ""
    raw = text(value, 40)
    if raw is None:
        return None
    raw = raw.replace(",", "")
    if not _NUMBER.fullmatch(raw):
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return number if abs(number) <= maximum else None


def trusted_source_url(url: str) -> str:
    """HTTPS on an exact allow-listed exchange/provider host, default port, no credentials,
    no whitespace/control characters or backslashes. Allow-listed names only, so no IP
    literal, localhost or private name can pass. Raises ValueError with fixed text."""
    if len(url) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("Unsupported source URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("Unsupported source URL") from None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or (parts.hostname or "").lower() not in ALLOWED_SOURCE_HOSTS
    ):
        raise ValueError("Unsupported source URL")
    return url


def safe_url(value: object, issues: list[Issue], pid: UUID, locator: str) -> str | None:
    raw = text(value, 2048)
    if raw is None:
        return None
    try:
        return trusted_source_url(raw)
    except ValueError:
        issues.append(
            issue("UNTRUSTED_SOURCE_URL", "Source URL dropped (not an allow-listed public "
                  "HTTPS exchange URL)", payload=pid, locator=locator)
        )
        return None


def isin_valid(value: str) -> bool:
    """ISO 6166 check digit (Luhn over the letter-expanded body)."""
    if not re.fullmatch(r"IN[A-Z0-9]{9}[0-9]", value):
        return False
    digits = "".join(str(int(c, 36)) for c in value[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - total % 10) % 10 == int(value[-1])


def isin_issuer(value: str) -> str:
    """The issuer part of an Indian ISIN (IN + type + 4-char issuer code)."""
    return value[:7]


def name_key(value: str) -> str:
    folded = re.sub(r"[^a-z0-9 ]", " ", value.casefold())
    words = ["limited" if w == "ltd" else w for w in folded.split()]
    return " ".join(words)


def source_ref(
    payload: RawPayload,
    pid: UUID,
    locator: str,
    *,
    record_id: object = None,
    category: str | None = None,
    url: str | None = None,
    published: datetime | None = None,
) -> EventSourceRef:
    rid = text(record_id, 200) if isinstance(record_id, str) else (
        str(record_id) if isinstance(record_id, int) and not isinstance(record_id, bool) else None
    )
    return EventSourceRef(
        provider=PAYLOAD_PROVIDER[payload.kind],
        authority=PAYLOAD_AUTHORITY[payload.kind],
        payload_id=pid,
        record_id=rid,
        locator=locator,
        raw_category=category[:1000] if category else None,
        source_url=url,
        published_at=published,
    )


# --- identity -------------------------------------------------------------------------------


@dataclass
class IdentityInputs:
    nse_master: list[tuple[RawPayload, UUID]] = field(default_factory=list)
    kite_instruments: list[tuple[RawPayload, UUID]] = field(default_factory=list)
    bse_header: list[tuple[RawPayload, UUID]] = field(default_factory=list)


_BSE_CODE_KEYS = ("SecurityCode", "Scripcode", "ScripCode", "SCRIP_CD")
_BSE_ISIN_KEYS = ("ISIN", "ISINNo", "IsinNo", "ISIN_NUMBER")
_BSE_NAME_KEYS = ("CompanyName", "Cmpname", "LONG_NAME", "SLONGNAME")
_BSE_SYMBOL_KEYS = ("SecurityId", "ScripID", "scrip_id")


def _first(row: dict[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        if key in row:
            value = row[key]
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            found = text(value, 300)
            if found:
                return found
    return None


def resolve_identity(
    request: MarketIntelligenceRequest, inputs: IdentityInputs, budget: Budget
) -> CompanyIdentity:
    """Deterministic, fail-closed identity. Exact keys only: no fuzzy name matching."""
    issues: list[Issue] = []
    evidence: list[IdentityEvidence] = []
    observed: dict[IdentityField, set[str]] = {f: set() for f in IdentityField}
    isin_providers: set[Provider] = set()  # exchanges whose own payload stated the ISIN

    def note(
        fld: IdentityField, value: str, provider: Provider, pid: UUID | None, loc: str
    ) -> None:
        evidence.append(
            IdentityEvidence(field=fld, value=value, provider=provider, payload_id=pid, locator=loc)
        )
        if pid is not None:
            observed[fld].add(name_key(value) if fld == IdentityField.NAME else value)
            if fld == IdentityField.ISIN:
                isin_providers.add(provider)

    for fld, value in (
        (IdentityField.NSE_SYMBOL, request.nse_symbol),
        (IdentityField.BSE_CODE, request.bse_code),
        (IdentityField.ISIN, request.isin),
        (IdentityField.NAME, request.company_name),
    ):
        if value:
            note(fld, value, Provider.CALLER, None, "request")
    if request.isin and not isin_valid(request.isin):
        issues.append(issue("INVALID_ISIN_CHECK_DIGIT", "Declared ISIN fails its check digit",
                            severity="ERROR"))
        return _identity(request, IdentityStatus.FAILED, evidence, issues, {})

    names: dict[Provider, str] = {}
    bse_anchored = False  # a BSE header stated the code together with a valid ISIN
    # NSE equity master: exact symbol (or exact ISIN when no symbol was declared).
    for payload, pid in inputs.nse_master:
        rows = csv_rows(payload, pid, issues, budget)
        hits = [
            (loc, r)
            for loc, r in rows
            if (request.nse_symbol and r.get("SYMBOL") == request.nse_symbol)
            or (not request.nse_symbol and request.isin and r.get("ISIN NUMBER") == request.isin)
        ]
        if rows and not hits:
            issues.append(issue("NSE_MASTER_NO_MATCH", "No NSE listing matches the declared key",
                                payload=pid))
        if len({r.get("ISIN NUMBER") for _, r in hits}) > 1 or len(hits) > 1:
            issues.append(issue("AMBIGUOUS_NSE_LISTING", "More than one NSE listing matches",
                                severity="ERROR", payload=pid))
            observed[IdentityField.ISIN].update(str(r.get("ISIN NUMBER")) for _, r in hits)
            continue
        for loc, r in hits:
            isin = r.get("ISIN NUMBER", "")
            if isin_valid(isin):
                note(IdentityField.ISIN, isin, Provider.NSE, pid, loc)
            else:
                issues.append(issue("INVALID_ISIN_CHECK_DIGIT", "NSE master ISIN rejected",
                                    severity="ERROR", payload=pid, locator=loc))
            note(IdentityField.NSE_SYMBOL, r["SYMBOL"], Provider.NSE, pid, loc)
            if r.get("NAME OF COMPANY"):
                names[Provider.NSE] = r["NAME OF COMPANY"]
                note(IdentityField.NAME, r["NAME OF COMPANY"], Provider.NSE, pid, loc)

    # Kite instrument master: exact NSE equity row.
    for payload, pid in inputs.kite_instruments:
        rows = csv_rows(payload, pid, issues, budget)
        hits = [
            (loc, r)
            for loc, r in rows
            if request.nse_symbol
            and r.get("EXCHANGE") == "NSE"
            and r.get("TRADINGSYMBOL") == request.nse_symbol
            and r.get("SEGMENT") == "NSE"
            and r.get("INSTRUMENT_TYPE") == "EQ"
        ]
        if len(hits) > 1:
            issues.append(issue("AMBIGUOUS_KITE_INSTRUMENT", "More than one Kite instrument "
                                "matches", severity="ERROR", payload=pid))
            observed[IdentityField.KITE_TOKEN].update(
                r.get("INSTRUMENT_TOKEN", "") for _, r in hits
            )
            continue
        for loc, r in hits:
            token = r.get("INSTRUMENT_TOKEN", "")
            if re.fullmatch(r"[0-9]{1,12}", token):
                note(IdentityField.KITE_TOKEN, token, Provider.KITE, pid, loc)
                note(IdentityField.NSE_SYMBOL, r["TRADINGSYMBOL"], Provider.KITE, pid, loc)
            else:
                issues.append(issue("INVALID_KITE_TOKEN", "Kite token rejected", severity="ERROR",
                                    payload=pid, locator=loc))
        if rows and not hits:
            issues.append(issue("KITE_INSTRUMENT_NO_MATCH", "No Kite NSE equity matches",
                                payload=pid))

    # BSE scrip header.
    for payload, pid in inputs.bse_header:
        body = decode_json(payload, pid, issues)
        if body is None:
            continue
        row = body[0] if isinstance(body, list) and body and isinstance(body[0], dict) else body
        if not isinstance(row, dict):
            issues.append(issue("INVALID_SOURCE_DATA", "BSE header has an unrecognised shape",
                                severity="ERROR", payload=pid))
            continue
        code, bse_isin = _first(row, _BSE_CODE_KEYS), _first(row, _BSE_ISIN_KEYS)
        if code is None and bse_isin is None:
            issues.append(issue("INVALID_SOURCE_DATA", "BSE header names no code or ISIN",
                                severity="ERROR", payload=pid))
            continue
        isin_ok = bse_isin is not None and isin_valid(bse_isin)
        if code and re.fullmatch(r"[0-9]{6}", code):
            # Recorded for conflict detection either way; verifies the code only with an ISIN
            # (a header that just echoes the requested scripcode proves nothing).
            note(IdentityField.BSE_CODE, code, Provider.BSE, pid, "$")
            if isin_ok:
                bse_anchored = True
            else:
                issues.append(issue("BSE_HEADER_WITHOUT_ISIN", "The BSE header states the code "
                                    "but no valid ISIN; the BSE code is not verified",
                                    payload=pid))
        if bse_isin is not None and isin_ok:
            note(IdentityField.ISIN, bse_isin, Provider.BSE, pid, "$")
        name = _first(row, _BSE_NAME_KEYS)
        if name:
            names[Provider.BSE] = name
            note(IdentityField.NAME, name, Provider.BSE, pid, "$")

    declared = {
        IdentityField.NSE_SYMBOL: request.nse_symbol,
        IdentityField.BSE_CODE: request.bse_code,
        IdentityField.ISIN: request.isin,
    }
    conflict = False
    for fld in (IdentityField.ISIN, IdentityField.NSE_SYMBOL, IdentityField.BSE_CODE,
                IdentityField.KITE_TOKEN):
        stated = declared.get(fld)
        values = observed[fld] | ({stated} if stated else set())
        values.discard("")
        if len(values) > 1:
            conflict = True
            issues.append(issue("IDENTITY_CONFLICT", f"Sources disagree on {fld.value}",
                                severity="ERROR"))
    if len({name_key(n) for n in names.values()}) > 1:
        issues.append(issue("NAME_VARIANT", "Exchanges spell the company name differently; "
                            "identity rests on codes, not names", severity="INFO"))
    resolved = {fld: next(iter(v)) for fld, v in observed.items() if len(v) == 1}
    if IdentityField.BSE_CODE in resolved and not bse_anchored:
        del resolved[IdentityField.BSE_CODE]
    if (IdentityField.NSE_SYMBOL in resolved and IdentityField.BSE_CODE in resolved
            and Provider.NSE not in isin_providers):
        # Both listings were verified, but no NSE source states an ISIN that ties the NSE
        # symbol to the BSE header's security (e.g. the NSE master failed and Kite verified
        # the symbol): the BSE listing, and an ISIN only BSE stated, are not attributed.
        del resolved[IdentityField.BSE_CODE]
        if isin_providers == {Provider.BSE}:
            resolved.pop(IdentityField.ISIN, None)
        issues.append(issue("LISTINGS_NOT_CROSS_VERIFIED", "No NSE source states the ISIN, so "
                            "the BSE listing cannot be tied to the NSE symbol; BSE code not "
                            "verified"))
    if conflict:
        return _identity(request, IdentityStatus.AMBIGUOUS, evidence, issues, {})
    confirmed_listing = [
        fld for fld in (IdentityField.NSE_SYMBOL, IdentityField.BSE_CODE)
        if declared.get(fld) and fld in resolved
    ]
    wanted = [
        fld for fld in (IdentityField.NSE_SYMBOL, IdentityField.BSE_CODE) if declared.get(fld)
    ]
    if not resolved:
        issues.append(issue("IDENTITY_UNCONFIRMED", "No source confirmed any declared key",
                            severity="ERROR"))
        status = IdentityStatus.FAILED
    elif len(confirmed_listing) == len(wanted) and IdentityField.ISIN in resolved:
        status = IdentityStatus.RESOLVED
    else:
        for fld in wanted:
            if fld not in resolved:
                issues.append(issue("IDENTITY_FIELD_UNCONFIRMED",
                                    f"Declared {fld.value} not confirmed by any source; rows "
                                    "keyed only by it are not attributed"))
        if IdentityField.ISIN not in resolved:
            issues.append(issue("ISIN_UNCONFIRMED", "No source stated the ISIN"))
        status = IdentityStatus.PARTIAL
    names_by_preference = [names.get(Provider.NSE), names.get(Provider.BSE), request.company_name]
    return _identity(
        request, status, evidence, issues, resolved,
        name=next((n for n in names_by_preference if n), None),
    )


def _identity(
    request: MarketIntelligenceRequest,
    status: IdentityStatus,
    evidence: list[IdentityEvidence],
    issues: list[Issue],
    resolved: dict[IdentityField, str],
    *,
    name: str | None = None,
) -> CompanyIdentity:
    usable = status in (IdentityStatus.RESOLVED, IdentityStatus.PARTIAL)
    # Verified identifiers only: a declared value never stands in for a provider's.
    isin = resolved.get(IdentityField.ISIN) if usable else None
    nse = resolved.get(IdentityField.NSE_SYMBOL) if usable else None
    bse = resolved.get(IdentityField.BSE_CODE) if usable else None
    token = resolved.get(IdentityField.KITE_TOKEN) if usable else None
    evidence.sort(key=lambda e: (e.field.value, e.provider.value, str(e.payload_id), e.locator))
    return CompanyIdentity(
        identity_id=uid("identity", request.isin or "-", request.nse_symbol or "-",
                        request.bse_code or "-"),
        status=status,
        isin=isin,
        nse_symbol=nse,
        bse_code=bse,
        declared_isin=request.isin,
        declared_nse_symbol=request.nse_symbol,
        declared_bse_code=request.bse_code,
        name=name if usable else None,
        kite_instrument_token=token,
        kite_exchange="NSE" if token else None,
        evidence=tuple(evidence),
        issues=tuple(issues),
    )


Admission = Literal["MATCH", "MISMATCH", "UNVERIFIED", "UNSTATED"]


def _key(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return text(value, 40)


def row_identity(
    identity: CompanyIdentity, symbol: object = None, isin: object = None, code: object = None
) -> Admission:
    """MATCH: a key the row states equals a verified identifier and no stated key
    contradicts the identity (verified or declared). MISMATCH: some stated key contradicts.
    UNVERIFIED: the row's keys can only be compared with unverified declarations.
    UNSTATED: the row states no key."""
    matched = unverified = False
    for given, verified, declared in (
        (symbol, identity.nse_symbol, identity.declared_nse_symbol),
        (isin, identity.isin, identity.declared_isin),
        (code, identity.bse_code, identity.declared_bse_code),
    ):
        value = _key(given)
        if value is None:
            continue
        if verified is not None:
            if value != verified:
                return "MISMATCH"
            matched = True
        elif declared is not None and value != declared:
            return "MISMATCH"
        else:
            unverified = True
    return "MATCH" if matched else "UNVERIFIED" if unverified else "UNSTATED"


def scope_identity(identity: CompanyIdentity, payload: RawPayload) -> Admission:
    """For a row that states no identifier: the payload must be company-scoped by the
    exchange's VERIFIED identifier (the query key, when the URL states one, must equal it)."""
    provider = PAYLOAD_PROVIDER[payload.kind]
    verified = identity.nse_symbol if provider == Provider.NSE else (
        identity.bse_code if provider == Provider.BSE else None
    )
    if verified is None:
        return "UNVERIFIED"
    keys = ("symbol",) if provider == Provider.NSE else ("scripcode", "strScrip")
    try:
        query = parse_qs(urlsplit(payload.url).query)
    except ValueError:
        return "MISMATCH"
    stated = {v for k in keys for v in query.get(k, [])}
    return "MISMATCH" if stated and stated != {verified} else "MATCH"


def admit(
    identity: CompanyIdentity, payload: RawPayload, counts: dict[str, int], *,
    symbol: object = None, isin: object = None, code: object = None,
) -> bool:
    """Gate one feed row; tally refusals by kind for `_row_issues`."""
    status = row_identity(identity, symbol, isin, code)
    if status == "UNSTATED":
        status = scope_identity(identity, payload)
    if status != "MATCH":
        counts[status] = counts.get(status, 0) + 1
    return status == "MATCH"


# --- filing rows ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FilingRow:
    """One announcement, normalized just enough for classification; raw meaning kept."""

    payload_id: UUID
    provider: Provider
    authority: Authority
    exchange: str
    locator: str
    record_id: str | None
    published_at: datetime | None
    published_on: date
    category: str | None
    subject: str
    body: str | None
    url: str | None
    source: EventSourceRef


def nse_announcements(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[FilingRow]:
    out: list[FilingRow] = []
    refused: dict[str, int] = {}
    undated = 0
    for loc, row in json_rows(payload, pid, issues, budget, "data"):
        if not admit(identity, payload, refused, symbol=row.get("symbol"),
                     isin=row.get("sm_isin")):
            continue
        moment = parse_moment(row.get("an_dt")) or parse_moment(row.get("sort_date"))
        if moment is None:
            undated += 1
            continue
        category = text(row.get("desc"), 1000)
        body = text(row.get("attchmntText"))
        subject = category or body or "Announcement"
        url = safe_url(row.get("attchmntFile"), issues, pid, loc)
        record = text(row.get("seq_id"), 64)
        out.append(
            FilingRow(
                payload_id=pid, provider=Provider.NSE, authority=Authority.PRIMARY_REGULATORY,
                exchange="NSE", locator=loc, record_id=record, published_at=moment,
                published_on=moment.date(), category=category, subject=subject, body=body,
                url=url,
                source=source_ref(payload, pid, loc, record_id=record, category=category,
                                  url=url, published=moment),
            )
        )
    _row_issues(issues, pid, refused, undated)
    return out


def bse_announcements(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[FilingRow]:
    out: list[FilingRow] = []
    refused: dict[str, int] = {}
    undated = 0
    for loc, row in json_rows(payload, pid, issues, budget, "Table"):
        if not admit(identity, payload, refused, isin=_first(row, _BSE_ISIN_KEYS),
                     code=row.get("SCRIP_CD")):
            continue
        moment = (parse_moment(row.get("NEWS_DT")) or parse_moment(row.get("DissemDT"))
                  or parse_moment(row.get("DT_TM")))
        if moment is None:
            undated += 1
            continue
        category = " / ".join(
            c for c in (text(row.get("CATEGORYNAME"), 300), text(row.get("SUBCATNAME"), 300)) if c
        ) or None
        subject = (
            text(row.get("NEWSSUB"), 1000) or text(row.get("HEADLINE"), 1000) or "Announcement"
        )
        attachment = text(row.get("ATTACHMENTNAME"), 200)
        url = None
        if attachment and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", attachment):
            url = safe_url(BSE_ATTACHMENT_BASE + attachment, issues, pid, loc)
        record = text(row.get("NEWSID"), 64)
        out.append(
            FilingRow(
                payload_id=pid, provider=Provider.BSE, authority=Authority.PRIMARY_REGULATORY,
                exchange="BSE", locator=loc, record_id=record, published_at=moment,
                published_on=moment.date(), category=category, subject=subject,
                body=text(row.get("HEADLINE")), url=url,
                source=source_ref(payload, pid, loc, record_id=record, category=category,
                                  url=url, published=moment),
            )
        )
    _row_issues(issues, pid, refused, undated)
    return out


def _row_issues(issues: list[Issue], pid: UUID, refused: dict[str, int], undated: int) -> None:
    if refused.get("MISMATCH"):
        issues.append(issue("ROW_IDENTITY_MISMATCH",
                            f"{refused['MISMATCH']} rows belong to another security and were "
                            "dropped", payload=pid))
    if refused.get("UNVERIFIED"):
        issues.append(issue("PROVIDER_IDENTITY_UNVERIFIED",
                            f"{refused['UNVERIFIED']} rows are keyed only by an identifier the "
                            "provider did not verify; kept out of the company's data",
                            payload=pid))
    if undated:
        issues.append(issue("MALFORMED_DATE", f"{undated} rows without a valid date were dropped",
                            payload=pid))


# --- corporate actions ------------------------------------------------------------------------

#: NSE: 'Rs 1.72 Per Share'; BSE: 'Rs. - 1.7200' (the dash form states a per-share amount).
_AMOUNT_PER_SHARE = re.compile(
    r"(?:Rs|Re|INR|₹)\.?\s*(\d{1,7}(?:\.\d{1,4})?)\s*(?:/-)?\s*(?:Per|/)\s*Sh"
    r"|(?:Rs|Re)\.?\s*-\s*(\d{1,7}(?:\.\d{1,4})?)",
    re.I,
)
_RATIO = re.compile(r"\b(\d{1,4})\s*:\s*(\d{1,4})\b")
_SPLIT = re.compile(
    r"From\s*(?:Rs|Re)\.?\s*(\d{1,6}(?:\.\d{1,4})?)\s*/?-?\s*(?:Per\s*Share)?\s*To\s*"
    r"(?:Rs|Re)\.?\s*(\d{1,6}(?:\.\d{1,4})?)",
    re.I,
)
_ACTION_KINDS: tuple[tuple[CorporateActionKind, re.Pattern[str]], ...] = (
    (CorporateActionKind.DEMERGER, re.compile(r"\bde-?merger\b|scheme of arrangement", re.I)),
    (CorporateActionKind.MERGER, re.compile(r"\bamalgamation\b|\bmerger\b", re.I)),
    (CorporateActionKind.DELISTING, re.compile(r"\bdelist", re.I)),
    (CorporateActionKind.BUYBACK, re.compile(r"\bbuy[\s-]?back\b", re.I)),
    (CorporateActionKind.BONUS, re.compile(r"\bbonus\b", re.I)),
    (CorporateActionKind.RIGHTS, re.compile(r"\brights\b", re.I)),
    (CorporateActionKind.SPLIT, re.compile(r"\bsplit\b|sub-?division", re.I)),
    (CorporateActionKind.DIVIDEND, re.compile(r"\bdividend\b", re.I)),
)


@dataclass(frozen=True)
class ActionRow:
    kind: CorporateActionKind
    subject: str
    announcement_date: date | None
    record_date: date | None
    ex_date: date | None
    bc_start: date | None
    bc_end: date | None
    payment_date: date | None
    amount: Decimal | None
    ratio: str | None
    face_from: Decimal | None
    face_to: Decimal | None
    source: EventSourceRef
    issues: tuple[Issue, ...]


def action_details(subject: str, pid: UUID, loc: str) -> tuple[
    CorporateActionKind, Decimal | None, str | None, Decimal | None, Decimal | None, list[Issue]
]:
    issues: list[Issue] = []
    kind = next((k for k, p in _ACTION_KINDS if p.search(subject)), CorporateActionKind.OTHER)
    amounts = {Decimal(a or b) for a, b in _AMOUNT_PER_SHARE.findall(subject)}
    amount = None
    if kind == CorporateActionKind.DIVIDEND:
        if len(amounts) == 1:
            amount = next(iter(amounts))
        elif len(amounts) > 1:
            issues.append(issue("MULTIPLE_AMOUNTS", "Several per-share amounts stated; none "
                                "selected", payload=pid, locator=loc))
    ratio = None
    if kind in (CorporateActionKind.BONUS, CorporateActionKind.RIGHTS):
        ratios = {f"{a}:{b}" for a, b in _RATIO.findall(subject)}
        ratio = next(iter(ratios)) if len(ratios) == 1 else None
    face_from = face_to = None
    if kind == CorporateActionKind.SPLIT and (m := _SPLIT.search(subject)):
        face_from, face_to = Decimal(m.group(1)), Decimal(m.group(2))
        if face_from <= 0 or face_to <= 0:
            face_from = face_to = None
    return kind, amount, ratio, face_from, face_to, issues


def nse_corporate_actions(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[ActionRow]:
    out: list[ActionRow] = []
    refused: dict[str, int] = {}
    for loc, row in json_rows(payload, pid, issues, budget, "data"):
        if not admit(identity, payload, refused, symbol=row.get("symbol"),
                     isin=row.get("isin")):
            continue
        subject = text(row.get("subject"), 1000)
        if subject is None:
            issues.append(issue("MALFORMED_ACTION", "Action without a subject", payload=pid,
                                locator=loc))
            continue
        kind, amount, ratio, f_from, f_to, own = action_details(subject, pid, loc)
        out.append(
            ActionRow(
                kind=kind, subject=subject,
                announcement_date=parse_day(row.get("caBroadcastDate")),
                record_date=parse_day(row.get("recDate")), ex_date=parse_day(row.get("exDate")),
                bc_start=parse_day(row.get("bcStartDate")), bc_end=parse_day(row.get("bcEndDate")),
                payment_date=None, amount=amount, ratio=ratio, face_from=f_from, face_to=f_to,
                source=source_ref(payload, pid, loc, category=subject),
                issues=tuple(own),
            )
        )
    _row_issues(issues, pid, refused, 0)
    return out


def bse_corporate_actions(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[ActionRow]:
    out: list[ActionRow] = []
    refused: dict[str, int] = {}
    for loc, row in json_rows(payload, pid, issues, budget, "Table"):
        if not admit(identity, payload, refused, isin=_first(row, _BSE_ISIN_KEYS),
                     code=row.get("scrip_code") or row.get("SCRIP_CD")):
            continue
        subject = text(row.get("Purpose") or row.get("purpose"), 1000)
        if subject is None:
            issues.append(issue("MALFORMED_ACTION", "BSE action without a purpose", payload=pid,
                                locator=loc))
            continue
        kind, amount, ratio, f_from, f_to, own = action_details(subject, pid, loc)
        out.append(
            ActionRow(
                kind=kind, subject=subject, announcement_date=None,
                record_date=parse_day(row.get("RD_Date") or row.get("RecordDate")),
                ex_date=parse_day(row.get("Ex_date") or row.get("ExDate")),
                bc_start=parse_day(row.get("BCRD_FROM")), bc_end=parse_day(row.get("BCRD_TO")),
                payment_date=parse_day(row.get("payment_date")), amount=amount, ratio=ratio,
                face_from=f_from, face_to=f_to, source=source_ref(payload, pid, loc,
                                                                    category=subject),
                issues=tuple(own),
            )
        )
    _row_issues(issues, pid, refused, 0)
    return out


# --- ownership and documents -----------------------------------------------------------------


def nse_shareholding(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[ShareholdingFiling]:
    filings: list[ShareholdingFiling] = []
    refused: dict[str, int] = {}
    for loc, row in json_rows(payload, pid, issues, budget, "data"):
        if not admit(identity, payload, refused, symbol=row.get("symbol"),
                     isin=row.get("isin")):
            continue
        period = parse_day(row.get("date"))
        if period is None:
            issues.append(issue("MALFORMED_DATE", "Shareholding filing without a period",
                                payload=pid, locator=loc))
            continue
        pct = {k: parse_number(row.get(k), maximum=Decimal(100)) for k in
               ("pr_and_prgrp", "public_val", "employeeTrusts")}
        record = text(row.get("recordId"), 64)
        submitted = parse_day(row.get("submissionDate"))
        filings.append(
            ShareholdingFiling(
                filing_id=uid("shareholding", identity.identity_id, period, record or loc),
                period_end=period,
                submitted_on=submitted,
                promoter_pct=pct["pr_and_prgrp"],
                public_pct=pct["public_val"],
                employee_trusts_pct=pct["employeeTrusts"],
                revised=text(row.get("revisedData"), 4) == "Y",
                xbrl_url=safe_url(row.get("xbrl"), issues, pid, loc),
                sources=(source_ref(payload, pid, loc, record_id=record,
                                    published=parse_moment(row.get("broadcastDate"))),),
            )
        )
    _row_issues(issues, pid, refused, 0)
    # A later filing for the same period supersedes the earlier one (never deleted).
    filings.sort(key=lambda f: (f.period_end, f.submitted_on or date.min, str(f.filing_id)))
    out: list[ShareholdingFiling] = []
    last: dict[date, ShareholdingFiling] = {}
    for filing in filings:
        prior = last.get(filing.period_end)
        if prior is not None:
            filing = filing.model_copy(update={"supersedes_filing_id": prior.filing_id})
        last[filing.period_end] = filing
        out.append(filing)
    return out


_SHP_FACT = r"<in-bse-shp:{tag}\b[^>]*>\s*([^<]{{0,200}}?)\s*<"
SHP_DECLARATIONS = {
    "promoter_pledged": "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged",
    "promoter_ndu": "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking",
    "promoter_other_encumbrance":
        "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU",
}


def _shp_fact(text: str, tag: str) -> str | None:
    m = re.search(_SHP_FACT.format(tag=tag), text)
    return m.group(1) if m else None


def shp_declarations(payload: RawPayload, pid: UUID, identity: CompanyIdentity,
                     issues: list[Issue]) -> tuple[date, dict[str, bool]] | None:
    """A shareholding-pattern XBRL's promoter encumbrance declarations, bound to the company
    (its verified ISIN, else NSE symbol) and to the filing's own report date. Text matching
    only - no XML parser, no entity or schema is ever resolved. None (with an issue) when
    the filing is another company's, undated, or does not state all three declarations."""
    if payload.status != FetchStatus.OK or payload.content is None:
        return None
    text = payload.content.decode("utf-8", errors="replace")  # size-bounded by RawPayload
    isin, symbol = _shp_fact(text, "ISIN"), _shp_fact(text, "Symbol")
    bound = (identity.isin is not None and isin == identity.isin) or (
        identity.isin is None and identity.nse_symbol is not None and symbol == identity.nse_symbol)
    if not bound:
        issues.append(issue("SHAREHOLDING_XBRL_UNBOUND", "Shareholding XBRL is not the verified "
                            "company's (ISIN / symbol mismatch); ignored", payload=pid))
        return None
    day = parse_day(_shp_fact(text, "DateOfReport"))
    values = {field: _shp_fact(text, tag) for field, tag in SHP_DECLARATIONS.items()}
    if day is None or any(v not in ("true", "false") for v in values.values()):
        issues.append(issue("SHAREHOLDING_XBRL_INCOMPLETE", "Shareholding XBRL without a report "
                            "date or a promoter encumbrance declaration; ignored", payload=pid))
        return None
    return day, {field: v == "true" for field, v in values.items()}


def nse_insider_trades(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[InsiderTrade]:
    out = []
    refused: dict[str, int] = {}
    for loc, row in json_rows(payload, pid, issues, budget, "data"):
        if not admit(identity, payload, refused, symbol=row.get("symbol")):
            continue
        person = text(row.get("acqName"), 1000)
        if person is None:
            continue
        record = text(row.get("pid"), 64) or text(row.get("did"), 64)
        big = Decimal(10) ** 15
        out.append(
            InsiderTrade(
                record_id=uid("insider", identity.identity_id, record or loc),
                person=person,
                person_category=text(row.get("personCategory"), 300),
                transaction_type=text(row.get("tdpTransactionType"), 100),
                mode=text(row.get("acqMode"), 300),
                security_type=text(row.get("secType"), 300),
                quantity=_non_negative(parse_number(row.get("secAcq"), maximum=big)),
                value_inr=_non_negative(parse_number(row.get("secVal"), maximum=big)),
                shares_before=_non_negative(parse_number(row.get("befAcqSharesNo"), maximum=big)),
                shares_after=_non_negative(parse_number(row.get("afterAcqSharesNo"), maximum=big)),
                from_date=parse_day(row.get("acqfromDt")),
                to_date=parse_day(row.get("acqtoDt")),
                intimation_date=parse_day(row.get("intimDt")),
                disclosed_at=parse_moment(row.get("date")),
                sources=(source_ref(payload, pid, loc, record_id=record),),
            )
        )
    _row_issues(issues, pid, refused, 0)
    return out


def _non_negative(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value >= 0 else None


def nse_documents(
    payload: RawPayload, pid: UUID, identity: CompanyIdentity, issues: list[Issue], budget: Budget
) -> list[FilingDocument]:
    kind = payload.kind
    out: list[FilingDocument] = []
    refused: dict[str, int] = {}
    keys = ("data",)
    for loc, row in json_rows(payload, pid, issues, budget, *keys):
        # The credit-rating feed is market-wide and filtered by issuer in `_document`; every
        # other document feed is company-scoped and gated like any other row.
        if kind != PayloadKind.NSE_CREDIT_RATINGS and not admit(
            identity, payload, refused, symbol=row.get("symbol"), isin=row.get("isin")
        ):
            continue
        doc = _document(kind, row, loc, payload, pid, identity, issues)
        if doc is not None:
            out.append(doc)
    _row_issues(issues, pid, refused, 0)
    return out


def _document(
    kind: PayloadKind,
    row: dict[str, Any],
    loc: str,
    payload: RawPayload,
    pid: UUID,
    identity: CompanyIdentity,
    issues: list[Issue],
) -> FilingDocument | None:
    if kind == PayloadKind.NSE_ANNUAL_REPORTS:
        start, end = text(row.get("fromYr"), 4), text(row.get("toYr"), 4)
        published = parse_moment(row.get("disseminationDateTime")) or parse_moment(
            row.get("broadcast_dttm")
        )
        url = safe_url(row.get("fileName"), issues, pid, loc)
        return FilingDocument(
            document_id=uid("filing", kind.value, identity.identity_id, url or loc),
            category=FilingCategory.ANNUAL_REPORT,
            title=f"Annual report {start}-{end}" if start and end else "Annual report",
            period_label=f"FY{start}-{end}" if start and end else None,
            basis=FinancialBasis.BOTH,
            revised=text(row.get("submission_type"), 20) == "Revised",
            published_on=published.date() if published else None,
            url=url,
            sources=(source_ref(payload, pid, loc, url=url, published=published),),
        )
    if kind == PayloadKind.NSE_FINANCIAL_RESULTS:
        consolidated = text(row.get("consolidated"), 40)
        basis = (
            FinancialBasis.CONSOLIDATED if consolidated == "Consolidated"
            else FinancialBasis.STANDALONE if consolidated in ("Non-Consolidated", "Standalone")
            else FinancialBasis.UNKNOWN
        )
        published = parse_moment(row.get("broadCastDate")) or parse_moment(row.get("filingDate"))
        url = safe_url(row.get("xbrl"), issues, pid, loc)
        audited = text(row.get("audited"), 20)
        return FilingDocument(
            document_id=uid("filing", kind.value, identity.identity_id,
                            text(row.get("seqNumber"), 64) or loc),
            category=FilingCategory.FINANCIAL_RESULT,
            title=(
                f"{text(row.get('relatingTo'), 100) or 'Result'} "
                f"({consolidated or 'basis not stated'})"
            ),
            period_label=text(row.get("period"), 40),
            period_start=parse_day(row.get("fromDate")),
            period_end=parse_day(row.get("toDate")),
            basis=basis,
            audited=None if audited is None else audited == "Audited",
            published_on=published.date() if published else None,
            url=url,
            sources=(source_ref(payload, pid, loc, record_id=row.get("seqNumber"), url=url,
                                published=published),),
        )
    if kind == PayloadKind.NSE_BRSR:
        url = safe_url(row.get("attachmentFile"), issues, pid, loc)
        start, end = row.get("fyFrom"), row.get("fyTo")
        submitted = parse_day(row.get("submissionDate"))
        return FilingDocument(
            document_id=uid("filing", kind.value, identity.identity_id, url or loc),
            category=FilingCategory.BRSR,
            title="Business responsibility and sustainability report",
            period_label=f"FY{start}-{end}" if isinstance(start, int) and isinstance(end, int)
            else None,
            basis=FinancialBasis.NOT_APPLICABLE,
            revised=text(row.get("revisionDate"), 20) is not None,
            published_on=submitted,
            url=url,
            sources=(source_ref(payload, pid, loc, url=url),),
        )
    if kind == PayloadKind.NSE_CREDIT_RATINGS:
        # The feed is market-wide: keep a row only when it names this company's symbol or an
        # instrument of this issuer (same 7-character ISIN issuer prefix).
        symbol, isin = text(row.get("Symbol"), 40), text(row.get("ISIN"), 20)
        own_symbol = identity.nse_symbol is not None and symbol == identity.nse_symbol
        own_issuer = (identity.isin is not None and isin is not None
                      and isin_issuer(isin) == isin_issuer(identity.isin))
        if not (own_symbol or own_issuer):
            return None
        published = parse_moment(row.get("BroadcastDateTime"))
        url = safe_url(row.get("XbrlFileName"), issues, pid, loc)
        agency = text(row.get("NameOfCRAgency"), 200) or "agency not stated"
        rating = text(row.get("CreditRating"), 100) or "rating not stated"
        action = text(row.get("RatingAction"), 100) or "action not stated"
        return FilingDocument(
            document_id=uid("filing", kind.value, identity.identity_id,
                            text(row.get("AppID"), 64) or loc),
            category=FilingCategory.CREDIT_RATING,
            title=f"{agency}: {rating} ({action})",
            period_end=parse_day(row.get("DateofCR")),
            basis=FinancialBasis.NOT_APPLICABLE,
            published_on=published.date() if published else parse_day(row.get("DateofCR")),
            url=url,
            sources=(source_ref(payload, pid, loc, record_id=row.get("AppID"), url=url,
                                published=published),),
        )
    return None


def by_kind(
    payloads: Iterable[tuple[RawPayload, UUID]], *kinds: PayloadKind
) -> list[tuple[RawPayload, UUID]]:
    return [(p, i) for p, i in payloads if p.kind in kinds]
