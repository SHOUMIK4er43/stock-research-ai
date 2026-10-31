"""P2 security hardening: regex/input bounds (P2-3), XML DTD/entity refusal (P2-4),
loopback-only bind host and owner-only local files (P2-5), safe external errors (P2-6)
and log secret redaction (P2-7). Offline; temporary paths only; no real credentials -
every "secret" here is a fake test value."""

import io
import logging
import os
import stat
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_research_plan import YESBANK_URL
from test_research_plan import full_research_engine as full_research_engine  # noqa: F401

from stock_research_ai import logging_setup
from stock_research_ai.adapters.documents.document_parsers import parse_spreadsheet
from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.secret_store import (
    MacOSKeychainSecretStore,
    resolve_analyst_api_key,
)
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import browser_jobs
from stock_research_ai.application.browser_jobs import BrowserJobManager, JobStatus
from stock_research_ai.application.document_structure import trailing_numbers
from stock_research_ai.application.management_statements import sentences
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalystExecutionSpec
from stock_research_ai.domain.document_intelligence import ExtractionLimits, ExtractionStatus
from stock_research_ai.domain.provenance import AcquiredPayload, RetrievalStatus, SourceRetrieval
from stock_research_ai.interfaces import http as http_interface
from stock_research_ai.logging_setup import configure_logging, get_logger

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
FAKE_SECRET = "sk-ant-p2-FAKE-0123456789abcdef"
POSIX = os.name == "posix"


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)  # the common default: what the fixes must not depend on
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- P2-3: trailing_numbers / last-word scans are linear --------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("", ("", [])),
        ("   ", ("", [])),
        ("Revenue", ("Revenue", [])),
        ("1,234.50", ("", ["1,234.50"])),
        ("Revenue from operations 2.1 1,234.50 1,100.00",
         ("Revenue from operations", ["2.1", "1,234.50", "1,100.00"])),
        ("Exceptional items - (5.00)", ("Exceptional items", ["-", "(5.00)"])),
        ("EBITDA margin 12.5% (3.2)%", ("EBITDA margin", ["12.5%", "(3.2)%"])),
        ("Not a number (3.2%)", ("Not a number (3.2%)", [])),
        ("Net debt – −450", ("Net debt", ["–", "−450"])),
        ("Tax expense  12 \t", ("Tax expense", ["12"])),
        ("Item x1 2", ("Item x1", ["2"])),
        ("Depreciation 2.2 & 2.3.2 2,011 1,863", ("Depreciation 2.2 & 2.3.2", ["2,011", "1,863"])),
    ],
)  # fmt: skip
def test_trailing_numbers_semantics_are_unchanged(line, expected):
    assert trailing_numbers(line) == expected


@pytest.mark.parametrize(
    "name,line",
    [
        ("20k trailing tokens", "Revenue " + "1 " * 20_000),
        ("50k whitespace run", "a" + " " * 50_000 + "b"),
        ("200k digits and commas", "a " + "1," * 100_000 + "x"),
        ("one 1M-character word", "9" * 1_000_000 + "x"),
        ("1M-character numeric word", "9" * 1_000_000),
    ],
)
def test_trailing_numbers_is_linear_on_adversarial_lines(name, line):
    started = time.perf_counter()
    label, numbers = trailing_numbers(line)
    assert time.perf_counter() - started < 1.0, name  # was 17-26 s for the first two
    assert len(label) + sum(map(len, numbers)) <= len(line)


def test_trailing_numbers_very_long_input_keeps_every_token():
    label, numbers = trailing_numbers("Revenue " + "1 " * 100_000)
    assert label == "Revenue" and len(numbers) == 100_000


def test_sentence_split_last_word_scan_is_linear_and_unchanged():
    assert sentences("Mr. Rao said growth was 12%. Margins improved. Next") == [
        (0, 28), (29, 46), (47, 51),
    ]  # fmt: skip
    hostile = "a" * 200_000 + "-b. Tail"
    started = time.perf_counter()
    spans = sentences(hostile)
    assert time.perf_counter() - started < 1.0  # 20k characters alone took ~2 s before
    assert spans == [(0, 200_003), (200_004, 200_008)]


