import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from stock_research_ai.domain.macro import MacroRequest
from stock_research_ai.domain.provenance import AcquiredPayload, SourceRetrieval

URL = "https://mcp.mospi.gov.in/"


class MospiSource:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def acquire(self, request: MacroRequest) -> tuple[AcquiredPayload, ...]:
        metadata: dict[str, object] = {
            "dataset": "CPI",
            "base_year": request.base_year,
            "level": "Group",
            "series": "Current",
        }
        filters = {
            "base_year": request.base_year,
            "series": "Current",
            "year": ",".join(sorted({p[:4] for p in request.periods})),
            "month_code": ",".join(sorted({str(int(p[5:])) for p in request.periods}, key=int)),
            "state_code": "99",
            "sector_code": "3",
            "group_code": "0",
            "subgroup_code": "0.99",
            "Format": "JSON",
            "limit": "100",
            "page": "1",
        }
        return (
            await self._get("get_metadata", metadata, request.metadata_file),
            await self._get("get_data", {"dataset": "CPI", "filters": filters}, request.data_file),
        )

    async def _get(
        self, tool: str, arguments: dict[str, object], path: Path | None
    ) -> AcquiredPayload:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        content = None
        content_type = None
        status, error, code, complete = "success", None, None, True
        try:
            if path:
                with path.open("rb") as stream:
                    content = stream.read(5_000_001)
            else:
                async with (
                    asyncio.timeout(40),
                    httpx.AsyncClient(
                        transport=self.transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=40,
                    ) as client,
                ):
                    async with client.stream(
                        "POST",
                        URL,
                        headers={"Accept": "application/json, text/event-stream"},
                        json=body,
                    ) as response:
                        code = response.status_code
                        content_type = response.headers.get("content-type")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk[: 5_000_001 - len(data)])
                            if len(data) > 5_000_000:
                                break
                        content = bytes(data)
                        if code != 200:
                            status, error = "failed", f"HTTP_{code}"
            if content is not None and len(content) > 5_000_000:
                content, status, error, complete = (
                    content[:5_000_000],
                    "failed",
                    "SIZE_LIMIT",
                    False,
                )
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            status, error, complete = "failed", type(exc).__name__, False
        retrieval = SourceRetrieval.model_validate(
            {
                "id": uuid4(),
                "provider": "mospi_official_mcp",
                "requested_url": URL,
                "final_url": URL,
                "request_method": "POST",
                "request_parameters": body,
                "retrieved_at": datetime.now(UTC),
                "status": status,
                "error": error,
                "http_status": code,
                "complete": complete,
                "mode": "replay" if path else "http",
                "content_type": content_type,
                "collector_version": "mospi-mcp/1",
            }
        )
        return AcquiredPayload(
            retrieval=retrieval, content=content, source_type=tool, file_extension="bin"
        )


def decode(content: bytes) -> dict[str, object]:
    from decimal import Decimal

    text = content.decode("utf-8")
    if text.lstrip().startswith("{"):
        envelope = json.loads(text, parse_float=Decimal)
    else:
        messages = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if len(messages) != 1:
            raise ValueError("Expected one complete MCP result")
        envelope = json.loads(messages[0], parse_float=Decimal)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
        raise ValueError("Malformed MCP result")
    result = envelope["result"]
    if result.get("isError"):
        raise ValueError("MoSPI returned an error")
    value = result["structuredContent"]
    if not isinstance(value, dict) or value.get("statusCode") is not True:
        raise ValueError("MoSPI unsuccessful data response")
    return value
