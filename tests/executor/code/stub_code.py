"""Worker-side test module for code tasks: a deterministic code agent and a fake GitHub API.

Imported by the worker via EXECUTOR_RUNNER_MODULES. Reuses ``stub_models`` for the stub router
and the stub ``project_dir`` (``RELAY_FAKE_FLAG_DIR/projects/<idea_id>``). The code model writes
``hello.py`` and a test for it, then answers with its summary; a ``BLOCK-<token>`` goal makes the
second request append to ``started-<token>`` and wait for ``release-<token>``. Every model call
is appended to ``code_calls.jsonl``.

GitHub: with ``RELAY_FAKE_GITHUB=1`` a respx router serves ``GITHUB_API_URL`` in this process.
Every request is appended to ``github.jsonl``; listing PRs answers ``[]``, creating one answers
PR #7, and anything else (a merge, say) answers 500.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import respx
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import tests.executor.agent.stub_models as research_stubs
from relay.executor.agent import agent as executor_agent

PR_URL = "https://github.com/acme/widget/pull/7"
HELLO = 'def hello() -> str:\n    return "hello"\n'
TEST_HELLO = (
    "from hello import hello\n\n\ndef test_hello() -> None:\n    assert hello() == 'hello'\n"
)
SUMMARY = {
    "title": "Add a hello function",
    "summary": "Adds hello() and a test for it.",
    "changes": ["hello.py: new hello()", "test_hello.py: covers hello()"],
    "how_to_run": "`python3 -m pytest -q`",
}


def _flags() -> Path:
    return Path(os.environ["RELAY_FAKE_FLAG_DIR"])


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    prompt = research_stubs._prompt(messages)
    with open(_flags() / "code_calls.jsonl", "a") as fh:
        fh.write(json.dumps({"prompt": prompt, "n": len(messages)}) + "\n")
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("write_file", {"path": "hello.py", "content": HELLO}),
                ToolCallPart("write_file", {"path": "test_hello.py", "content": TEST_HELLO}),
            ]
        )
    requests = sum(isinstance(m, ModelRequest) for m in messages)
    if (m := re.search(r"BLOCK-(\w+)", prompt)) and requests == 2:
        with open(_flags() / f"started-{m.group(1)}", "a") as fh:
            fh.write("started\n")
        while not (_flags() / f"release-{m.group(1)}").exists():
            time.sleep(0.05)
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, SUMMARY)])


def _record(request: httpx.Request) -> None:
    body = json.loads(request.content) if request.content else None
    entry = {"method": request.method, "path": request.url.path, "json": body}
    with open(_flags() / "github.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _pulls(request: httpx.Request) -> httpx.Response:
    _record(request)
    if request.method == "GET":
        return httpx.Response(200, json=[])
    return httpx.Response(201, json={"html_url": PR_URL, "number": 7})


def _other(request: httpx.Request) -> httpx.Response:
    _record(request)
    return httpx.Response(500, json={"message": "not faked"})


if "RELAY_FAKE_FLAG_DIR" in os.environ:
    model = FunctionModel(respond, model_name="stub-code", profile=research_stubs.LOCAL_PROFILE)
    executor_agent.configure_code_agent(model, routes={"easy": model, "hard": model})
    if os.environ.get("RELAY_FAKE_GITHUB") == "1":
        api = os.environ["GITHUB_API_URL"].rstrip("/")
        router = respx.MockRouter(assert_all_called=False, assert_all_mocked=False)
        router.route(url__regex=rf"^{re.escape(api)}/repos/acme/widget/pulls").mock(
            side_effect=_pulls
        )
        router.route(url__startswith=api).mock(side_effect=_other)
        router.route().pass_through()
        router.start()