# --- P2-4: XML with any DTD is refused, wherever and however it is encoded ----------------------

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
LAUGHS = (
    '<!DOCTYPE worksheet [<!ENTITY a "aaaaaaaaaa">'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
    '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>'
)
LONG_PROLOG = '<?xml version="1.0" encoding="UTF-8"?><!--' + "x" * 4_000 + "-->"


def _workbook(sheet: bytes | str, workbook_xml: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            workbook_xml
            or f'<workbook {NS} {REL}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def _sheet(text: str, prolog: str = "") -> str:
    return (
        f'{prolog}<worksheet {NS}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{text}'
        "</t></is></c></row></sheetData></worksheet>"
    )


def _parse(content: bytes):
    return parse_spreadsheet(content, ExtractionLimits())


def test_normal_xml_with_predefined_and_character_entities_still_parses():
    document = _parse(_workbook(_sheet("R&amp;D &lt;5&gt; &#8377;12 &quot;ok&quot;")))
    assert document.status is ExtractionStatus.SUCCESS
    assert document.pages[0].text == 'R&D <5> ₹12 "ok"\n'


@pytest.mark.parametrize(
    "sheet",
    [
        # A DTD after a long prolog comment slipped past the 2 KB byte sniff ...
        _sheet("&d;", LONG_PROLOG + LAUGHS),
        # ... and so did any DTD in a UTF-16 part.
        ('<?xml version="1.0" encoding="UTF-16"?>' + _sheet("&d;", LAUGHS)).encode("utf-16"),
        # External file and network entities.
        _sheet("&x;", LONG_PROLOG + '<!DOCTYPE worksheet [<!ENTITY x SYSTEM "file:///etc/passwd">]>'),
        _sheet("&x;", LONG_PROLOG + '<!DOCTYPE worksheet [<!ENTITY x SYSTEM "https://evil.example/x">]>'),
        _sheet("x", LONG_PROLOG + '<!DOCTYPE worksheet SYSTEM "https://evil.example/x.dtd">'),
        _sheet("x", LONG_PROLOG + '<!DOCTYPE w [<!ENTITY % p SYSTEM "file:///etc/hosts"> %p;]>'),
        # Short-prolog DTDs keep failing as before.
        _sheet("&d;", LAUGHS),
        _sheet("x", "<!DOCTYPE x>"),
    ],
    ids=["long-prolog-laughs", "utf16-laughs", "external-file", "external-http",
         "external-dtd", "parameter-entity", "short-laughs", "bare-doctype"],
)  # fmt: skip
def test_any_dtd_in_a_streamed_part_is_refused(sheet):
    document = _parse(_workbook(sheet))
    assert document.status is ExtractionStatus.MALFORMED
    assert not document.pages
    assert {code for code, *_ in document.warnings} == {"MALFORMED_SPREADSHEET"}


def test_any_dtd_in_a_whole_read_index_part_is_refused():
    workbook_xml = (
        LONG_PROLOG + LAUGHS.replace("worksheet", "workbook")
        + f'<workbook {NS} {REL}><sheets><sheet name="&d;" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )  # fmt: skip
    document = _parse(_workbook(_sheet("x"), workbook_xml))
    assert document.status is ExtractionStatus.MALFORMED and not document.pages


@pytest.mark.parametrize(
    "sheet", [_sheet("&undeclared;"), _sheet("x").replace("</worksheet>", ""), b"\xff\xfe\x00<"]
)
def test_malformed_xml_and_undeclared_entities_fail_closed(sheet):
    assert _parse(_workbook(sheet)).status is ExtractionStatus.MALFORMED


def test_deeply_nested_xml_is_handled_without_recursion_or_blowup():
    depth = 50_000
    sheet = f"<worksheet {NS}>" + "<x>" * depth + "</x>" * depth + "</worksheet>"
    started = time.perf_counter()
    document = _parse(_workbook(sheet))
    assert time.perf_counter() - started < 10
    assert document.status is ExtractionStatus.SUCCESS
    assert [page.text for page in document.pages] == [""]


# --- P2-5: loopback-only bind host ------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", " LocalHost "])
def test_loopback_http_hosts_are_accepted(host):
    assert Settings(_env_file=None, http_host=host).http_host == host.strip().lower()


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "::1", "[::1]", "192.168.1.20", "10.0.0.5", "172.16.0.1", "8.8.8.8",
     "2606:4700::1111", "127.0.0.2", "example.com", "localhost.evil.example", ""],
)  # fmt: skip
def test_any_other_http_host_is_refused(host):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, http_host=host)


