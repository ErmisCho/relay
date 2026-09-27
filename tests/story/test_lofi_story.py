"""A whole spoken brainstorm, end to end: from a vague research idea to delegated work (TASK-53).

In the spirit of ``transcript.txt``: the user starts with a loose idea (a local lo-fi beat
sketching tool), thinks out loud with relay, and the idea converges on two well-defined tasks,
a research write-up and a first version of the tool. The story runs through the REAL Delegator
(configured models, real hooks and tools, over a socket, the way ElevenLabs calls it) and the
REAL executor worker, on a throwaway database and a temporary projects root.

Success means the voice agent delegated the right things (exactly one research document and one
code task, each after a spoken yes to its read-back, nothing else) and the executor created the
idea's project folder and left research Markdown and working, tested code in it.

Opt-in and slow (minutes, real model calls): ``RELAY_LLM_TESTS=1 RELAY_LIVE_STORY=1``.
``RELAY_STORY_PROJECTS_ROOT`` keeps the project folder somewhere to inspect afterwards.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text

from relay.config import get_settings
from relay.delegator import wiring
from relay.delegator.app import create_app
from relay.executor.common import close_dbos_clients
from relay.store.db import create_engine, create_sessionmaker
from tests.conftest import REPO_ROOT
from tests.delegator.conftest import SECRET
from tests.delegator.test_luna import body_for, converse, free_port
from tests.executor.agent.conftest import ResearchWorker

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1" or os.environ.get("RELAY_LIVE_STORY") != "1",
    reason="live story test: set RELAY_LLM_TESTS=1 RELAY_LIVE_STORY=1",
)

READBACK = "just to confirm:"
ASSENT = "Yes, go ahead."
DECLINE = "Not yet, I'm still thinking it through."
NUDGE = "That's everything for that one. Please go ahead with it."
#: What the Delegator says when it starts a plainly instructed task without a read-back.
DIRECT = "On it."
MAX_NUDGES = 2
DISPATCH_WAIT_S = 30.0
TASKS_WAIT_S = 45 * 60


@dataclass(frozen=True)
class Beat:
    """One thing the user says; ``propose`` beats must end in a read-back the user agrees to."""

    says: str
    expect: Literal["talk", "propose"] = "talk"


STORY: tuple[Beat, ...] = (
    Beat(
        "Okay, random brain dump. A lot of my musician friends use AI for music now, but I "
        "want my own little tool that sketches a quick lo-fi beat from an idea, locally on my "
        "Mac, no cloud stuff. I honestly don't know where to start."
    ),
    Beat("What actually makes a beat sound lo-fi? Like the tempo, the drums, the chords?"),
    Beat(
        "Okay, I think this is two things. First, look into how lo-fi hip hop beats are "
        "built: typical tempo, swing, drum patterns and jazzy chord progressions, and which "
        "free open-source tools can generate MIDI locally. Write that up for me. Leave out "
        "anything about selling or distributing music.",
        "propose",
    ),
    Beat(
        "Second thing: build me a first version of the tool. A small Python command-line "
        "script that writes a four-bar lo-fi beat as a MIDI file, drums plus a jazzy chord "
        "progression, using only the Python standard library, with a couple of tests. No "
        "audio rendering or user interface yet.",
        "propose",
    ),
    Beat("Great, that's all for now. Thanks!"),
)


@dataclass
class Story:
    url: str
    engine: Engine
    session: uuid.UUID = field(default_factory=uuid.uuid4)
    history: list[dict[str, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    declined: int = 0
    direct: int = 0

    async def say(self, client: httpx.AsyncClient, utterance: str) -> str:
        self.history.append({"role": "user", "content": utterance})
        _, reply = await converse(client, self.url, body_for(self.history, self.session))
        self.history.append({"role": "assistant", "content": reply})
        self.log += [f"you:   {utterance}", f"relay: {reply}"]
        print(f"\nyou:   {utterance}\nrelay: {reply}", flush=True)
        return reply

    def task_count(self) -> int:
        with self.engine.connect() as c:
            return int(c.execute(text("SELECT count(*) FROM tasks")).scalar_one())

    async def wait_for_tasks(self, n: int) -> None:
        deadline = time.monotonic() + DISPATCH_WAIT_S
        while self.task_count() < n:
            assert time.monotonic() < deadline, f"no dispatch after the yes (want {n} tasks)"
            await asyncio.sleep(0.5)

    async def play(self, client: httpx.AsyncClient, beat: Beat) -> None:
        reply = await self.say(client, beat.says)
        if beat.expect == "talk":
            # A read-back the story is not ready for: say no, which must dispatch nothing.
            if READBACK in reply.lower():
                self.declined += 1
                before = self.task_count()
                await self.say(client, DECLINE)
                await asyncio.sleep(5)
                assert self.task_count() == before, "a declined read-back was dispatched"
            return
        want = self.task_count() + 1
        for _ in range(MAX_NUDGES):
            if READBACK in reply.lower() or reply.startswith(DIRECT):
                break
            reply = await self.say(client, NUDGE)
        if reply.startswith(DIRECT):
            self.direct += 1  # a plain instruction: started without a read-back
        else:
            assert READBACK in reply.lower(), f"no read-back or start for: {beat.says!r}"
            await self.say(client, ASSENT)
        await self.wait_for_tasks(want)


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    keep = os.environ.get("RELAY_STORY_PROJECTS_ROOT")
    root = Path(keep).expanduser() / time.strftime("story-%Y%m%d-%H%M%S") if keep else tmp_path
    root = (root / "projects").resolve()
    root.mkdir(parents=True)
    return root


@pytest.fixture
def story_env(exec_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Delegator's dispatch reads global settings: point them at the test database."""
    monkeypatch.chdir(REPO_ROOT)  # the real .env: model refs and provider keys
    monkeypatch.setenv("DATABASE_URL", exec_db_url)
    monkeypatch.setenv("ENABLED_KINDS", "research,code")
    monkeypatch.setenv("GITHUB_TOKEN", "")  # never publish from a test: branch only
    get_settings.cache_clear()
    yield
    close_dbos_clients()
    get_settings.cache_clear()


