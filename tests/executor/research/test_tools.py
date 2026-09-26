"""fetch_url limits and the Markdown rendering of a brief."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import httpx2
import pytest
import respx
from pydantic_ai.models.openai import OpenAIChatModel

from relay.config import Settings
from relay.executor.research import (
    FetchError,
    ResearchBrief,
    fetch_url_text,
    render_markdown,
    tools,
)
from relay.executor.research.agent import build_research_model
from relay.executor.research.runner import artifact_path, write_document

URL = "https://example.com/page"
# Stub DNS: no test touches the network, and names can be made to resolve anywhere.
DNS = {
    "example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "intranet.example": ["10.1.2.3"],
    "mixed.example": ["93.184.215.14", "192.168.1.20"],
}


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in DNS:
            raise OSError(f"stub DNS: unknown host {host}")
        return DNS[host]

    monkeypatch.setattr(tools, "_resolve_host", resolve)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "example.com/x"])
@respx.mock(assert_all_mocked=True)
async def test_fetch_rejects_non_http_urls_without_requesting(url: str) -> None:
    with pytest.raises(FetchError, match="http"):
        await fetch_url_text(url)
    assert not respx.calls


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/api/tags",
        "http://127.0.0.1:11434/api/tags",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://intranet.example/",
        "http://mixed.example/",
    ],
)
@respx.mock(assert_all_mocked=True)
async def test_fetch_refuses_non_public_hosts_without_requesting(url: str) -> None:
    with pytest.raises(FetchError, match="non-public host"):
        await fetch_url_text(url)
    assert not respx.calls


@respx.mock
async def test_fetch_revalidates_every_redirect_hop() -> None:
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": "/next"}))
    nxt = respx.get("https://example.com/next").mock(
        return_value=httpx.Response(301, headers={"Location": "http://10.0.0.5/secret"})
    )
    private = respx.get("http://10.0.0.5/secret").mock(return_value=httpx.Response(200))
    with pytest.raises(FetchError, match="non-public host '10.0.0.5'"):
        await fetch_url_text(URL)
    assert nxt.called and not private.called


@respx.mock
async def test_fetch_stops_after_max_redirects() -> None:
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": URL}))
    with pytest.raises(FetchError, match="more than 5 redirects"):
        await fetch_url_text(URL)
    assert len(respx.calls) == 6


@respx.mock
async def test_fetch_enforces_size_limit_while_streaming() -> None:
    # No Content-Length: the limit must hold for chunked bodies, not only declared sizes.
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"x" * 1000

    respx.get(URL).mock(return_value=httpx.Response(200, stream=chunks()))  # type: ignore[arg-type]
    with pytest.raises(FetchError, match="larger than 50000 bytes"):
        await fetch_url_text(URL, max_bytes=50_000)


@respx.mock
async def test_fetch_turns_timeouts_into_fetch_error() -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out"):
        await fetch_url_text(URL)


@respx.mock
async def test_fetch_extracts_readable_text_and_truncates() -> None:
    para = "Solar kettles use a vacuum tube to heat water in direct sunlight. " * 20
    html = (
        "<html><head><script>var tracking = 1;</script></head><body><nav>Menu</nav>"
        f"<article><h1>Solar kettles</h1><p>{para}</p><p>{para}</p></article></body></html>"
    )
    respx.get(URL).mock(return_value=httpx.Response(200, html=html))
    text = await fetch_url_text(URL, max_chars=500)
    assert "vacuum tube" in text and "tracking" not in text
    assert text.endswith("[truncated at 500 characters]") and len(text) < 560


def test_sources_section_lists_each_url_once_and_rewrite_is_idempotent(tmp_path: Path) -> None:
    brief = ResearchBrief(
        title="Tide tables",
        summary="Two sources.",
        body_markdown="Body.",
        sources=["https://a.example/x", "https://b.example/", "https://a.example/x"],
    )
    md = render_markdown(brief)
    assert md.split("## Sources\n\n", 1)[1] == "- <https://a.example/x>\n- <https://b.example/>\n"

    path = artifact_path(str(tmp_path), "idea", "task")
    write_document(path, "stale partial content that is longer than the document")
    write_document(path, md)
    assert path.read_text() == md and [p.name for p in path.parent.iterdir()] == ["task.md"]


def test_fallback_model_is_built_from_settings() -> None:
    s = Settings(
        research_model="ollama:qwen3.8:latest",
        research_fallback_model="gemma4:e4b",
        ollama_base_url="http://ollama.test:11434/v1",
    )
    primary, fallback = build_research_model(s).models
    assert (primary.model_name, fallback.model_name) == ("qwen3.8:latest", "gemma4:e4b")
    for model in (primary, fallback):
        assert isinstance(model, OpenAIChatModel)
        assert model.base_url == "http://ollama.test:11434/v1/"
        # A hung endpoint must hand over to the fallback quickly, not after ~10 min of retries.
        assert model.client.max_retries == 0
        timeout = model.client.timeout
        assert isinstance(timeout, httpx2.Timeout)
        assert (timeout.connect, timeout.read) == (5.0, 120.0)