def test_http_host_from_the_environment_is_validated_too(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_HOST", "0.0.0.0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_bind_policy_and_trusted_hosts_are_the_same_list():
    from stock_research_ai.config import LOOPBACK_BIND_HOSTS

    assert http_interface.LOOPBACK_HOSTS == LOOPBACK_BIND_HOSTS == ("127.0.0.1", "localhost")


# --- P2-5: owner-only data directory and files ------------------------------------------------


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
async def test_database_directory_and_file_are_owner_only(tmp_path, umask_022):
    database = SQLiteDatabase(tmp_path / "home" / ".stock-research-ai" / "research.sqlite3")
    try:
        await database.initialize()
    finally:
        await database.close()
    assert _mode(tmp_path / "home") == 0o700
    assert _mode(tmp_path / "home" / ".stock-research-ai") == 0o700
    assert _mode(tmp_path / "home" / ".stock-research-ai" / "research.sqlite3") == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_pairing_file_is_never_wider_than_0600(tmp_path, umask_022, monkeypatch):
    path = tmp_path / "data" / "browser_auth.json"
    modes: list[int] = []
    real_replace = os.replace

    def watching_replace(source, destination):  # the moment the content becomes visible
        modes.append(_mode(Path(source)))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", watching_replace)
    store = PairingStore(path)
    store.complete_pairing(EXTENSION_ORIGIN, store.issue_pairing_code())
    assert modes and set(modes) == {0o600}
    assert _mode(path) == 0o600 and _mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["browser_auth.json"]  # no temp left


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_existing_wider_pairing_file_is_replaced_owner_only(tmp_path, umask_022):
    path = tmp_path / "browser_auth.json"
    path.write_text("{}")
    os.chmod(path, 0o644)
    PairingStore(path).issue_pairing_code()
    assert _mode(path) == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
async def test_artifact_directories_and_files_are_owner_only(tmp_path, umask_022):
    from datetime import UTC, datetime
    from uuid import uuid4

    root = tmp_path / "artifacts"
    store = FileRawArtifactStore(root)
    artifact = await store.put(
        AcquiredPayload(
            retrieval=SourceRetrieval(
                id=uuid4(), provider="test", requested_url="https://example.com/a",
                final_url="https://example.com/a", retrieved_at=datetime.now(UTC),
                status=RetrievalStatus.SUCCESS,
            ),
            content=b"%PDF-1.7 bytes",
            source_type="company_document",
            file_extension="pdf",
        )
    )  # fmt: skip
    path = root / artifact.content_reference
    assert _mode(path) == 0o600
    for directory in (root, root / "sha256", path.parent):
        assert _mode(directory) == 0o700


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_existing_user_chosen_directories_are_never_chmodded(tmp_path, umask_022):
    chosen = tmp_path / "Documents"
    chosen.mkdir()
    os.chmod(chosen, 0o755)
    PairingStore(chosen / "auth.json").issue_pairing_code()
    configure_logging(chosen / "service.log")
    assert _mode(chosen) == 0o755
    assert _mode(chosen / "auth.json") == 0o600 and _mode(chosen / "service.log") == 0o600


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_rotated_log_files_are_created_owner_only(tmp_path, umask_022, monkeypatch):
    monkeypatch.setattr(logging_setup, "_MAX_BYTES", 300)
    log_path = tmp_path / "logs" / "service.log"
    logger = configure_logging(log_path)
    for i in range(40):
        logger.info("diagnostic line %d %s", i, "x" * 40)
    files = sorted(log_path.parent.iterdir())
    assert len(files) > 2  # rotated at least twice
    assert {_mode(f) for f in files} == {0o600}
    assert _mode(log_path.parent) == 0o700


# --- P2-6: safe external errors ---------------------------------------------------------------

HOSTILE_VALUES = [
    FAKE_SECRET,
    "/Users/alice/.ssh/id_rsa",
    'quote" \' <tag> \x00\x1b[31m\r\n',
    "A" * 50_000,
]
HOSTILE_IDS = ["fake-secret", "absolute-path", "quotes-control-chars", "50k-chars"]


def _execution_factory() -> AnalystExecutionSpec:
    return AnalystExecutionSpec(template_id="full-research-v1", template_version=1)


@pytest.fixture
async def p2_client(full_research_engine, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", EXTENSION_ORIGIN)
    engine, store, _model = full_research_engine
    manager = BrowserJobManager(engine, store, ScreenerBroadIndustrySource(), _execution_factory)
    pairing = PairingStore(tmp_path / "auth.json")
    token = pairing.complete_pairing(EXTENSION_ORIGIN, pairing.issue_pairing_code())
    app = http_interface.create_app(engine=engine, job_manager=manager, pairing_store=pairing)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client, {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}


def _reflects(response: httpx.Response, value: str) -> bool:
    text = response.text
    needles = {value, value[:40], repr(value)[1:41]}
    return any(n and n in text for n in needles) or "\x00" in text or "\x1b" in text


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=HOSTILE_IDS)
async def test_validation_errors_never_echo_the_submitted_value(p2_client, value):
    client, headers = p2_client
    for path, body in [
        ("/v1/pair", {"pairing_code": [value]}),
        ("/v1/pair", {"pairing_code": "x", value: 1}),
        ("/v1/research-jobs", {"portal": value, "source_url": YESBANK_URL}),
        ("/v1/research-jobs", {"portal": "SCREENER", "source_url": {"u": value}}),
        ("/v1/summaries", {"portal": "SCREENER", "source_url": [value], "extra": value}),
    ]:
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 422, (path, response.status_code)
        assert not _reflects(response, value), (path, response.text[:300])
        detail = response.json()["detail"]
        assert detail and all(set(e) == {"loc", "msg", "type"} for e in detail)
        assert len(response.content) < 4_000


async def test_validation_error_still_says_what_was_wrong(p2_client):
    client, headers = p2_client
    response = await client.post("/v1/pair", json={"pairing_code": 5, "zz": 1}, headers=headers)
    detail = response.json()["detail"]
    assert {"loc": ["body", "pairing_code"], "msg": "Input should be a valid string",
            "type": "string_type"} in detail  # fmt: skip
    assert {"loc": ["body", "(unexpected field)"], "msg": "Extra inputs are not permitted",
            "type": "extra_forbidden"} in detail  # fmt: skip


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=HOSTILE_IDS)
async def test_unsupported_source_url_responses_do_not_echo_the_url(p2_client, value):
    client, headers = p2_client
    url = "https://www.screener.in/company/" + value
    for path in ("/v1/research-jobs", "/v1/summaries"):
        response = await client.post(
            path, json={"portal": "SCREENER", "source_url": url}, headers=headers
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "UNSUPPORTED_SOURCE_URL"
        assert not _reflects(response, value)


# The URL's port is parsed by the standard library, whose ValueError quotes the raw port
# text ("Port could not be cast to integer value as '<text>'"): it must never reach a
# response. (Only characters a URL port can carry: no '/', '?', '#' or '@'.)
PORT_VALUES = [FAKE_SECRET.replace("-", "_"), "A" * 50_000, "C%3A%5CUsers%5Calice"]
PORT_IDS = ["fake-secret", "50k-chars", "encoded-path"]


@pytest.mark.parametrize("value", PORT_VALUES, ids=PORT_IDS)
async def test_malformed_port_is_never_echoed(p2_client, value):
    client, headers = p2_client
    url = f"https://www.screener.in:{value}/company/INFY/"
    for path in ("/v1/research-jobs", "/v1/summaries"):
        response = await client.post(
            path, json={"portal": "SCREENER", "source_url": url}, headers=headers
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "UNSUPPORTED_SOURCE_URL"
        assert not _reflects(response, value)


@pytest.mark.parametrize("value", PORT_VALUES, ids=PORT_IDS)
def test_url_validators_refuse_a_malformed_port_with_fixed_text(value):
    from stock_research_ai.adapters.documents.source import validate_url as validate_document
    from stock_research_ai.adapters.screener.source import validate_url as validate_screener
    from stock_research_ai.application.browser_jobs import _canonical_key
    from stock_research_ai.application.evidence_catalogue import canonical_url

    for validate, host in ((validate_document, "example.com"),
                           (validate_screener, "www.screener.in")):  # fmt: skip
        with pytest.raises(ValueError) as caught:
            validate(f"https://{host}:{value}/company/INFY/")
        assert value[:40] not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        canonical_url(f"https://example.com:{value}/report.pdf")
    assert value[:40] not in str(caught.value)
    key = _canonical_key("SCREENER", f"https://www.screener.in:{value}/company/INFY/",
                         "FULL_RESEARCH_V1")  # fmt: skip
    assert key[1] != "https://www.screener.in/company/INFY/"  # never folded into INFY


# urlsplit() itself raises a ValueError quoting the raw netloc when NFKC normalization turns
# a character into one of '/?#@:' ("netloc '<text>' contains invalid characters under NFKC
# normalization"). Every source-URL rejection must use fixed text instead. Each case is
# (url, markers that must never appear in the response).
NFKC_MARKER = "sk-ant-api03-FAKE-nfkc-0000"
URL_REJECTIONS = [
    (f"https://{NFKC_MARKER}\uff0f.www.screener.in/company/YESBANK/", [NFKC_MARKER]),
    ("https://www.screener.in:FAKEPORT_marker/company/INFY/", ["FAKEPORT_marker"]),
    ("https://FAKEUSER:FAKEPASS@www.screener.in/company/INFY/", ["FAKEUSER", "FAKEPASS"]),
    ("https://www.screener.in/company/INFY/\x00\x1bFAKECTRL/", ["FAKECTRL"]),
    ("https://www.scr\x07FAKECTRLHOST.in/company/INFY/", ["FAKECTRLHOST"]),
    ("https://" + "h" * 5_000 + "\uff03FAKELONGHOST.screener.in/company/X/", ["h" * 40]),
    ("https://www.screener.in/company/\"FAKEQUOTE'/", ["FAKEQUOTE"]),
    ("https://www.screener.in\\FAKEBACKSLASH\\company\\INFY\\", ["FAKEBACKSLASH"]),
    ("https://www.screener.in/FAKEBADPATH/../etc/", ["FAKEBADPATH"]),
    (f"https://{FAKE_SECRET}.www.screener.in/company/INFY/", [FAKE_SECRET]),
    ("https://www.screener.in/company/INFY/C:/Users/alice/.ssh/FAKEPATH", ["/Users/alice"]),
]
URL_REJECTION_IDS = [
    "nfkc-netloc", "malformed-port", "credentials", "control-chars-path",
    "control-chars-host", "long-nfkc-host", "quotes", "backslashes", "invalid-path",
    "token-in-netloc", "filesystem-path",
]  # fmt: skip
_STDLIB_TEXT = ("NFKC", "netloc", "invalid characters", "Port could not be cast", "ValueError",
                "Traceback", "/Users/", "site-packages")  # fmt: skip


@pytest.mark.parametrize(("url", "markers"), URL_REJECTIONS, ids=URL_REJECTION_IDS)
async def test_rejected_source_urls_get_a_fixed_non_reflective_error(p2_client, url, markers):
    client, headers = p2_client
    for path in ("/v1/research-jobs", "/v1/summaries"):
        response = await client.post(
            path, json={"portal": "SCREENER", "source_url": url}, headers=headers
        )
        assert response.status_code == 422, (path, response.status_code)
        assert response.json()["detail"] == {
            "code": "UNSUPPORTED_SOURCE_URL",
            "message": "Not a supported Screener company page",
        }
        assert url not in response.text
        for marker in markers:
            assert not _reflects(response, marker), (path, response.text[:300])
        for text in _STDLIB_TEXT:
            assert text not in response.text, (path, text)


def test_every_url_parser_refuses_an_nfkc_invalid_netloc_with_fixed_text():
    from stock_research_ai.adapters.documents.evidence_fetcher import unsupported_code
    from stock_research_ai.adapters.documents.source import validate_url as validate_document
    from stock_research_ai.adapters.screener.evidence import ScreenerEvidenceSource
    from stock_research_ai.adapters.screener.source import validate_url as validate_screener
    from stock_research_ai.application.browser_jobs import _canonical_key
    from stock_research_ai.application.evidence_catalogue import canonical_url
    from stock_research_ai.domain.evidence import _http_url

    for host in ("www.screener.in", "example.com"):
        for scheme in ("https", "http"):
            url = f"{scheme}://{NFKC_MARKER}\uff0f.{host}/company/INFY/"
            for parse in (validate_document, validate_screener, canonical_url, _http_url):
                with pytest.raises(ValueError) as caught:
                    parse(url)
                assert NFKC_MARKER not in str(caught.value)
                assert "NFKC" not in str(caught.value)
                assert caught.value.__cause__ is None and caught.value.__suppress_context__
            assert unsupported_code(url) == "MALFORMED_URL"
            assert ScreenerEvidenceSource._retrieval_state(url) is None
    key = _canonical_key("SCREENER", f"https://{NFKC_MARKER}\uff0f.www.screener.in/company/INFY/",
                         "FULL_RESEARCH_V1")  # fmt: skip
    assert key[1] != "https://www.screener.in/company/INFY/"  # never folded into INFY


def test_supported_source_urls_are_unchanged_by_the_parser_guards():
    from stock_research_ai.adapters.screener.source import validate_url as validate_screener
    from stock_research_ai.application.browser_jobs import _canonical_key

    for url in (YESBANK_URL, "https://www.screener.in/company/INFY/",
                "https://www.screener.in:443/company/infy/consolidated/"):  # fmt: skip
        assert validate_screener(url) == url
    key = _canonical_key("SCREENER", "https://WWW.screener.in:443/company/infy/",
                         "FULL_RESEARCH_V1")  # fmt: skip
    assert key[1] == "https://www.screener.in/company/INFY/"


async def test_failed_job_shows_a_stable_message_and_logs_the_detail(
    p2_client, monkeypatch, tmp_path
):
    """The raw exception (paths, SQL, a token) reaches only the local service log."""
    client, headers = p2_client
    log_path = tmp_path / "service.log"
    configure_logging(log_path, known_secrets=[FAKE_SECRET])
    leaky = (
        f"(sqlite3.OperationalError) disk I/O error [SQL: INSERT INTO reports VALUES (?)] "
        f"[parameters: ('{FAKE_SECRET}',)] at /Users/alice/.stock-research-ai/research.sqlite3"
    )

    async def failing_run(*args, **kwargs):
        raise RuntimeError(leaky)

    monkeypatch.setattr(browser_jobs, "run_full_research", failing_run)
    launched = await client.post(
        "/v1/research-jobs", json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=headers,
    )  # fmt: skip
    job_id = launched.json()["job_id"]
    for _ in range(500):
        response = await client.get(f"/v1/research-jobs/{job_id}", headers=headers)
        if response.json()["status"] == JobStatus.FAILED:
            break
        await __import__("asyncio").sleep(0.01)
    failure = response.json()["failure"]
    assert failure["code"] == "EVIDENCE_ACQUISITION_FAILED"
    assert failure["message"] == (
        f"Company evidence could not be acquired. Details are in the local service log "
        f"(job {job_id})."
    )
    for fragment in (FAKE_SECRET, "/Users/alice", "INSERT INTO", "sqlite3"):
        assert fragment not in response.text
    logged = log_path.read_text()
    assert job_id in logged and "disk I/O error" in logged and "INSERT INTO" in logged
    assert FAKE_SECRET not in logged and "[REDACTED]" in logged


# --- P2-7: log secret redaction ---------------------------------------------------------------


def _log_text(log_path: Path) -> str:
    for handler in logging.getLogger("stock_research_ai").handlers:
        handler.flush()
    return log_path.read_text()


def test_keychain_loaded_credential_is_redacted_from_service_logs(tmp_path):
    import subprocess

    log_path = tmp_path / "service.log"
    configure_logging(log_path)  # started before the credential is ever loaded

    def runner(argv, stdin=None):
        return subprocess.CompletedProcess(argv, 0, FAKE_SECRET + "\n", "")

    value, source = resolve_analyst_api_key(
        Settings(_env_file=None), MacOSKeychainSecretStore(runner=runner)
    )
    assert (value, source) == (FAKE_SECRET, "keychain")
    get_logger("analyst").info("provider call failed: key=%s", value)
    get_logger("analyst").info(f"inline Bearer-less header x-api-key:{FAKE_SECRET}!")
    text = _log_text(log_path)
    assert FAKE_SECRET not in text and text.count("[REDACTED]") == 2
    assert "provider call failed: key=" in text  # the diagnostic itself survives


def test_environment_credential_is_redacted(tmp_path):
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    resolve_analyst_api_key(
        Settings(_env_file=None, analyst_provider="aicredits", aicredits_api_key=FAKE_SECRET),
        MacOSKeychainSecretStore(runner=lambda argv, stdin=None: None),  # type: ignore[arg-type,return-value]
    )
    get_logger("x").warning("request to provider with %s", FAKE_SECRET)
    assert FAKE_SECRET not in _log_text(log_path)


def test_issued_pairing_code_and_bearer_token_are_redacted(tmp_path):
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    store = PairingStore(tmp_path / "auth.json")
    code = store.issue_pairing_code()
    token = store.complete_pairing(EXTENSION_ORIGIN, code)
    get_logger("pairing").info("debug: code=%s token=%s in url ?t=%s&x=1", code, token, token)
    text = _log_text(log_path)
    assert code not in text and token not in text
    assert text.count("[REDACTED]") == 3


def test_invalid_presented_tokens_are_never_registered(tmp_path):
    store = PairingStore(tmp_path / "auth.json")
    store.complete_pairing(EXTENSION_ORIGIN, store.issue_pairing_code())
    before = dict(logging_setup._registered_secrets)
    for i in range(200):
        assert not store.verify(EXTENSION_ORIGIN, f"attacker-chosen-value-{i:04d}")
    assert logging_setup._registered_secrets == before


def test_kite_credentials_are_redacted(tmp_path):
    from stock_research_ai.adapters.kite.source import KiteSource

    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    KiteSource(api_key="kite-fake-api-key-1", access_token="kite-fake-access-token-2")
    get_logger("kite").info("token kite-fake-api-key-1:kite-fake-access-token-2")
    text = _log_text(log_path)
    assert "kite-fake" not in text and "token [REDACTED]:[REDACTED]" in text


def test_secrets_in_exception_tracebacks_are_redacted(tmp_path):
    log_path = tmp_path / "service.log"
    configure_logging(log_path, known_secrets=[FAKE_SECRET])
    try:
        raise RuntimeError(f"401 from provider for key {FAKE_SECRET}")
    except RuntimeError:
        get_logger("analyst").exception("analysis failed")
    text = _log_text(log_path)
    assert "Traceback" in text and "401 from provider for key [REDACTED]" in text
    assert FAKE_SECRET not in text


@pytest.mark.parametrize("value", [None, "", "a", "1234567"])
def test_empty_or_trivial_values_never_cause_pathological_redaction(tmp_path, value):
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    logging_setup.register_secret(value)
    get_logger("x").info("Revenue 1234567 grew; a Company Secretary signed")
    assert "Revenue 1234567 grew; a Company Secretary signed" in _log_text(log_path)


def test_overlapping_secrets_are_redacted_whole_and_registry_is_bounded(tmp_path):
    log_path = tmp_path / "service.log"
    configure_logging(log_path)
    logging_setup.register_secret("token-prefix")
    logging_setup.register_secret("token-prefix-and-more")
    get_logger("x").info("value token-prefix-and-more end")
    assert "value [REDACTED] end" in _log_text(log_path)
    for i in range(500):
        logging_setup.register_secret(f"generated-secret-{i:04d}")
    assert len(logging_setup._registered_secrets) == logging_setup._MAX_REGISTERED_SECRETS


def test_serve_http_registers_configured_credentials_for_redaction(tmp_path, monkeypatch):
    import uvicorn

    from stock_research_ai.__main__ import main

    log_path = tmp_path / "service.log"
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(log_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_API_KEY", FAKE_SECRET)
    monkeypatch.setattr(http_interface, "create_app", lambda settings: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: get_logger("x").info("key %s", FAKE_SECRET))
    main(["serve-http"])
    text = _log_text(log_path)
    assert "key [REDACTED]" in text and FAKE_SECRET not in text