@pytest.fixture
def story_worker(
    exec_db_url: str, projects_root: Path, tmp_path: Path, story_env: None
) -> Iterator[ResearchWorker]:
    flags = tmp_path / "worker"
    flags.mkdir()
    env = {"EXECUTOR_PROJECTS_ROOT": str(projects_root), "ENABLED_KINDS": "research,code"}
    w = ResearchWorker(exec_db_url, flags, "", {**env, "GITHUB_TOKEN": ""})
    w.start()
    try:
        yield w
    finally:
        w.stop()


@pytest.fixture
async def delegator(exec_db_url: str, story_env: None) -> AsyncIterator[str]:
    base = get_settings()
    refs = (base.delegator_model, base.research_easy_model, base.research_hard_model)
    if any(r.startswith("openai:") for r in refs) and not base.openai_api_key:
        pytest.skip("the configured models need OPENAI_API_KEY")
    settings = base.model_copy(
        update={
            "delegator_shared_secret": SECRET,
            "demo_passcode": "",
            "router_shadow": [],
            "router_active": "none",
        }
    )
    engine = create_engine(exec_db_url)
    app = create_app(
        settings,
        registry=wiring.build_registry(),
        hooks=wiring.build_hooks(),
        sessionmaker=create_sessionmaker(engine),
        warm_dbos=False,
    )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task
    await engine.dispose()


async def wait_until_finished(engine: Engine) -> list[dict[str, object]]:
    deadline = time.monotonic() + TASKS_WAIT_S
    last = ""
    while True:
        with engine.connect() as c:
            rows = [dict(r) for r in c.execute(text("SELECT * FROM tasks")).mappings()]
        now = ", ".join(f"{r['kind']}={r['status']}" for r in rows)
        if now != last:
            print(f"\n[{time.strftime('%H:%M:%S')}] tasks: {now}", flush=True)
            last = now
        if all(r["status"] in ("succeeded", "failed") for r in rows):
            return rows
        assert time.monotonic() < deadline, f"tasks still running: {now}"
        await asyncio.sleep(5)


def tree(folder: Path) -> list[Path]:
    return sorted(
        p.relative_to(folder)
        for p in folder.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(folder).parts
    )


