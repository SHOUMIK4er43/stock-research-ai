"""Box 15: the product's Detailed Research generator (live acquisition -> stored report).

What the extension's "Detailed Research" runs, with no stage file and no CLI:

    Screener page URL -> capture_stage (existing adapters; the acquired page binds the
    company identity; Box 9 peers from the page's own peer table) -> run_detailed_research
    (Boxes 4-14, offline, in a worker thread) -> JSON / Markdown / HTML / PDF in the store.

The stage lives only in memory; the raw pages and documents captured on the way go to a
private per-run work directory that is removed afterwards.
"""

import asyncio
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from stock_research_ai.adapters.rendering.acquire import (
    FetchEvidence,
    FetchExchange,
    FetchPage,
    FetchPeers,
    StageRequest,
    capture_stage,
    live_fetchers,
)
from stock_research_ai.adapters.rendering.pdf import render_pdf
from stock_research_ai.adapters.rendering.store import FileFinalReportStore
from stock_research_ai.application.final_report_jobs import Progress
from stock_research_ai.application.report_orchestrator import (
    DetailedResearchResult,
    PdfRenderer,
    run_detailed_research,
)
from stock_research_ai.private_files import ensure_private_dir

Fetchers = tuple[FetchPage, FetchEvidence, FetchExchange, FetchPeers]
FetcherFactory = Callable[..., Fetchers]


class LiveFinalReportGenerator:
    def __init__(self, store: FileFinalReportStore, work_root: Path, *,
                 pdf: PdfRenderer | None = render_pdf,
                 fetchers: FetcherFactory = live_fetchers,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._work_root = work_root
        self._pdf = pdf
        self._fetchers = fetchers
        self._clock = clock

    async def __call__(self, source_url: str, as_of: date,
                       progress: Progress) -> DetailedResearchResult:
        now = self._clock()
        ensure_private_dir(self._work_root)
        work = self._work_root / uuid4().hex
        ensure_private_dir(work)
        try:
            progress("ACQUIRING")
            page, evidence, exchange, peers = self._fetchers(work, now=now)
            stage = await capture_stage(StageRequest(source_url=source_url, as_of=as_of),
                                        now=now, fetch_page=page, fetch_evidence=evidence,
                                        fetch_exchange=exchange, fetch_peers=peers)
            progress("BUILDING")
            return await asyncio.to_thread(run_detailed_research, stage, store=self._store,
                                           pdf=self._pdf)
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)


__all__ = ["LiveFinalReportGenerator"]
