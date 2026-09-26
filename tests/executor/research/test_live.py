"""Opt-in live run against the configured local models (RELAY_LLM_TESTS=1).

Uses the real worker with the real research module and no stub models, so it exercises the
Ollama models, DuckDuckGo search (needs network) and fetch_url end to end.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.executor.dispatch import start_task
from tests.executor.conftest import Seed, task_row, wait_for
from tests.executor.research.conftest import ResearchWorker

# Signs of the excluded topic (recipes / baking instructions) in the brief body.
EXCLUDED = re.compile(
    r"\brecipes?\b|\bpreheat|\bknead|\bbake (?:at|for)\b|\boven\b|\bstep \d"
    r"|\b\d+\s?(?:g|grams?|ml|cups?|tbsp|tsp|teaspoons?|tablespoons?)\b",
    re.IGNORECASE,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1" or os.environ.get("RELAY_LIVE_RESEARCH") != "1",
    reason="live LLM test: set RELAY_LLM_TESTS=1 RELAY_LIVE_RESEARCH=1",
)


async def test_live_research_brief_respects_exclusion(
    db: async_sessionmaker[AsyncSession],
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    goal = "A short brief on how sourdough starters work (the microbiology, in plain words)."
    excludes = "Recipes or baking instructions of any kind."
    s = seed_research(goal, excludes)
    t0 = time.monotonic()
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", client=dbos_client
    )
    wait_for(
        lambda: task_row(sync_engine, started.task_id)["status"] in ("succeeded", "failed"),
        timeout=1500,
    )
    row = task_row(sync_engine, started.task_id)
    assert row["status"] == "succeeded", row["error"]
    with sync_engine.connect() as c:
        url: str = c.execute(
            text("SELECT url FROM artifacts WHERE task_id = :t"), {"t": started.task_id}
        ).scalar_one()
    doc = Path(unquote(urlsplit(url).path)).read_text()
    body, _, tail = doc.partition("## Sources")
    sources = [ln for ln in tail.splitlines() if ln.startswith("- <http")]
    served = [ln for ln in worker.log.read_text().splitlines() if "served by" in ln]
    hits = sorted({m.group(0).lower() for m in EXCLUDED.finditer(body)})
    print(
        f"\nLIVE duration={time.monotonic() - t0:.0f}s sources={len(sources)} "
        f"excluded_hits={hits}\n{served[-1] if served else 'served: ?'}\n{doc}"
    )
    assert sources
    assert served, "worker did not log which model served the requests"
    assert not hits, f"excluded topic (recipes/baking instructions) in body: {hits}"