async def test_live_story_from_research_idea_to_delegated_work(
    delegator: str, story_worker: ResearchWorker, sync_engine: Engine, projects_root: Path
) -> None:
    story = Story(delegator, sync_engine)
    async with httpx.AsyncClient(timeout=120) as client:
        for beat in STORY:
            await story.play(client, beat)

    # --- the voice agent delegated the right things, and only those -------------------------
    with sync_engine.connect() as c:
        commitments = [
            dict(r)
            for r in c.execute(text("SELECT * FROM commitments ORDER BY created_at")).mappings()
        ]
    for cm in commitments:
        print(
            f"\ncommitment [{cm['artifact_kind']}] {cm['goal']!r}\n  excludes: "
            f"{cm['scope_excludes']!r}\n  assent: {cm['assent_utterance']!r}"
        )
    assert [cm["artifact_kind"] for cm in commitments] == ["document", "pull_request"]
    assert all(cm["assent_utterance"] and cm["assented_at"] for cm in commitments)
    research, code = (str(cm["goal"]).lower() for cm in commitments)
    assert any(k in research for k in ("lo-fi", "lofi", "lo fi")), research
    assert "midi" in code and "python" in code, code
    assert commitments[0]["idea_id"] == commitments[1]["idea_id"], "two ideas for one story"

    # --- the executor ran both, in one new project folder -----------------------------------
    rows = await wait_until_finished(sync_engine)
    by_kind = {str(r["kind"]): r for r in rows}
    assert sorted(by_kind) == ["code", "research"], rows
    with sync_engine.connect() as c:
        artifacts = {
            str(kind): (str(url), str(summary))
            for kind, url, summary in c.execute(
                text(
                    "SELECT t.kind, a.url, a.summary FROM artifacts a "
                    "JOIN tasks t ON t.id = a.task_id"
                )
            )
        }
    for kind, (url, summary) in artifacts.items():
        print(f"\n{kind} artifact: {url}\n  {summary}")
    for kind, row in by_kind.items():
        print(f"{kind}: {row['status']} served by {row['served_model']} error={row['error']}")
    assert all(r["status"] == "succeeded" for r in rows), story_worker.log.read_text()[-4000:]

    folders = [p for p in projects_root.iterdir() if p.is_dir()]
    assert len(folders) == 1, folders
    folder = folders[0]
    files = tree(folder)
    print(f"\nproject folder {folder}:\n" + "\n".join(f"  {f}" for f in files))

    # Research: the brief is in the project folder, sourced.
    docs = [folder / f for f in files if f.suffix == ".md" and f.parts[0] == "research"]
    assert len(docs) == 1, f"no research Markdown in the project folder: {files}"
    brief = docs[0].read_text()
    assert len(brief) > 800 and "## Sources" in brief and "- <http" in brief, brief[:2000]
    doc_url = artifacts["research"][0]
    assert Path(unquote(urlsplit(doc_url).path)).read_text() == brief

    # Code: Python source plus tests on a relay/ branch, and the tests passed.
    py = [f for f in files if f.suffix == ".py"]
    tests = [f for f in py if f.name.startswith("test_") or "tests" in f.parts]
    assert tests and len(py) > len(tests), py
    assert "tests passed" in artifacts["code"][1], artifacts["code"][1]
    branches = subprocess.run(
        ["git", "-C", str(folder), "branch", "--list", "relay/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches.strip(), "no relay/ branch in the project folder"
    print(
        f"\nstory: {len(story.log) // 2} turns, {story.direct} direct start(s), "
        f"{story.declined} early read-back(s) declined"
    )


# The owner's own voice session (2026-09-27, ASR text verbatim): asking for suggestions got a
# read-back, and the instruction that followed got a second one instead of starting.
VOICE_SESSION = (
    "Hey. Okay. Random brain dump. A lot of my musician friends use AI for music now, but I "
    "want my own little tool that sketches a quick lo-fi beat from an idea locally on my Mac. "
    "No cloud stuff. I honestly don't know where to start. Could you suggest something?",
    "Yeah. So, what I want is two things. The first one, um, I want a document, a Markdown "
    "document, uh, and you can look into how lo-fi hip-hop beats are built, so the typical "
    "tempo, swing, drum patterns, and jazzy chord progressions, and then which, uh, free "
    "open-source tools can generate MIDI locally. So, could you write up, that up for me? "
    "Thank you.",
)


async def test_live_voice_session_asks_nothing_back(delegator: str, sync_engine: Engine) -> None:
    """Suggestions get an answer, the instruction starts work; no "Just to confirm" at all."""
    story = Story(delegator, sync_engine)
    before = story.task_count()  # the module's database is shared with the other story
    async with httpx.AsyncClient(timeout=120) as client:
        first = await story.say(client, VOICE_SESSION[0])
        assert READBACK not in first.lower() and not first.startswith(DIRECT), first
        assert story.task_count() == before
        second = await story.say(client, VOICE_SESSION[1])
        assert second.startswith(DIRECT), second
        await story.wait_for_tasks(before + 1)
    with sync_engine.connect() as c:
        kind, goal = c.execute(
            text("SELECT artifact_kind, goal FROM commitments ORDER BY created_at DESC LIMIT 1")
        ).one()
    print(f"\ncommitment [{kind}] {goal!r}")
    assert kind == "document" and "lo-fi" in goal.lower()
