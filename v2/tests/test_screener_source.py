import httpx
import pytest

from stock_research_ai.adapters.screener.source import ScreenerSource, validate_url
from stock_research_ai.domain.extraction import ExtractionRequest

URL = "https://www.screener.in/company/YESBANK/consolidated/"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.screener.in/company/YESBANK/",
        "https://evil.test/company/YESBANK/",
        "https://www.screener.in/login/",
        "https://user:password@www.screener.in/company/YESBANK/",
        URL + "?token=private",
        URL + "#section",
        "https://www.screener.in:8443/company/YESBANK/",
    ],
)
def test_only_public_company_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "status,expected", [(200, "success"), (403, "blocked"), (429, "blocked"), (500, "failed")]
)
async def test_mock_http_preserves_response_bytes(status, expected):
    data = b"<html>\r\n  exact \xc2\xa0 bytes </html>"
    source = ScreenerSource(
        httpx.MockTransport(
            lambda request: httpx.Response(
                status, content=data, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
    )
    payload = await source.acquire(ExtractionRequest(provider="screener", url=URL))
    assert payload.content == data
    assert payload.retrieval.status == expected
    assert payload.retrieval.http_status == status


async def test_redirect_uses_final_url_and_does_not_send_auth():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if len(visited) == 1:
            return httpx.Response(302, headers={"location": "/company/YESBANK/"})
        return httpx.Response(200, text="<html>final</html>", headers={"content-type": "text/html"})

    payload = await ScreenerSource(httpx.MockTransport(handler)).acquire(
        ExtractionRequest(provider="screener", url=URL)
    )
    assert payload.retrieval.final_url == "https://www.screener.in/company/YESBANK/"
    assert payload.retrieval.requested_url == URL
    assert len(visited) == 2


async def test_rejects_external_redirect_without_fetching_target():
    visited = []

    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/"}, content=b"redirect")

    payload = await ScreenerSource(httpx.MockTransport(handler)).acquire(
        ExtractionRequest(provider="screener", url=URL)
    )
    assert payload.retrieval.status == "failed"
    assert visited == [URL]


async def test_network_error_records_failed_retrieval():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    payload = await ScreenerSource(httpx.MockTransport(handler)).acquire(
        ExtractionRequest(provider="screener", url=URL)
    )
    assert payload.retrieval.status == "failed" and payload.content is None


async def test_replay_does_not_construct_http_client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not create HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    path = tmp_path / "raw.html"
    path.write_bytes(b"exact fixture bytes")
    payload = await ScreenerSource().acquire(
        ExtractionRequest(provider="screener", url=URL, replay_path=path)
    )
    assert payload.content == path.read_bytes() and payload.retrieval.mode == "replay"
